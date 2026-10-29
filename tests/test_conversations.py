import asyncio
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from llm import generate_answer
from main import app
from memory import ConversationStore, MAX_HISTORY_TURNS


@patch.dict("os.environ", {"TOOL_TRANSPORT": "direct"})
class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app.state.conversations = ConversationStore()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_continue_and_isolate_conversations(self):
        generate = AsyncMock(return_value="first answer")
        with patch("main.run_agent", generate):
            first = (await self.client.post("/chat", json={"message": "learn FastAPI"})).json()
            generate.return_value = "second answer"
            second = await self.client.post("/chat", json={
                "message": "what am I learning?", "conversation_id": first["conversation_id"]
            })
            self.assertEqual(second.json()["conversation_id"], first["conversation_id"])
            self.assertEqual(generate.call_args.args[0][:3], [
                {"role": "user", "content": "learn FastAPI"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "what am I learning?"},
            ])
            other = (await self.client.post("/chat", json={"message": "new topic"})).json()
            self.assertNotEqual(other["conversation_id"], first["conversation_id"])
            self.assertEqual(generate.call_args.args[0][0]["content"], "new topic")

    async def test_unknown_invalid_and_empty_input(self):
        generate = AsyncMock()
        with patch("main.run_agent", generate):
            for payload, status in [
                ({"message": "hi", "conversation_id": str(uuid4())}, 404),
                ({"message": "hi", "conversation_id": "bad"}, 422),
                ({"message": "   "}, 422),
            ]:
                self.assertEqual((await self.client.post("/chat", json=payload)).status_code, status)
        generate.assert_not_awaited()

    async def test_failed_turn_is_not_saved(self):
        store = ConversationStore()
        identifier, _ = await store.chat("first", None, AsyncMock(return_value="answer"))
        before = [dict(item) for item in store.conversations[identifier].messages]
        with self.assertRaises(httpx.ConnectError):
            await store.chat("failed", identifier, AsyncMock(side_effect=httpx.ConnectError("offline")))
        self.assertEqual(store.conversations[identifier].messages, before)
        with self.assertRaises(httpx.ConnectError):
            await store.chat("failed new", None, AsyncMock(side_effect=httpx.ConnectError("offline")))
        self.assertEqual(len(store.conversations), 1)

    async def test_history_keeps_complete_recent_turns(self):
        store = ConversationStore()
        identifier = None
        for index in range(MAX_HISTORY_TURNS + 2):
            identifier, _ = await store.chat(str(index), identifier, AsyncMock(return_value="answer"))
        messages = store.conversations[identifier].messages
        self.assertEqual(len(messages), MAX_HISTORY_TURNS * 2)
        self.assertEqual(messages[0], {"role": "user", "content": "2"})
        self.assertEqual(messages[-1]["role"], "assistant")

    async def test_concurrent_turns_use_updated_history(self):
        store = ConversationStore()
        identifier, _ = await store.chat("first", None, AsyncMock(return_value="answer"))
        started, release = asyncio.Event(), asyncio.Event()

        async def slow(messages):
            started.set()
            await release.wait()
            return "slow answer"

        first = asyncio.create_task(store.chat("second", identifier, slow))
        await started.wait()
        generate = AsyncMock(return_value="third answer")
        second = asyncio.create_task(store.chat("third", identifier, generate))
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(generate.call_args.args[0][3]["content"], "slow answer")

    async def test_llm_sends_history_to_provider(self):
        captured = []

        def handler(request):
            import json
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        messages = [{"role": "user", "content": "first"},
                    {"role": "assistant", "content": "answer"},
                    {"role": "user", "content": "follow up"}]
        with patch.dict(os.environ, {"LLM_API_KEY": "test", "LLM_BASE_URL": "https://model.test/v1", "LLM_MODEL": "test"}), patch(
            "llm.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(transport=transport, **kwargs)
        ):
            self.assertEqual(await generate_answer(messages), "OK")
        self.assertEqual(captured[0]["messages"][0]["role"], "system")
        self.assertEqual(captured[0]["messages"][1:], messages)

    async def test_database_recovers_history_after_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "conversations.db"
            app.state.conversations = ConversationStore(path)
            with patch("main.run_agent", AsyncMock(return_value="saved answer")):
                response = await self.client.post("/chat", json={"message": "remember FastAPI"})
            self.assertEqual(response.status_code, 200)
            identifier = response.json()["conversation_id"]
            # 新建存储实例模拟服务重启，内存中不含上一次的历史。
            app.state.conversations = ConversationStore(path)
            generate = AsyncMock(return_value="FastAPI")
            with patch("main.run_agent", generate):
                response = await self.client.post("/chat", json={
                    "message": "what did I say?", "conversation_id": identifier
                })
            self.assertEqual(response.status_code, 200)
            self.assertEqual(generate.call_args.args[0][:2], [
                {"role": "user", "content": "remember FastAPI"},
                {"role": "assistant", "content": "saved answer"},
            ])

    async def test_database_failure_preserves_saved_and_in_memory_history(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "conversations.db"
            store = ConversationStore(path)
            identifier, _ = await store.chat("first", None, AsyncMock(return_value="answer"))
            before = [dict(item) for item in store.conversations[identifier].messages]
            app.state.conversations = store
            with patch.object(store.repository, "save", side_effect=sqlite3.OperationalError("disk error")), patch(
                "main.run_agent", AsyncMock(return_value="unsaved answer")
            ):
                response = await self.client.post("/chat", json={
                    "message": "unsaved", "conversation_id": str(identifier)
                })
            self.assertEqual(response.status_code, 503)
            self.assertEqual(store.conversations[identifier].messages, before)
            self.assertEqual(ConversationStore(path).repository.load(identifier), before)


if __name__ == "__main__":
    unittest.main()
