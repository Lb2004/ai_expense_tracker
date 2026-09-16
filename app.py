import streamlit as st
from sqlalchemy import select

from database import db_session, init_db
from llm_client import stream_chat_response
from schemas import ChatMessage, Role

st.set_page_config(page_title="Expense Tracker", page_icon="💰", layout="wide")


@st.cache_resource
def _setup_database() -> None:
    init_db()


_setup_database()

# ---------------------------------------------------------------------------
# Helper — create session via internal auth functions
# ---------------------------------------------------------------------------
# Fix #5: The direct imports of signup_user / login_user from
# expense_mcp_server.py bypass the MCP/HTTP boundary.  This is an
# INTENTIONAL, SCOPED EXCEPTION — acceptable because:
#   1. These are internal auth functions, NOT MCP-exposed tools (fix #1).
#   2. Both processes share the same SQLite DB file on the same machine.
#   3. Only the auth flow uses this path; all expense CRUD goes through MCP.
# A production system would use a proper auth endpoint (e.g. REST /login)
# separate from the MCP protocol.

from expense_mcp_server import signup_user, login_user  # noqa: E402


# ---------------------------------------------------------------------------
# Session state defaults
# ---------------------------------------------------------------------------
if "user_id" not in st.session_state:
    st.session_state.user_id = None
if "username" not in st.session_state:
    st.session_state.username = None
if "session_token" not in st.session_state:
    st.session_state.session_token = None
if "messages" not in st.session_state:
    st.session_state.messages = []  # list[dict] with "role" and "content"

# ---------------------------------------------------------------------------
# Login / Signup screen — now with password (Fix #2)
# ---------------------------------------------------------------------------
if st.session_state.user_id is None:
    st.title("💰 Expense Tracker")
    st.caption("Log in or sign up to start tracking expenses.")

    tab_login, tab_signup = st.tabs(["Log In", "Sign Up"])

    with tab_login:
        with st.form("login_form"):
            username = st.text_input("Username", placeholder="e.g. alice", key="login_user")
            password = st.text_input("Password", type="password", key="login_pass")
            submitted = st.form_submit_button("Log in", type="primary")
            if submitted and username and username.strip() and password:
                username = username.strip().lower()
                try:
                    user_id, token = login_user(username, password)
                    st.session_state.user_id = user_id
                    st.session_state.username = username
                    st.session_state.session_token = token
                    st.session_state.messages = []
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))

    with tab_signup:
        with st.form("signup_form"):
            new_username = st.text_input("Username", placeholder="e.g. alice", key="signup_user")
            new_password = st.text_input("Password", type="password", key="signup_pass")
            confirm_password = st.text_input("Confirm Password", type="password", key="signup_confirm")
            signed_up = st.form_submit_button("Sign up", type="primary")
            if signed_up and new_username and new_username.strip() and new_password:
                if new_password != confirm_password:
                    st.error("Passwords do not match.")
                elif len(new_password) < 6:
                    st.error("Password must be at least 6 characters.")
                else:
                    new_username = new_username.strip().lower()
                    try:
                        user_id, token = signup_user(new_username, new_password)
                        st.session_state.user_id = user_id
                        st.session_state.username = new_username
                        st.session_state.session_token = token
                        st.session_state.messages = []
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
    st.stop()

# ---------------------------------------------------------------------------
# Main chat interface (user is logged in)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header(f"👤 {st.session_state.username}")

    # Feature #21: Monthly summary from MCP resource (displayed below).
    # For now, we read summary data via a direct helper call since the
    # MCP resource client requires an async context.  A full production
    # implementation would use the MCP client's read_resource method.
    try:
        from expense_mcp_server import resolve_session, SessionLocal, Expense, Budget
        from datetime import datetime, timezone
        user_id = resolve_session(st.session_state.session_token)
        with SessionLocal() as _sess:
            today = datetime.now(timezone.utc).date()
            first_of_month = today.replace(day=1)
            month_expenses = _sess.scalars(
                select(Expense).where(
                    Expense.user_id == user_id,
                    Expense.date >= first_of_month,
                    Expense.date <= today,
                )
            ).all()
            total_spent = sum(e.amount for e in month_expenses)
            expense_count = len(month_expenses)

            budget_row = _sess.scalars(
                select(Budget).where(Budget.user_id == user_id)
            ).first()

        col1, col2 = st.columns(2)
        col1.metric("Expenses", expense_count)
        col2.metric("Spent", f"₹{total_spent:,.0f}")

        if budget_row:
            remaining = budget_row.monthly_limit - total_spent
            st.metric("Remaining Budget", f"₹{remaining:,.0f}")
            progress = min(total_spent / budget_row.monthly_limit, 1.0) if budget_row.monthly_limit > 0 else 0
            st.progress(progress, text=f"{progress:.0%} of ₹{budget_row.monthly_limit:,.0f}")
    except Exception:
        st.caption("Summary unavailable")

    if st.button("Log out", use_container_width=True):
        st.session_state.user_id = None
        st.session_state.username = None
        st.session_state.session_token = None
        st.session_state.messages = []
        st.rerun()

    st.divider()
    st.caption(
        'Try: *"Add a ₹150 lunch expense today"* or *"List my expenses"* '
        'or *"Can I afford a ₹15,000 phone?"* '
        'or *"What is the gold price in INR?"*'
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
                    session_token=st.session_state.session_token,
                    username=st.session_state.username,
                )
            )
    except (ConnectionError, OSError) as exc:
        # Fix #7: Graceful handling when MCP servers are not running.
        st.error(
            "Unable to connect to the expense service — "
            "is expense_mcp_server.py running on the configured port?"
        )
    except Exception as exc:
        # Check if this is an httpx connection error (avoid importing httpx
        # at the top level since it's an indirect dependency).
        exc_type = type(exc).__name__
        if "Connect" in exc_type or "connection" in str(exc).lower():
            st.error(
                "Unable to connect to the expense service — "
                "is expense_mcp_server.py running on the configured port?"
            )
        else:
            st.error(f"LLM request failed: {exc}")
    else:
        if reply and str(reply).strip():
            st.session_state.messages.append(
                {"role": "assistant", "content": str(reply).strip()}
            )
            st.rerun()
