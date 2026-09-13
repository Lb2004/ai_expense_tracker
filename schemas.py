from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Role(str, Enum):
    user = "user"
    assistant = "assistant"


class ChatMessage(BaseModel):
    role: Role
    content: str


class ConversationOut(BaseModel):
    id: str
    created_at: datetime
    title: str


class MessageOut(BaseModel):
    id: str
    conversation_id: str
    role: Role
    content: str
    timestamp: datetime


class ChatRequest(BaseModel):
    conversation_id: str
    message: str = Field(min_length=1)


class ChatResponse(BaseModel):
    conversation_id: str
    content: str
