import json
import sqlite3

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from context_memory import active_memory
from memory_repository import visible_turn


class MemoryQuery(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=8)


class ReadHistory(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    turn_number: int = Field(ge=1)
    offset: int = Field(default=0, ge=0, le=10000000)
    character_count: int = Field(default=6000, ge=1, le=12000)


MEMORY_TOOLS = []
for name, model, description in [
    ('search_memory', MemoryQuery, '查找当前范围内用户已确认的长期记忆。其他会话原文、待确认或已撤回候选不会返回；结果不是仓库实时状态。'),
    ('search_history', MemoryQuery, '关键词查找当前会话已归档的用户及最终回答，跨越摘要和最近10轮边界；最多搜索最近2000轮。'),
    ('read_history', ReadHistory, '按轮次分页读取本会话原始用户及最终回答，返回来源链接；不含内部推理和原始工具内容。'),
]:
    MEMORY_TOOLS.append({'type': 'function', 'function': {'name': name, 'description': description, 'parameters': model.model_json_schema()}})


async def execute_memory_tool(call):
    context = active_memory.get()
    models = {'search_memory': MemoryQuery, 'search_history': MemoryQuery, 'read_history': ReadHistory}
    try:
        if not context:
            raise HTTPException(503, '当前会话没有可用的持久记忆存储。')
        args = models[call.function.name].model_validate_json(call.function.arguments)
        repo = context['repository']; conversation_id = context['conversation_id']
        if call.function.name == 'search_memory':
            results = await run_in_threadpool(repo.search_scoped_memories, context['scope_key'], conversation_id, args.query, args.limit)
            payload = {'results': results, 'mode': 'approved_keyword_memory'}
        elif call.function.name == 'search_history':
            try:
                results = await run_in_threadpool(repo.search_history, conversation_id, args.query, args.limit)
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                results = []  # First turn has not been committed yet.
            payload = {'results': results, 'mode': 'conversation_keyword_history'}
        else:
            row = await run_in_threadpool(repo.read_turn, conversation_id, args.turn_number)
            text = visible_turn(row['messages'])
            if args.offset > len(text):
                raise HTTPException(422, '偏移超出历史原文长度。')
            end = min(len(text), args.offset + args.character_count)
            payload = {'content': text[args.offset:end], 'total_characters': len(text), 'offset': args.offset,
                       'next_offset': end if end < len(text) else None,
                       'url': f'/memory/{conversation_id}/turns/{args.turn_number}', 'title': f'历史第 {args.turn_number} 轮'}
        return json.dumps({'ok': True, 'memory': payload}, ensure_ascii=False)
    except ValidationError:
        result = {'ok': False, 'error': '记忆工具参数无效；不能指定其他会话或项目。'}
    except HTTPException as exc:
        result = {'ok': False, 'status': exc.status_code, 'error': exc.detail}
    except sqlite3.Error:
        result = {'ok': False, 'status': 503, 'error': '记忆数据库暂时不可用。'}
    return json.dumps(result, ensure_ascii=False)
