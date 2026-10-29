import json
import sqlite3

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool


class KnowledgeArguments(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    query: str = Field(min_length=1, max_length=1000)
    top_k: int = Field(default=5, ge=1, le=8)


KNOWLEDGE_TOOL = {'type': 'function', 'function': {
    'name': 'search_knowledge',
    'description': '以 BM25+向量混合检索当前会话绑定的上传知识库，返回片段、来源链接和行号。索引或配置错误会明确返回；无命中时不能编造文档结论。知识库由服务端固定。',
    'parameters': KnowledgeArguments.model_json_schema(),
}}


async def search_knowledge(call, knowledge):
    try:
        args = KnowledgeArguments.model_validate_json(call.function.arguments)
        store, kb_id = knowledge
        result = await run_in_threadpool(store.search, kb_id, args.query, args.top_k)
        return json.dumps({'ok': True, 'knowledge': result}, ensure_ascii=False)
    except ValidationError:
        result = {'ok': False, 'error': '参数无效，只接受 query 和 top_k，不能指定其他知识库。'}
    except HTTPException as exc:
        result = {'ok': False, 'status': exc.status_code, 'error': exc.detail}
    except sqlite3.Error:
        result = {'ok': False, 'status': 503, 'error': '知识库数据库暂时不可用。'}
    return json.dumps(result, ensure_ascii=False)
