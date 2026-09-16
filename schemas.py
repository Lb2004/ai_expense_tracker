from enum import Enum

from pydantic import BaseModel


class Role(str, Enum):
    user = "user"
    assistant = "assistant"


class ChatMessage(BaseModel):
    """What the LLM sees: role and text only."""

    role: Role
    content: str
