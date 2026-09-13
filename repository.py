from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from models import Conversation, Message
from schemas import ChatMessage, ConversationOut, MessageOut, Role


def create_conversation(session: Session) -> ConversationOut:
    conversation = Conversation()
    session.add(conversation)
    session.flush()
    return ConversationOut(
        id=conversation.id,
        created_at=conversation.created_at,
        title="New conversation",
    )


def list_conversations(session: Session) -> list[ConversationOut]:
    stmt = (
        select(Conversation)
        .options(selectinload(Conversation.messages))
        .order_by(Conversation.created_at.desc())
    )
    conversations = session.scalars(stmt).all()
    return [_to_conversation_out(item) for item in conversations]


def get_conversation(session: Session, conversation_id: str) -> Conversation | None:
    return session.get(Conversation, conversation_id)


def list_messages(session: Session, conversation_id: str) -> list[MessageOut]:
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.timestamp.asc(), Message.id.asc())
    )
    return [_to_message_out(item) for item in session.scalars(stmt).all()]


def add_message(
    session: Session,
    conversation_id: str,
    role: Role | str,
    content: str,
) -> MessageOut:
    message = Message(
        conversation_id=conversation_id,
        role=Role(role).value,
        content=content,
    )
    session.add(message)
    session.flush()
    return _to_message_out(message)


def history_as_chat_messages(
    messages: Sequence[MessageOut],
    *,
    exclude_last: bool = False,
) -> list[ChatMessage]:
    items = list(messages)
    if exclude_last and items:
        items = items[:-1]
    return [ChatMessage(role=item.role, content=item.content) for item in items]


def _conversation_title(conversation: Conversation) -> str:
    for message in conversation.messages:
        if message.role == Role.user.value and message.content.strip():
            text = message.content.strip().replace("\n", " ")
            return text if len(text) <= 48 else f"{text[:45]}..."
    return "New conversation"


def _to_conversation_out(conversation: Conversation) -> ConversationOut:
    return ConversationOut(
        id=conversation.id,
        created_at=conversation.created_at,
        title=_conversation_title(conversation),
    )


def _to_message_out(message: Message) -> MessageOut:
    return MessageOut(
        id=message.id,
        conversation_id=message.conversation_id,
        role=Role(message.role),
        content=message.content,
        timestamp=message.timestamp,
    )
