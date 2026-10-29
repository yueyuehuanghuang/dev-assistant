"""Offline server fixture: real MCP protocol, deterministic GitHub responses."""
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github
import collaboration_tools


async def fixture(repository, endpoint, params=None, **kwargs):
    if repository == 'slow/repo':
        await asyncio.sleep(60)
    return {'number': 3, 'title': 'Fixture issue', 'body': 'A reproducible crash',
            'state': 'open', 'html_url': 'https://github.com/demo/project/issues/3'}


github.github_json = fixture
collaboration_tools.github_json = fixture

from mcp_server import main

if __name__ == '__main__':
    asyncio.run(main())
