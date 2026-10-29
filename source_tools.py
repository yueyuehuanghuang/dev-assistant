"""Read GitHub source at immutable commits; never execute repository contents."""
import base64
import binascii
import re
from urllib.parse import quote

from fastapi import HTTPException

from github import DIRECTORY_LIMIT, REPOSITORY_PATTERN, github_json

FILE_BYTE_LIMIT = 200_000
FILE_CHAR_LIMIT = 16_000


def validate_location(repository: str, path: str) -> None:
    if not re.fullmatch(REPOSITORY_PATTERN, repository) or repository.split('/')[1] in {'.', '..'}:
        raise HTTPException(422, '仓库格式应为 owner/repo。')
    if (len(path) > 1024 or '\\' in path or any(ord(c) < 32 for c in path)
            or (path and any(p in {'', '.', '..'} for p in path.split('/')))):
        raise HTTPException(422, '路径必须是仓库内的相对路径，根目录使用空字符串。')


async def resolve_commit(repository: str, ref: str | None) -> str:
    validate_location(repository, '')
    # HEAD resolves the default branch without an additional metadata request.
    document = await github_json(repository, 'commits/' + quote(ref or 'HEAD', safe=''))
    sha = document.get('sha') if isinstance(document, dict) else None
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-fA-F]{40}', sha):
        raise HTTPException(502, '无法解析 GitHub commit。')
    return sha


async def read_source(repository: str, path: str, commit: str, *, directory: bool,
                      start_line: int = 1, line_count: int = 120) -> dict:
    validate_location(repository, path)
    document = await github_json(repository, 'contents' + ('/' + quote(path, safe='/') if path else ''), {'ref': commit})
    url = f'https://github.com/{repository}/' + ('tree' if directory else 'blob') + f'/{commit}' + ('/' + quote(path, safe='/') if path else '')
    common = {'repository': repository, 'path': path, 'commit': commit, 'url': url}
    if directory:
        if not isinstance(document, list):
            raise HTTPException(422, '指定路径不是目录。')
        try:
            entries = [{'name': item['name'], 'path': item['path'], 'type': item['type']} for item in document[:DIRECTORY_LIMIT]]
        except (KeyError, TypeError):
            raise HTTPException(502, '无法解析目录内容。') from None
        return {**common, 'entries': entries, 'truncated': len(document) >= DIRECTORY_LIMIT,
                'note': '最多展示 100 项；GitHub Contents API 单目录最多返回 1000 项。'}
    if not isinstance(document, dict) or document.get('type') != 'file':
        raise HTTPException(422, '只支持普通文件，不支持目录、符号链接或子模块。')
    size = document.get('size')
    if not isinstance(size, int) or size > FILE_BYTE_LIMIT:
        raise HTTPException(422, '文件大小无效或超过 200000 字节，请选择较小文件。')
    if document.get('encoding') != 'base64' or not isinstance(document.get('content'), str):
        raise HTTPException(422, '文件内容编码不支持。')
    try:
        raw = base64.b64decode(''.join(document['content'].split()), validate=True)
        if len(raw) > FILE_BYTE_LIMIT:
            raise HTTPException(422, '文件超过 200000 字节。')
        text = raw.decode('utf-8')
    except (ValueError, binascii.Error):
        raise HTTPException(422, '只支持 UTF-8 文本文件，无法读取二进制或其他编码。') from None
    if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
        raise HTTPException(422, '不支持二进制文件。')
    lines = text.splitlines()
    if start_line > len(lines) and not (start_line == 1 and not lines):
        raise HTTPException(422, '起始行超出文件范围。')
    selected = lines[start_line - 1:start_line - 1 + line_count]
    numbered = []
    used = 0
    for number, line in enumerate(selected, start_line):
        rendered = f'{number}: {line}'
        if used + len(rendered) + 1 > FILE_CHAR_LIMIT:
            if not numbered:
                raise HTTPException(422, '单行内容过长，无法完整展示。')
            break
        numbered.append(rendered)
        used += len(rendered) + 1
    end = start_line + len(numbered) - 1
    return {**common, 'url': url + (f'#L{start_line}-L{end}' if numbered else ''),
            'content': '\n'.join(numbered), 'start_line': start_line, 'end_line': end,
            'total_lines': len(lines), 'truncated': start_line > 1 or end < len(lines),
            'next_start_line': end + 1 if end < len(lines) else None}
