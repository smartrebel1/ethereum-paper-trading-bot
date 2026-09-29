"""Persistence for :class:`app.models.configuration_version.ConfigurationVersion`."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.common.hashing import config_hash
from app.models.configuration_version import ConfigurationVersion
from app.repositories.base import get_or_create

#: Never persist these (secrets). ``public_dict`` already strips them, but the
#: filter is repeated here so a future caller cannot leak by mistake.
SECRET_KEYS = frozenset({"gemini_api_key", "api_key", "secret", "token", "password"})


def _strip_secrets(config: dict) -> dict:
    return {k: v for k, v in config.items() if k.lower() not in SECRET_KEYS}


def ensure_registered(
    session: Session,
    *,
    config: dict,
    now: datetime,
    software_version: str,
    scope: str = "app",
) -> tuple[ConfigurationVersion, bool]:
    clean = _strip_secrets(dict(config))
    digest = config_hash(clean)
    return get_or_create(
        session,
        ConfigurationVersion,
        scope=scope,
        config_hash=digest,
        defaults={
            "config": clean,
            "software_version": software_version,
            "registered_at": now,
        },
    )


def latest(session: Session, *, scope: str = "app") -> ConfigurationVersion | None:
    stmt = (
        select(ConfigurationVersion)
        .where(ConfigurationVersion.scope == scope)
        .order_by(ConfigurationVersion.registered_at.desc())
        .limit(1)
    )
    return session.execute(stmt).scalars().first()


__all__ = ["SECRET_KEYS", "ensure_registered", "latest"]
