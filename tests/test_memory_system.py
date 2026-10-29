import json
import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
from fastapi import HTTPException

from agent import run_agent
from context_memory import estimated_tokens, prepare_context, active_memory
from main import app
from memory import ConversationStore
from memory_repository import visible_turn
from test_agent import tool_message, final_message


@patch.dict('os.environ', {'TOOL_TRANSPORT': 'direct', 'CONTEXT_TOKEN_BUDGET': '16000'})
class MemorySystemTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'chat.db'
        self.store = ConversationStore(self.path)

    async def seed(self, message='first', project=None, kb=None):
        return (await self.store.chat(message, None, AsyncMock(return_value='answer'), memory_project=project, knowledge_base_id=kb))[0]

    async def test_archives_every_turn_and_restores_summary(self):
        identifier = await self.seed('Original decision: Python 3.12')
        for i in range(13):
            await self.store.chat(f'Follow {i}', identifier, AsyncMock(return_value='answer'))
        self.assertEqual(sum(m['role'] == 'user' for m in self.store.get_history(identifier)), 10)
        self.assertEqual(self.store.get_memory_state(identifier)['archived_turns'], 14)
        self.assertTrue(self.store.get_memory_state(identifier)['summary'])
        restored = ConversationStore(self.path)
        self.assertIn('Python 3.12', visible_turn(restored.repository.read_turn(identifier, 1)['messages']))
        self.assertEqual(restored.get_memory_state(identifier)['summary'], self.store.get_memory_state(identifier)['summary'])

    async def test_only_explicit_user_requests_become_pending_candidates(self):
        identifier = await self.seed('记住：项目使用 Python 3.12', 'demo')
        items = self.store.repository.list_memories(identifier)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['status'], 'pending')
        self.assertEqual(self.store.repository.search_memories(identifier, 'Python'), [])
        await self.store.chat('ordinary question', identifier, AsyncMock(return_value='记住：助手虚构的事实'))
        self.assertEqual(len(self.store.repository.list_memories(identifier)), 1)

    async def test_approved_only_shared_with_same_project_and_kb(self):
        first = await self.seed('记住：项目使用 Python 3.12', 'demo')
        item = self.store.repository.list_memories(first)[0]
        self.store.repository.change_memory(first, item['id'], 'approve', item['version'])
        captured = []
        async def generate(messages):
            captured.extend(messages); return 'answer'
        second, _ = await self.store.chat('what runtime?', None, generate, memory_project='demo')
        self.assertTrue(any('Python 3.12' in m.get('content', '') for m in captured if m['role'] == 'system'))
        self.assertEqual(len(self.store.repository.search_memories(second, 'Python')), 1)
        other = await self.seed('hello', 'another')
        self.assertFalse(self.store.repository.search_memories(other, 'Python'))
        ungrouped = await self.seed('hello')
        self.assertFalse(self.store.repository.search_memories(ungrouped, 'Python'))
        kb = '12345678-1234-1234-1234-123456789abc'
        with_kb = await self.seed('hello', 'demo', kb)
        self.assertFalse(self.store.repository.search_memories(with_kb, 'Python'))

    async def test_replacement_revoke_and_version_conflict(self):
        identifier = await self.seed('hello', 'demo')
        old = self.store.repository.propose_memory(identifier, 'Use Python 3.11')
        self.store.repository.change_memory(identifier, old['id'], 'approve', 1)
        new = self.store.repository.propose_memory(identifier, 'Use Python 3.12')
        self.store.repository.change_memory(identifier, new['id'], 'approve', 1, old['id'])
        approved = self.store.repository.list_memories(identifier, 'approved')
        self.assertEqual([m['content'] for m in approved], ['Use Python 3.12'])
        with self.assertRaises(HTTPException) as error:
            self.store.repository.change_memory(identifier, new['id'], 'archive', 1)
        self.assertEqual(error.exception.status_code, 409)
        self.store.repository.change_memory(identifier, new['id'], 'archive', 2)
        self.assertFalse(self.store.repository.search_memories(identifier, 'Python'))
        self.store.repository.change_memory(identifier, new['id'], 'reopen', 3)
        self.assertFalse(self.store.repository.search_memories(identifier, 'Python'))
        self.store.repository.change_memory(identifier, new['id'], 'approve', 4)
        self.assertEqual(self.store.repository.list_memories(identifier, 'approved')[0]['version'], 5)

    async def test_concurrent_agent_memory_contexts_do_not_leak(self):
        identifiers = []
        for name in ['alpha', 'beta']:
            identifier = await self.seed(f'记住：Python 约定属于 {name}', name)
            item = self.store.repository.list_memories(identifier)[0]
            self.store.repository.change_memory(identifier, item['id'], 'approve', 1)
            identifiers.append(identifier)
        async def completion(messages, **kwargs):
            await asyncio.sleep(0)
            if messages[-1]['role'] == 'user':
                return tool_message('recall', '{"query":"Python"}', 'search_memory')
            result = json.loads(messages[-1]['content'])['memory']['results']
            return final_message(result[0]['content'])
        with patch('agent.request_completion', completion):
            results = await asyncio.gather(*(self.store.chat('recall', identifier, run_agent) for identifier in identifiers))
        self.assertIn('alpha', results[0][1]); self.assertNotIn('beta', results[0][1])
        self.assertIn('beta', results[1][1]); self.assertNotIn('alpha', results[1][1])
        self.assertIsNone(active_memory.get())

    async def test_candidate_and_archive_roll_back_together(self):
        identifier = await self.seed('initial')
        before = self.store.get_memory_state(identifier)
        original = self.store.repository.save_memory_state
        def fail_after_write(*args):
            original(*args)
            raise sqlite3.OperationalError('disk failed')
        with patch.object(self.store.repository, 'save_memory_state', fail_after_write), self.assertRaises(sqlite3.OperationalError):
            await self.store.chat('记住：不能落盘的候选', identifier, AsyncMock(return_value='answer'))
        restored = ConversationStore(self.path)
        self.assertEqual(restored.get_memory_state(identifier)['archived_turns'], before['archived_turns'])
        self.assertFalse(restored.repository.list_memories(identifier))
        self.assertEqual(self.store.get_history(identifier), restored.get_history(identifier))

    async def test_failed_model_saves_no_archive_or_candidate(self):
        identifier = await self.seed()
        with self.assertRaises(RuntimeError):
            await self.store.chat('记住：失败轮次', identifier, AsyncMock(side_effect=RuntimeError('offline')))
        self.assertEqual(self.store.get_memory_state(identifier)['archived_turns'], 1)
        self.assertFalse(self.store.repository.list_memories(identifier))
        self.assertIsNone(active_memory.get())

    def test_budget_preserves_whole_tool_turns_and_never_mutates_archive(self):
        history = []
        for i in range(5):
            history.extend([{'role': 'user', 'content': 'request' + 'x' * 3500},
                tool_message(f'call_{i}'), {'role': 'tool', 'tool_call_id': f'call_{i}', 'content': json.dumps({'ok': True, 'content': 'x' * 30000})},
                final_message('answer')])
        original = json.dumps(history)
        with patch.dict('os.environ', {'CONTEXT_TOKEN_BUDGET': '5000'}):
            context, meta = prepare_context(history, 'next', [], [])
        self.assertLessEqual(estimated_tokens(context), 2500)
        self.assertGreater(meta['dropped_context_turns'], 0)
        ids = {c['id'] for m in context for c in m.get('tool_calls', [])}
        self.assertTrue(all(m['tool_call_id'] in ids for m in context if m['role'] == 'tool'))
        self.assertEqual(json.dumps(history), original)

    def test_small_budget_defers_excess_summary_and_memory(self):
        summary = [{'user_excerpt': '字' * 400, 'answer_excerpt': '字' * 250}] * 3
        approved = [{'content': '字' * 1000}]
        with patch.dict('os.environ', {'CONTEXT_TOKEN_BUDGET': '4096'}):
            messages, meta = prepare_context([], 'small question', summary, approved)
        self.assertLessEqual(estimated_tokens(messages), 1596)
        self.assertEqual(len(summary), 3); self.assertEqual(len(approved), 1)

    async def test_large_current_message_fails_without_saving(self):
        with patch.dict('os.environ', {'CONTEXT_TOKEN_BUDGET': '4096'}), self.assertRaises(HTTPException) as error:
            await self.store.chat('字' * 12000, None, AsyncMock(return_value='answer'))
        self.assertEqual(error.exception.status_code, 413)
        with self.store.repository.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0], 0)

    async def test_tools_recall_archive_and_reject_cross_scope_arguments(self):
        identifier = await self.seed('Original decision: Python 3.12', 'demo')
        responses = [tool_message(arguments='{"query":"Python"}', name='search_history'), final_message('found')]
        with patch('agent.request_completion', AsyncMock(side_effect=responses)):
            await self.store.chat('what did we say?', identifier, run_agent)
        tools = [m for m in self.store.get_history(identifier) if m['role'] == 'tool']
        self.assertTrue(json.loads(tools[-1]['content'])['memory']['results'])
        responses = [tool_message('bad', '{"query":"Python","conversation_id":"another"}', 'search_memory'), final_message('error')]
        with patch('agent.request_completion', AsyncMock(side_effect=responses)):
            await self.store.chat('try', identifier, run_agent)
        self.assertFalse(json.loads([m for m in self.store.get_history(identifier) if m['role'] == 'tool'][-1]['content'])['ok'])

    async def test_api_history_memory_lifecycle_and_escaping(self):
        identifier = await self.seed('记住：<script>unsafe()</script> Python', 'demo')
        app.state.conversations = self.store
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            data = (await client.get(f'/api/conversations/{identifier}/memory')).json()
            item = data['items'][0]
            response = await client.post(f'/api/conversations/{identifier}/memories/{item["id"]}/actions', json={'action': 'approve', 'version': 1})
            self.assertEqual(response.status_code, 200)
            source = await client.get(f'/memory/{identifier}/items/{item["id"]}')
            self.assertIn('&lt;script&gt;', source.text)
            self.assertNotIn('<script>', source.text)
            turns = (await client.get(f'/conversations/{identifier}/transcript?limit=1')).json()
            self.assertEqual(turns['turns'][0]['turn_number'], 1)
            self.assertIsNone(turns['next_offset'])
            self.assertNotIn('reasoning_content', json.dumps(turns))
            response = await client.post('/chat/stream', json={'message': 'switch', 'conversation_id': str(identifier), 'memory_project': 'other'})
            self.assertEqual(response.status_code, 409)

    async def test_old_database_migration_preserves_available_turns(self):
        old = Path(self.temp.name) / 'old.db'
        with closing(sqlite3.connect(old)) as db, db:
            db.execute('CREATE TABLE conversations(id TEXT PRIMARY KEY,messages TEXT NOT NULL,updated_at TEXT DEFAULT CURRENT_TIMESTAMP)')
            db.execute('INSERT INTO conversations(id,messages) VALUES (?,?)', ('12345678-1234-1234-1234-123456789abc', json.dumps([{'role': 'user', 'content': 'old'}, final_message('saved')])) )
        restored = ConversationStore(old)
        identifier = UUID('12345678-1234-1234-1234-123456789abc')
        self.assertEqual(restored.get_memory_state(identifier)['archived_turns'], 1)
        self.assertIn('old', visible_turn(restored.repository.read_turn(identifier, 1)['messages']))
