from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: UUID | None = None
    repository: str | None = Field(default=None, max_length=140)
    knowledge_base_id: UUID | None = None
    memory_project: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator('memory_project')
    @classmethod
    def validate_project(cls, value):
        if value is not None and (not value.strip() or any(ord(c) < 32 for c in value)):
            raise ValueError('记忆项目名不能为空或包含控制字符')
        return value.strip() if value else None

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return value


class ChatResponse(BaseModel):
    answer: str
    conversation_id: UUID
