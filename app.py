import streamlit as st

from database import db_session, init_db
from llm_client import stream_chat_response
from repository import (
    add_message,
    create_conversation,
    delete_conversation,
    history_as_chat_messages,
    list_conversations,
    list_messages,
)
from schemas import ConversationOut, Role

st.set_page_config(page_title="StockBot", page_icon="💬", layout="wide")


@st.cache_resource
def _setup_database() -> None:
    init_db()


_setup_database()

if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = None


def _active_conversation(conversations: list[ConversationOut]) -> str | None:
    current_id = st.session_state.conversation_id
    if current_id and any(item.id == current_id for item in conversations):
        return current_id
    if conversations:
        st.session_state.conversation_id = conversations[0].id
        return conversations[0].id
    st.session_state.conversation_id = None
    return None


with db_session() as session:
    conversations = list_conversations(session)
    conversation_id = _active_conversation(conversations)
    messages = list_messages(session, conversation_id) if conversation_id else []

with st.sidebar:
    st.header("Conversations")
    if st.button("New conversation", key="new_conversation", use_container_width=True):
        # Reuse the open chat when it is still empty so we do not stack blanks.
        if not (conversation_id and not messages):
            with db_session() as session:
                created = create_conversation(session)
                st.session_state.conversation_id = created.id
        st.rerun()

    for item in conversations:
        selected = item.id == conversation_id
        open_col, delete_col = st.columns([4, 1])
        with open_col:
            if st.button(
                item.title,
                key=f"conv-{item.id}",
                use_container_width=True,
                type="primary" if selected else "secondary",
            ):
                st.session_state.conversation_id = item.id
                st.rerun()
        with delete_col:
            if st.button("✕", key=f"del-{item.id}", use_container_width=True, help="Delete conversation"):
                with db_session() as session:
                    delete_conversation(session, item.id)
                if st.session_state.conversation_id == item.id:
                    st.session_state.conversation_id = None
                st.rerun()

st.title("StockBot")
st.caption("A chatbot that can answer questions about stocks and the stock market.")

for message in messages:
    with st.chat_message(message.role.value):
        st.markdown(message.content)

prompt = st.chat_input("Send a message")
if prompt:
    prior = history_as_chat_messages(messages)

    with st.chat_message("user"):
        st.markdown(prompt)

    try:
        with st.chat_message("assistant"):
            reply = st.write_stream(stream_chat_response(prompt, prior))
    except Exception as exc:
        st.error(f"LLM request failed: {exc}")
    else:
        if reply and str(reply).strip():
            with db_session() as session:
                if conversation_id is None:
                    conversation_id = create_conversation(session).id
                    st.session_state.conversation_id = conversation_id
                add_message(session, conversation_id, Role.user, prompt)
                add_message(session, conversation_id, Role.assistant, str(reply).strip())
            st.rerun()

