"""One SDK stdio connection per Agent turn, closed on success/error/cancel."""
import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import os
from pathlib import Path
import sys

from fastapi import HTTPException
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class RemoteTools:
    def __init__(self, session, tools):
        self.session = session
        self.tools = tools
        self.names = {t['function']['name'] for t in tools}

    async def call(self, call):
        try:
            arguments = json.loads(call.function.arguments)
            if call.function.name not in self.names or not isinstance(arguments, dict):
                raise ValueError()
        except (ValueError, TypeError):
            return json.dumps({'ok': False, 'error': '未知工具或无效 JSON 参数。'}, ensure_ascii=False)
        try:
            result = await self.session.call_tool(call.function.name, arguments)
            texts = [c.text for c in result.content if c.type == 'text']
            payload = json.loads(texts[0]) if len(texts) == 1 else None
            if not isinstance(payload, dict) or type(payload.get('ok')) is not bool:
                raise ValueError()
            if result.isError and payload['ok']:
                raise ValueError()
            return json.dumps(payload, ensure_ascii=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            return json.dumps({'ok': False, 'error': 'MCP 工具调用失败或返回格式无效，请检查服务。'}, ensure_ascii=False)


@asynccontextmanager
async def github_session(server_path=None):
    root = Path(__file__).resolve().parent
    # Pass only the GitHub credential, not the model key or the entire parent environment.
    env = {'PYTHONIOENCODING': 'utf-8', 'PYTHONUTF8': '1',
           'GITHUB_TOKEN': os.getenv('GITHUB_TOKEN', '')}
    parameters = StdioServerParameters(command=sys.executable,
        args=[str(server_path or root / 'mcp_server.py')], cwd=str(root), env=env)
    connected = False
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
                await session.initialize()
                listing = await session.list_tools()
                tools = [{'type': 'function', 'function': {'name': t.name,
                         'description': t.description or '', 'parameters': t.inputSchema}}
                         for t in listing.tools]
                if not tools:
                    raise ValueError('No MCP tools')
                connected = True
                yield RemoteTools(session, tools)
    except asyncio.CancelledError:
        raise
    except HTTPException:
        raise
    except Exception:
        if connected:
            raise
        raise HTTPException(503, 'MCP 服务启动、连接或处理失败；检查依赖和服务配置。') from None


async def inspect():
    async with github_session() as remote:
        print(json.dumps({'transport': 'stdio', 'tools': [t['function']['name'] for t in remote.tools]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(inspect())
