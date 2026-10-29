"""Explicit storage selection; a failed PostgreSQL connection never falls back."""
import os
from pathlib import Path
from memory import ConversationStore
from knowledge import KnowledgeStore
from knowledge_service import KnowledgeService


def create_stores(root: Path):
    url = os.getenv('DATABASE_URL', '').strip()
    conversations = ConversationStore(None if url else root / '.data' / 'conversations.db')
    if url:
        from postgres import PostgresConversations, PostgresKnowledge
        conversations.repository = PostgresConversations(url)
        knowledge = PostgresKnowledge(url)
    else:
        knowledge = KnowledgeStore(root / '.data' / 'knowledge.db')
    backend = os.getenv('VECTOR_BACKEND', 'milvus' if url else 'local').strip()
    if backend not in {'milvus', 'local'}:
        raise ValueError('VECTOR_BACKEND must be milvus or local')
    if backend == 'milvus':
        from vector_store import MilvusVectors
        vectors = MilvusVectors()
    else:
        vectors = None
    return conversations, KnowledgeService(knowledge, vectors)
