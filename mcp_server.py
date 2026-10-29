"""Reusable read-only GitHub MCP server. stdout is reserved for MCP."""
import asyncio
import json
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from tool_runtime import TOOLS, ToolCall, execute_tool

server = Server('dev-assistant-github')
pins = {}
tool_lock = asyncio.Lock()


@server.list_tools()
async def list_tools():
    return [types.Tool(name=t['function']['name'], description=t['function']['description'],
                       inputSchema=t['function']['parameters'],
                       annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)) for t in TOOLS]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    call = ToolCall(id='mcp', type='function', function={'name': name, 'arguments': json.dumps(arguments)})
    async with tool_lock:
        result = await execute_tool(call, pins)
    return types.CallToolResult(content=[types.TextContent(type='text', text=result)],
                                isError=not json.loads(result)['ok'])


async def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == '__main__':
    asyncio.run(main())
