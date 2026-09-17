"""
Expense Tracker MCP Server — standalone HTTP process on port 8001.

Self-contained: imports shared model definitions and sets up its own
DB engine so it can run independently of the Streamlit app.  All
mutating/querying tools use session_token for auth — no raw user_id
accepted from callers.

Known limitation: The server binds to 127.0.0.1 by default
(see MCPServer.run_streamable_http_async), which is intentional for
this POC — it should NOT be exposed on 0.0.0.0 without adding
proper transport-level authentication.

Run:  python expense_mcp_server.py
Test: open MCP Inspector at http://127.0.0.1:8001/mcp
"""

import asyncio
import re
import secrets
from datetime import date, datetime, timedelta, timezone

import sqlparse
import bcrypt as _bcrypt_lib
from pydantic import BaseModel
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session, sessionmaker

from mcp.server.mcpserver import MCPServer
from shared_models import Base, Budget, Expense, User, UserSession, _utcnow

# ---------------------------------------------------------------------------
# Database setup (self-contained — its own engine, separate from app's)
# ---------------------------------------------------------------------------

DATABASE_URL = "sqlite:///./expenses.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, expire_on_commit=False
)

SESSION_EXPIRY_HOURS = 24


@event.listens_for(engine, "connect")
def _enable_fk(dbapi_conn, _rec):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# Create tables on import (idempotent).
Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Password helpers (bcrypt)
# ---------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    """Hash a plaintext password with bcrypt.  Never stores or logs plain."""
    return _bcrypt_lib.hashpw(plain.encode("utf-8"), _bcrypt_lib.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plaintext password against a bcrypt hash."""
    return _bcrypt_lib.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------


def create_session(user_id: int) -> str:
    """Create a new session token for a user.  Returns the token string.

    uses secrets.token_urlsafe(32) for cryptographically secure
    token generation instead of uuid.uuid4().
    """
    token = secrets.token_urlsafe(32)
    now = _utcnow()
    with SessionLocal() as session:
        sess = UserSession(
            token=token,
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(hours=SESSION_EXPIRY_HOURS),
        )
        session.add(sess)
        session.commit()
    return token


def resolve_session(token: str) -> int:
    """Validate a session token and return the associated user_id.
    Raises ValueError if the token is invalid or expired.

    expires_at is actively checked — an expired token is rejected
    even if it exists in the DB.
    """
    with SessionLocal() as session:
        stmt = select(UserSession).where(UserSession.token == token)
        sess = session.scalars(stmt).first()
        if sess is None:
            raise ValueError("Invalid session token.")
        # Explicit expiry enforcement — this MUST remain; see regression
        # test test_expired_session_rejected_by_tool.
        if sess.expires_at.replace(tzinfo=timezone.utc) < _utcnow():
            raise ValueError("Session token has expired.")
        return sess.user_id


# ---------------------------------------------------------------------------
# User signup / login (internal, NOT MCP-exposed) — Fix #1, #2
# ---------------------------------------------------------------------------
# These functions are called ONLY from app.py's login flow, never via MCP.
# Fix #1: create_user_session was previously decorated with @mcp.tool(),
# making it callable by the agent with an arbitrary user_id and no password.
# It is now a plain internal function and must NEVER appear in tools/list.


def signup_user(username: str, password: str) -> tuple[int, str]:
    """Register a new user with bcrypt-hashed password.  Returns (user_id, token).
    Raises ValueError if username already taken.
    """
    pw_hash = hash_password(password)
    with SessionLocal() as session:
        existing = session.scalars(
            select(User).where(User.username == username)
        ).first()
        if existing is not None:
            raise ValueError(f"Username '{username}' is already taken.")
        user = User(username=username, password_hash=pw_hash)
        session.add(user)
        session.commit()
        user_id = user.id
    token = create_session(user_id)
    return user_id, token


def login_user(username: str, password: str) -> tuple[int, str]:
    """Authenticate a user and return (user_id, token).
    Raises ValueError if credentials are wrong.
    """
    with SessionLocal() as session:
        user = session.scalars(
            select(User).where(User.username == username)
        ).first()
        if user is None:
            raise ValueError("Invalid username or password.")
        if not verify_password(password, user.password_hash):
            raise ValueError("Invalid username or password.")
        user_id = user.id
    token = create_session(user_id)
    return user_id, token


# ---------------------------------------------------------------------------
# SQL safety helpers for query_expenses
# ---------------------------------------------------------------------------

# Columns the LLM is allowed to reference in SELECT / WHERE / GROUP BY / ORDER BY
ALLOWED_COLUMNS = {"amount", "category", "description", "date", "id"}
ALLOWED_AGGREGATES = {"sum", "avg", "count", "min", "max"}

# Pattern to extract identifiers (rough but effective for allowlisting)
_IDENT_RE = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b")

# Dangerous keywords that must never appear
_DANGEROUS_KW = {
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "create",
    "replace",
    "truncate",
    "exec",
    "execute",
    "grant",
    "revoke",
    "attach",
    "detach",
    "pragma",
}


def validate_sql(raw_sql: str) -> str:
    """Validate and sanitize a SQL query for safety.
    Returns the cleaned SQL or raises ValueError.

    Fix #3: queries now run against the temp VIEW 'my_expenses', not the
    raw 'expenses' table.  This function validates that the query only
    references 'my_expenses' and uses allowed columns.
    """
    # Parse with sqlparse
    parsed = sqlparse.parse(raw_sql.strip())
    if not parsed:
        raise ValueError("Empty or unparsable SQL.")
    stmt = parsed[0]

    # Must be a SELECT statement
    if stmt.get_type() != "SELECT":
        raise ValueError("Only SELECT queries are allowed.")

    sql_lower = raw_sql.lower()

    # Check for dangerous keywords
    for kw in _DANGEROUS_KW:
        if re.search(rf"\b{kw}\b", sql_lower):
            raise ValueError(f"Forbidden keyword: {kw}")

    # Check for semicolons (multi-statement injection)
    if ";" in raw_sql:
        raise ValueError("Multiple statements are not allowed.")

    # Strip quoted string literals before identifier extraction so that
    # string values like 'food' don't get flagged as unknown identifiers.
    stripped_sql = re.sub(r"'[^']*'", "", sql_lower)

    # Extract all identifiers and check against allowlist
    identifiers = set(_IDENT_RE.findall(stripped_sql))
    # Remove SQL keywords, aggregates, and known safe tokens
    sql_keywords = {
        "select", "from", "where", "and", "or", "not", "in", "between",
        "like", "is", "null", "as", "on", "by", "group", "order", "having",
        "limit", "offset", "asc", "desc", "distinct", "case", "when", "then",
        "else", "end", "cast", "coalesce", "ifnull", "strftime", "substr",
        "length", "lower", "upper", "trim", "round", "abs", "total",
        "my_expenses", "true", "false",
    }
    safe_tokens = ALLOWED_COLUMNS | ALLOWED_AGGREGATES | sql_keywords
    unsafe = identifiers - safe_tokens
    # Filter out numeric-looking tokens, parameter placeholders, and
    # single-letter identifiers (common table aliases like e, t, x).
    unsafe = {
        t for t in unsafe
        if not t.isdigit() and t != "uid" and len(t) > 1
    }
    if unsafe:
        raise ValueError(
            f"Disallowed column(s) or identifier(s): {', '.join(sorted(unsafe))}. "
            f"Allowed columns: {', '.join(sorted(ALLOWED_COLUMNS))}"
        )

    return raw_sql.strip()


# ---------------------------------------------------------------------------
# Pydantic response models
# ---------------------------------------------------------------------------


class ExpenseOut(BaseModel):
    id: int
    amount: float
    category: str
    description: str
    date: str  # ISO format


# ---------------------------------------------------------------------------
# MCP Server & Tools
# ---------------------------------------------------------------------------

mcp = MCPServer("expense-server")

# NOTE: create_user_session / signup_user / login_user are NOT registered
# as MCP tools.  They are internal functions called only from app.py.
# Fix #1: This is intentional — these must NEVER appear in tools/list.


@mcp.tool()
async def add_expense(
    session_token: str,
    amount: float,
    category: str,
    description: str,
    date: str = "",
) -> str:
    """Add a new expense for the authenticated user.

    Args:
        session_token: The session token of the authenticated user.
        amount: The amount of the expense in Rupees (₹) (positive number).
        category: A short category label (e.g. "food", "transport", "entertainment").
        description: A brief description of what the expense was for.
        date: The date of the expense in ISO format (YYYY-MM-DD). Defaults to today if empty or "today".

    Returns:
        A confirmation message with the new expense ID.
    """
    # Resolve session
    try:
        user_id = resolve_session(session_token)
    except ValueError as exc:
        return f"Error: {exc}"

    # Fix #10: Reject non-positive amounts.
    if amount <= 0:
        return f"Error: Amount must be positive, got {amount}."

    # Validate date
    clean_date = (date or "").strip()
    if not clean_date or clean_date.lower() == "today":
        parsed_date = datetime.now(timezone.utc).date()
        date = parsed_date.isoformat()
    else:
        try:
            parsed_date = datetime.strptime(clean_date, "%Y-%m-%d").date()
        except ValueError:
            return f"Error: Invalid date format '{date}'. Use YYYY-MM-DD."

    def _insert() -> int:
        with SessionLocal() as session:
            expense = Expense(
                user_id=user_id,
                amount=amount,
                category=category.strip().lower(),
                description=description.strip(),
                date=parsed_date,
            )
            session.add(expense)
            session.commit()
            return expense.id

    expense_id = await asyncio.to_thread(_insert)
    return f"Expense #{expense_id} added: ₹{amount:.2f} for '{category}' on {date}."


@mcp.tool()
async def delete_expense(session_token: str, expense_id: int) -> str:
    """Delete an expense, but only if it belongs to the authenticated user.

    Args:
        session_token: The session token of the authenticated user.
        expense_id: The ID of the expense to delete.

    Returns:
        A confirmation or error message.
    """
    try:
        user_id = resolve_session(session_token)
    except ValueError as exc:
        return f"Error: {exc}"

    def _delete() -> str:
        with SessionLocal() as session:
            expense = session.get(Expense, expense_id)
            if expense is None:
                return f"Error: Expense #{expense_id} not found."
            if expense.user_id != user_id:
                return f"Error: Expense #{expense_id} does not belong to you."
            session.delete(expense)
            session.commit()
            return f"Expense #{expense_id} deleted."

    return await asyncio.to_thread(_delete)


@mcp.tool()
async def list_expenses(session_token: str, limit: int = 50) -> list[dict]:
    """List expenses for the authenticated user.

    Args:
        session_token: The session token of the authenticated user.
        limit: Maximum number of expenses to return (default 50, max 200).

    Returns:
        A list of expense dicts, or an error string.
    """
    try:
        user_id = resolve_session(session_token)
    except ValueError as exc:
        return [{"error": str(exc)}]

    # Fix #8: cap at 200 to prevent unbounded result sets.
    effective_limit = min(max(limit, 1), 200)

    def _query() -> list[dict]:
        with SessionLocal() as session:
            stmt = (
                select(Expense)
                .where(Expense.user_id == user_id)
                .order_by(Expense.date.desc())
                .limit(effective_limit)
            )
            rows = session.scalars(stmt).all()
            return [
                ExpenseOut(
                    id=row.id,
                    amount=row.amount,
                    category=row.category,
                    description=row.description,
                    date=str(row.date),
                ).model_dump()
                for row in rows
            ]

    return await asyncio.to_thread(_query)


@mcp.tool()
async def get_expense_schema() -> dict:
    """Get the schema of queryable expense columns.

    Returns the column names, types, and allowed aggregate functions
    that can be used with the query_expenses tool. No authentication required.

    Returns:
        A dict describing the queryable schema.
    """
    return {
        "table": "my_expenses",
        "columns": {
            "id": "integer — unique expense ID",
            "amount": "float — expense amount in ₹",
            "category": "string — category label (e.g. food, transport)",
            "description": "string — brief description",
            "date": "date — ISO format YYYY-MM-DD",
        },
        "allowed_aggregates": sorted(ALLOWED_AGGREGATES),
        "notes": (
            "All queries are automatically scoped to the authenticated user "
            "via a temporary VIEW named 'my_expenses'. Write your SQL "
            "against 'my_expenses', NOT 'expenses'. Only SELECT statements "
            "are allowed. Do NOT include user_id in your SQL."
        ),
    }


@mcp.tool()
async def query_expenses(session_token: str, sql_query: str) -> list[dict] | str:
    """Run a safe, read-only SQL query against the authenticated user's expenses.

    The query is validated for safety (SELECT-only, column allowlist enforced).
    A temporary VIEW 'my_expenses' scoped to the authenticated user is created
    automatically — your query should reference 'my_expenses', not 'expenses'.

    Args:
        session_token: The session token of the authenticated user.
        sql_query: A SELECT query using only allowed columns (amount, category, description, date, id)
                   against the 'my_expenses' table.
                   Do NOT include user_id filters — they are auto-injected via the VIEW.
                   Example: "SELECT category, SUM(amount) FROM my_expenses GROUP BY category"

    Returns:
        Query results as a list of dicts, or an error string.
    """
    try:
        user_id = resolve_session(session_token)
    except ValueError as exc:
        return f"Error: {exc}"

    try:
        validated_sql = validate_sql(sql_query)
    except ValueError as exc:
        return f"SQL validation error: {exc}"

    # Fix #3: Use a per-request temporary VIEW instead of regex-based
    # _inject_user_filter.  The temp VIEW exposes only allowlisted columns
    # and is scoped to the authenticated user via a parameterized WHERE.
    # This approach is immune to table-alias problems (e.g. "FROM expenses e")
    # that broke the old regex injection.

    def _execute() -> list[dict]:
        with SessionLocal() as session:
            # Create temp VIEW scoped to this user.
            # NOTE: SQLite does not allow bound parameters in CREATE VIEW
            # statements.  This is safe because user_id is an int returned
            # by resolve_session() (server-controlled), not user input.
            # Always drop any stale temp view on this connection before creating
            session.execute(text("DROP VIEW IF EXISTS my_expenses"))
            session.execute(
                text(
                    f"CREATE TEMP VIEW my_expenses AS "
                    f"SELECT id, amount, category, description, date "
                    f"FROM expenses WHERE user_id = {int(user_id)}"
                )
            )
            try:
                result = session.execute(text(validated_sql))
                columns = list(result.keys())
                rows = result.fetchmany(200)  # cap at 200 rows
                return [dict(zip(columns, row)) for row in rows]
            finally:
                # Clean up the temp view
                session.execute(text("DROP VIEW IF EXISTS my_expenses"))

    try:
        return await asyncio.to_thread(_execute)
    except Exception as exc:
        return f"Query execution error: {exc}"


@mcp.tool()
async def set_budget(session_token: str, monthly_limit: float) -> str:
    """Set or update the monthly budget for the authenticated user.

    Args:
        session_token: The session token of the authenticated user.
        monthly_limit: The monthly budget limit in Rupees (₹). Must be positive.

    Returns:
        A confirmation message.
    """
    try:
        user_id = resolve_session(session_token)
    except ValueError as exc:
        return f"Error: {exc}"

    if monthly_limit <= 0:
        return f"Error: Budget must be positive, got ₹{monthly_limit:.2f}."

    def _upsert() -> str:
        with SessionLocal() as session:
            budget = session.scalars(
                select(Budget).where(Budget.user_id == user_id)
            ).first()
            if budget is None:
                budget = Budget(user_id=user_id, monthly_limit=monthly_limit)
                session.add(budget)
            else:
                budget.monthly_limit = monthly_limit
            session.commit()
            return f"Monthly budget set to ₹{monthly_limit:,.2f}."

    return await asyncio.to_thread(_upsert)


@mcp.tool()
async def can_i_afford(session_token: str, item_description: str, item_cost: float) -> dict | str:
    """Check if the user can afford an item based on their monthly budget and spending.

    Compares the item cost against the remaining budget for the current month.
    Requires the user to have set a budget via set_budget first.

    Args:
        session_token: The session token of the authenticated user.
        item_description: A short description of the item (e.g. "iPhone 16", "dinner at a restaurant").
        item_cost: The cost of the item in Rupees (₹). Must be positive.

    Returns:
        A structured dict with affordability analysis, or an error string.
    """
    try:
        user_id = resolve_session(session_token)
    except ValueError as exc:
        return f"Error: {exc}"

    if item_cost <= 0:
        return f"Error: Item cost must be positive, got ₹{item_cost:.2f}."

    def _analyze() -> dict:
        with SessionLocal() as session:
            # Get user's budget
            budget_row = session.scalars(
                select(Budget).where(Budget.user_id == user_id)
            ).first()
            if budget_row is None:
                return {
                    "error": "no_budget_set",
                    "message": "You haven't set a monthly budget yet. Please set one first using set_budget (e.g. 'Set my monthly budget to ₹30,000').",
                }
            monthly_budget = budget_row.monthly_limit

            # Calculate current month's spending
            today = datetime.now(timezone.utc).date()
            first_of_month = today.replace(day=1)
            stmt = select(Expense).where(
                Expense.user_id == user_id,
                Expense.date >= first_of_month,
                Expense.date <= today,
            )
            month_expenses = session.scalars(stmt).all()
            spent_this_month = sum(e.amount for e in month_expenses)
            remaining = monthly_budget - spent_this_month

            affordable = item_cost <= remaining

            if affordable:
                verdict = (
                    f"Yes, you can afford '{item_description}' at ₹{item_cost:,.2f}. "
                    f"You have ₹{remaining:,.2f} remaining this month "
                    f"(budget: ₹{monthly_budget:,.2f}, spent: ₹{spent_this_month:,.2f})."
                )
            else:
                shortfall = item_cost - remaining
                verdict = (
                    f"No, '{item_description}' at ₹{item_cost:,.2f} exceeds your remaining budget. "
                    f"You have ₹{remaining:,.2f} remaining this month "
                    f"(budget: ₹{monthly_budget:,.2f}, spent: ₹{spent_this_month:,.2f}). "
                    f"You're short by ₹{shortfall:,.2f}."
                )

            return {
                "affordable": affordable,
                "item_description": item_description,
                "item_cost": item_cost,
                "monthly_budget": monthly_budget,
                "spent_this_month": spent_this_month,
                "remaining": remaining,
                "verdict": verdict,
            }

    return await asyncio.to_thread(_analyze)


# ---------------------------------------------------------------------------
# MCP Resource — monthly summary 
# ---------------------------------------------------------------------------

@mcp.resource(
    "expense://summary/{session_token}/monthly",
    name="monthly_summary",
    title="Monthly Expense Summary",
    description=(
        "Returns a JSON summary of the authenticated user's monthly spending: "
        "total spent, top category, comparison to last month, and remaining budget."
    ),
    mime_type="application/json",
)
async def monthly_summary_resource(session_token: str) -> dict:
    """MCP Resource: monthly spending summary for the authenticated user."""
    try:
        user_id = resolve_session(session_token)
    except ValueError as exc:
        return {"error": str(exc)}

    import json

    def _build_summary() -> dict:
        with SessionLocal() as session:
            today = datetime.now(timezone.utc).date()
            first_of_month = today.replace(day=1)

            # This month's expenses
            this_month = session.scalars(
                select(Expense).where(
                    Expense.user_id == user_id,
                    Expense.date >= first_of_month,
                    Expense.date <= today,
                )
            ).all()

            total_spent = sum(e.amount for e in this_month)

            # Top category
            cat_totals: dict[str, float] = {}
            for e in this_month:
                cat_totals[e.category] = cat_totals.get(e.category, 0) + e.amount
            top_category = max(cat_totals, key=cat_totals.get) if cat_totals else "none"
            top_category_amount = cat_totals.get(top_category, 0)

            # Last month comparison
            if first_of_month.month == 1:
                last_month_start = first_of_month.replace(year=first_of_month.year - 1, month=12)
            else:
                last_month_start = first_of_month.replace(month=first_of_month.month - 1)
            last_month_end = first_of_month - timedelta(days=1)

            last_month_expenses = session.scalars(
                select(Expense).where(
                    Expense.user_id == user_id,
                    Expense.date >= last_month_start,
                    Expense.date <= last_month_end,
                )
            ).all()
            last_month_total = sum(e.amount for e in last_month_expenses)

            if last_month_total > 0:
                pct_change = ((total_spent - last_month_total) / last_month_total) * 100
            else:
                pct_change = 100.0 if total_spent > 0 else 0.0

            # Budget
            budget_row = session.scalars(
                select(Budget).where(Budget.user_id == user_id)
            ).first()
            remaining_budget = (budget_row.monthly_limit - total_spent) if budget_row else None
            monthly_limit = budget_row.monthly_limit if budget_row else None

            return {
                "month": first_of_month.isoformat(),
                "total_spent": round(total_spent, 2),
                "top_category": top_category,
                "top_category_amount": round(top_category_amount, 2),
                "last_month_total": round(last_month_total, 2),
                "pct_change_vs_last_month": round(pct_change, 1),
                "monthly_budget": monthly_limit,
                "monthly_limit": monthly_limit,
                "remaining_budget": round(remaining_budget, 2) if remaining_budget is not None else None,
                "expense_count": len(this_month),
            }

    return await asyncio.to_thread(_build_summary)


if __name__ == "__main__":
    # Binds to 127.0.0.1:8001 (localhost only, intentional for POC — see #18)
    mcp.run(transport="streamable-http", port=8001)
