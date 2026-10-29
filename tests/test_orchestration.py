import json
import unittest
from unittest.mock import AsyncMock, patch

from agent import AgentProtocolError, AgentResult, _run_agent
from orchestration import workflow
from evidence import sources_for_turn


class OrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_routes_specialists_and_preserves_real_sources(self):
        messages = [{'role': 'user', 'content': 'Compare source and issue'}]
        plan = {'role': 'assistant', 'content': json.dumps({'tasks': [
            {'agent': 'code', 'task': 'Read source'}, {'agent': 'issue', 'task': 'Read issue'}, {'agent': 'pr', 'task': 'Read PR'}]})}
        call = {'id': 'same-id', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}
        def result():
            return AgentResult('finding', [messages[0], {'role': 'assistant', 'tool_calls': [dict(call)]},
                {'role': 'tool', 'tool_call_id': 'same-id', 'content': json.dumps({'ok': True, 'url': 'https://github.com/a/b/blob/abc/main.py'})},
                {'role': 'assistant', 'content': 'finding'}])
        worker = AsyncMock(side_effect=[result(), result(), result()])
        with patch('orchestration.request_completion', AsyncMock(side_effect=[plan, {'role': 'assistant', 'content': 'summary'}])), patch('orchestration._run_agent', worker):
            completed = await workflow(messages, None, None)
        self.assertEqual(completed.answer, 'summary')
        self.assertEqual(worker.call_args_list[0].args[4], {'fetch_repository', 'list_directory', 'read_file'})
        self.assertEqual(worker.call_args_list[1].args[4], {'fetch_repository', 'fetch_issue'})
        self.assertEqual(worker.call_args_list[2].args[4], {'fetch_repository', 'fetch_pull_request', 'read_pr_diff', 'read_file'})
        ids = [c['id'] for m in completed.messages for c in m.get('tool_calls', [])]
        self.assertEqual(len(set(ids)), 3)
        self.assertEqual(len(sources_for_turn(completed.messages)), 1)
        self.assertEqual(messages, [{'role': 'user', 'content': 'Compare source and issue'}])

    async def test_invalid_or_oversized_plan_fails_before_execution(self):
        for content in ['not JSON', '{"tasks":[{"agent":"shell","task":"run"}]}', json.dumps({'tasks': [{'agent': 'code', 'task': 'x'}] * 4})]:
            with patch('orchestration.request_completion', AsyncMock(return_value={'content': content})), patch('orchestration._run_agent', AsyncMock()) as worker:
                with self.assertRaises(AgentProtocolError):
                    await workflow([{'role': 'user', 'content': 'x'}], None, None)
                worker.assert_not_awaited()

    async def test_specialist_cannot_execute_other_roles_tool(self):
        response = {'role': 'assistant', 'tool_calls': [{'id': 'x', 'type': 'function', 'function': {'name': 'fetch_issue', 'arguments': '{}'}}]}
        with patch('agent.request_completion', AsyncMock(return_value=response)), patch('agent.execute_tool', AsyncMock()) as execute:
            with self.assertRaises(AgentProtocolError):
                await _run_agent([{'role': 'user', 'content': 'x'}], allowed_tools={'read_file'})
            execute.assert_not_awaited()
