import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from agent import run_agent
from llm import EmptyModelResponseError
from mcp_client import github_session
from tool_runtime import ToolCall

FIXTURE = Path(__file__).with_name('fixture_mcp_server.py')


def call(name, arguments):
    return ToolCall(id='test', type='function', function={'name': name, 'arguments': json.dumps(arguments)})


class McpTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_server_handshake_discovery_and_validation(self):
        async with github_session() as remote:
            self.assertEqual(len(remote.tools), 6)
            self.assertIn('read_pr_diff', remote.names)
            result = json.loads(await remote.call(call('read_file', {'repository': 'owner/repo', 'path': '../secret'})))
            self.assertFalse(result['ok'])
            self.assertEqual(result['status'], 422)
            result = json.loads(await remote.call(call('not_allowed', {})))
            self.assertFalse(result['ok'])

    async def test_real_protocol_success_and_invalid_schema(self):
        async with github_session(FIXTURE) as remote:
            result = json.loads(await remote.call(call('fetch_issue', {'repository': 'demo/project', 'number': 3})))
            self.assertTrue(result['ok'])
            self.assertEqual(result['issue']['title'], 'Fixture issue')
            result = json.loads(await remote.call(call('fetch_issue', {'repository': 'demo/project', 'number': 0})))
            self.assertFalse(result['ok'])

    async def test_cancel_closes_connection_and_next_session_works(self):
        started = asyncio.Event()
        async def work():
            async with github_session(FIXTURE) as remote:
                started.set()
                await remote.call(call('fetch_issue', {'repository': 'slow/repo', 'number': 3}))
        task = asyncio.create_task(work())
        await asyncio.wait_for(started.wait(), 30)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        async with github_session(FIXTURE) as remote:
            self.assertEqual(len(remote.tools), 6)

    async def test_agent_preserves_model_error_in_mcp_mode(self):
        with patch.dict('os.environ', {'TOOL_TRANSPORT': 'mcp'}), patch('agent.request_completion', AsyncMock(side_effect=EmptyModelResponseError)), self.assertRaises(EmptyModelResponseError):
            await run_agent([{'role': 'user', 'content': 'hello'}])
