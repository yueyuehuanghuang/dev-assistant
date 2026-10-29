"""Small local knowledge store: UTF-8 ingestion, traceable chunks and BM25 retrieval."""
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from fastapi import HTTPException

MAX_UPLOAD = 1_000_000
MAX_DOCUMENTS = 100
MAX_CHUNKS = 2000
EXTENSIONS = {'.txt', '.md', '.markdown', '.log', '.json', '.csv'}
STOPWORDS = {'the', 'a', 'an', 'is', 'are', 'of', 'to', 'and', 'in', 'for', 'what', 'how', 'does', 'please'}


def terms(text):
    result = []
    for word in re.findall(r'[a-z0-9_]+|[\u3400-\u9fff]+', text.lower()):
        if '\u3400' <= word[0] <= '\u9fff':
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
            if len(word) == 1:
                result.append(word)
        elif word not in STOPWORDS:
            result.append(word)
    return result


def chunk_text(text, size=1000, overlap=160):
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind('\n\n', start + size // 2, end)
            if boundary < 0:
                boundary = text.rfind('\n', start + size // 2, end)
            if boundary >= 0:
                end = boundary + 1
        left, right = start, end
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        if left < right:
            chunks.append({'content': text[left:right], 'start_offset': left, 'end_offset': right,
                           'start_line': text.count('\n', 0, left) + 1,
                           'end_line': text.count('\n', 0, right - 1) + 1})
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return chunks


def decode_document(filename, data):
    # No client filename ever becomes a filesystem path.
    name = filename.replace('\\', '/').rsplit('/', 1)[-1].strip()
    if not name or len(name) > 180 or any(ord(c) < 32 for c in name):
        raise HTTPException(422, '文件名无效或过长。')
    if Path(name).suffix.lower() not in EXTENSIONS:
        raise HTTPException(415, '目前支持 UTF-8 的 TXT、Markdown、LOG、JSON、CSV 文本。')
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, '文件超过 1 MB。')
    try:
        text = data.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
    except UnicodeDecodeError:
        raise HTTPException(422, '请上传 UTF-8 编码的文本文件。') from None
    if not text.strip() or any(ord(c) < 32 and c not in '\n\t' for c in text):
        raise HTTPException(422, '文档为空或包含二进制内容。')
    return name, text


class KnowledgeStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
                    name TEXT NOT NULL, sha256 TEXT NOT NULL, content TEXT NOT NULL,
                    archived INTEGER NOT NULL DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(kb_id, sha256));
                CREATE TABLE IF NOT EXISTS knowledge_chunks (
                    id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES knowledge_documents(id),
                    ordinal INTEGER NOT NULL, content TEXT NOT NULL,
                    start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
                    start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                    term_counts TEXT NOT NULL, token_count INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_documents_kb ON knowledge_documents(kb_id);
                CREATE INDEX IF NOT EXISTS idx_chunks_document ON knowledge_chunks(document_id);
                CREATE TABLE IF NOT EXISTS knowledge_vectors (
                    chunk_id TEXT PRIMARY KEY REFERENCES knowledge_chunks(id), vector TEXT NOT NULL);
            ''')
            columns = {row[1] for row in db.execute('PRAGMA table_info(knowledge_bases)')}
            for name, definition in [('revision', 'INTEGER NOT NULL DEFAULT 0'), ('indexed_revision', 'INTEGER NOT NULL DEFAULT -1'),
                                     ('embedding_contract', 'TEXT'), ('index_state', "TEXT NOT NULL DEFAULT 'pending'"), ('index_error', 'TEXT')]:
                if name not in columns:
                    db.execute(f'ALTER TABLE knowledge_bases ADD COLUMN {name} {definition}')
            db.execute("UPDATE knowledge_bases SET index_state='interrupted', index_error='索引任务因服务重启而中断，请重新构建。' WHERE index_state='indexing'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, name):
        name = name.strip()
        if not name or len(name) > 80:
            raise HTTPException(422, '知识库名称应为 1–80 字。')
        identifier = str(uuid4())
        with self.connect() as db:
            db.execute('INSERT INTO knowledge_bases(id, name) VALUES (?, ?)', (identifier, name))
        return self.get(identifier)

    def get(self, kb_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM knowledge_bases WHERE id=?', (str(kb_id),)).fetchone()
        if row is None:
            raise HTTPException(404, '知识库不存在。')
        return dict(row)

    def list_bases(self):
        with self.connect() as db:
            rows = db.execute('''SELECT k.*, COUNT(DISTINCT d.id) AS document_count, COUNT(c.id) AS chunk_count
                FROM knowledge_bases k LEFT JOIN knowledge_documents d ON d.kb_id=k.id AND d.archived=0
                LEFT JOIN knowledge_chunks c ON c.document_id=d.id
                GROUP BY k.id ORDER BY k.created_at DESC, k.id DESC LIMIT 200''').fetchall()
        return [dict(row) for row in rows]

    def list_documents(self, kb_id):
        self.get(kb_id)
        with self.connect() as db:
            rows = db.execute('''SELECT d.id, d.kb_id, d.name, d.sha256, d.archived, d.created_at,
                length(d.content) AS character_count, COUNT(c.id) AS chunk_count
                FROM knowledge_documents d LEFT JOIN knowledge_chunks c ON c.document_id=d.id
                WHERE d.kb_id=? GROUP BY d.id ORDER BY d.created_at DESC, d.id DESC''', (str(kb_id),)).fetchall()
        return [dict(row) for row in rows]

    def ingest(self, kb_id, filename, data):
        self.get(kb_id)
        name, text = decode_document(filename, data)
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        chunks = chunk_text(text)
        identifier = str(uuid4())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT id, archived FROM knowledge_documents WHERE kb_id=? AND sha256=?', (str(kb_id), digest)).fetchone()
            if old:
                return {'document_id': old['id'], 'duplicate': True, 'archived': bool(old['archived'])}
            count = db.execute('SELECT COUNT(*) FROM knowledge_documents WHERE kb_id=?', (str(kb_id),)).fetchone()[0]
            total = db.execute('SELECT COUNT(*) FROM knowledge_chunks c JOIN knowledge_documents d ON c.document_id=d.id WHERE d.kb_id=?', (str(kb_id),)).fetchone()[0]
            if count >= MAX_DOCUMENTS or total + len(chunks) > MAX_CHUNKS:
                raise HTTPException(413, '知识库已达 100 个文档或 2000 个片段上限（含归档）。')
            db.execute('INSERT INTO knowledge_documents(id,kb_id,name,sha256,content) VALUES (?,?,?,?,?)', (identifier, str(kb_id), name, digest, text))
            for index, chunk in enumerate(chunks):
                counts = Counter(terms(chunk['content']) + terms(name) * 2)
                db.execute('''INSERT INTO knowledge_chunks(id,document_id,ordinal,content,start_offset,end_offset,start_line,end_line,term_counts,token_count)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''', (str(uuid4()), identifier, index, chunk['content'], chunk['start_offset'], chunk['end_offset'],
                    chunk['start_line'], chunk['end_line'], json.dumps(counts), sum(counts.values())))
            self._touch(db, kb_id)
        return {'document_id': identifier, 'duplicate': False, 'archived': False, 'chunk_count': len(chunks)}

    def set_archived(self, kb_id, document_id, archived):
        with self.connect() as db:
            cursor = db.execute('UPDATE knowledge_documents SET archived=? WHERE id=? AND kb_id=?', (int(archived), str(document_id), str(kb_id)))
            if cursor.rowcount == 0:
                raise HTTPException(404, '文档不存在或不属于该知识库。')
            self._touch(db, kb_id)
        return {'document_id': str(document_id), 'archived': archived}

    @staticmethod
    def _touch(db, kb_id):
        db.execute("UPDATE knowledge_bases SET revision=revision+1, index_state=CASE WHEN index_state='indexing' THEN index_state ELSE 'stale' END WHERE id=?", (str(kb_id),))

    def begin_index(self, kb_id):
        self.get(kb_id)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            kb = db.execute('SELECT * FROM knowledge_bases WHERE id=?', (str(kb_id),)).fetchone()
            if kb['index_state'] == 'indexing':
                raise HTTPException(409, '该知识库正在构建索引，请等待完成。')
            rows = db.execute('''SELECT c.id,c.content,d.name FROM knowledge_chunks c JOIN knowledge_documents d ON c.document_id=d.id
                WHERE d.kb_id=? AND d.archived=0 ORDER BY c.id''', (str(kb_id),)).fetchall()
            if not rows:
                raise HTTPException(422, '知识库没有可索引的文档。')
            db.execute("UPDATE knowledge_bases SET index_state='indexing', index_error=NULL WHERE id=?", (str(kb_id),))
        return kb['revision'], [dict(row) for row in rows]

    def finish_index(self, kb_id, revision, chunks, vectors, contract):
        if len(chunks) != len(vectors):
            raise HTTPException(502, '索引向量数量与片段数量不符。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT revision FROM knowledge_bases WHERE id=?', (str(kb_id),)).fetchone()[0]
            if current != revision:
                raise HTTPException(409, '索引期间文档发生变化，未替换旧索引，请重新构建。')
            db.execute('DELETE FROM knowledge_vectors WHERE chunk_id IN (SELECT c.id FROM knowledge_chunks c JOIN knowledge_documents d ON c.document_id=d.id WHERE d.kb_id=?)', (str(kb_id),))
            db.executemany('INSERT INTO knowledge_vectors(chunk_id,vector) VALUES (?,?)',
                           [(chunk['id'], json.dumps(vector)) for chunk, vector in zip(chunks, vectors)])
            db.execute("UPDATE knowledge_bases SET indexed_revision=?,embedding_contract=?,index_state='ready',index_error=NULL WHERE id=?",
                       (revision, json.dumps(contract, sort_keys=True), str(kb_id)))

    def fail_index(self, kb_id, detail):
        with self.connect() as db:
            db.execute("UPDATE knowledge_bases SET index_state='error', index_error=? WHERE id=?", (detail, str(kb_id)))

    def vector_snapshot(self, kb_id, contract):
        with self.connect() as db:
            db.execute('BEGIN')
            kb = db.execute('SELECT * FROM knowledge_bases WHERE id=?', (str(kb_id),)).fetchone()
            if kb is None:
                raise HTTPException(404, '知识库不存在。')
            if kb['index_state'] != 'ready' or kb['revision'] != kb['indexed_revision']:
                raise HTTPException(409, '向量索引尚未就绪或资料已变化，请先构建/更新索引；也可显式选择关键词检索。')
            if json.loads(kb['embedding_contract'] or 'null') != contract:
                raise HTTPException(409, 'Embedding 模型、服务或维度与索引不一致，请重新构建索引。')
            rows = db.execute('''SELECT c.*, d.name, v.vector FROM knowledge_chunks c JOIN knowledge_documents d ON c.document_id=d.id
                JOIN knowledge_vectors v ON v.chunk_id=c.id WHERE d.kb_id=? AND d.archived=0''', (str(kb_id),)).fetchall()
        return kb['revision'], [dict(row) for row in rows]

    def check_revision(self, kb_id, revision):
        if self.get(kb_id)['revision'] != revision:
            raise HTTPException(409, '检索期间文档发生变化，请重试。')

    def get_chunk(self, kb_id, chunk_id):
        with self.connect() as db:
            row = db.execute('''SELECT c.*, d.name, d.kb_id FROM knowledge_chunks c JOIN knowledge_documents d ON c.document_id=d.id
                WHERE c.id=? AND d.kb_id=? AND d.archived=0''', (str(chunk_id), str(kb_id))).fetchone()
        if not row:
            raise HTTPException(404, '片段不存在或文档已归档。')
        result = dict(row)
        result.pop('term_counts'); result.pop('token_count')
        return result

    def search(self, kb_id, query, top_k=5):
        self.get(kb_id)
        query = query.strip()
        if not query or len(query) > 1000 or type(top_k) is not int or not 1 <= top_k <= 8:
            raise HTTPException(422, '查询应为 1–1000 字，top_k 为 1–8。')
        words = set(terms(query)[:128])
        with self.connect() as db:
            rows = db.execute('''SELECT c.*, d.name, d.kb_id FROM knowledge_chunks c JOIN knowledge_documents d ON c.document_id=d.id
                WHERE d.kb_id=? AND d.archived=0''', (str(kb_id),)).fetchall()
        parsed = [(dict(row), json.loads(row['term_counts'])) for row in rows]
        average = sum(row['token_count'] for row, _ in parsed) / max(len(parsed), 1) or 1
        frequencies = {word: sum(word in counts for _, counts in parsed) for word in words}
        ranked = []
        for row, counts in parsed:
            score = 0.0
            for word in words:
                tf = counts.get(word, 0)
                if tf:
                    idf = math.log(1 + (len(parsed) - frequencies[word] + .5) / (frequencies[word] + .5))
                    score += idf * tf * 2.2 / (tf + 1.2 * (.25 + .75 * row['token_count'] / average))
            if score > 0:
                row.pop('term_counts'); row.pop('token_count')
                row.update(score=round(score, 6), title=row['name'],
                           url=f"/knowledge/{kb_id}/chunks/{row['id']}")
                ranked.append(row)
        ranked.sort(key=lambda row: (-row['score'], row['document_id'], row['ordinal']))
        return {'knowledge_base_id': str(kb_id), 'query': query, 'retrieval_mode': 'bm25',
                'results': ranked[:top_k], 'total_matches': len(ranked),
                'note': '本地关键词检索（中英文分词），分数不是置信度；未启用语义向量检索。'}
