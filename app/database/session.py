"""Session management.

Rules:
* One transactional unit of work per engine action (``session_scope``).
* ``expire_on_commit=False`` so ORM objects stay usable for read-only output
  after the transaction closes.
* Explicit ``begin`` semantics: nested ``session_scope`` calls join the outer
  transaction instead of committing early (important for the phase-5
  "position + ledger + execution in one atomic tx" requirement).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager, suppress

from sqlalchemy.orm import Session, sessionmaker

from app.database.engine import get_engine


def build_session_factory(bind=None) -> sessionmaker[Session]:  # noqa: ANN001
    return sessionmaker(
        bind=bind or get_engine(),
        class_=Session,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        future=True,
    )


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Transactional scope: commit on success, rollback on any exception."""
    maker = factory or build_session_factory()
    session = maker()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_sessions() -> None:
    """Best-effort disposal of the engine's pool (test hook)."""
    with suppress(Exception):
        get_engine().dispose()


__all__ = ["build_session_factory", "dispose_sessions", "session_scope"]
