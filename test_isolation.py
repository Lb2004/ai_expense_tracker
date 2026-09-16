"""
Automated test suite for expense tracker MCP server isolation & safety.

Tests call MCP tool functions DIRECTLY (bypassing HTTP transport) using
a fresh in-memory SQLite database for each test to ensure isolation.

Known limitation (#15): These tests call tool functions directly in
Python rather than over HTTP, so HTTP serialization/network failure
paths are untested.  A production test suite should add integration
tests that exercise the full HTTP stack.

Known limitation (#16): Test assertions using date.today() carry a
theoretical midnight-rollover flakiness risk.  E.g. if a test starts
at 23:59:59 and finishes at 00:00:01, date comparisons may fail.
This is low-priority and acceptable for a POC test suite.

Run: python -m pytest test_isolation.py -v
"""

import asyncio
import secrets
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


def _create_user(username: str, password: str = "testpass123") -> int:
    """Create a user directly in the test DB and return user_id."""
    import expense_mcp_server as srv
    pw_hash = srv.hash_password(password)
    with srv.SessionLocal() as session:
        user = srv.User(username=username, password_hash=pw_hash)
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
    token = secrets.token_urlsafe(32)
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


# ===========================================================================
# REGRESSION-PROOF SECURITY TESTS (fixes #1, #2, #3)
# ===========================================================================


# ---------------------------------------------------------------------------
# Regression Test — Fix #1: create_user_session NOT in MCP tools/list
# ---------------------------------------------------------------------------
# WHAT WOULD MAKE THIS FAIL: Re-adding @mcp.tool() decorator to
# create_user_session (or signup_user / login_user) in expense_mcp_server.py.

def test_create_user_session_not_in_tool_list():
    """Fix #1 regression: create_user_session must not appear in tools/list.

    If someone re-adds @mcp.tool() to create_user_session, signup_user,
    or login_user, this test FAILS — proving the fix is enforced.
    """
    import expense_mcp_server as srv

    # Get all registered MCP tool names
    tool_names = set()
    for tool in srv.mcp._tool_manager._tools.values():
        tool_names.add(tool.name)

    # None of the internal auth functions should be MCP-exposed
    assert "create_user_session" not in tool_names, \
        "create_user_session must NOT be an MCP tool — it was exposed to callers!"
    assert "signup_user" not in tool_names, \
        "signup_user must NOT be an MCP tool"
    assert "login_user" not in tool_names, \
        "login_user must NOT be an MCP tool"
    assert "create_session" not in tool_names, \
        "create_session must NOT be an MCP tool"

    # Positive check: expected tools ARE present
    expected_tools = {"add_expense", "delete_expense", "list_expenses",
                      "get_expense_schema", "query_expenses", "set_budget",
                      "can_i_afford"}
    for t in expected_tools:
        assert t in tool_names, f"Expected tool '{t}' missing from MCP tools"


# ---------------------------------------------------------------------------
# Regression Test — Fix #2a: Token is NOT UUID format (uses secrets)
# ---------------------------------------------------------------------------
# WHAT WOULD MAKE THIS FAIL: Reverting create_session() to use
# str(uuid.uuid4()) instead of secrets.token_urlsafe(32).

def test_token_is_not_uuid_format():
    """Fix #2 regression: tokens must use secrets.token_urlsafe, not uuid4.

    UUID4 tokens are 36 chars with dashes in the pattern 8-4-4-4-12.
    secrets.token_urlsafe(32) produces ~43 URL-safe chars with no dashes.
    If someone reverts to uuid.uuid4(), this test FAILS.
    """
    uid = _create_user("alice_token_fmt")
    token = _create_token(uid)

    # secrets.token_urlsafe(32) should NOT be a valid UUID
    try:
        uuid.UUID(token)
        is_uuid = True
    except ValueError:
        is_uuid = False

    assert not is_uuid, (
        f"Token '{token}' is a valid UUID — this means create_session() "
        f"is using uuid.uuid4() instead of secrets.token_urlsafe(32)!"
    )

    # Should be URL-safe base64 (~43 chars, no dashes in UUID pattern)
    assert len(token) >= 40, f"Token too short ({len(token)} chars), expected ~43"
    assert "-" not in token or not all(
        c in "0123456789abcdef-" for c in token
    ), "Token looks like a UUID hex pattern"


# ---------------------------------------------------------------------------
# Regression Test — Fix #2b: Expired session rejected BY TOOL CALL
# ---------------------------------------------------------------------------
# WHAT WOULD MAKE THIS FAIL: Removing the expires_at check from
# resolve_session() in expense_mcp_server.py.

