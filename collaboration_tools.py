"""Read-only Issue/PR tools using the shared GitHub transport."""
from fastapi import HTTPException

from github import github_json
from source_tools import validate_location

BODY_LIMIT = 12000
PATCH_LIMIT = 6000
DIFF_LIMIT = 24000
FILES_PER_PAGE = 20


def validate_number(repository: str, number: int):
    validate_location(repository, '')
    if type(number) is not int or not 1 <= number <= 2147483647:
        raise HTTPException(422, 'Issue/PR 编号必须为正整数。')


def details(document: dict, repository: str) -> dict:
    try:
        body = document.get('body') or ''
        return {'repository': repository, 'number': document['number'],
                'title': document['title'], 'state': document['state'],
                'url': document['html_url'], 'body': body[:BODY_LIMIT],
                'body_truncated': len(body) > BODY_LIMIT,
                'updated_at': document.get('updated_at'),
                'author': (document.get('user') or {}).get('login')}
    except (KeyError, TypeError, AttributeError):
        raise HTTPException(502, '无法解析 GitHub Issue/PR。') from None


async def fetch_issue(repository: str, number: int) -> dict:
    validate_number(repository, number)
    document = await github_json(repository, f'issues/{number}')
    if not isinstance(document, dict):
        raise HTTPException(502, '无法解析 GitHub Issue。')
    result = details(document, repository)
    # GitHub issues endpoint also returns PRs. Do not mislabel them as issues.
    result.update(kind='pull_request' if 'pull_request' in document else 'issue',
                  comments_count=document.get('comments', 0), comments_included=False)
    return result


async def fetch_pull_request(repository: str, number: int) -> dict:
    validate_number(repository, number)
    document = await github_json(repository, f'pulls/{number}')
    if not isinstance(document, dict):
        raise HTTPException(502, '无法解析 GitHub PR。')
    result = details(document, repository)
    try:
        result.update(kind='pull_request', base_sha=document['base']['sha'],
                      head_sha=document['head']['sha'],
                      head_repository=(document['head'].get('repo') or {}).get('full_name'),
                      base_repository=(document['base'].get('repo') or {}).get('full_name'),
                      draft=document.get('draft', False), merged=document.get('merged', False),
                      changed_files=document['changed_files'], additions=document.get('additions'),
                      deletions=document.get('deletions'), comments_included=False)
        if not all(isinstance(result[key], str) and len(result[key]) == 40 for key in ('base_sha', 'head_sha')):
            raise ValueError()
        return result
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HTTPException(502, '无法解析 PR 版本信息。') from None


async def read_pr_diff(repository: str, number: int, page: int = 1,
                       expected_version: tuple | None = None) -> dict:
    validate_number(repository, number)
    if type(page) is not int or not 1 <= page <= 150:
        raise HTTPException(422, 'diff 页码必须在 1 到 150 之间。')
    before = await fetch_pull_request(repository, number)
    version = (before['base_sha'], before['head_sha'])
    if expected_version is not None and version != expected_version:
        raise HTTPException(409, 'PR 版本已变化，请在下一轮重新读取 PR 和 diff。')
    documents = await github_json(repository, f'pulls/{number}/files', {'page': page, 'per_page': FILES_PER_PAGE})
    after = await fetch_pull_request(repository, number)
    if version != (after['base_sha'], after['head_sha']):
        raise HTTPException(409, '读取期间 PR 版本发生变化，请重试。')
    if not isinstance(documents, list):
        raise HTTPException(502, '无法解析 PR 文件列表。')
    files = []
    remaining = DIFF_LIMIT
    try:
        for item in documents:
            patch = item.get('patch')
            if patch is not None and not isinstance(patch, str):
                raise TypeError()
            shown = patch[:min(PATCH_LIMIT, remaining)] if patch is not None else None
            remaining -= len(shown or '')
            files.append({'path': item['filename'], 'previous_path': item.get('previous_filename'),
                          'status': item['status'], 'additions': item['additions'], 'deletions': item['deletions'],
                          'url': item.get('blob_url'), 'patch': shown,
                          'patch_available': patch is not None,
                          'patch_truncated': patch is not None and len(shown) < len(patch)})
    except (KeyError, TypeError, AttributeError):
        raise HTTPException(502, '无法解析 PR diff。') from None
    total = before['changed_files']
    end = (page - 1) * FILES_PER_PAGE + len(files)
    next_page = page + 1 if files and end < min(total, 3000) and page < 150 else None
    return {'repository': repository, 'number': number, 'url': before['url'],
            'base_sha': version[0], 'head_sha': version[1], 'page': page, 'files': files,
            'total_files': total, 'next_page': next_page,
            'truncated': page > 1 or end < total or any(f['patch_truncated'] or not f['patch_available'] for f in files),
            'note': 'diff 为 GitHub 返回的 patch，可能缺失或被上游截断，不能保证完整；接口最多提供 3000 个文件。未读取评论及 Review。'}
