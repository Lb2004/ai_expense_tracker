import streamlit as st
from sqlalchemy import select

from database import db_session, init_db
from llm_client import stream_chat_response
from models import User
from schemas import ChatMessage, Role

st.set_page_config(page_title="Expense Tracker", page_icon="💰", layout="wide")


@st.cache_resource
def _setup_database() -> None:
    init_db()


_setup_database()

# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "username" not in st.session_state:
    st.session_state.username = None
if "messages" not in st.session_state:
    st.session_state.messages = []  # list[dict] with "role" and "content"

# ---------------------------------------------------------------------------
# Login screen — simple username box, creates User row if needed
# ---------------------------------------------------------------------------
if st.session_state.user_id is None:
    st.title("💰 Expense Tracker")
    st.caption("Log in with a username to start tracking expenses.")

    with st.form("login_form"):
        username = st.text_input("Username", placeholder="e.g. alice")
        submitted = st.form_submit_button("Log in", type="primary")
        if submitted and username and username.strip():
            username = username.strip().lower()
            with db_session() as session:
                stmt = select(User).where(User.username == username)
                user = session.scalars(stmt).first()
                if user is None:
                    user = User(username=username)
                    session.add(user)
                    session.flush()
                st.session_state.user_id = user.id
                st.session_state.username = user.username
                st.session_state.messages = []
            st.rerun()
    st.stop()

# ---------------------------------------------------------------------------
# Main chat interface (user is logged in)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header(f"👤 {st.session_state.username}")
    st.caption(f"User ID: `{st.session_state.user_id}`")

    # Show count of user's expenses directly from DB
    with db_session() as session:
        from models import Expense
        count = len(session.scalars(select(Expense).where(Expense.user_id == st.session_state.user_id)).all())
    st.metric("Logged Expenses", count)

    if st.button("Log out", use_container_width=True):
        st.session_state.user_id = None
        st.session_state.username = None
        st.session_state.messages = []
        st.rerun()

    st.divider()
    st.caption(
        "Try: *\"Add a ₹150 lunch expense today\"* or *\"List my expenses\"* "
        "or *\"Delete expense #3\"*"
    )

st.title("💰 Expense Tracker")
st.caption(f"Logged in as **{st.session_state.username}** — chat to manage your expenses.")

# Render existing messages
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Chat input
prompt = st.chat_input("Send a message")
if prompt:
    # Show user message immediately
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Build history for the LLM
    history = [
        ChatMessage(role=Role(m["role"]), content=m["content"])
        for m in st.session_state.messages[:-1]  # exclude current prompt
    ]

    try:
        with st.chat_message("assistant"):
            reply = st.write_stream(
                stream_chat_response(
                    prompt,
                    history,
                    user_id=st.session_state.user_id,
                    username=st.session_state.username,
                )
            )
    except Exception as exc:
        st.error(f"LLM request failed: {exc}")
    else:
        if reply and str(reply).strip():
            st.session_state.messages.append(
                {"role": "assistant", "content": str(reply).strip()}
            )
            st.rerun()
