"""
Expense Tracker MCP Server — standalone HTTP process on port 8001.

Self-contained: defines its own models and DB access so it can run
independently of the Streamlit app.  All mutating/querying tools use
session_token for auth — no raw user_id accepted from callers.

Run:  python expense_mcp_server.py
Test: open MCP Inspector at http://127.0.0.1:8001/mcp
"""

import asyncio
import re
import uuid
from datetime import date, datetime, timedelta, timezone

import sqlparse
from pydantic import BaseModel
from sqlalchemy import (
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    select,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from mcp.server.mcpserver import MCPServer

# ---------------------------------------------------------------------------
# Database setup (self-contained — mirrors the app's database.py)
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


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# SQLAlchemy models (duplicated so the server is independently runnable)
# ---------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expenses: Mapped[list["Expense"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    user: Mapped[User] = relationship(back_populates="expenses")


class UserSession(Base):
    """Server-validated session tokens — never trust caller-provided user_id."""

    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(
        String(36), unique=True, nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Budget(Base):
    """Per-user monthly budget — one row per user."""

    __tablename__ = "budgets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    monthly_limit: Mapped[float] = mapped_column(Float, nullable=False, default=50000.0)


# Create tables on import (idempotent).
Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

DEFAULT_BUDGET = 50000.0


def create_session(user_id: int) -> str:
    """Create a new session token for a user. Returns the token string."""
    token = str(uuid.uuid4())
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
    """
    with SessionLocal() as session:
        stmt = select(UserSession).where(UserSession.token == token)
        sess = session.scalars(stmt).first()
        if sess is None:
            raise ValueError(f"Invalid session token.")
        if sess.expires_at.replace(tzinfo=timezone.utc) < _utcnow():
            raise ValueError(f"Session token has expired.")
        return sess.user_id


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
        # Use word-boundary matching to avoid false positives (e.g. "description" containing "create"... no but "delete" in "deleted")
        if re.search(rf"\b{kw}\b", sql_lower):
            raise ValueError(f"Forbidden keyword: {kw}")

    # Check for semicolons (multi-statement injection)
    if ";" in raw_sql:
        raise ValueError("Multiple statements are not allowed.")

    # Extract all identifiers and check against allowlist
    identifiers = set(_IDENT_RE.findall(sql_lower))
    # Remove SQL keywords, aggregates, and known safe tokens
    sql_keywords = {
        "select", "from", "where", "and", "or", "not", "in", "between",
        "like", "is", "null", "as", "on", "by", "group", "order", "having",
        "limit", "offset", "asc", "desc", "distinct", "case", "when", "then",
        "else", "end", "cast", "coalesce", "ifnull", "strftime", "substr",
        "length", "lower", "upper", "trim", "round", "abs", "total",
        "expenses", "true", "false",
    }
    safe_tokens = ALLOWED_COLUMNS | ALLOWED_AGGREGATES | sql_keywords
    unsafe = identifiers - safe_tokens
    # Filter out numeric-looking tokens and parameter placeholders
    unsafe = {t for t in unsafe if not t.isdigit() and t != "uid"}
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


@mcp.tool()
async def create_user_session(user_id: int) -> str:
    """Create a session token for a user at login time.

    This is called by the app at login — NOT by the LLM during conversation.

    Args:
        user_id: The ID of the user logging in.

    Returns:
        A session token string (UUID) valid for 24 hours.
    """
    def _create() -> str:
        with SessionLocal() as session:
            user = session.get(User, user_id)
            if user is None:
                raise ValueError(f"No user with id {user_id}")
        return create_session(user_id)

    try:
        token = await asyncio.to_thread(_create)
    except ValueError as exc:
        return f"Error: {exc}"
    return token


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

    if amount <= 0:
        return f"Error: Amount must be positive, got {amount}."

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
async def list_expenses(session_token: str) -> list[dict]:
    """List all expenses for the authenticated user.

    Args:
        session_token: The session token of the authenticated user.

    Returns:
        A list of expense dicts, or an error string.
    """
    try:
        user_id = resolve_session(session_token)
    except ValueError as exc:
        return [{"error": str(exc)}]

    def _query() -> list[dict]:
        with SessionLocal() as session:
            stmt = (
                select(Expense)
                .where(Expense.user_id == user_id)
                .order_by(Expense.date.desc())
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
        "table": "expenses",
        "columns": {
            "id": "integer — unique expense ID",
            "amount": "float — expense amount in ₹",
            "category": "string — category label (e.g. food, transport)",
            "description": "string — brief description",
            "date": "date — ISO format YYYY-MM-DD",
        },
        "allowed_aggregates": sorted(ALLOWED_AGGREGATES),
        "notes": (
            "All queries are automatically scoped to the authenticated user. "
            "Do NOT include user_id in your SQL. Only SELECT statements are allowed."
        ),
    }


@mcp.tool()
async def query_expenses(session_token: str, sql_query: str) -> list[dict] | str:
    """Run a safe, read-only SQL query against the authenticated user's expenses.

    The query is validated for safety (SELECT-only, column allowlist enforced).
    A WHERE clause scoping results to the authenticated user is automatically injected.

    Args:
        session_token: The session token of the authenticated user.
        sql_query: A SELECT query using only allowed columns (amount, category, description, date, id).
                   Do NOT include user_id filters — they are auto-injected.
                   Example: "SELECT category, SUM(amount) FROM expenses GROUP BY category"

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

    # Auto-inject user_id filter
    # Strategy: wrap the user's query and add WHERE user_id = :uid
    # We inject into the original query by finding FROM expenses and adding WHERE
    safe_sql = _inject_user_filter(validated_sql, user_id)

    def _execute() -> list[dict]:
        with SessionLocal() as session:
            result = session.execute(
                text(safe_sql),
                {"uid": user_id},
            )
            columns = list(result.keys())
            rows = result.fetchmany(200)  # cap at 200 rows
            return [dict(zip(columns, row)) for row in rows]

    try:
        return await asyncio.to_thread(_execute)
    except Exception as exc:
        return f"Query execution error: {exc}"


def _inject_user_filter(sql: str, user_id: int) -> str:
    """Inject WHERE user_id = :uid into a SELECT ... FROM expenses query."""
    # Case-insensitive replacement
    # Find "FROM expenses" and inject "WHERE user_id = :uid" after it
    pattern = re.compile(r"(FROM\s+expenses)", re.IGNORECASE)
    match = pattern.search(sql)
    if not match:
        raise ValueError("Query must reference the 'expenses' table.")

    insert_pos = match.end()
    rest = sql[insert_pos:].strip()

    if rest.upper().startswith("WHERE"):
        # Already has a WHERE — add AND
        injected = sql[:insert_pos] + " WHERE user_id = :uid AND " + rest[5:].strip()
    else:
        injected = sql[:insert_pos] + " WHERE user_id = :uid " + rest

    return injected


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


if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8001)
