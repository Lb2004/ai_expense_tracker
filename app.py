from collections.abc import Iterator
from contextlib import contextmanager

import streamlit as st
from sqlalchemy.orm import Session

from database import SessionLocal, init_db
from llm_client import stream_chat_response
from repository import (
    add_message,
    create_conversation,
    history_as_chat_messages,
    list_conversations,
    list_messages,
)
from schemas import ConversationOut, Role


@contextmanager
def db_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


st.set_page_config(page_title="StockBot", page_icon="💬", layout="wide")
init_db()

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None


def _load_conversations(session: Session) -> tuple[str, list[ConversationOut]]:
    conversations = list_conversations(session)
    conversation_id = st.session_state.conversation_id
    if conversation_id and any(item.id == conversation_id for item in conversations):
        return conversation_id, conversations
    if conversations:
        st.session_state.conversation_id = conversations[0].id
        return conversations[0].id, conversations
    created = create_conversation(session)
    session.flush()
    st.session_state.conversation_id = created.id
    return created.id, list_conversations(session)


with st.sidebar:
    st.header("Conversations")
    if st.button("New conversation", use_container_width=True):
        with db_session() as session:
            created = create_conversation(session)
            st.session_state.conversation_id = created.id
        st.rerun()

    with db_session() as session:
        current_id, conversations = _load_conversations(session)

    for item in conversations:
        selected = item.id == current_id
        if st.button(
            item.title,
            key=f"conv-{item.id}",
            use_container_width=True,
            type="primary" if selected else "secondary",
        ):
            st.session_state.conversation_id = item.id
            st.rerun()

st.title("StockBot")
st.caption("A chatbot that can answer questions about stocks and the stock market.")

with db_session() as session:
    conversation_id, _ = _load_conversations(session)
    messages = list_messages(session, conversation_id)

for message in messages:
    with st.chat_message(message.role.value):
        st.markdown(message.content)

prompt = st.chat_input("Send a message")
if prompt:
    with db_session() as session:
        add_message(session, conversation_id, Role.user, prompt)
        prior = history_as_chat_messages(
            list_messages(session, conversation_id),
            exclude_last=True,
        )

    with st.chat_message("user"):
        st.markdown(prompt)

    try:
        with st.chat_message("assistant"):
            reply = st.write_stream(stream_chat_response(prompt, prior))
    except Exception as exc:
        st.error(f"LLM request failed: {exc}")
    else:
        with db_session() as session:
            add_message(session, conversation_id, Role.assistant, reply or "")
        st.rerun()
