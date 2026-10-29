import asyncio
from typing import Any
from copy import deepcopy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4
from fastapi import HTTPException

from storage import ConversationRepository
from agent import AgentResult
from context_memory import active_memory, estimated_tokens, extend_summary, prepare_context
from memory_repository import memory_scope

MAX_HISTORY_TURNS = 10


class ConversationNotFoundError(Exception):
    """找不到指定会话。"""


@dataclass
class Conversation:
    messages: list[dict[str, Any]] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    knowledge_base_id: str | None = None
    memory_project: str | None = None
    summary: list = field(default_factory=list)
    context_meta: dict = field(default_factory=dict)
    archive: list = field(default_factory=list)


class ConversationStore:
    """管理单进程会话，可选 SQLite 持久化。"""

    def __init__(self, database_path: Path | None = None) -> None:
        self.conversations: dict[UUID, Conversation] = {}
        self.repository = ConversationRepository(database_path) if database_path else None

    def get_history(self, conversation_id: UUID) -> list[dict[str, Any]]:
        conversation = self.conversations.get(conversation_id)
        if conversation is not None:
            return deepcopy(conversation.messages)
        saved = self.repository.load(conversation_id) if self.repository else None
        if saved is None:
            raise ConversationNotFoundError
        return saved

    def get_knowledge_base_id(self, conversation_id: UUID):
        self.get_history(conversation_id)
        conversation = self.conversations.get(conversation_id)
        return conversation.knowledge_base_id if conversation else self.repository.load_scope(conversation_id)

    def resolve_scope(self, conversation_id, requested):
        requested = str(requested) if requested else None
        if conversation_id is None:
            return requested
        existing = self.get_knowledge_base_id(conversation_id)
        if requested is not None and requested != existing:
            raise HTTPException(409, '会话的知识库已固定；切换知识库请新建会话。')
        return existing

    def get_memory_state(self, conversation_id):
        self.get_history(conversation_id)
        if self.repository:
            return self.repository.memory_state(conversation_id)
        item = self.conversations[conversation_id]
        return {'summary': item.summary, 'context_meta': item.context_meta, 'memory_project': item.memory_project,
                'archived_turns': len(item.archive), 'scope_key': memory_scope(conversation_id, item.memory_project, item.knowledge_base_id)}

    def resolve_project(self, conversation_id, requested):
        if conversation_id is None:
            return requested
        existing = self.get_memory_state(conversation_id)['memory_project']
        if requested is not None and requested != existing:
            raise HTTPException(409, '会话的记忆项目已固定，切换项目请新建对话。')
        return existing

    async def chat(
        self,
        message: str,
        conversation_id: UUID | None,
        generate: Callable[[list[dict[str, Any]]], Awaitable[str | AgentResult]],
        knowledge_base_id=None,
        memory_project=None,
    ) -> tuple[UUID, str]:
        if conversation_id is None:
            conversation_id = uuid4()
            conversation = Conversation(knowledge_base_id=str(knowledge_base_id) if knowledge_base_id else None, memory_project=memory_project)
        else:
            conversation = self.conversations.get(conversation_id)
            if conversation is None and self.repository is not None:
                saved = self.repository.load(conversation_id)
                if saved is not None:
                    state = self.repository.memory_state(conversation_id)
                    conversation = Conversation(messages=saved, knowledge_base_id=self.repository.load_scope(conversation_id),
                        memory_project=state['memory_project'], summary=state['summary'], context_meta=state['context_meta'])
                    self.conversations[conversation_id] = conversation
            if conversation is None:
                raise ConversationNotFoundError

        # 同一会话串行处理，避免两个请求读取相同旧历史后覆盖彼此。
        async with conversation.lock:
            if knowledge_base_id is not None and str(knowledge_base_id) != conversation.knowledge_base_id:
                raise HTTPException(409, '切换知识库请新建会话。')
            if memory_project is not None and memory_project != conversation.memory_project:
                raise HTTPException(409, '切换记忆项目请新建会话。')
            scope = memory_scope(conversation_id, conversation.memory_project, conversation.knowledge_base_id)
            approved = []
            if self.repository:
                with self.repository.connect() as db:
                    rows = db.execute("SELECT id,kind,content FROM memory_items WHERE scope_key=? AND status='approved' ORDER BY updated_at DESC,id DESC LIMIT 8", (scope,)).fetchall()
                for row in rows:
                    item = {**dict(row), 'url': f'/memory/{conversation_id}/items/{row["id"]}'}
                    if estimated_tokens(approved + [item]) <= 1000:
                        approved.append(item)
            messages, context_meta = prepare_context(conversation.messages, message, conversation.summary, approved)
            token = active_memory.set({'repository': self.repository, 'conversation_id': conversation_id, 'scope_key': scope} if self.repository else None)
            try:
                result = await generate(messages)
            finally:
                active_memory.reset(token)

            # 模型成功后才提交本轮；失败时原历史保持不变。
            if isinstance(result, AgentResult):
                answer = result.answer
                new_turn = result.messages
            else:
                answer = result
                new_turn = [{'role': 'user', 'content': message}, {"role": "assistant", "content": answer}]
            completed = conversation.messages + new_turn
            # 按用户轮次截断，不能截掉工具请求却保留孤立的 tool 结果。
            starts = [i for i, item in enumerate(completed) if item["role"] == "user"]
            start = starts[-MAX_HISTORY_TURNS] if len(starts) > MAX_HISTORY_TURNS else 0
            recent_messages = completed[start:]
            summary = extend_summary(conversation.summary, completed[:start])
            state = {'summary': summary, 'context_meta': context_meta, 'memory_project': conversation.memory_project, 'scope_key': scope}
            # 数据库成功提交后才更新内存；写入失败时保留原有历史。
            if self.repository is not None:
                self.repository.save(conversation_id, recent_messages, conversation.knowledge_base_id, memory_state=state, new_turn=new_turn)
            conversation.messages = recent_messages
            conversation.summary = summary
            conversation.context_meta = context_meta
            if self.repository is None:
                conversation.archive.append(deepcopy(new_turn))
            self.conversations[conversation_id] = conversation
            return conversation_id, answer
