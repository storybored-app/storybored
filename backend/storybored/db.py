"""Database engine + session helpers (SQLModel over sqlite)."""

from collections.abc import Iterator

from fastapi import Request
from sqlmodel import Session, SQLModel, create_engine

from storybored.config import Settings


def create_db_engine(settings: Settings):
    settings.data_path.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{settings.db_path}"
    return create_engine(url, connect_args={"check_same_thread": False})


#: columns added after v1 — create_all() never alters existing tables, so new
#: columns get a one-shot ADD COLUMN guard here (SQLite has no real migrations)
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "shot": {"frame_position": "VARCHAR NOT NULL DEFAULT 'first'"},
}


def _ensure_columns(engine) -> None:
    from sqlalchemy import text

    with engine.connect() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {
                row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
            }
            if not existing:  # table doesn't exist yet — create_all handles it
                continue
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        conn.commit()


def init_db(engine) -> None:
    # Import models so all tables are registered on SQLModel.metadata.
    from storybored import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
    _ensure_columns(engine)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request, bound to the app's engine."""
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        yield session
