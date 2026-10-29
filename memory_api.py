from html import escape
import json
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from evidence import sources_for_turn
from memory_repository import visible_turn

router = APIRouter(tags=['memory'])


def repository(request):
    repo = request.app.state.conversations.repository
    if not repo:
        raise HTTPException(503, '记忆管理需要启用 SQLite 持久化。')
    return repo


class Proposal(BaseModel):
    content: str = Field(min_length=1, max_length=1200)
    kind: Literal['fact', 'decision', 'preference', 'task'] = 'fact'

    @field_validator('content')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('记忆内容不能为空')
        return value.strip()


class Action(BaseModel):
    action: Literal['approve', 'reject', 'archive', 'reopen']
    version: int = Field(ge=1, strict=True)
    supersedes_id: UUID | None = None


@router.get('/api/conversations/{conversation_id}/memory')
def status(conversation_id: UUID, request: Request):
    repo = repository(request)
    state = repo.memory_state(conversation_id)
    items = repo.list_memories(conversation_id)
    return {**state, 'items': items, 'pending_count': sum(i['status'] == 'pending' for i in items)}


@router.post('/api/conversations/{conversation_id}/memories', status_code=201)
def propose(conversation_id: UUID, payload: Proposal, request: Request):
    return repository(request).propose_memory(conversation_id, payload.content, payload.kind)


@router.post('/api/conversations/{conversation_id}/memories/{item_id}/actions')
def change(conversation_id: UUID, item_id: UUID, payload: Action, request: Request):
    return repository(request).change_memory(conversation_id, item_id, payload.action, payload.version, payload.supersedes_id)


@router.get('/api/conversations/{conversation_id}/memories/{item_id}/events')
def events(conversation_id: UUID, item_id: UUID, request: Request):
    repo = repository(request)
    scope = repo.memory_state(conversation_id)['scope_key']
    with repo.connect() as db:
        if not db.execute('SELECT 1 FROM memory_items WHERE id=? AND scope_key=?', (str(item_id), scope)).fetchone():
            raise HTTPException(404, '记忆不存在或不属于当前范围。')
        rows = db.execute('SELECT action,version,created_at FROM memory_events WHERE memory_id=? ORDER BY id', (str(item_id),)).fetchall()
    return {'events': [dict(row) for row in rows]}


@router.get('/conversations/{conversation_id}/transcript')
def transcript(conversation_id: UUID, request: Request, offset: int = Query(0, ge=0, le=1000000), limit: int = Query(20, ge=1, le=50)):
    rows, next_offset = repository(request).transcript(conversation_id, offset, limit)
    visible = []
    for row in rows:
        messages = json.loads(row['messages'])
        visible.append({'turn_number': row['ordinal'], 'created_at': row['created_at'],
                        'messages': [{'role': m['role'], 'content': m['content']} for m in messages
                            if m['role'] in {'user', 'assistant'} and not m.get('tool_calls') and m.get('content')],
                        'sources': sources_for_turn(messages)})
    return {'turns': visible, 'next_offset': next_offset}


def source_page(title, content):
    return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>记忆来源</title><style>body{max-width:900px;margin:40px auto;padding:20px;font:16px/1.8 system-ui;background:#f8faf8;color:#25312e}'
        'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#edf2eb;padding:20px;border-radius:10px}a{color:#326b4b}</style>'
        '<a href="/">← 返回聊天</a><h1>' + escape(title) + '</h1><pre>' + escape(content) + '</pre></html>',
        headers={'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})


@router.get('/memory/{conversation_id}/turns/{ordinal}', response_class=HTMLResponse)
def history_source(conversation_id: UUID, ordinal: int, request: Request):
    row = repository(request).read_turn(conversation_id, ordinal)
    return source_page(f'历史第 {ordinal} 轮 · 原始用户及最终回答', visible_turn(row['messages']))


@router.get('/memory/{conversation_id}/items/{item_id}', response_class=HTMLResponse)
def memory_source(conversation_id: UUID, item_id: UUID, request: Request):
    repo = repository(request)
    scope = repo.memory_state(conversation_id)['scope_key']
    with repo.connect() as db:
        item = db.execute("SELECT * FROM memory_items WHERE id=? AND scope_key=? AND status='approved'", (str(item_id), scope)).fetchone()
    if not item:
        raise HTTPException(404, '已确认记忆不存在或已撤回。')
    text = item['content'] + '\n\n原始出处：\n' + item['source_quote'] + '\n\n确认后版本：' + str(item['version'])
    return source_page('用户已确认的长期记忆 · ' + item['kind'], text)
