from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class Role(str, Enum):
    user = "user"
    assistant = "assistant"


class ChatMessage(BaseModel):
    """What the LLM sees: role and text only."""

    role: Role
    content: str


class ConversationOut(BaseModel):
    id: str
    created_at: datetime
    title: str


class MessageOut(BaseModel):
    """What the UI and database use, including ids and timestamps."""

    id: str
    conversation_id: str
    role: Role
    content: str
    timestamp: datetime


class PricePoint(BaseModel):
    date: str
    close: float