def test_expired_session_rejected_by_tool():
    """Fix #2 regression: an expired token is rejected when used with a tool.

    This sets expires_at to the past and confirms that a tool call
    with that token returns an error.  If the expiry check in
    resolve_session() were removed, this test FAILS — the expired
    token would be accepted and list_expenses would succeed.
    """
    uid = _create_user("bob_expiry_tool")
    expired_token = _create_expired_token(uid)

    # Add an expense via a valid token first
    valid_token = _create_token(uid)
    result = _run(add_expense(valid_token, 100.0, "food", "lunch", date.today().isoformat()))
    assert "added" in result.lower()

    # Now try listing with the expired token — must be rejected
    result = _run(list_expenses(expired_token))
    assert isinstance(result, list)
    assert len(result) == 1
    assert "error" in result[0]
    assert "expired" in str(result[0]["error"]).lower()


# ---------------------------------------------------------------------------
# Regression Test — Fix #3: Temp VIEW handles table aliases
# ---------------------------------------------------------------------------
# WHAT WOULD MAKE THIS FAIL: Reverting to the old _inject_user_filter
# regex approach, which breaks on "FROM expenses e" (table aliases).

def test_query_with_table_alias():
    """Fix #3 regression: queries with table aliases work correctly.

    The old regex-based _inject_user_filter broke on aliases like
    'FROM expenses e'.  The temp VIEW approach handles this natively
    because the query runs against 'my_expenses', not 'expenses'.
    Reverting to the regex approach would fail because there's no
    'expenses' table reference for the regex to match.
    """
    uid = _create_user("alice_alias")
    token = _create_token(uid)

    _run(add_expense(token, 250.0, "food", "dinner", date.today().isoformat()))
    _run(add_expense(token, 100.0, "transport", "bus", date.today().isoformat()))

    # Query with a table alias — this would break with the old regex approach
    result = _run(query_expenses(
        token, "SELECT e.amount, e.category FROM my_expenses e WHERE e.category = 'food'"
    ))
    assert isinstance(result, list), f"Expected list, got: {result}"
    assert len(result) == 1
    assert result[0]["amount"] == 250.0
    assert result[0]["category"] == "food"


# ===========================================================================
# EXISTING TESTS (updated for new auth model)
# ===========================================================================


# ---------------------------------------------------------------------------
# Test: Session creation and resolution
# ---------------------------------------------------------------------------

def test_session_creates_and_resolves():
    """Token creation + resolution works correctly."""
    import expense_mcp_server as srv

    uid = _create_user("alice")
    token = _create_token(uid)

    assert isinstance(token, str)
    assert len(token) >= 40  # secrets.token_urlsafe(32) → ~43 chars

    resolved_uid = srv.resolve_session(token)
    assert resolved_uid == uid


# ---------------------------------------------------------------------------
# Test: Expired sessions are rejected
# ---------------------------------------------------------------------------

def test_expired_session_rejected():
    """Expired tokens raise ValueError."""
    import expense_mcp_server as srv

    uid = _create_user("bob_expired")
    token = _create_expired_token(uid)

    with pytest.raises(ValueError, match="expired"):
        srv.resolve_session(token)


# ---------------------------------------------------------------------------
# Test: Add and list isolation
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
# Test: Cross-user deletion rejected
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
# Test: query_expenses — SELECT only
# ---------------------------------------------------------------------------

def test_query_expenses_select_only():
    """DROP TABLE / DELETE / INSERT SQL is rejected."""
    uid = _create_user("alice_sql")
    token = _create_token(uid)

    result = _run(query_expenses(token, "DROP TABLE my_expenses"))
    assert "error" in str(result).lower() or "forbidden" in str(result).lower()

    result = _run(query_expenses(token, "DELETE FROM my_expenses WHERE id = 1"))
    assert "error" in str(result).lower() or "forbidden" in str(result).lower()

    result = _run(query_expenses(token, "INSERT INTO my_expenses (amount) VALUES (100)"))
    assert "error" in str(result).lower() or "forbidden" in str(result).lower()


# ---------------------------------------------------------------------------
# Test: query_expenses — column allowlist
# ---------------------------------------------------------------------------

def test_query_expenses_column_allowlist():
    """SELECT user_id FROM my_expenses is rejected."""
    uid = _create_user("alice_allowlist")
    token = _create_token(uid)

    result = _run(query_expenses(token, "SELECT user_id FROM my_expenses"))
    assert "error" in str(result).lower() or "disallowed" in str(result).lower()


