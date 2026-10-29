import base64
import binascii
import os
import re

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel


router = APIRouter(prefix="/github", tags=["github"])
REPOSITORY_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}"
README_LIMIT = 12000
DIRECTORY_LIMIT = 100


class RepositorySnapshot(BaseModel):
    full_name: str
    url: str
    description: str | None
    language: str | None
    default_branch: str
    stars: int
    root_entries: list[str]
    root_truncated: bool
    readme: str | None
    readme_truncated: bool


async def github_json(repository: str, endpoint: str, params=None, *, allow_missing=False):
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    token = os.getenv('GITHUB_TOKEN', '').strip()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(f'https://api.github.com/repos/{repository}' + ('/' + endpoint if endpoint else ''), headers=headers, params=params)
            if allow_missing and response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
    except httpx.TimeoutException:
        raise HTTPException(504, 'GitHub 请求超时。') from None
    except httpx.RequestError:
        raise HTTPException(502, '无法连接 GitHub。') from None
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 403 and exc.response.headers.get('x-ratelimit-remaining') == '0':
            status = 429
        if status not in {401, 403, 404, 409, 429}:
            status = 502
        raise HTTPException(status, 'GitHub 读取失败，请检查仓库、路径、版本及访问权限。') from None
    except ValueError:
        raise HTTPException(502, 'GitHub 返回了无效 JSON。') from None


async def fetch_repository(repository: str) -> RepositorySnapshot:
    if not re.fullmatch(REPOSITORY_PATTERN, repository) or repository.split("/")[1] in {".", ".."}:
        raise HTTPException(422, "仓库格式应为 owner/repo，例如 fastapi/fastapi。")
    try:
        metadata = await github_json(repository, '')
        params = {"ref": metadata["default_branch"]}
        entries = await github_json(repository, 'contents', params, allow_missing=True)
        if entries is None:
            entries = []
        document = await github_json(repository, 'readme', params, allow_missing=True)
        readme = None
        if document is not None:
            if document.get('encoding') != 'base64':
                raise ValueError('Unsupported README encoding')
            readme = base64.b64decode(document['content']).decode('utf-8', errors='replace')
        return RepositorySnapshot(
            full_name=metadata["full_name"], url=metadata["html_url"],
            description=metadata.get("description"), language=metadata.get("language"),
            default_branch=metadata["default_branch"], stars=metadata["stargazers_count"],
            root_entries=[item["name"] + ("/" if item["type"] == "dir" else "") for item in entries[:DIRECTORY_LIMIT]],
            root_truncated=len(entries) > DIRECTORY_LIMIT,
            readme=readme[:README_LIMIT] if readme is not None else None,
            readme_truncated=readme is not None and len(readme) > README_LIMIT,
        )
    except (ValueError, KeyError, TypeError, binascii.Error):
        raise HTTPException(502, "无法解析 GitHub 返回的仓库内容。") from None


@router.get("/repos/{owner}/{repo}", response_model=RepositorySnapshot)
async def get_repository(owner: str, repo: str) -> RepositorySnapshot:
    return await fetch_repository(f"{owner}/{repo}")
