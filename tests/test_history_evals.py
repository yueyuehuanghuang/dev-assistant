import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

import httpx

from evidence import sources_for_turn
from evaluate import evaluate_case, canonical_call, supported_url
from main import app
from memory import ConversationStore


class HistoryEvalTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_paginates_and_survives_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'history.db'
            app.state.conversations = ConversationStore(path)
            ids = []
            for i in range(3):
                identifier = uuid4(); ids.append(str(identifier))
                app.state.conversations.repository.save(identifier, [{'role': 'user', 'content': f'Question {i}'}, {'role': 'assistant', 'content': 'answer'}])
            app.state.conversations = ConversationStore(path)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                first = (await client.get('/conversations?limit=2')).json()
                second = (await client.get('/conversations?limit=2&offset=2')).json()
                self.assertEqual(first['next_offset'], 2)
                self.assertIsNone(second['next_offset'])
                self.assertEqual({c['conversation_id'] for c in first['conversations'] + second['conversations']}, set(ids))
                self.assertEqual((await client.get('/conversations?limit=999')).status_code, 422)

    async def test_history_returns_actual_sources_but_not_reasoning(self):
        app.state.conversations = ConversationStore()
        url = 'https://github.com/demo/project/issues/3'
        async def generate(messages):
            from agent import AgentResult
            return AgentResult(answer='summary', messages=[messages[-1],
                {'role': 'tool', 'content': json.dumps({'ok': True, 'issue': {'url': url, 'title': 'Crash'}})},
                {'role': 'assistant', 'content': 'summary', 'reasoning_content': 'hidden'}])
        identifier, _ = await app.state.conversations.chat('read issue', None, generate)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = (await client.get(f'/conversations/{identifier}')).json()
        self.assertEqual(response['messages'][-1]['sources'][0]['url'], url)
        self.assertNotIn('reasoning_content', str(response))

    def test_sources_ignore_failed_tools_old_turn_and_embedded_content(self):
        messages = [{'role': 'user', 'content': 'old'},
                    {'role': 'tool', 'content': json.dumps({'ok': True, 'url': 'https://github.com/old/repo'})},
                    {'role': 'user', 'content': 'new'},
                    {'role': 'tool', 'content': json.dumps({'ok': False, 'url': 'https://github.com/fail/repo'})},
                    {'role': 'tool', 'content': json.dumps({'ok': True, 'body': {'url': 'https://github.com/injected/repo'}, 'url': 'javascript:bad'})}]
        self.assertEqual(sources_for_turn(messages), [])

    async def test_evaluation_detects_wrong_answer_and_missing_citation(self):
        case = {'id': 'negative', 'prompt': 'read', 'tools': [], 'replay_answer': 'unrelated',
                'answer_terms': ['expected'], 'required_urls': ['https://github.com/demo/project']}
        result = await evaluate_case(case)
        self.assertFalse(result['passed'])
        self.assertFalse(result['answer_terms_ok'])
        self.assertFalse(result['required_citations_present'])

    def test_evaluation_normalizes_default_page(self):
        base = {'repository': 'owner/repo', 'number': 3}
        self.assertEqual(canonical_call('read_pr_diff', base), canonical_call('read_pr_diff', {**base, 'page': 1}))

    def test_citations_accept_only_lines_actually_read(self):
        url = 'https://github.com/a/b/blob/' + 'a' * 40 + '/main.py'
        evidence = {url + '#L3-L8'}
        self.assertTrue(supported_url(url + '#L4', evidence))
        self.assertTrue(supported_url(url + '#L4-L7', evidence))
        self.assertFalse(supported_url(url + '#L1', evidence))
        self.assertFalse(supported_url(url + '#L4-L99', evidence))
        self.assertFalse(supported_url(url.replace('a' * 40, 'b' * 40) + '#L4', evidence))

    async def test_markdown_url_labels_do_not_corrupt_citations(self):
        url = 'https://github.com/a/b/issues/1'
        case = {'id': 'link', 'prompt': 'read', 'tools': [
            {'name': 'fetch_issue', 'arguments': {'repository': 'a/b', 'number': 1}, 'result': {'ok': True, 'url': url}}],
            'replay_answer': f'[{url}]({url})', 'answer_terms': [], 'required_urls': [url]}
        self.assertTrue((await evaluate_case(case))['passed'])
