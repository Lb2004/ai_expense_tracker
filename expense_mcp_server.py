"""
Expense Tracker MCP Server — standalone HTTP process on port 8001.

Self-contained: defines its own Pydantic models and DB access so it can
run independently of the Streamlit app.  Tools take user_id as a plain
parameter (auth hardening is out of scope for this rough pass).

Run:  python expense_mcp_server.py
Test: open MCP Inspector at http://127.0.0.1:8001/mcp
"""

import asyncio
from datetime import date, datetime, timezone

from pydantic import BaseModel
from sqlalchemy import Date, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event, select
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
# Database setup (mirrors the app's database.py but is fully self-contained)
# ---------------------------------------------------------------------------

DATABASE_URL = "sqlite:///./expenses.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


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
# SQLAlchemy models (duplicated here so the server is independently runnable)
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    expenses: Mapped[list["Expense"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class Expense(Base):
    __tablename__ = "expenses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    user: Mapped[User] = relationship(back_populates="expenses")


# Create tables on import (idempotent).
Base.metadata.create_all(bind=engine)


# ---------------------------------------------------------------------------
# Pydantic response models (self-contained — not imported from schemas.py)
# ---------------------------------------------------------------------------

class ExpenseOut(BaseModel):
    id: int
    user_id: int
    amount: float
    category: str
    description: str
    date: str  # ISO format string


# ---------------------------------------------------------------------------
# MCP Server & Tools
# ---------------------------------------------------------------------------

mcp = MCPServer("expense-server")


@mcp.tool()
async def add_expense(
    user_id: int,
    amount: float,
    category: str,
    description: str,
    date: str = "",
) -> str:
    """Add a new expense for a user.

    Args:
        user_id: The ID of the user who owns this expense.
        amount: The amount of the expense in Rupees (₹) (positive number).
        category: A short category label (e.g. "food", "transport", "entertainment").
        description: A brief description of what the expense was for.
        date: The date of the expense in ISO format (YYYY-MM-DD). Defaults to today's date if empty or "today".

    Returns:
        A confirmation message with the new expense ID.
    """
    # Validate date format or default to today
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
            # Verify user exists
            user = session.get(User, user_id)
            if user is None:
                raise ValueError(f"No user with id {user_id}")
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

    try:
        expense_id = await asyncio.to_thread(_insert)
    except ValueError as exc:
        return f"Error: {exc}"

    return f"Expense #{expense_id} added: ₹{amount:.2f} for '{category}' on {date}."


@mcp.tool()
async def delete_expense(user_id: int, expense_id: int) -> str:
    """Delete an expense, but only if it belongs to the given user.

    Args:
        user_id: The ID of the user requesting deletion.
        expense_id: The ID of the expense to delete.

    Returns:
        A confirmation or error message.
    """

    def _delete() -> str:
        with SessionLocal() as session:
            expense = session.get(Expense, expense_id)
            if expense is None:
                return f"Error: Expense #{expense_id} not found."
            if expense.user_id != user_id:
                return f"Error: Expense #{expense_id} does not belong to user {user_id}."
            session.delete(expense)
            session.commit()
            return f"Expense #{expense_id} deleted."

    return await asyncio.to_thread(_delete)


@mcp.tool()
async def list_expenses(user_id: int) -> list[dict]:
    """List all expenses for a user.

    Args:
        user_id: The ID of the user whose expenses to list.

    Returns:
        A list of expense dicts with id, amount, category, description, and date.
        Returns an empty list if the user has no expenses.
    """

    def _query() -> list[dict]:
        with SessionLocal() as session:
            stmt = select(Expense).where(Expense.user_id == user_id).order_by(Expense.date.desc())
            rows = session.scalars(stmt).all()
            return [
                ExpenseOut(
                    id=row.id,
                    user_id=row.user_id,
                    amount=row.amount,
                    category=row.category,
                    description=row.description,
                    date=str(row.date),
                ).model_dump()
                for row in rows
            ]

    return await asyncio.to_thread(_query)


if __name__ == "__main__":
    mcp.run(transport="streamable-http", port=8001)
