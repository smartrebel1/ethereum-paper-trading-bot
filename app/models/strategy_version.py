"""Registered strategy builds (name + version + config hash + full config).

Written at startup by :func:`app.repositories.strategy_versions.ensure_registered`.
`(name, version, config_hash)` is unique, so the same build cannot be
registered twice while a *changed parameter* always creates a new row.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import TS


class StrategyVersion(Base):
    __tablename__ = "strategy_versions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    software_version: Mapped[str] = mapped_column(String(16), nullable=False, default="0.1.0")
    registered_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (UniqueConstraint("name", "version", "config_hash", name="uq_strategy_versions_nvh"),)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<StrategyVersion {self.name} {self.version} {self.config_hash[:12]}>"


__all__ = ["StrategyVersion"]
