import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import UUID
from typing import Any
from memory_repository import MemoryRepositoryMixin


class ConversationRepository(MemoryRepositoryMixin):
    """将每个会话最近的消息保存到 SQLite；每次操作后关闭连接。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    messages TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            columns = {row[1] for row in connection.execute('PRAGMA table_info(conversations)')}
            if 'knowledge_base_id' not in columns:
                connection.execute('ALTER TABLE conversations ADD COLUMN knowledge_base_id TEXT')
            self.initialize_memory(connection)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys = ON')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def load(self, conversation_id: UUID) -> list[dict[str, Any]] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT messages FROM conversations WHERE id = ?",
                (str(conversation_id),),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def load_scope(self, conversation_id: UUID):
        with self.connect() as connection:
            row = connection.execute('SELECT knowledge_base_id FROM conversations WHERE id=?', (str(conversation_id),)).fetchone()
        return row[0] if row else None

    def save(self, conversation_id: UUID, messages: list[dict[str, Any]], knowledge_base_id=None, *, memory_state=None, new_turn=None) -> None:
        # 参数绑定避免将消息内容拼接成 SQL；上下文管理器会提交或回滚事务。
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO conversations (id, messages, knowledge_base_id) VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    messages = excluded.messages,
                    knowledge_base_id = excluded.knowledge_base_id,
                    updated_at = CURRENT_TIMESTAMP""",
                (str(conversation_id), json.dumps(messages, ensure_ascii=False), knowledge_base_id),
            )
            if memory_state is not None and new_turn is not None:
                self.save_memory_state(connection, conversation_id, memory_state, new_turn)

    def list_recent(self, limit=30, offset=0):
        with self.connect() as connection:
            rows = connection.execute(
                '''SELECT c.id, COALESCE((SELECT t.messages FROM conversation_turns t WHERE t.conversation_id=c.id ORDER BY t.ordinal LIMIT 1),c.messages) AS messages,
                    c.updated_at FROM conversations c ORDER BY c.updated_at DESC,c.id DESC LIMIT ? OFFSET ?''',
                (limit, offset)).fetchall()
        return [{'conversation_id': row[0],
                 'title': next((m['content'].splitlines()[0][:60] for m in json.loads(row[1]) if m['role'] == 'user'), '新会话'),
                 'updated_at': row[2].replace(' ', 'T') + 'Z'} for row in rows]
