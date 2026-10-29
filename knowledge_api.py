from html import escape
import re
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, File, Request, UploadFile, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from knowledge import MAX_UPLOAD

router = APIRouter(tags=['knowledge'])


class CreateKnowledge(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator('name')
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError('名称不能为空')
        return value.strip()


class SearchKnowledge(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=8, strict=True)
    mode: Literal['bm25', 'vector', 'hybrid'] = 'hybrid'


class KnowledgeUploadLimit:
    """Bound the multipart envelope before Starlette parses/spools an upload."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'POST' or not re.fullmatch(r'/api/knowledge/[^/]+/documents', scope['path']):
            return await self.app(scope, receive, send)
        limit = MAX_UPLOAD + 65536
        parts = []; total = 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            total += len(message.get('body', b''))
            if total > limit:
                return await JSONResponse({'detail': '上传请求超过 1 MB 文件及表单大小限制。'}, status_code=413)(scope, receive, send)
            parts.append(message.get('body', b''))
            if not message.get('more_body'):
                break
        pending = True

        async def replay():
            nonlocal pending
            if pending:
                pending = False
                return {'type': 'http.request', 'body': b''.join(parts), 'more_body': False}
            return await receive()
        await self.app(scope, replay, send)


@router.post('/api/knowledge', status_code=201)
def create_knowledge(payload: CreateKnowledge, request: Request):
    return request.app.state.knowledge.create(payload.name)


@router.get('/api/knowledge')
def list_knowledge(request: Request):
    return {'knowledge_bases': request.app.state.knowledge.list_bases(), 'retrieval_mode': 'hybrid',
            'embedding': request.app.state.knowledge.config_status()}


@router.get('/api/knowledge/{kb_id}')
def get_knowledge(kb_id: UUID, request: Request):
    service = request.app.state.knowledge
    return {**service.get(kb_id), 'embedding': service.config_status()}


@router.post('/api/knowledge/{kb_id}/reindex', status_code=202)
def reindex(kb_id: UUID, request: Request, background: BackgroundTasks):
    service = request.app.state.knowledge
    revision, chunks, config = service.start_index(kb_id)
    background.add_task(service.index_job, str(kb_id), revision, chunks, config)
    return {'knowledge_base_id': str(kb_id), 'index_state': 'indexing', 'chunk_count': len(chunks), 'revision': revision}


@router.get('/api/knowledge/{kb_id}/documents')
def documents(kb_id: UUID, request: Request):
    return {'documents': request.app.state.knowledge.list_documents(kb_id)}


@router.post('/api/knowledge/{kb_id}/documents', status_code=201)
async def upload(kb_id: UUID, request: Request, file: UploadFile = File(...)):
    try:
        data = await file.read(MAX_UPLOAD + 1)
        return await run_in_threadpool(request.app.state.knowledge.ingest, kb_id, file.filename or '', data)
    finally:
        await file.close()


@router.post('/api/knowledge/{kb_id}/documents/{document_id}/archive')
def archive(kb_id: UUID, document_id: UUID, request: Request):
    return request.app.state.knowledge.set_archived(kb_id, document_id, True)


@router.post('/api/knowledge/{kb_id}/documents/{document_id}/restore')
def restore(kb_id: UUID, document_id: UUID, request: Request):
    return request.app.state.knowledge.set_archived(kb_id, document_id, False)


@router.post('/api/knowledge/{kb_id}/search')
def search(kb_id: UUID, payload: SearchKnowledge, request: Request):
    return request.app.state.knowledge.search(kb_id, payload.query, payload.top_k, payload.mode)


@router.get('/api/knowledge/{kb_id}/chunks/{chunk_id}')
def chunk(kb_id: UUID, chunk_id: UUID, request: Request):
    return request.app.state.knowledge.get_chunk(kb_id, chunk_id)


@router.get('/knowledge/{kb_id}/chunks/{chunk_id}', response_class=HTMLResponse)
def view_chunk(kb_id: UUID, chunk_id: UUID, request: Request):
    item = request.app.state.knowledge.get_chunk(kb_id, chunk_id)
    title = escape(item['name'])
    content = '\n'.join(f'{number}: {line}' for number, line in enumerate(item['content'].splitlines(), item['start_line']))
    return HTMLResponse(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · 知识来源</title>
        <style>body{{max-width:900px;margin:40px auto;padding:20px;font:16px/1.8 system-ui;color:#25312e;background:#f8faf8}}
        pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef2eb;padding:24px;border-radius:12px}}a{{color:#326b4b}}</style>
        <a href="/">← 返回聊天</a><h1>{title}</h1>
        <p>来源片段 · 第 {item['start_line']}–{item['end_line']} 行 · 文档字符偏移 {item['start_offset']}–{item['end_offset']}（末端不含）</p>
        <pre>{escape(content)}</pre><p>文档为上传资料，内容未经真实性核验。</p></html>''',
        headers={'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"})