# ---------------------------------------------------------------------------
# Test: can_i_afford basic
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
# Test: Adversarial cross-user scenario
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
    result = _run(query_expenses(token_a, "SELECT SUM(amount) FROM my_expenses"))
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


# ===========================================================================
# NEW TESTS for fixes #2, #8, #10
# ===========================================================================


# ---------------------------------------------------------------------------
# Test: Password hashing — signup, login, wrong password
# ---------------------------------------------------------------------------

def test_password_signup_and_login():
    """signup_user hashes password; login_user verifies it correctly."""
    import expense_mcp_server as srv

    user_id, token = srv.signup_user("pw_test_user", "correctpassword")
    assert isinstance(user_id, int)
    assert isinstance(token, str)

    # Verify the password is stored as a bcrypt hash, not plaintext
    with srv.SessionLocal() as session:
        user = session.get(srv.User, user_id)
        assert user.password_hash != "correctpassword", \
            "Password stored in plaintext!"
        assert user.password_hash.startswith("$2"), \
            f"Password hash doesn't look like bcrypt: {user.password_hash[:10]}..."

    # Login with correct password succeeds
    login_id, login_token = srv.login_user("pw_test_user", "correctpassword")
    assert login_id == user_id
    assert isinstance(login_token, str)


def test_wrong_password_rejected():
    """Login with incorrect password is rejected."""
    import expense_mcp_server as srv

    srv.signup_user("pw_wrong_test", "rightpassword")

    with pytest.raises(ValueError, match="Invalid username or password"):
        srv.login_user("pw_wrong_test", "wrongpassword")


def test_duplicate_username_rejected():
    """Signing up with an existing username is rejected."""
    import expense_mcp_server as srv

    srv.signup_user("unique_user", "password123")

    with pytest.raises(ValueError, match="already taken"):
        srv.signup_user("unique_user", "anotherpassword")


# ---------------------------------------------------------------------------
# Test: Amount validation (#10)
# ---------------------------------------------------------------------------

def test_add_expense_rejects_non_positive_amount():
    """add_expense rejects amount <= 0 with a clear error."""
    uid = _create_user("alice_amount")
    token = _create_token(uid)

    result = _run(add_expense(token, 0, "food", "free lunch", date.today().isoformat()))
    assert "error" in result.lower()
    assert "positive" in result.lower()

    result = _run(add_expense(token, -50.0, "food", "refund", date.today().isoformat()))
    assert "error" in result.lower()
    assert "positive" in result.lower()

    # Valid amount should work
    result = _run(add_expense(token, 100.0, "food", "lunch", date.today().isoformat()))
    assert "added" in result.lower()


# ---------------------------------------------------------------------------
# Test: Pagination / limit (#8)
# ---------------------------------------------------------------------------

def test_list_expenses_pagination():
    """list_expenses respects the limit parameter and caps at 200."""
    uid = _create_user("alice_pagination")
    token = _create_token(uid)

    # Add 10 expenses
    for i in range(10):
        _run(add_expense(token, 100.0 + i, "food", f"item {i}", date.today().isoformat()))

    # Default limit (50) returns all 10
    all_expenses = _run(list_expenses(token))
    assert len(all_expenses) == 10

    # Limit of 3 returns only 3
    limited = _run(list_expenses(token, limit=3))
    assert len(limited) == 3

    # Limit of 1 returns 1
    single = _run(list_expenses(token, limit=1))
    assert len(single) == 1


# ---------------------------------------------------------------------------
# Test: query_expenses with aggregates works via temp VIEW
# ---------------------------------------------------------------------------

def test_query_expenses_aggregate():
    """Aggregate queries work correctly through the temp VIEW."""
    uid = _create_user("alice_agg")
    token = _create_token(uid)

    _run(add_expense(token, 100.0, "food", "lunch", date.today().isoformat()))
    _run(add_expense(token, 200.0, "food", "dinner", date.today().isoformat()))
    _run(add_expense(token, 150.0, "transport", "taxi", date.today().isoformat()))

    result = _run(query_expenses(
        token, "SELECT category, SUM(amount) as total FROM my_expenses GROUP BY category ORDER BY total DESC"
    ))
    assert isinstance(result, list)
    assert len(result) == 2
    # Food total should be 300, transport 150
    assert result[0]["category"] == "food"
    assert result[0]["total"] == 300.0
