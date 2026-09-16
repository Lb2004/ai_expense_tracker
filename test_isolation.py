"""
Automated test suite for expense tracker MCP server isolation & safety.

Tests call MCP tool functions DIRECTLY (bypassing HTTP transport) using
a fresh in-memory SQLite database for each test to ensure isolation.

Run: python -m pytest test_isolation.py -v
"""

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture(autouse=True)
def _use_test_db(monkeypatch):
    """Replace the MCP server's database with a fresh in-memory SQLite DB."""
    import expense_mcp_server as srv

    # StaticPool ensures all threads share the same in-memory DB connection.
    # Without this, asyncio.to_thread would get a separate (empty) DB.
    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(test_engine, "connect")
    def _enable_fk(dbapi_conn, _rec):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    test_session_local = sessionmaker(
        bind=test_engine, autoflush=False, autocommit=False, expire_on_commit=False
    )

    # Patch BEFORE creating tables so the tool functions use the test DB
    monkeypatch.setattr(srv, "engine", test_engine)
    monkeypatch.setattr(srv, "SessionLocal", test_session_local)

    # Create ALL tables on the test engine
    srv.Base.metadata.create_all(bind=test_engine)

    yield

    # Tables are dropped when the in-memory DB connection closes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _create_user(username: str) -> int:
    """Create a user directly in the test DB and return user_id."""
    import expense_mcp_server as srv
    with srv.SessionLocal() as session:
        user = srv.User(username=username)
        session.add(user)
        session.commit()
        return user.id


def _create_token(user_id: int) -> str:
    """Create a session token for a user."""
    import expense_mcp_server as srv
    return srv.create_session(user_id)


def _create_expired_token(user_id: int) -> str:
    """Create an already-expired session token."""
    import expense_mcp_server as srv
    token = str(uuid.uuid4())
    now = srv._utcnow()
    with srv.SessionLocal() as session:
        sess = srv.UserSession(
            token=token,
            user_id=user_id,
            created_at=now - timedelta(hours=25),
            expires_at=now - timedelta(hours=1),
        )
        session.add(sess)
        session.commit()
    return token


# ---------------------------------------------------------------------------
# Import tool functions (they read module globals at call time, so patching works)
# ---------------------------------------------------------------------------

from expense_mcp_server import (
    add_expense,
    can_i_afford,
    delete_expense,
    list_expenses,
    query_expenses,
    set_budget,
)


# ---------------------------------------------------------------------------
# Test 1: Session creation and resolution
# ---------------------------------------------------------------------------

def test_session_creates_and_resolves():
    """Token creation + resolution works correctly."""
    import expense_mcp_server as srv

    uid = _create_user("alice")
    token = _create_token(uid)

    assert isinstance(token, str)
    assert len(token) == 36  # UUID format

    resolved_uid = srv.resolve_session(token)
    assert resolved_uid == uid


# ---------------------------------------------------------------------------
# Test 2: Expired sessions are rejected
# ---------------------------------------------------------------------------

def test_expired_session_rejected():
    """Expired tokens raise ValueError."""
    import expense_mcp_server as srv

    uid = _create_user("bob_expired")
    token = _create_expired_token(uid)

    with pytest.raises(ValueError, match="expired"):
        srv.resolve_session(token)


# ---------------------------------------------------------------------------
# Test 3: Add and list isolation
# ---------------------------------------------------------------------------

def test_add_and_list_isolation():
    """User A's expenses are invisible to User B."""
    uid_a = _create_user("alice_iso")
    uid_b = _create_user("bob_iso")
    token_a = _create_token(uid_a)
    token_b = _create_token(uid_b)

    result = _run(add_expense(token_a, 100.0, "food", "lunch", date.today().isoformat()))
    assert "added" in result.lower()

    result = _run(add_expense(token_a, 200.0, "transport", "taxi", date.today().isoformat()))
    assert "added" in result.lower()

    expenses_a = _run(list_expenses(token_a))
    assert len(expenses_a) == 2

    expenses_b = _run(list_expenses(token_b))
    assert len(expenses_b) == 0


# ---------------------------------------------------------------------------
# Test 4: Cross-user deletion rejected
# ---------------------------------------------------------------------------

