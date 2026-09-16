from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from config import get_settings

# shared_models.py is dependency-free, so this import is safe at the
# top level — no circular-import workaround needed.  (Fix #17: the old
# local import inside init_db() was required because models.py used to
# import Base from database.py, creating a cycle.  shared_models.py
# defines its own Base, breaking the cycle.)
from shared_models import Base, Budget, Expense, User, UserSession  # noqa: F401


_url = get_settings().database_url
# Streamlit reruns on a different thread than the one that opened SQLite.
_connect_args = {"check_same_thread": False} if _url.startswith("sqlite") else {}
engine = create_engine(_url, connect_args=_connect_args)
SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)


@event.listens_for(engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    # SQLite ignores ForeignKey() unless this pragma is on for the connection.
    if engine.dialect.name != "sqlite":
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db() -> None:
    # All models are imported at the top level from shared_models, so
    # their tables are already registered on Base by the time we get here.
    Base.metadata.create_all(bind=engine)


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
