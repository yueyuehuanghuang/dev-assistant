"""Exercise the compiled graph, scope propagation and chat commit boundaries."""
import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from agent import AgentResult, run_agent
from context_memory import active_memory
from evidence import sources_for_turn
from llm import EmptyModelResponseError
from main import app
from memory import ConversationStore
from orchestration import WORKFLOW_GRAPH, WorkflowContext, workflow


def plan(*roles):
    return {'role': 'assistant', 'content': json.dumps({'tasks': [
        {'agent': role, 'task': f'Check {role}'} for role in roles]})}


def initial_state(message='question'):
    return {'messages': [{'role': 'user', 'content': message}], 'tasks': [],
            'task_index': 0, 'findings': [], 'evidence_messages': [], 'answer': ''}


def finding(messages):
    return AgentResult('observed', [deepcopy(messages[-1]), {'role': 'assistant', 'content': 'observed'}])


@patch.dict('os.environ', {'AGENT_MODE': 'multi', 'TOOL_TRANSPORT': 'direct'})
class LangGraphTests(unittest.IsolatedAsyncioTestCase):
    async def test_compiled_graph_visits_workers_in_plan_order(self):
        async def worker(messages, *args):
            return finding(messages)
        inputs = initial_state()
        original = deepcopy(inputs)
        with patch('orchestration.request_completion', AsyncMock(side_effect=[plan('pr', 'code', 'pr'),
                {'role': 'assistant', 'content': 'done'}])), patch('orchestration._run_agent', worker):
            updates = [part async for part in WORKFLOW_GRAPH.astream(
                inputs, context=WorkflowContext(), stream_mode='updates', config={'recursion_limit': 12})]
        self.assertEqual([next(iter(part)) for part in updates],
                         ['planner', 'orchestrator', 'pr', 'orchestrator', 'code', 'orchestrator', 'pr', 'orchestrator', 'synthesis'])
        self.assertEqual(inputs, original)
        self.assertEqual(updates[-1]['synthesis']['answer'], 'done')

    async def test_empty_plan_skips_workers_and_streams_only_final_answer(self):
        events = []
        async def emit(event, data):
            events.append((event, data))
        async def complete(messages, **kwargs):
            if 'on_delta' in kwargs:
                await kwargs['on_delta']('hello')
                return {'role': 'assistant', 'content': 'hello'}
            return plan()
        with patch('orchestration.request_completion', complete), patch('orchestration._run_agent', AsyncMock()) as worker:
            result = await workflow([{'role': 'user', 'content': 'hi'}], emit, None)
        worker.assert_not_awaited()
        self.assertEqual(result.answer, 'hello')
        self.assertEqual([data['node'] for event, data in events if event == 'status'],
                         ['planner', 'orchestrator', 'synthesis'])
        self.assertTrue(all(data['orchestration'] == 'langgraph' for event, data in events if event == 'status'))
        self.assertEqual([data['text'] for event, data in events if event == 'delta'], ['hello'])

    async def test_runtime_resources_and_pins_are_isolated_between_chats(self):
        seen = {}
        async def complete(messages, **kwargs):
            return plan('code', 'code') if 'You are the Planner.' in messages[0]['content'] else {'content': 'done'}
        async def worker(messages, emit, remote, knowledge, allowed, pins):
            question = messages[-1]['content']
            pins.setdefault('owner', question)
            await asyncio.sleep(0)
            seen.setdefault(question, []).append((remote, knowledge, pins, active_memory.get()))
            self.assertEqual(pins['owner'], question)
            return finding(messages)
        remote_a, remote_b = object(), object()
        knowledge_a, knowledge_b = object(), object()
        async def invoke(name, remote, knowledge):
            token = active_memory.set({'scope_key': name})
            try:
                return await workflow([{'role': 'user', 'content': name}], None, knowledge, remote)
            finally:
                active_memory.reset(token)
        with patch('orchestration.request_completion', complete), patch('orchestration._run_agent', worker):
            await asyncio.gather(invoke('A', remote_a, knowledge_a), invoke('B', remote_b, knowledge_b))
        self.assertIs(seen['A'][0][0], remote_a)
        self.assertIs(seen['B'][0][1], knowledge_b)
        self.assertIs(seen['A'][0][2], seen['A'][1][2])
        self.assertIsNot(seen['A'][0][2], seen['B'][0][2])
        self.assertEqual(seen['A'][0][3]['scope_key'], 'A')
        self.assertEqual(seen['B'][1][3]['scope_key'], 'B')
        self.assertIsNone(active_memory.get())

    async def test_memory_tools_keep_scope_and_real_evidence_through_graph(self):
        with TemporaryDirectory() as directory:
            store = ConversationStore(Path(directory) / 'chat.db')
            identifier, _ = await store.chat('Original decision: Python 3.12', None, AsyncMock(return_value='saved'))
            responses = [
                {'role': 'assistant', 'tool_calls': [{'id': 'history', 'type': 'function',
                    'function': {'name': 'search_history', 'arguments': '{"query":"Python"}'}}]},
                {'role': 'assistant', 'content': 'The original decision was Python 3.12.'}]
            with patch('orchestration.request_completion', AsyncMock(side_effect=[plan('knowledge'),
                    {'role': 'assistant', 'content': 'Python 3.12'}])), patch('agent.request_completion', AsyncMock(side_effect=responses)):
                await store.chat('Recall Python', identifier, run_agent)
            history = store.get_history(identifier)
            tools = [m for m in history if m['role'] == 'tool']
            self.assertTrue(json.loads(tools[-1]['content'])['memory']['results'])
            self.assertTrue(sources_for_turn(history)[0]['url'].startswith(f'/memory/{identifier}/'))
            self.assertEqual(store.get_memory_state(identifier)['archived_turns'], 2)
            self.assertIsNone(active_memory.get())

    async def test_worker_failure_and_cancellation_never_commit_half_a_turn(self):
        for cancelled in [False, True]:
            with self.subTest(cancelled=cancelled), TemporaryDirectory() as directory:
                store = ConversationStore(Path(directory) / 'chat.db')
                identifier, _ = await store.chat('initial', None, AsyncMock(return_value='saved'))
                before = deepcopy(store.get_history(identifier))
                entered = asyncio.Event()
                async def fail(messages, *args):
                    entered.set()
                    if cancelled:
                        await asyncio.Event().wait()
                    raise RuntimeError('worker offline')
                with patch('orchestration.request_completion', AsyncMock(return_value=plan('code'))), patch('orchestration._run_agent', fail):
                    task = asyncio.create_task(store.chat('new request', identifier, run_agent))
                    await asyncio.wait_for(entered.wait(), 5)
                    if cancelled:
                        task.cancel()
                    with self.assertRaises(asyncio.CancelledError if cancelled else RuntimeError):
                        await task
                self.assertEqual(ConversationStore(store.repository.path).get_history(identifier), before)
                self.assertEqual(store.get_memory_state(identifier)['archived_turns'], 1)
                self.assertIsNone(active_memory.get())

    async def test_sse_chat_runs_graph_and_commits_done_with_real_tool_sources(self):
        async def worker(messages, emit, *args):
            await emit('delta', {'text': 'worker draft must stay private'})
            await emit('tool_start', {'id': 'reused-id', 'name': 'read_file'})
            await emit('tool_end', {'id': 'reused-id', 'name': 'read_file', 'ok': True, 'message': 'read'})
            call = {'id': 'reused-id', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}
            return AgentResult('observed', [messages[-1], {'role': 'assistant', 'tool_calls': [call]},
                {'role': 'tool', 'tool_call_id': 'reused-id', 'content': json.dumps({'ok': True, 'url': 'https://github.com/a/b/blob/abc/main.py'})},
                {'role': 'assistant', 'content': 'observed'}])
        async def complete(messages, **kwargs):
            if 'on_delta' not in kwargs:
                return plan('code', 'code')
            await kwargs['on_delta']('final answer')
            return {'content': 'final answer'}
        with TemporaryDirectory() as directory:
            store = ConversationStore(Path(directory) / 'chat.db')
            with patch.object(app.state, 'conversations', store), patch('orchestration.request_completion', complete), patch('orchestration._run_agent', worker):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    response = await client.post('/chat/stream', json={'message': 'read source'})
            self.assertEqual(response.status_code, 200)
            frames = [(frame.splitlines()[0][7:], json.loads(frame.split('data: ', 1)[1]))
                      for frame in response.text.split('\n\n') if frame.startswith('event:')]
            self.assertEqual(frames[-1][0], 'done')
            self.assertEqual(frames[-1][1]['answer'], 'final answer')
            self.assertEqual(len(frames[-1][1]['sources']), 1)
            self.assertEqual([data['text'] for event, data in frames if event == 'delta'], ['final answer'])
            starts = [data['id'] for event, data in frames if event == 'tool_start']
            ends = [data['id'] for event, data in frames if event == 'tool_end']
            self.assertEqual(starts, ends)
            self.assertEqual(len(set(starts)), 2)

    async def test_synthesis_failure_does_not_return_a_successful_answer(self):
        with patch('orchestration.request_completion', AsyncMock(side_effect=[plan(), {'content': ''}])):
            with self.assertRaises(EmptyModelResponseError):
                await workflow([{'role': 'user', 'content': 'hi'}], None, None)

    async def test_mcp_session_is_shared_by_nodes_and_closed_on_success_or_error(self):
        remote = object()
        for fails in [False, True]:
            with self.subTest(fails=fails):
                sessions, captured = [], []
                @asynccontextmanager
                async def session():
                    sessions.append('opened')
                    try:
                        yield remote
                    finally:
                        sessions.append('closed')
                async def worker(messages, emit, connection, *args):
                    captured.append(connection)
                    if fails:
                        raise RuntimeError('specialist failed')
                    return finding(messages)
                with patch.dict('os.environ', {'TOOL_TRANSPORT': 'mcp'}), patch('mcp_client.github_session', session), patch('orchestration._run_agent', worker), patch('orchestration.request_completion', AsyncMock(side_effect=[
                        plan('code', 'issue'), {'content': 'done'}])):
                    if fails:
                        with self.assertRaisesRegex(RuntimeError, 'specialist failed'):
                            await run_agent([{'role': 'user', 'content': 'x'}])
                    else:
                        self.assertEqual((await run_agent([{'role': 'user', 'content': 'x'}])).answer, 'done')
                self.assertEqual(sessions, ['opened', 'closed'])
                self.assertTrue(all(item is remote for item in captured))
                self.assertEqual(len(captured), 1 if fails else 2)
