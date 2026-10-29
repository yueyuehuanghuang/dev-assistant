import asyncio
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx

from agent import AgentResult, run_agent
from llm import EmptyModelResponseError, stream_completion
from main import app
from memory import ConversationStore
from schemas import ChatRequest
from streaming import chat_stream


def chunk(delta, finish=None):
    return 'data: ' + json.dumps({'choices': [{'delta': delta, 'finish_reason': finish}]}) + '\n\n'


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, text):
        self.data = text.encode('utf-8')

    async def __aiter__(self):
        for start in range(0, len(self.data), 7):
            yield self.data[start:start + 7]


@patch.dict("os.environ", {"TOOL_TRANSPORT": "direct"})
class StreamingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app.state.conversations = ConversationStore()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()

    async def completion(self, text):
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=ByteStream(text)))
        deltas = []

        async def emit(text):
            deltas.append(text)

        async with httpx.AsyncClient(transport=transport) as client:
            message = await stream_completion(client, 'https://model.test', 'test', {}, emit)
        return message, deltas

    async def test_real_provider_chunks_and_reasoning_is_not_emitted(self):
        text = chunk({'reasoning_content': 'private-provider-state'}) + chunk({'content': '你'}) + chunk({'content': '好'}, 'stop') + 'data: [DONE]\n\n'
        message, deltas = await self.completion(text)
        self.assertEqual(deltas, ['你', '好'])
        self.assertEqual(message['content'], '你好')
        self.assertEqual(message['reasoning_content'], 'private-provider-state')

    async def test_fragmented_tool_arguments(self):
        text = chunk({'tool_calls': [{'index': 0, 'id': 'call_1', 'type': 'function', 'function': {'name': 'fetch_repository', 'arguments': '{"repository":'}}]})
        text += chunk({'tool_calls': [{'index': 0, 'function': {'arguments': '"owner/repo"}'}}]}, 'tool_calls') + 'data: [DONE]\n\n'
        message, deltas = await self.completion(text)
        self.assertEqual(json.loads(message['tool_calls'][0]['function']['arguments']), {'repository': 'owner/repo'})
        self.assertEqual(deltas, [])

    async def test_truncation_and_missing_done_fail(self):
        for text in [chunk({'content': 'partial'}), chunk({'content': 'partial'}, 'length') + 'data: [DONE]\n\n']:
            with self.assertRaises(EmptyModelResponseError):
                await self.completion(text)

    async def test_sse_success_commits_then_done_and_restores_visible_history(self):
        async def generate(messages, emit):
            await emit('status', {'message': 'generating', 'reset_answer': True})
            await emit('delta', {'text': 'hello'})
            return AgentResult('hello', messages[-1:] + [{'role': 'assistant', 'content': 'hello', 'reasoning_content': 'hidden'}])

        with TemporaryDirectory() as directory:
            path = Path(directory) / 'history.db'
            app.state.conversations = ConversationStore(path)
            with patch('streaming.run_agent', generate):
                response = await self.client.post('/chat/stream', json={'message': 'hi'})
            self.assertEqual(response.status_code, 200)
            self.assertIn('text/event-stream', response.headers['content-type'])
            frames = [frame for frame in response.text.split('\n\n') if frame]
            self.assertTrue(frames[-1].startswith('event: done'))
            data = json.loads(frames[-1].split('data: ', 1)[1])
            app.state.conversations = ConversationStore(path)
            restored = await self.client.get('/conversations/' + data['conversation_id'])
            self.assertEqual(restored.status_code, 200)
            self.assertEqual(restored.json()['messages'][-1], {'role': 'assistant', 'content': 'hello', 'sources': []})
            self.assertNotIn('reasoning_content', restored.text)

    async def test_failure_emits_error_and_does_not_commit(self):
        async def fail(messages, emit):
            await emit('delta', {'text': 'partial'})
            raise EmptyModelResponseError

        with patch('streaming.run_agent', fail):
            response = await self.client.post('/chat/stream', json={'message': 'hi'})
        self.assertIn('event: error', response.text)
        self.assertNotIn('event: done', response.text)
        self.assertEqual(app.state.conversations.conversations, {})

    async def test_database_failure_never_emits_done(self):
        async def generate(messages, emit):
            await emit('delta', {'text': 'answer'})
            return AgentResult('answer', messages[-1:] + [{'role': 'assistant', 'content': 'answer'}])

        with TemporaryDirectory() as directory:
            store = ConversationStore(Path(directory) / 'history.db')
            app.state.conversations = store
            with patch('streaming.run_agent', generate), patch.object(
                store.repository, 'save', side_effect=sqlite3.OperationalError('disk error')
            ):
                response = await self.client.post('/chat/stream', json={'message': 'hi'})
            self.assertIn('event: error', response.text)
            self.assertNotIn('event: done', response.text)
            self.assertEqual(store.conversations, {})

    async def test_close_stream_cancels_generation_and_releases_session_lock(self):
        store = app.state.conversations
        identifier, _ = await store.chat('first', None, AsyncMock(return_value='answer'))
        cancelled = asyncio.Event()

        async def wait(messages, emit):
            try:
                await emit('delta', {'text': 'partial'})
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with patch('streaming.run_agent', wait):
            response = await chat_stream(ChatRequest(message='hi', conversation_id=identifier), SimpleNamespace(app=app))
            iterator = response.body_iterator
            await anext(iterator)
            await anext(iterator)
            await iterator.aclose()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(store.conversations[identifier].lock.locked())
        self.assertEqual(len(store.conversations[identifier].messages), 2)

    async def test_tool_events_and_delta_order(self):
        events = []

        async def emit(event, data):
            events.append((event, data))

        count = 0

        async def completion(messages, on_delta, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                return {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call', 'type': 'function', 'function': {'name': 'fetch_repository', 'arguments': 'bad'}}]}
            await on_delta('answer')
            return {'role': 'assistant', 'content': 'answer'}

        with patch('agent.request_completion', completion):
            await run_agent([{'role': 'user', 'content': 'hi'}], emit=emit)
        self.assertEqual([event for event, _ in events], ['status', 'tool_start', 'tool_end', 'status', 'delta'])
        self.assertFalse(events[2][1]['ok'])

    async def test_validation_and_page_assets(self):
        self.assertEqual((await self.client.post('/chat/stream', json={'message': ''})).status_code, 422)
        self.assertEqual((await self.client.get('/conversations/00000000-0000-0000-0000-000000000001')).status_code, 404)
        for path in ['/', '/static/app.mjs', '/static/stream.mjs', '/static/style.css']:
            self.assertEqual((await self.client.get(path)).status_code, 200)
