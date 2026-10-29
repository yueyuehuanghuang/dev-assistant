"""Archive, confirmation lifecycle and scoped recall, sharing the chat transaction."""
import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException

from context_memory import candidate_from_user, turns
from knowledge import terms


def memory_scope(conversation_id, project=None, kb_id=None):
    if project:
        return 'project:' + project.casefold() + '|kb:' + (kb_id or '-')
    return 'kb:' + kb_id if kb_id else 'conversation:' + str(conversation_id)


class MemoryRepositoryMixin:
    def initialize_memory(self, db):
        columns = {r[1] for r in db.execute('PRAGMA table_info(conversations)')}
        for name, definition in [('summary', "TEXT NOT NULL DEFAULT '[]'"), ('context_meta', "TEXT NOT NULL DEFAULT '{}'"), ('memory_project', 'TEXT')]:
            if name not in columns:
                db.execute(f'ALTER TABLE conversations ADD COLUMN {name} {definition}')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS conversation_turns (
                id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                ordinal INTEGER NOT NULL, messages TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(conversation_id,ordinal));
            CREATE TABLE IF NOT EXISTS memory_items (
                id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, source_conversation_id TEXT NOT NULL,
                source_turn INTEGER, source_quote TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                digest TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(scope_key,kind,digest));
            CREATE TABLE IF NOT EXISTS memory_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id TEXT NOT NULL, action TEXT NOT NULL,
                version INTEGER NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory_items(scope_key,status);
        ''')
        # Preserve all history still available in old databases, without inventing lost turns.
        rows = db.execute('SELECT id,messages FROM conversations WHERE NOT EXISTS (SELECT 1 FROM conversation_turns t WHERE t.conversation_id=conversations.id)').fetchall()
        for row in rows:
            for ordinal, group in enumerate(turns(json.loads(row['messages'])), 1):
                db.execute('INSERT INTO conversation_turns(id,conversation_id,ordinal,messages) VALUES (?,?,?,?)',
                           (str(uuid4()), row['id'], ordinal, json.dumps(group, ensure_ascii=False)))

    def memory_state(self, conversation_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM conversations WHERE id=?', (str(conversation_id),)).fetchone()
            if row is None:
                raise HTTPException(404, '会话不存在。')
            count = db.execute('SELECT COUNT(*) FROM conversation_turns WHERE conversation_id=?', (str(conversation_id),)).fetchone()[0]
            scope = memory_scope(conversation_id, row['memory_project'], row['knowledge_base_id'])
            pending = db.execute("SELECT COUNT(*) FROM memory_items WHERE scope_key=? AND status='pending'", (scope,)).fetchone()[0]
        return {'summary': json.loads(row['summary']), 'context_meta': json.loads(row['context_meta']),
                'memory_project': row['memory_project'], 'knowledge_base_id': row['knowledge_base_id'],
                'archived_turns': count, 'pending_count': pending, 'scope_key': scope}

    def save_memory_state(self, db, conversation_id, state, new_turn):
        db.execute('UPDATE conversations SET summary=?,context_meta=?,memory_project=? WHERE id=?',
                   (json.dumps(state['summary'], ensure_ascii=False), json.dumps(state['context_meta']), state['memory_project'], str(conversation_id)))
        ordinal = db.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM conversation_turns WHERE conversation_id=?', (str(conversation_id),)).fetchone()[0]
        db.execute('INSERT INTO conversation_turns(id,conversation_id,ordinal,messages) VALUES (?,?,?,?)',
                   (str(uuid4()), str(conversation_id), ordinal, json.dumps(new_turn, ensure_ascii=False)))
        user = next((m.get('content', '') for m in new_turn if m['role'] == 'user'), '')
        candidate = candidate_from_user(user)
        if candidate:
            self._insert_candidate(db, state['scope_key'], conversation_id, ordinal, **candidate)

    def _insert_candidate(self, db, scope, conversation_id, turn, kind, content, source_quote):
        digest = hashlib.sha256(content.strip().encode()).hexdigest()
        old = db.execute('SELECT * FROM memory_items WHERE scope_key=? AND kind=? AND digest=?', (scope, kind, digest)).fetchone()
        if old:
            return dict(old)
        identifier = str(uuid4())
        db.execute('INSERT INTO memory_items(id,scope_key,source_conversation_id,source_turn,source_quote,kind,content,digest) VALUES (?,?,?,?,?,?,?,?)',
                   (identifier, scope, str(conversation_id), turn, source_quote, kind, content.strip(), digest))
        db.execute("INSERT INTO memory_events(memory_id,action,version) VALUES (?,'proposed',1)", (identifier,))
        return dict(db.execute('SELECT * FROM memory_items WHERE id=?', (identifier,)).fetchone())

    def propose_memory(self, conversation_id, content, kind='fact'):
        state = self.memory_state(conversation_id)
        with self.connect() as db:
            return self._insert_candidate(db, state['scope_key'], conversation_id, None, kind, content, '用户在记忆管理页面手动提交')

    def list_memories(self, conversation_id, status=None):
        state = self.memory_state(conversation_id)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM memory_items WHERE scope_key=? AND (CAST(? AS TEXT) IS NULL OR status=?) ORDER BY updated_at DESC,id DESC LIMIT 200',
                              (state['scope_key'], status, status)).fetchall()
        return [dict(row) for row in rows]

    def change_memory(self, conversation_id, item_id, action, version, supersedes=None):
        state = self.memory_state(conversation_id)
        targets = {'approve': 'approved', 'reject': 'rejected', 'archive': 'archived', 'reopen': 'pending'}
        if action not in targets:
            raise HTTPException(422, '未知记忆操作。')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            item = db.execute('SELECT * FROM memory_items WHERE id=? AND scope_key=?', (str(item_id), state['scope_key'])).fetchone()
            if item is None:
                raise HTTPException(404, '记忆不存在或不属于当前范围。')
            if item['version'] != version:
                raise HTTPException(409, '记忆已被修改，请刷新后再操作。')
            if (action in {'approve', 'reject'} and item['status'] != 'pending') or (action == 'archive' and item['status'] != 'approved') or (action == 'reopen' and item['status'] not in {'rejected', 'archived'}):
                raise HTTPException(409, '当前记忆状态不允许该操作。')
            if supersedes:
                old = db.execute("SELECT * FROM memory_items WHERE id=? AND scope_key=? AND status='approved'", (str(supersedes), state['scope_key'])).fetchone()
                if action != 'approve' or old is None or old['id'] == str(item_id):
                    raise HTTPException(409, '被替换记忆必须是同一范围中已确认的另一条记忆。')
                db.execute("UPDATE memory_items SET status='archived',version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (str(supersedes),))
                db.execute("INSERT INTO memory_events(memory_id,action,version) VALUES (?,'superseded',?)", (str(supersedes), old['version'] + 1))
            db.execute('UPDATE memory_items SET status=?,version=version+1,updated_at=CURRENT_TIMESTAMP WHERE id=?', (targets[action], str(item_id)))
            db.execute('INSERT INTO memory_events(memory_id,action,version) VALUES (?,?,?)', (str(item_id), action, version + 1))
        return {'id': str(item_id), 'status': targets[action], 'version': version + 1}

    def search_memories(self, conversation_id, query, limit=5):
        state = self.memory_state(conversation_id)
        return self.search_scoped_memories(state['scope_key'], conversation_id, query, limit)

    def search_scoped_memories(self, scope, conversation_id, query, limit=5):
        words = set(terms(query))
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM memory_items WHERE scope_key=? AND status='approved' ORDER BY updated_at DESC,id DESC LIMIT 200", (scope,)).fetchall()]
        scored = [(len(words & set(terms(row['content']))), row) for row in rows]
        scored.sort(key=lambda pair: (-pair[0], pair[1]['id']))
        return [{**row, 'url': f'/memory/{conversation_id}/items/{row["id"]}', 'title': row['kind']} for score, row in scored if score > 0][:limit]

    def transcript(self, conversation_id, offset=0, limit=20):
        self.memory_state(conversation_id)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM conversation_turns WHERE conversation_id=? ORDER BY ordinal LIMIT ? OFFSET ?',
                              (str(conversation_id), limit + 1, offset)).fetchall()
        return [dict(row) for row in rows[:limit]], offset + limit if len(rows) > limit else None

    def read_turn(self, conversation_id, ordinal):
        self.memory_state(conversation_id)
        with self.connect() as db:
            row = db.execute('SELECT * FROM conversation_turns WHERE conversation_id=? AND ordinal=?', (str(conversation_id), ordinal)).fetchone()
        if row is None:
            raise HTTPException(404, '历史轮次不存在。')
        return dict(row)

    def search_history(self, conversation_id, query, limit=5):
        self.memory_state(conversation_id)
        words = set(terms(query))
        with self.connect() as db:
            rows = db.execute('SELECT ordinal,messages FROM conversation_turns WHERE conversation_id=? ORDER BY ordinal DESC LIMIT 2000', (str(conversation_id),)).fetchall()
        matches = []
        for row in rows:
            text = visible_turn(row['messages'])
            score = len(words & set(terms(text)))
            if score:
                positions = [text.lower().find(word) for word in words if word in text.lower()]
                start = max(0, min(positions, default=0) - 80)
                matches.append({'turn_number': row['ordinal'], 'excerpt': text[start:start + 900], 'score': score,
                                'url': f'/memory/{conversation_id}/turns/{row["ordinal"]}', 'title': f'历史第 {row["ordinal"]} 轮'})
        return sorted(matches, key=lambda row: (-row['score'], -row['turn_number']))[:limit]


def visible_turn(messages):
    if isinstance(messages, str):
        messages = json.loads(messages)
    return '\n\n'.join(m['role'] + ': ' + str(m.get('content') or '') for m in messages
                       if m['role'] in {'user', 'assistant'} and not m.get('tool_calls') and m.get('content'))
