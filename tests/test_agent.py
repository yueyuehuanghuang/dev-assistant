import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from agent import AgentLimitError, AgentProtocolError, MAX_TOOL_CALLS, run_agent
from github import RepositorySnapshot
from memory import ConversationStore, MAX_HISTORY_TURNS


def tool_message(identifier="call_1", arguments='{"repository":"owner/repo"}', name="fetch_repository"):
    return {"role": "assistant", "content": None, "reasoning_content": "test-provider-state", "tool_calls": [{
        "id": identifier, "type": "function",
        "function": {"name": name, "arguments": arguments},
    }]}


def final_message(answer="Final answer"):
    return {"role": "assistant", "content": answer}


@patch.dict("os.environ", {"TOOL_TRANSPORT": "direct"})
class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_question_does_not_execute_tool(self):
        with patch("agent.request_completion", AsyncMock(return_value=final_message())), patch("tool_runtime.fetch_repository", AsyncMock()) as fetch:
            result = await run_agent([{"role": "user", "content": "What is Python?"}])
        fetch.assert_not_awaited()
        self.assertEqual(result.answer, "Final answer")
        self.assertEqual(len(result.messages), 2)

    async def test_tool_result_is_returned_with_matching_id(self):
        snapshot = RepositorySnapshot(full_name="owner/repo", url="https://github.com/owner/repo",
            description="Test", language="Python", default_branch="main", stars=1,
            root_entries=["README.md"], root_truncated=False, readme="Test README", readme_truncated=False)
        seen = []

        async def completion(messages, **kwargs):
            # 拷贝当时的请求，避免之后追加消息影响断言。
            seen.append(json.loads(json.dumps(messages)))
            return tool_message() if len(seen) == 1 else final_message()

        original = [{"role": "user", "content": "Read owner/repo"}]
        with patch("agent.request_completion", completion), patch("tool_runtime.fetch_repository", AsyncMock(return_value=snapshot)) as fetch:
            result = await run_agent(original)
        fetch.assert_awaited_once_with("owner/repo")
        self.assertEqual(len(original), 1)
        self.assertEqual(seen[1][-1]["tool_call_id"], "call_1")
        self.assertTrue(json.loads(seen[1][-1]["content"])["ok"])
        self.assertEqual(seen[1][-2]["reasoning_content"], "test-provider-state")
        self.assertEqual([item["role"] for item in result.messages], ["user", "assistant", "tool", "assistant"])

    async def test_unknown_tool_and_bad_arguments_are_not_executed(self):
        for args, name in [("not json", "fetch_repository"), ('{"repository":123}', "fetch_repository"),
                           ('{"repository":"owner/repo","extra":true}', "fetch_repository"),
                           ('{"repository":"owner/repo"}', "run_shell")]:
            with patch("agent.request_completion", AsyncMock(side_effect=[tool_message(arguments=args, name=name), final_message()])), patch("tool_runtime.fetch_repository", AsyncMock()) as fetch:
                result = await run_agent([{"role": "user", "content": "hello"}])
            fetch.assert_not_awaited()
            self.assertFalse(json.loads(result.messages[2]["content"])["ok"])

    async def test_github_error_is_returned_as_tool_error(self):
        with patch("agent.request_completion", AsyncMock(side_effect=[tool_message(), final_message("Repository missing")])), patch(
            "tool_runtime.fetch_repository", AsyncMock(side_effect=HTTPException(404, "missing"))
        ):
            result = await run_agent([{"role": "user", "content": "read repository"}])
        self.assertEqual(json.loads(result.messages[2]["content"])["status"], 404)

    async def test_calls_are_bounded_and_final_request_disables_tools(self):
        responses = [tool_message(f"call_{i}", arguments="bad") for i in range(MAX_TOOL_CALLS)] + [final_message()]
        completion = AsyncMock(side_effect=responses)
        with patch("agent.request_completion", completion):
            result = await run_agent([{"role": "user", "content": "hello"}])
        self.assertEqual(result.answer, "Final answer")
        self.assertEqual(completion.call_args.kwargs["tool_choice"], "none")
        completion = AsyncMock(side_effect=responses[:-1] + [tool_message("extra")])
        with patch("agent.request_completion", completion), self.assertRaises(AgentLimitError):
            await run_agent([{"role": "user", "content": "hello"}])

    async def test_malformed_and_repeated_tool_ids_fail(self):
        for responses in [[{"tool_calls": "invalid"}], [tool_message(), tool_message()]]:
            with patch("agent.request_completion", AsyncMock(side_effect=responses)), patch("tool_runtime.fetch_repository", AsyncMock(side_effect=HTTPException(404, "missing"))), self.assertRaises(AgentProtocolError):
                await run_agent([{"role": "user", "content": "hello"}])

    async def test_tool_turns_survive_sqlite_restart_and_trim_as_complete_turns(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            store = ConversationStore(path)
            with patch("agent.request_completion", AsyncMock(side_effect=[tool_message(arguments="bad"), final_message()])):
                identifier, _ = await store.chat("first", None, run_agent)
            store = ConversationStore(path)
            with patch("agent.request_completion", AsyncMock(return_value=final_message())) as completion:
                await store.chat("follow up", identifier, run_agent)
                history = completion.call_args.args[0]
                self.assertEqual(history[2]["tool_call_id"], "call_1")
                for index in range(MAX_HISTORY_TURNS):
                    await store.chat(f"turn {index}", identifier, run_agent)
            saved = store.repository.load(identifier)
            self.assertEqual(sum(item["role"] == "user" for item in saved), MAX_HISTORY_TURNS)
            self.assertFalse(any(item["role"] == "tool" for item in saved))

    async def test_agent_failure_does_not_commit_partial_tool_turn(self):
        store = ConversationStore()
        with patch("agent.request_completion", AsyncMock(side_effect=[tool_message(arguments="bad"), RuntimeError("provider offline")])), self.assertRaises(RuntimeError):
            await store.chat("hello", None, run_agent)
        self.assertEqual(store.conversations, {})
