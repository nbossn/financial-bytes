from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from src.config import settings

_is_sqlite = settings.database_url.startswith("sqlite")
_connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(
    settings.database_url,
    pool_pre_ping=not _is_sqlite,
    connect_args=_connect_args,
    **({} if _is_sqlite else {"pool_size": 5, "max_overflow": 10}),
)

if _is_sqlite:
    # WAL mode: allows concurrent readers + one writer; required for multi-thread pipeline
    from sqlalchemy import event, text

    @event.listens_for(engine, "connect")
    def _set_wal(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA journal_mode=WAL")
        dbapi_conn.execute("PRAGMA busy_timeout=10000")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Context manager for database sessions."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
