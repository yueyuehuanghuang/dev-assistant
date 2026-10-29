CREATE TABLE conversations (
                    id TEXT PRIMARY KEY,
                    messages TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text
                , knowledge_base_id TEXT, summary TEXT NOT NULL DEFAULT '[]', context_meta TEXT NOT NULL DEFAULT '{}', memory_project TEXT);
CREATE TABLE conversation_turns (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                ordinal INTEGER NOT NULL, messages TEXT NOT NULL, created_at TEXT DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text,
                UNIQUE(conversation_id,ordinal));
CREATE TABLE memory_items (
                id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, source_conversation_id TEXT NOT NULL,
                source_turn INTEGER, source_quote TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                digest TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text, updated_at TEXT DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text,
                UNIQUE(scope_key,kind,digest));
CREATE TABLE memory_events (
                id BIGSERIAL PRIMARY KEY, memory_id TEXT NOT NULL, action TEXT NOT NULL,
                version INTEGER NOT NULL, created_at TEXT DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text);
CREATE INDEX idx_memory_scope ON memory_items(scope_key,status);
CREATE TABLE knowledge_bases (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text, revision INTEGER NOT NULL DEFAULT 0, indexed_revision INTEGER NOT NULL DEFAULT -1, embedding_contract TEXT, index_state TEXT NOT NULL DEFAULT 'pending', index_error TEXT);
CREATE TABLE knowledge_documents (
                    id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
                    name TEXT NOT NULL, sha256 TEXT NOT NULL, content TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text,
                    UNIQUE(kb_id, sha256));
CREATE TABLE knowledge_chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
                    ordinal INTEGER NOT NULL, content TEXT NOT NULL,
                    start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
                    start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                    term_counts TEXT NOT NULL, token_count INTEGER NOT NULL);
CREATE TABLE knowledge_vectors (
                    chunk_id TEXT PRIMARY KEY REFERENCES knowledge_chunks(id), vector TEXT NOT NULL);
CREATE INDEX idx_documents_kb ON knowledge_documents(kb_id);
CREATE INDEX idx_chunks_document ON knowledge_chunks(document_id);
