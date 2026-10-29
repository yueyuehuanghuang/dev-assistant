"""Versioned, transactional PostgreSQL schema upgrades and read-only SQLite import.

python scripts/migrate.py upgrade
python scripts/migrate.py import-sqlite --directory .data
Import requires empty target tables; the entire import rolls back on any error.
"""
import argparse
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

TABLES = ('conversations', 'conversation_turns', 'memory_items', 'memory_events',
          'knowledge_bases', 'knowledge_documents', 'knowledge_chunks', 'knowledge_vectors')


def upgrade(db):
    db.execute('SELECT pg_advisory_xact_lock(74628301)')
    db.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TIMESTAMPTZ DEFAULT now())')
    for migration in sorted((ROOT / 'migrations').glob('*.sql')):
        version = int(migration.stem.split('_')[0])
        if db.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (version,)).fetchone():
            continue
        db.execute(migration.read_text(encoding='utf-8'))
        db.execute('INSERT INTO schema_migrations(version) VALUES (%s)', (version,))


def import_sqlite(db, directory):
    db.execute('SELECT pg_advisory_xact_lock(74628301)')
    if any(db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in TABLES):
        raise ValueError('Import requires an empty target; existing PostgreSQL data was preserved.')
    counts = {}
    for filename, tables in [('conversations.db', TABLES[:4]), ('knowledge.db', TABLES[4:])]:
        path = (directory / filename).resolve()
        if not path.exists():
            raise ValueError(f'Missing source database: {filename}')
        with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as source:
            source.row_factory = sqlite3.Row
            for table in tables:
                rows = source.execute(f'SELECT * FROM {table}').fetchall()
                for row in rows:
                    columns = list(row.keys())
                    sql = f'INSERT INTO {table} ({",".join(columns)}) VALUES ({",".join(["%s"] * len(columns))})'
                    db.execute(sql, tuple(row))
                actual = db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                if actual != len(rows):
                    raise ValueError(f'Count mismatch: {table}')
                # Compare every imported field before committing, not only counts.
                for row in rows:
                    copied = db.execute(f'SELECT {",".join(row.keys())} FROM {table} WHERE id=%s' if table != 'knowledge_vectors' else
                                        'SELECT chunk_id,vector FROM knowledge_vectors WHERE chunk_id=%s', (row[0],)).fetchone()
                    if tuple(row) != tuple(copied):
                        raise ValueError(f'Content mismatch: {table}')
                counts[table] = actual
    db.execute("SELECT setval(pg_get_serial_sequence('memory_events','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM memory_events")
    # Existing SQLite vectors remain available for audit; Milvus must be rebuilt.
    db.execute("UPDATE knowledge_bases SET index_state='stale',indexed_revision=-1")
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['upgrade', 'import-sqlite'])
    parser.add_argument('--directory', type=Path, default=ROOT / '.data')
    args = parser.parse_args()
    load_dotenv(ROOT / '.env')
    import psycopg
    with psycopg.connect(os.environ['DATABASE_URL']) as db:
        upgrade(db)
        if args.command == 'import-sqlite':
            print(import_sqlite(db, args.directory))
        else:
            print('PostgreSQL schema is up to date.')
