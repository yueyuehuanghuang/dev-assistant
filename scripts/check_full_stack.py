"""Live local Embedding + Milvus + PostgreSQL + multi-agent acceptance test.

Calls the configured chat model. Uses synthetic documents in an isolated schema.
"""
import asyncio
import os
from pathlib import Path
import sys
from uuid import uuid4
from unittest.mock import patch

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
from agent import run_agent
from evidence import sources_for_turn


async def check(url, knowledge, kb):
    store = ConversationStore()
    store.repository = PostgresConversations(url)
    knowledge.store.ingest(kb, 'acceptance.md',
        'Acceptance fixture: The ORCHID service access token expires after 17 minutes. This is synthetic test data.'.encode())
    revision, chunks, config = knowledge.start_index(kb)
    await asyncio.to_thread(knowledge.index_job, kb, revision, chunks, config)
    assert knowledge.get(kb)['index_state'] == 'ready', knowledge.get(kb)
    events = []
    async def emit(event, data):
        if event == 'status' and data.get('orchestration') == 'langgraph':
            events.append(data['node'])
    async def generate(messages):
        return await run_agent(messages, emit, knowledge=(knowledge, kb))
    with patch.dict(os.environ, {'AGENT_MODE': 'multi'}):
        identifier, answer = await store.chat(
            '请让知识库专用 Agent 使用 search_knowledge 检索 ORCHID token expiration。根据知识库说明访问令牌多久过期，并引用来源。',
            None, generate, knowledge_base_id=kb)
    sources = sources_for_turn(store.get_history(identifier))
    assert '17' in answer and sources, 'Model did not answer with the fixture and real sources'
    assert {'planner', 'orchestrator', 'knowledge', 'synthesis'} <= set(events), events
    assert PostgresConversations(url).read_turn(identifier, 1)
    print('PASS: LangGraph planner/orchestrator/knowledge/synthesis; real local Embedding -> Milvus -> PostgreSQL archive; grounded 17-minute answer')


if __name__ == '__main__':
    base = os.environ['DATABASE_URL']
    schema = 'live_' + uuid4().hex
    with psycopg.connect(base, autocommit=True) as db:
        db.execute(f'CREATE SCHEMA {schema}')
    url = make_conninfo(base, options=f'-csearch_path={schema}')
    vectors, kb = MilvusVectors(), None
    try:
        with psycopg.connect(url) as db:
            upgrade(db)
        knowledge = KnowledgeService(PostgresKnowledge(url), vectors)
        kb = knowledge.create('Live acceptance fixture')['id']
        asyncio.run(check(url, knowledge, kb))
    finally:
        from embeddings import EmbeddingConfig
        collection = vectors.collection(EmbeddingConfig.from_env().contract)
        if kb and vectors.client.has_collection(collection):
            vectors.client.delete(collection, filter=f'kb_id == "{kb}"')
        with psycopg.connect(base, autocommit=True) as db:
            db.execute(f'DROP SCHEMA {schema} CASCADE')
