"""Real PostgreSQL + Milvus smoke test in an isolated schema/collection.

Uses deterministic fixture embeddings, not the production Embedding service.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / '.env')
import psycopg
from psycopg.conninfo import make_conninfo
from scripts.migrate import upgrade
from postgres import PostgresConversations, PostgresKnowledge
from memory import ConversationStore
from knowledge_service import KnowledgeService
from vector_store import MilvusVectors
from fastapi import HTTPException


async def check(url, vectors):
    store = ConversationStore()
    store.repository = PostgresConversations(url)
    identifier, _ = await store.chat('remember: runtime Python', None, AsyncMock(return_value='saved'), memory_project='fixture')
    item = store.repository.list_memories(identifier)[0]
    store.repository.change_memory(identifier, item['id'], 'approve', 1)
    assert store.repository.search_memories(identifier, 'Python')
    try:
        store.repository.change_memory(identifier, item['id'], 'archive', 1)
        raise AssertionError('CAS must reject stale versions')
    except HTTPException as exc:
        assert exc.status_code == 409
    assert PostgresConversations(url).read_turn(identifier, 1)
    assert store.repository.list_recent()[0]['conversation_id'] == str(identifier)
    knowledge = PostgresKnowledge(url)
    service = KnowledgeService(knowledge, vectors)
    kb = knowledge.create('fixture')['id']
    other = knowledge.create('isolated')['id']
    knowledge.ingest(kb, 'guide.md', b'Tokens expire after fifteen minutes.')
    knowledge.ingest(other, 'secret.md', b'Other knowledge base secret.')
    assert knowledge.list_bases()
    assert knowledge.list_documents(kb)
    revision, chunks, config = service.start_index(kb)
    with patch('knowledge_service.EmbeddingClient.embed', return_value=[[1., 0., 0.]]):
        service.index_job(kb, revision, chunks, config)
        assert knowledge.get(kb)['index_state'] == 'ready', knowledge.get(kb)
        result = service.search(kb, 'token', mode='hybrid')
        assert result['results'] and all(row['document_id'] == knowledge.list_documents(kb)[0]['id'] for row in result['results'])
        with patch.dict(os.environ, {'EMBEDDING_DIMENSIONS': '4'}):
            try:
                service.search(kb, 'token')
                raise AssertionError('Embedding dimension mismatch accepted')
            except HTTPException as exc:
                assert exc.status_code == 409
        assert vectors.search(other, knowledge.get(kb)['vector_generation'], [1., 0., 0.], config.contract) == []
        doc = knowledge.list_documents(kb)[0]['id']
        knowledge.set_archived(kb, doc, True)
        try:
            service.search(kb, 'token')
            raise AssertionError('Stale index accepted')
        except HTTPException as exc:
            assert exc.status_code == 409
    return config.contract


if __name__ == '__main__':
    base = os.environ['DATABASE_URL']
    schema = 'smoke_' + uuid4().hex
    with psycopg.connect(base, autocommit=True) as db:
        db.execute(f'CREATE SCHEMA {schema}')
    url = make_conninfo(base, options=f'-csearch_path={schema}')
    vectors = MilvusVectors()
    model = 'fixture-' + uuid4().hex
    try:
        with psycopg.connect(url) as db:
            upgrade(db)
            upgrade(db)  # idempotent migrations
        with patch.dict(os.environ, {'EMBEDDING_BASE_URL': 'https://fixture.test/v1', 'EMBEDDING_API_KEY': 'fixture', 'EMBEDDING_MODEL': model, 'EMBEDDING_DIMENSIONS': '3'}):
            contract = asyncio.run(check(url, vectors))
        print('PASS: migrations, PostgreSQL chat/archive/memory CAS, Milvus HNSW hybrid retrieval, KB isolation, stale index rejection')
    finally:
        from embeddings import EmbeddingConfig
        with patch.dict(os.environ, {'EMBEDDING_BASE_URL': 'https://fixture.test/v1', 'EMBEDDING_API_KEY': 'fixture', 'EMBEDDING_MODEL': model, 'EMBEDDING_DIMENSIONS': '3'}):
            collection = vectors.collection(EmbeddingConfig.from_env().contract)
        if vectors.client.has_collection(collection):
            vectors.client.drop_collection(collection)
        vectors.client.close()
        with psycopg.connect(base, autocommit=True) as db:
            db.execute(f'DROP SCHEMA {schema} CASCADE')
