import base64
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from github import fetch_repository, README_LIMIT
from main import app
from memory import ConversationStore


@patch.dict("os.environ", {"TOOL_TRANSPORT": "direct"})
class GitHubTests(unittest.IsolatedAsyncioTestCase):
    def transport(self, readme_status=200, metadata_status=200, readme="Project README"):
        def handler(request):
            self.assertEqual(request.url.host, "api.github.com")
            if request.url.path.endswith("/readme"):
                return httpx.Response(readme_status, json={
                    "encoding": "base64", "content": base64.b64encode(readme.encode()).decode()
                })
            if request.url.path.endswith("/contents"):
                return httpx.Response(200, json=[{"name": "src", "type": "dir"}])
            return httpx.Response(metadata_status, headers={"x-ratelimit-remaining": "0"}, json={
                "full_name": "owner/repo", "html_url": "https://github.com/owner/repo",
                "default_branch": "main", "stargazers_count": 1,
            })
        return httpx.MockTransport(handler)

    async def fetch(self, **options):
        real = httpx.AsyncClient
        transport = self.transport(**options)
        with patch("github.httpx.AsyncClient", side_effect=lambda **kwargs: real(transport=transport, **kwargs)):
            return await fetch_repository("owner/repo")

    async def test_read_repository_and_truncate(self):
        snapshot = await self.fetch(readme="x" * (README_LIMIT + 1))
        self.assertEqual(snapshot.root_entries, ["src/"])
        self.assertEqual(len(snapshot.readme), README_LIMIT)
        self.assertTrue(snapshot.readme_truncated)

    async def test_missing_readme_is_valid(self):
        snapshot = await self.fetch(readme_status=404)
        self.assertIsNone(snapshot.readme)
        self.assertFalse(snapshot.readme_truncated)

    async def test_repository_missing_and_rate_limited(self):
        for upstream, expected in [(404, 404), (403, 429), (401, 401), (500, 502)]:
            with self.assertRaises(HTTPException) as caught:
                await self.fetch(metadata_status=upstream)
            self.assertEqual(caught.exception.status_code, expected)

    async def test_invalid_repository_never_requests_network(self):
        with patch("github.httpx.AsyncClient") as client:
            for name in ["https://github.com/owner/repo", "owner/..", "owner/repo/extra"]:
                with self.assertRaises(HTTPException):
                    await fetch_repository(name)
            client.assert_not_called()

    async def test_chat_uses_and_remembers_repository_snapshot(self):
        snapshot = await self.fetch()
        app.state.conversations = ConversationStore()
        generate = AsyncMock(return_value="Based on README")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            with patch("main.fetch_repository", AsyncMock(return_value=snapshot)) as fetch, patch("main.run_agent", generate):
                first = await client.post("/chat", json={"message": "Introduce project", "repository": "owner/repo"})
                self.assertEqual(first.status_code, 200)
                self.assertIn("Project README", generate.call_args.args[0][0]["content"])
                second = await client.post("/chat", json={
                    "message": "What is the root directory?",
                    "conversation_id": first.json()["conversation_id"],
                })
                self.assertEqual(second.status_code, 200)
                self.assertIn("Project README", generate.call_args.args[0][0]["content"])
                fetch.assert_awaited_once()
            with patch("main.fetch_repository", AsyncMock(side_effect=HTTPException(404, "missing"))), patch("main.run_agent", generate):
                before = generate.await_count
                failed = await client.post("/chat", json={"message": "hi", "repository": "owner/missing"})
                self.assertEqual(failed.status_code, 404)
                self.assertEqual(generate.await_count, before)
