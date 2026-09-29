"""Persistence for :class:`app.models.strategy_version.StrategyVersion`."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config.strategy_config import StrategyConfig
from app.models.strategy_version import StrategyVersion
from app.repositories.base import get_or_create


def ensure_registered(
    session: Session,
    config: StrategyConfig,
    *,
    now: datetime,
    software_version: str,
) -> tuple[StrategyVersion, bool]:
    """Register the strategy build if unseen. Returns ``(row, created)``."""
    return get_or_create(
        session,
        StrategyVersion,
        name=config.name,
        version=config.version,
        config_hash=config.config_hash,
        defaults={
            "config": config.to_json_dict(),
            "software_version": software_version,
            "registered_at": now,
        },
    )


def by_hash(session: Session, config_hash: str) -> StrategyVersion | None:
    stmt = select(StrategyVersion).where(StrategyVersion.config_hash == config_hash)
    return session.execute(stmt).scalars().first()


def all_versions(session: Session) -> list[StrategyVersion]:
    stmt = select(StrategyVersion).order_by(StrategyVersion.registered_at.desc())
    return list(session.execute(stmt).scalars().all())


__all__ = ["all_versions", "by_hash", "ensure_registered"]
