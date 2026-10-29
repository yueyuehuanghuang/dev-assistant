import base64
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from agent import run_agent
from source_tools import read_source, resolve_commit, validate_location
from test_agent import tool_message, final_message

SHA = 'a' * 40


@patch.dict("os.environ", {"TOOL_TRANSPORT": "direct"})
class SourceTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, document, **kwargs):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=document)
        real = httpx.AsyncClient
        with patch('github.httpx.AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            result = await read_source('owner/repo', 'src/example.py', SHA, directory=False, **kwargs)
        self.assertEqual(requests[0].url.params['ref'], SHA)
        self.assertEqual(requests[0].url.host, 'api.github.com')
        return result

    def document(self, text):
        raw = text.encode()
        return {'type': 'file', 'size': len(raw), 'encoding': 'base64',
                'content': base64.b64encode(raw).decode()}

    async def test_file_pagination_and_citations(self):
        result = await self.read(self.document('one\ntwo\nthree\n'), start_line=2, line_count=1)
        self.assertEqual(result['content'], '2: two')
        self.assertEqual(result['next_start_line'], 3)
        self.assertTrue(result['truncated'])
        self.assertIn(f'/{SHA}/src/example.py#L2-L2', result['url'])

    async def test_reject_binary_large_wrong_type_and_out_of_range(self):
        for document, options in [
            (self.document('\x00binary'), {}),
            ({**self.document('x'), 'size': 200001}, {}),
            ({'type': 'symlink'}, {}),
            ([], {}),
            (self.document('x'), {'start_line': 2}),
            (self.document('x' * 16001), {}),
            ({**self.document('x'), 'content': '%%%invalid'}, {}),
        ]:
            with self.subTest(document=str(document)[:60]), self.assertRaises(HTTPException):
                await self.read(document, **options)

    async def test_character_limit_preserves_complete_lines(self):
        result = await self.read(self.document(('x' * 1000 + '\n') * 30))
        self.assertLessEqual(len(result['content']), 16000)
        self.assertEqual(result['next_start_line'], result['end_line'] + 1)

    def test_invalid_paths(self):
        for path in ['../secret', '/etc/passwd', 'src/../x', 'src\\x', 'a//b', 'x\n']:
            with self.assertRaises(HTTPException):
                validate_location('owner/repo', path)

    async def test_directory_limit_and_commit_resolution(self):
        async def get(repository, endpoint, params=None):
            if endpoint.startswith('commits/'):
                self.assertEqual(endpoint, 'commits/feature%2Ftest')
                return {'sha': SHA}
            self.assertEqual(params, {'ref': SHA})
            return [{'name': str(i), 'path': str(i), 'type': 'file'} for i in range(105)]
        with patch('source_tools.github_json', get):
            self.assertEqual(await resolve_commit('owner/repo', 'feature/test'), SHA)
            result = await read_source('owner/repo', '', SHA, directory=True)
        self.assertEqual(len(result['entries']), 100)
        self.assertTrue(result['truncated'])

    async def test_agent_pins_commit_and_resets_next_turn(self):
        responses = [
            tool_message('dir', '{"repository":"owner/repo"}', 'list_directory'),
            tool_message('file', '{"repository":"owner/repo","path":"main.py"}', 'read_file'),
            tool_message('switch', '{"repository":"owner/repo","path":"main.py","ref":"other"}', 'read_file'),
            final_message(),
        ]
        with patch('agent.request_completion', AsyncMock(side_effect=responses)), patch('tool_runtime.resolve_commit', AsyncMock(return_value=SHA)) as resolve, patch('tool_runtime.read_source', AsyncMock(return_value={'commit': SHA})) as read:
            result = await run_agent([{'role': 'user', 'content': 'Read source'}])
            resolve.assert_awaited_once()
            self.assertEqual(read.await_count, 2)
            self.assertTrue(all(c.args[2] == SHA for c in read.await_args_list))
            errors = [json.loads(m['content']) for m in result.messages if m['role'] == 'tool']
            self.assertEqual(errors[-1]['status'], 422)
        with patch('agent.request_completion', AsyncMock(side_effect=[tool_message('next', '{"repository":"owner/repo"}', 'list_directory'), final_message()])), patch('tool_runtime.resolve_commit', AsyncMock(return_value='b' * 40)) as resolve, patch('tool_runtime.read_source', AsyncMock(return_value={})):
            await run_agent([*result.messages, {'role': 'user', 'content': 'Refresh'}])
            resolve.assert_awaited_once()

    async def test_invalid_tool_arguments_never_access_network(self):
        for args in ['{"repository":"owner/repo","path":"../x"}',
                     '{"repository":"owner/repo","path":"x","line_count":201}',
                     '{"repository":"owner/repo","path":"x","start_line":true}']:
            with patch('agent.request_completion', AsyncMock(side_effect=[tool_message(arguments=args, name='read_file'), final_message()])), patch('tool_runtime.resolve_commit', AsyncMock()) as resolve:
                result = await run_agent([{'role': 'user', 'content': 'Read'}])
                resolve.assert_not_awaited()
                self.assertFalse(json.loads(result.messages[2]['content'])['ok'])
