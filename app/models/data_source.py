"""Data provenance: which dataset produced a result.

A ``dataset_hash`` (see :func:`app.common.hashing.dataset_hash`) pins the exact
candle series a backtest/live run consumed. If the hash in a report does not
match what the archive produces today, the report is not comparable — that is
the point.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base
from app.database.types import TS


class DataSource(Base):
    __tablename__ = "data_sources"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False, default="unknown")
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    start_time: Mapped[datetime | None] = mapped_column(TS)
    end_time: Mapped[datetime | None] = mapped_column(TS)
    candle_count: Mapped[int | None] = mapped_column()
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    registered_at: Mapped[datetime] = mapped_column(TS, nullable=False)

    __table_args__ = (
        UniqueConstraint("name", "dataset_hash", name="uq_data_sources_name_hash"),
        Index("ix_data_sources_sym_tf", "symbol", "timeframe"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DataSource {self.name} {self.dataset_hash[:12]} {self.symbol} {self.timeframe}>"


__all__ = ["DataSource"]
