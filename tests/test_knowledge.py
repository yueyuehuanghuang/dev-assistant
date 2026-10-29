import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
from fastapi import HTTPException

from agent import run_agent
from embeddings import EmbeddingClient, EmbeddingConfig, normalize_vector
from evidence import sources_for_turn
from knowledge import KnowledgeStore, chunk_text, decode_document
from knowledge_service import KnowledgeService
from main import app
from memory import ConversationStore
from test_agent import tool_message, final_message

ENV = {'EMBEDDING_BASE_URL': 'https://embedding.test/v1', 'EMBEDDING_MODEL': 'fixture-v1',
       'EMBEDDING_API_KEY': 'fixture-secret', 'EMBEDDING_DIMENSIONS': '3', 'EMBEDDING_MIN_SIMILARITY': '.35',
       'EMBEDDING_SEND_DIMENSIONS': 'false', 'TOOL_TRANSPORT': 'direct'}


def embed_fixture(texts):
    # Test-only semantic fixture. Production has no fake embedding fallback.
    return [[0., 1., 0.] if 'Backup' in text else [1., 0., 0.] for text in texts]


@patch.dict('os.environ', ENV)
class KnowledgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = KnowledgeStore(self.path / 'knowledge.db')
        self.service = KnowledgeService(self.store)
        self.kb = self.store.create('Project docs')['id']

    def index(self):
        revision, chunks, config = self.service.start_index(self.kb)
        with patch('knowledge_service.EmbeddingClient.embed', side_effect=embed_fixture):
            self.service.index_job(self.kb, revision, chunks, config)

    def test_ingestion_dedup_and_persistence(self):
        first = self.store.ingest(self.kb, '../tokens.md', b'Access token expires after fifteen minutes.\r\n')
        second = self.store.ingest(self.kb, 'renamed.txt', b'Access token expires after fifteen minutes.\n')
        self.assertTrue(second['duplicate'])
        self.assertEqual(first['document_id'], second['document_id'])
        restored = KnowledgeStore(self.path / 'knowledge.db')
        self.assertEqual(restored.list_documents(self.kb)[0]['name'], 'tokens.md')
        self.assertEqual(len(restored.search(self.kb, 'token')['results']), 1)

    def test_chunk_offsets_and_long_lines(self):
        text = ('段落内容\n' * 150) + '\n\n' + 'x' * 2400
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(c['content']) <= 1000 for c in chunks))
        for c in chunks:
            self.assertEqual(c['content'], text[c['start_offset']:c['end_offset']])
            self.assertEqual(c['start_line'], text.count('\n', 0, c['start_offset']) + 1)
        self.assertEqual(chunks[-1]['end_offset'], len(text))

    def test_upload_rejects_unsupported_binary_empty_and_oversize(self):
        for name, data, status in [('x.exe', b'text', 415), ('x.txt', b'\xff', 422), ('x.md', b'\x00x', 422),
                                   ('x.txt', b' \n ', 422), ('x.txt', b'a' * 1000001, 413)]:
            with self.subTest(name=name, status=status), self.assertRaises(HTTPException) as error:
                decode_document(name, data)
            self.assertEqual(error.exception.status_code, status)

    def test_keyword_isolation_and_chinese(self):
        other = self.store.create('Other')['id']
        self.store.ingest(self.kb, '设计.md', '登录令牌的有效期为十五分钟。'.encode())
        self.store.ingest(other, 'secret.txt', b'Other repository secret token')
        result = self.store.search(self.kb, '令牌有效期')
        self.assertTrue(result['results'])
        self.assertTrue(all(r['kb_id'] == self.kb for r in result['results']))
        self.assertFalse(self.store.search(self.kb, 'completelyunrelated')['results'])
        self.assertFalse(self.store.search(other, '有效期')['results'])

    def test_hybrid_retrieves_vector_only_match(self):
        self.store.ingest(self.kb, 'auth.md', b'Access token expires after fifteen minutes.')
        self.store.ingest(self.kb, 'backup.md', b'Backup retention is seven days.')
        self.index()
        self.assertFalse(self.store.search(self.kb, '凭证多久失效')['results'])
        with patch('knowledge_service.EmbeddingClient.embed', side_effect=embed_fixture):
            result = self.service.search(self.kb, '凭证多久失效')
        self.assertEqual(result['retrieval_mode'], 'hybrid')
        self.assertEqual(result['results'][0]['name'], 'auth.md')
        self.assertEqual(result['results'][0]['vector_score'], 1)
        self.assertEqual(len(result['results']), 1)

    def test_archiving_excludes_results_and_invalidates_vectors(self):
        document = self.store.ingest(self.kb, 'auth.md', b'Access token expires after fifteen minutes.')
        self.index()
        self.store.set_archived(self.kb, document['document_id'], True)
        self.assertFalse(self.service.search(self.kb, 'token', mode='bm25')['results'])
        with self.assertRaises(HTTPException) as error:
            self.service.search(self.kb, 'token')
        self.assertEqual(error.exception.status_code, 409)
        self.store.set_archived(self.kb, document['document_id'], False)
        self.assertTrue(self.service.search(self.kb, 'token', mode='bm25')['results'])

    def test_vector_config_change_rejected(self):
        self.store.ingest(self.kb, 'auth.md', b'Access token')
        self.index()
        with patch.dict('os.environ', {'EMBEDDING_MODEL': 'different'}), self.assertRaises(HTTPException) as error:
            self.service.search(self.kb, 'token')
        self.assertEqual(error.exception.status_code, 409)

    def test_mutation_during_query_rejected(self):
        doc = self.store.ingest(self.kb, 'auth.md', b'Access token')
        self.index()
        def mutate(texts):
            self.store.set_archived(self.kb, doc['document_id'], True)
            return embed_fixture(texts)
        with patch('knowledge_service.EmbeddingClient.embed', side_effect=mutate), self.assertRaises(HTTPException) as error:
            self.service.search(self.kb, 'token')
        self.assertEqual(error.exception.status_code, 409)

    def test_concurrent_index_and_restart_are_explicit(self):
        self.store.ingest(self.kb, 'auth.md', b'Access token')
        self.service.start_index(self.kb)
        with self.assertRaises(HTTPException) as error:
            self.service.start_index(self.kb)
        self.assertEqual(error.exception.status_code, 409)
        restored = KnowledgeStore(self.path / 'knowledge.db')
        self.assertEqual(restored.get(self.kb)['index_state'], 'interrupted')

    async def test_stream_knowledge_scope_and_sources(self):
        self.store.ingest(self.kb, 'auth.md', b'Access token expires after fifteen minutes.')
        self.index()
        app.state.knowledge = self.service
        app.state.conversations = ConversationStore(self.path / 'conversations.db')
        responses = [tool_message(arguments='{"query":"token"}', name='search_knowledge'), final_message('Fifteen minutes.')]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            with patch('agent.request_completion', AsyncMock(side_effect=responses)), patch('knowledge_service.EmbeddingClient.embed', side_effect=embed_fixture):
                response = await client.post('/chat/stream', json={'message': 'token expiry', 'knowledge_base_id': self.kb})
            self.assertEqual(response.status_code, 200)
            frame = next(f for f in response.text.split('\n\n') if f.startswith('event: done'))
            data = json.loads(frame.split('data: ', 1)[1])
            self.assertEqual(data['knowledge_base_id'], self.kb)
            self.assertEqual(len(data['sources']), 1)
            self.assertIn('search_knowledge', response.text)
            other = self.store.create('Other')['id']
            conflict = await client.post('/chat/stream', json={'message': 'ask', 'conversation_id': data['conversation_id'], 'knowledge_base_id': other})
            self.assertEqual(conflict.status_code, 409)
            app.state.conversations = ConversationStore(self.path / 'conversations.db')
            restored = (await client.get('/conversations/' + data['conversation_id'])).json()
            self.assertEqual(restored['knowledge_base_id'], self.kb)
            self.assertEqual(restored['messages'][-1]['sources'], data['sources'])

    async def test_api_missing_embedding_never_silently_falls_back(self):
        self.store.ingest(self.kb, 'auth.md', b'Access token')
        app.state.knowledge = self.service
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            with patch.dict('os.environ', {'EMBEDDING_BASE_URL': ''}):
                self.assertEqual((await client.post(f'/api/knowledge/{self.kb}/reindex')).status_code, 503)
                self.assertEqual((await client.post(f'/api/knowledge/{self.kb}/search', json={'query': 'token'})).status_code, 503)
                response = await client.post(f'/api/knowledge/{self.kb}/search', json={'query': 'token', 'mode': 'bm25'})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['retrieval_mode'], 'bm25')

    def test_failed_rebuild_keeps_old_vectors(self):
        self.store.ingest(self.kb, 'auth.md', b'Access token')
        self.index()
        with self.store.connect() as db:
            original = db.execute('SELECT vector FROM knowledge_vectors').fetchall()[0][0]
        revision, chunks, config = self.service.start_index(self.kb)
        with patch('knowledge_service.EmbeddingClient.embed', side_effect=HTTPException(502, 'fixture failure')):
            self.service.index_job(self.kb, revision, chunks, config)
        self.assertEqual(self.store.get(self.kb)['index_state'], 'error')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT vector FROM knowledge_vectors').fetchall()[0][0], original)

    def test_ingestion_during_index_does_not_commit_stale_vectors(self):
        self.store.ingest(self.kb, 'auth.md', b'Access token')
        revision, chunks, config = self.service.start_index(self.kb)
        self.store.ingest(self.kb, 'new.md', b'New knowledge')
        with patch('knowledge_service.EmbeddingClient.embed', side_effect=embed_fixture):
            self.service.index_job(self.kb, revision, chunks, config)
        self.assertEqual(self.store.get(self.kb)['index_state'], 'error')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM knowledge_vectors').fetchone()[0], 0)

    async def test_api_upload_search_source_and_scope(self):
        app.state.knowledge = self.service
        app.state.conversations = ConversationStore(self.path / 'conversations.db')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/knowledge/{self.kb}/documents', files={'file': ('auth.md', b'Token validity is fifteen minutes. <script>unsafe()</script>')})
            self.assertEqual(response.status_code, 201)
            result = (await client.post(f'/api/knowledge/{self.kb}/search', json={'query': 'token', 'mode': 'bm25'})).json()
            source = await client.get(result['results'][0]['url'])
            self.assertIn('&lt;script&gt;', source.text)
            self.assertNotIn('<script>', source.text)
            self.assertIn('Content-Security-Policy'.lower(), source.headers)
            with patch('knowledge_service.EmbeddingClient.embed', side_effect=embed_fixture):
                self.assertEqual((await client.post(f'/api/knowledge/{self.kb}/reindex')).status_code, 202)
            self.assertEqual(self.store.get(self.kb)['index_state'], 'ready')
            with patch('main.run_agent', AsyncMock(return_value='answer')) as generate:
                first = (await client.post('/chat', json={'message': 'ask', 'knowledge_base_id': self.kb})).json()
                self.assertEqual(generate.call_args.kwargs['knowledge'][1], self.kb)
                app.state.conversations = ConversationStore(self.path / 'conversations.db')
                follow = await client.post('/chat', json={'message': 'follow', 'conversation_id': first['conversation_id']})
                self.assertEqual(follow.status_code, 200)
                self.assertEqual(generate.call_args.kwargs['knowledge'][1], self.kb)
            other = self.store.create('Other')['id']
            with patch('main.run_agent', AsyncMock()) as generate:
                conflict = await client.post('/chat', json={'message': 'ask', 'conversation_id': first['conversation_id'], 'knowledge_base_id': other})
                self.assertEqual(conflict.status_code, 409)
                generate.assert_not_awaited()

    async def test_api_rejects_multipart_over_limit_and_cross_kb_document(self):
        app.state.knowledge = self.service
        document = self.store.ingest(self.kb, 'auth.md', b'Access token')
        other = self.store.create('Other')['id']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(f'/api/knowledge/{self.kb}/documents', files={'file': ('large.txt', b'x' * 1100000)})
            self.assertEqual(response.status_code, 413)
            response = await client.post(f'/api/knowledge/{other}/documents/{document["document_id"]}/archive')
            self.assertEqual(response.status_code, 404)

    async def test_agent_search_has_fixed_scope_and_real_sources(self):
        self.store.ingest(self.kb, 'auth.md', b'Access token expires after fifteen minutes.')
        self.index()
        calls = [tool_message(arguments='{"query":"token"}', name='search_knowledge'), final_message('answer')]
        with patch('agent.request_completion', AsyncMock(side_effect=calls)) as completion, patch('knowledge_service.EmbeddingClient.embed', side_effect=embed_fixture):
            result = await run_agent([{'role': 'user', 'content': 'What is token expiry?'}], knowledge=(self.service, self.kb))
        self.assertEqual(result.messages[0]['role'], 'user')
        self.assertIn('search_knowledge', [t['function']['name'] for t in completion.call_args.kwargs['tools']])
        self.assertEqual(len(sources_for_turn(result.messages)), 1)
        self.assertIn('/knowledge/' + self.kb, sources_for_turn(result.messages)[0]['url'])
        with patch('agent.request_completion', AsyncMock(side_effect=[tool_message(arguments=json.dumps({'query': 'token', 'knowledge_base_id': 'other'}), name='search_knowledge'), final_message()])):
            result = await run_agent([{'role': 'user', 'content': 'ask'}], knowledge=(self.service, self.kb))
        self.assertFalse(json.loads(result.messages[2]['content'])['ok'])

    async def test_knowledge_tool_operates_alongside_real_mcp_github_session(self):
        self.store.ingest(self.kb, 'auth.md', b'Access token')
        self.index()
        with patch.dict('os.environ', {'TOOL_TRANSPORT': 'mcp'}), patch('agent.request_completion', AsyncMock(side_effect=[tool_message(arguments='{"query":"token"}', name='search_knowledge'), final_message()])), patch('knowledge_service.EmbeddingClient.embed', side_effect=embed_fixture):
            result = await run_agent([{'role': 'user', 'content': 'ask'}], knowledge=(self.service, self.kb))
        self.assertTrue(json.loads(result.messages[2]['content'])['ok'])