def test_delete_cross_user_rejected():
    """User A cannot delete User B's expense."""
    uid_a = _create_user("alice_del")
    uid_b = _create_user("bob_del")
    token_a = _create_token(uid_a)
    token_b = _create_token(uid_b)

    result = _run(add_expense(token_a, 500.0, "electronics", "headphones", date.today().isoformat()))
    assert "added" in result.lower()

    expenses_a = _run(list_expenses(token_a))
    expense_id = expenses_a[0]["id"]

    result = _run(delete_expense(token_b, expense_id))
    assert "error" in result.lower()

    expenses_a_after = _run(list_expenses(token_a))
    assert len(expenses_a_after) == 1


# ---------------------------------------------------------------------------
# Test 5: query_expenses — SELECT only
# ---------------------------------------------------------------------------

def test_query_expenses_select_only():
    """DROP TABLE / DELETE / INSERT SQL is rejected."""
    uid = _create_user("alice_sql")
    token = _create_token(uid)

    result = _run(query_expenses(token, "DROP TABLE expenses"))
    assert "error" in str(result).lower() or "forbidden" in str(result).lower()

    result = _run(query_expenses(token, "DELETE FROM expenses WHERE id = 1"))
    assert "error" in str(result).lower() or "forbidden" in str(result).lower()

    result = _run(query_expenses(token, "INSERT INTO expenses (user_id, amount) VALUES (1, 100)"))
    assert "error" in str(result).lower() or "forbidden" in str(result).lower()


# ---------------------------------------------------------------------------
# Test 6: query_expenses — column allowlist
# ---------------------------------------------------------------------------

def test_query_expenses_column_allowlist():
    """SELECT user_id FROM expenses is rejected."""
    uid = _create_user("alice_allowlist")
    token = _create_token(uid)

    result = _run(query_expenses(token, "SELECT user_id FROM expenses"))
    assert "error" in str(result).lower() or "disallowed" in str(result).lower()


# ---------------------------------------------------------------------------
# Test 7: can_i_afford basic
# ---------------------------------------------------------------------------

def test_can_i_afford_basic():
    """Returns correct affordable flag based on spending vs budget."""
    uid = _create_user("alice_afford")
    token = _create_token(uid)

    result = _run(set_budget(token, 10000.0))
    assert "10,000" in result

    _run(add_expense(token, 8000.0, "rent", "monthly rent", date.today().isoformat()))

    # Remaining = 2000, item costs 1500 → affordable
    result = _run(can_i_afford(token, "dinner", 1500.0))
    assert isinstance(result, dict)
    assert result["affordable"] is True
    assert result["remaining"] == 2000.0

    # Remaining = 2000, item costs 3000 → not affordable
    result = _run(can_i_afford(token, "concert tickets", 3000.0))
    assert isinstance(result, dict)
    assert result["affordable"] is False


# ---------------------------------------------------------------------------
# Test 8: Adversarial cross-user scenario
# ---------------------------------------------------------------------------

def test_adversarial_cross_user():
    """User A cannot access User B's data via any tool."""
    uid_a = _create_user("attacker")
    uid_b = _create_user("victim")
    token_a = _create_token(uid_a)
    token_b = _create_token(uid_b)

    # Victim adds expenses and sets budget
    _run(add_expense(token_b, 5000.0, "food", "groceries", date.today().isoformat()))
    _run(add_expense(token_b, 3000.0, "transport", "uber", date.today().isoformat()))
    _run(set_budget(token_b, 20000.0))

    # Attacker sees nothing
    attacker_expenses = _run(list_expenses(token_a))
    assert len(attacker_expenses) == 0

    # Attacker cannot delete victim's expense
    victim_expenses = _run(list_expenses(token_b))
    victim_expense_id = victim_expenses[0]["id"]
    result = _run(delete_expense(token_a, victim_expense_id))
    assert "error" in result.lower()

    # Attacker's query_expenses returns nothing
    result = _run(query_expenses(token_a, "SELECT SUM(amount) FROM expenses"))
    assert isinstance(result, list)
    row = result[0] if result else {}
    total = list(row.values())[0] if row else 0
    assert total is None or total == 0

    # Attacker has no budget set — should get an error, not victim's budget
    result = _run(can_i_afford(token_a, "phone", 1000.0))
    assert isinstance(result, dict)
    assert result.get("error") == "no_budget_set"

    # Victim's data untouched
    victim_after = _run(list_expenses(token_b))
    assert len(victim_after) == 2
