import asyncio
import json
import streamlit as st

from config import get_settings
from database import init_db
from llm_client import stream_chat_response
from pydantic_ai.mcp import FastMCPClient
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

async def _fetch_monthly_summary_mcp(session_token: str) -> dict:
    """Fetch monthly spending summary from the expense MCP server resource."""
    settings = get_settings()
    client = FastMCPClient(settings.mcp_server_url)
    async with client:
        uri = f"expense://summary/{session_token}/monthly"
        contents = await client.read_resource(uri)
        if not contents:
            raise RuntimeError("Empty response from monthly summary resource.")
        data = json.loads(contents[0].text)
        if "error" in data:
            raise ValueError(data["error"])
        return data


def get_monthly_summary(session_token: str) -> dict:
    """Synchronous wrapper around _fetch_monthly_summary_mcp using asyncio.run."""
    return asyncio.run(_fetch_monthly_summary_mcp(session_token))


# ---------------------------------------------------------------------------
# Main chat interface (user is logged in)
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header(f"👤 {st.session_state.username}")

    try:
        summary = get_monthly_summary(st.session_state.session_token)
        expense_count = summary.get("expense_count", 0)
        total_spent = summary.get("total_spent", 0.0)
        monthly_limit = (
            summary.get("monthly_limit")
            if summary.get("monthly_limit") is not None
            else summary.get("monthly_budget")
        )

        col1, col2 = st.columns(2)
        col1.metric("Expenses", expense_count)
        col2.metric("Spent", f"₹{total_spent:,.0f}")

        if monthly_limit is not None:
            remaining = summary.get("remaining_budget", monthly_limit - total_spent)
            st.metric("Remaining Budget", f"₹{remaining:,.0f}")
            raw_progress = (total_spent / monthly_limit) if monthly_limit > 0 else 0.0
            progress = max(0.0, min(raw_progress, 1.0))
            st.progress(progress, text=f"{progress:.0%} of ₹{monthly_limit:,.0f}")
    except ValueError as exc:
        # Session expired or invalid — auto logout and redirect to login
        st.session_state.user_id = None
        st.session_state.username = None
        st.session_state.session_token = None
        st.session_state.messages = []
        st.warning(f"Session ended: {exc}. Please log in again.")
        st.rerun()
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
