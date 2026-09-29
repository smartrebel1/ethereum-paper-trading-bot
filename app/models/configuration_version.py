"""Registered configuration snapshots (env-derived, secrets stripped).

Recorded at startup next to the strategy version. Two runs that disagree on
results but agree on config hashes differ for a reason *other* than
configuration — which is the first question any post-mortem asks.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import TS


class ConfigurationVersion(Base):
    __tablename__ = "configuration_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="app")
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    software_version: Mapped[str] = mapped_column(String(16), nullable=False, default="0.1.0")
    registered_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (UniqueConstraint("scope", "config_hash", name="uq_configuration_versions_scope_hash"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ConfigurationVersion {self.scope} {self.config_hash[:12]}>"


__all__ = ["ConfigurationVersion"]
