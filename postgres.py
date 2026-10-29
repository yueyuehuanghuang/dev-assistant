"""PostgreSQL repositories using the existing parameterized repository contract.

Schema changes are applied explicitly by scripts/migrate.py, never on import.
All repository transactions take a database advisory lock. This preserves the
SQLite writer serialization semantics across processes, including memory CAS.
"""
from contextlib import contextmanager
import sqlite3

from storage import ConversationRepository
from knowledge import KnowledgeStore


class Record(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def row_factory(cursor):
    names = [column.name for column in (cursor.description or [])]
    return lambda values: Record(zip(names, values))


class Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        if sql.strip().upper() in {'BEGIN', 'BEGIN IMMEDIATE'}:
            return self.connection.execute('SELECT 1')
        sql = sql.replace('CURRENT_TIMESTAMP', "(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text")
        return self.connection.execute(sql.replace('?', '%s'), parameters)

    def executemany(self, sql, rows):
        cursor = self.connection.cursor()
        cursor.executemany(sql.replace('?', '%s'), rows)
        return cursor


class PostgresRepository:
    def __init__(self, url):
        self.url = url

    @contextmanager
    def connect(self):
        import psycopg
        try:
            with psycopg.connect(self.url, row_factory=row_factory, connect_timeout=10) as db:
                db.execute("SET LOCAL TIME ZONE 'UTC'")
                db.execute('SELECT pg_advisory_xact_lock(74628301)')
                yield Connection(db)
        except psycopg.Error as exc:
            # Preserve the public storage-error contract without exposing DSNs.
            raise sqlite3.DatabaseError('PostgreSQL operation failed') from exc


class PostgresConversations(PostgresRepository, ConversationRepository):
    pass


class PostgresKnowledge(PostgresRepository, KnowledgeStore):
    pass