class EmbeddingTests(unittest.TestCase):
    def test_missing_config_is_explicit(self):
        with patch.dict('os.environ', {'EMBEDDING_BASE_URL': ''}), self.assertRaises(HTTPException) as error:
            EmbeddingConfig.from_env()
        self.assertEqual(error.exception.status_code, 503)

    def test_vectors_reject_nonfinite_zero_wrong_dimensions(self):
        for vector in [[0, 0, 0], [1, 2], [1, float('nan'), 0], [True, 0, 0]]:
            with self.assertRaises(ValueError):
                normalize_vector(vector, 3)

    def test_http_contract_batch_order_and_normalization(self):
        config = EmbeddingConfig('https://embedding.test/v1', 'fixture', 3, 'secret')
        real = httpx.Client
        def handler(request):
            self.assertEqual(str(request.url), 'https://embedding.test/v1/embeddings')
            self.assertEqual(request.headers['authorization'], 'Bearer secret')
            self.assertNotIn('dimensions', json.loads(request.content))
            return httpx.Response(200, json={'data': [{'index': 1, 'embedding': [0, 2, 0]}, {'index': 0, 'embedding': [3, 0, 0]}]})
        with patch('embeddings.httpx.Client', side_effect=lambda **kw: real(transport=httpx.MockTransport(handler), **kw)):
            result = EmbeddingClient(config).embed(['a', 'b'])
        self.assertEqual(result, [[1, 0, 0], [0, 1, 0]])

    def test_http_error_and_dimension_mismatch_are_sanitized(self):
        config = EmbeddingConfig('https://embedding.test/v1', 'fixture', 3, 'secret')
        real = httpx.Client
        for response in [httpx.Response(401, json={'error': 'sensitive-key'}), httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1, 0]}]})]:
            with patch('embeddings.httpx.Client', side_effect=lambda **kw: real(transport=httpx.MockTransport(lambda request: response), **kw)), self.assertRaises(HTTPException) as error:
                EmbeddingClient(config).embed(['a'])
            self.assertNotIn('sensitive-key', error.exception.detail)
