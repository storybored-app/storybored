"""Database engine + session helpers (SQLModel over sqlite)."""

from collections.abc import Iterator

from fastapi import Request
from sqlmodel import Session, SQLModel, create_engine

from storybored.config import Settings


def create_db_engine(settings: Settings):
    settings.data_path.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{settings.db_path}"
    return create_engine(url, connect_args={"check_same_thread": False})


def init_db(engine) -> None:
    # Import models so all tables are registered on SQLModel.metadata.
    from storybored import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session(request: Request) -> Iterator[Session]:
    """FastAPI dependency: one session per request, bound to the app's engine."""
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        yield session
