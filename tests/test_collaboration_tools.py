import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from agent import run_agent
from collaboration_tools import fetch_issue, fetch_pull_request, read_pr_diff
from github import github_json
from test_agent import tool_message, final_message


def pr(head='b' * 40, count=21):
    return {'number': 7, 'title': 'Fix bug', 'body': 'Explanation', 'state': 'open',
            'html_url': 'https://github.com/owner/repo/pull/7', 'changed_files': count,
            'base': {'sha': 'a' * 40, 'repo': {'full_name': 'owner/repo'}},
            'head': {'sha': head, 'repo': {'full_name': 'fork/repo'}}}


def file(patch='@@ -1 +1 @@\n-old\n+new'):
    return {'filename': 'new.py', 'previous_filename': 'old.py', 'status': 'renamed',
            'additions': 1, 'deletions': 1, 'patch': patch, 'blob_url': 'https://github.com/owner/repo/blob/b/new.py'}


@patch.dict("os.environ", {"TOOL_TRANSPORT": "direct"})
class CollaborationTests(unittest.IsolatedAsyncioTestCase):
    async def test_issue_body_limit_and_pr_discrimination(self):
        document = {**pr(), 'body': 'x' * 12001, 'pull_request': {}, 'comments': 3}
        with patch('collaboration_tools.github_json', AsyncMock(return_value=document)):
            result = await fetch_issue('owner/repo', 7)
        self.assertEqual(result['kind'], 'pull_request')
        self.assertEqual(len(result['body']), 12000)
        self.assertTrue(result['body_truncated'])
        self.assertFalse(result['comments_included'])

    async def test_pr_details_include_fork_and_commits(self):
        with patch('collaboration_tools.github_json', AsyncMock(return_value=pr())):
            result = await fetch_pull_request('owner/repo', 7)
        self.assertEqual(result['head_repository'], 'fork/repo')
        self.assertEqual(result['head_sha'], 'b' * 40)

    async def test_diff_pagination_and_missing_patch(self):
        files = [file() for _ in range(19)] + [file(None)]
        request = AsyncMock(side_effect=[pr(), files, pr()])
        with patch('collaboration_tools.github_json', request):
            result = await read_pr_diff('owner/repo', 7)
        self.assertEqual(result['next_page'], 2)
        self.assertTrue(result['truncated'])
        self.assertFalse(result['files'][-1]['patch_available'])
        self.assertEqual(result['files'][0]['previous_path'], 'old.py')
        self.assertEqual(request.await_args_list[1].args, ('owner/repo', 'pulls/7/files', {'page': 1, 'per_page': 20}))

    async def test_diff_char_budget_and_last_page(self):
        with patch('collaboration_tools.github_json', AsyncMock(side_effect=[pr(count=5), [file('x' * 7000) for _ in range(5)], pr(count=5)])):
            result = await read_pr_diff('owner/repo', 7)
        self.assertLessEqual(sum(len(f['patch']) for f in result['files']), 24000)
        self.assertTrue(result['files'][-1]['patch_truncated'])
        self.assertIsNone(result['next_page'])

    async def test_diff_rejects_concurrent_or_previous_version_change(self):
        with patch('collaboration_tools.github_json', AsyncMock(side_effect=[pr(), [file()], pr('c' * 40)])), self.assertRaises(HTTPException) as error:
            await read_pr_diff('owner/repo', 7)
        self.assertEqual(error.exception.status_code, 409)
        request = AsyncMock(return_value=pr())
        with patch('collaboration_tools.github_json', request), self.assertRaises(HTTPException):
            await read_pr_diff('owner/repo', 7, expected_version=('a' * 40, 'c' * 40))
        self.assertEqual(request.await_count, 1)

    async def test_shared_transport_maps_errors_without_leaking_response(self):
        real = httpx.AsyncClient
        for status, expected in [(401, 401), (403, 429), (404, 404), (500, 502)]:
            def handler(request):
                self.assertEqual(request.method, 'GET')
                self.assertEqual(request.url.host, 'api.github.com')
                return httpx.Response(status, headers={'x-ratelimit-remaining': '0'}, json={'message': 'secret upstream'})
            with patch('github.httpx.AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw)), self.assertRaises(HTTPException) as error:
                await fetch_issue('owner/repo', 7)
            self.assertEqual(error.exception.status_code, expected)
            self.assertNotIn('secret', error.exception.detail)
        with patch('github.httpx.AsyncClient', side_effect=lambda **kw: real(transport=httpx.MockTransport(lambda req: httpx.Response(404)), **kw)):
            self.assertIsNone(await github_json('owner/repo', 'readme', allow_missing=True))

    async def test_agent_pr_diff_tool_results_and_stream_events(self):
        responses = [tool_message('pr', '{"repository":"owner/repo","number":7}', 'fetch_pull_request'),
                     tool_message('diff', '{"repository":"owner/repo","number":7}', 'read_pr_diff'), final_message()]
        events = []
        async def emit(kind, data):
            events.append((kind, data))
        with patch('agent.request_completion', AsyncMock(side_effect=responses)), patch('tool_runtime.fetch_pull_request', AsyncMock(return_value={'base_sha': 'a' * 40, 'head_sha': 'b' * 40})), patch('tool_runtime.read_pr_diff', AsyncMock(return_value={'base_sha': 'a' * 40, 'head_sha': 'b' * 40, 'files': []})) as diff:
            result = await run_agent([{'role': 'user', 'content': 'Review PR 7'}], emit)
        diff.assert_awaited_once_with('owner/repo', 7, 1, ('a' * 40, 'b' * 40))
        self.assertTrue(all(json.loads(m['content'])['ok'] for m in result.messages if m['role'] == 'tool'))
        self.assertEqual(len([e for e in events if e[0] == 'tool_end']), 2)

    async def test_invalid_numbers_and_pages_do_not_execute(self):
        for name, args in [('fetch_issue', {'number': 0}), ('fetch_pull_request', {'number': True}),
                           ('read_pr_diff', {'number': 7, 'page': 151})]:
            with patch('agent.request_completion', AsyncMock(side_effect=[tool_message(arguments=json.dumps({'repository': 'owner/repo', **args}), name=name), final_message()])), patch('github.httpx.AsyncClient') as client:
                result = await run_agent([{'role': 'user', 'content': 'Read'}])
                client.assert_not_called()
                self.assertFalse(json.loads(result.messages[2]['content'])['ok'])

    async def test_malformed_documents_are_reported(self):
        for function in [fetch_issue, fetch_pull_request]:
            with patch('collaboration_tools.github_json', AsyncMock(return_value={})), self.assertRaises(HTTPException) as error:
                await function('owner/repo', 7)
            self.assertEqual(error.exception.status_code, 502)
