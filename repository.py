from collections.abc import Sequence

from sqlalchemy import case, select
from sqlalchemy.orm import Session, selectinload

from models import Conversation, Message
from schemas import ChatMessage, ConversationOut, MessageOut, Role

DEFAULT_TITLE = "New conversation"
_TITLE_MAX = 48


def _title_from_text(content: str) -> str:
    text = content.strip().replace("\n", " ")
    if not text:
        return DEFAULT_TITLE
    return text if len(text) <= _TITLE_MAX else f"{text[: _TITLE_MAX - 3]}..."


def create_conversation(session: Session) -> ConversationOut:
    conversation = Conversation()
    session.add(conversation)
    # Flush assigns the UUID without committing; db_session() commits later.
    session.flush()
    return _to_conversation_out(conversation)


def list_conversations(session: Session) -> list[ConversationOut]:
    stmt = select(Conversation).order_by(Conversation.created_at.desc())
    return [_to_conversation_out(item) for item in session.scalars(stmt).all()]


def delete_conversation(session: Session, conversation_id: str) -> None:
    conversation = session.get(Conversation, conversation_id)
    if conversation is not None:
        session.delete(conversation)


def list_messages(session: Session, conversation_id: str) -> list[MessageOut]:
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        # User message precedes assistant message if timestamps match.
        .order_by(
            Message.timestamp.asc(),
            case((Message.role == Role.user.value, 0), else_=1),
        )
    )
    return [_to_message_out(item) for item in session.scalars(stmt).all()]


def add_message(
    session: Session,
    conversation_id: str,
    role: Role,
    content: str,
) -> MessageOut:
    message = Message(
        conversation_id=conversation_id,
        role=role.value,
        content=content,
    )
    session.add(message)
    session.flush()
    if role == Role.user:
        conversation = session.get(Conversation, conversation_id)
        if conversation is not None and conversation.title == DEFAULT_TITLE:
            conversation.title = _title_from_text(content)
    return _to_message_out(message)


def history_as_chat_messages(messages: Sequence[MessageOut]) -> list[ChatMessage]:
    """LLM history is only role + text, not DB ids or timestamps."""
    return [ChatMessage(role=item.role, content=item.content) for item in messages]


def backfill_conversation_titles(session: Session) -> None:
    """Fill titles for rows that still have the default (older databases)."""
    stmt = (
        select(Conversation)
        .where(Conversation.title == DEFAULT_TITLE)
        .options(selectinload(Conversation.messages))
    )
    for conversation in session.scalars(stmt):
        for message in conversation.messages:
            if message.role == Role.user.value and message.content.strip():
                conversation.title = _title_from_text(message.content)
                break


def _to_conversation_out(conversation: Conversation) -> ConversationOut:
    return ConversationOut(
        id=conversation.id,
        created_at=conversation.created_at,
        title=conversation.title,
    )


def _to_message_out(message: Message) -> MessageOut:
    return MessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        role=Role(message.role),
        content=message.content,
        timestamp=message.timestamp,
    )
