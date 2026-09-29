"""Schema-shape tests: structure, invariants and type discipline.

These run without a database — they assert on ``Base.metadata``, so a model
regression fails here (fast) instead of in an integration test (slow).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Float, Numeric

from app.database.base import Base
from app.database.types import FixedPoint, UTCDateTime
from app.models import ALL_TABLES


def test_all_expected_tables_registered() -> None:
    assert set(Base.metadata.tables) == set(ALL_TABLES)
    assert len(ALL_TABLES) == 16


def test_signal_carries_the_explicit_execution_target() -> None:
    signals = Base.metadata.tables["signals"]
    assert "target_execution_open_time" in signals.columns
    assert "target_execution_candle_id" in signals.columns
    assert "signal_candle_open_time" in signals.columns
    assert "strategy_config_hash" in signals.columns
    check_names = {c.name for c in signals.constraints if c.__class__.__name__ == "CheckConstraint"}
    assert "ck_signals_target_after_candle" in check_names


def test_order_and_execution_are_paper_only_by_construction() -> None:
    for table_name, check_name in (
        ("orders", "ck_orders_paper_only"),
        ("executions", "ck_executions_paper_only"),
    ):
        table = Base.metadata.tables[table_name]
        names = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
        assert check_name in names


def test_order_state_check_enumerates_six_states() -> None:
    orders = Base.metadata.tables["orders"]
    check = next(c for c in orders.constraints if getattr(c, "name", None) == "ck_orders_state_valid")
    sqltext = str(check.sqltext)
    for state in (
        "CREATED",
        "PENDING",
        "TARGET_REACHED",
        "EXECUTED",
        "TARGET_MISSED",
        "CANCELLED",
    ):
        assert state in sqltext


def test_partial_unique_index_on_open_positions() -> None:
    positions = Base.metadata.tables["positions"]
    index = next(i for i in positions.indexes if i.name == "uq_positions_one_open_per_sym_tf")
    assert index.unique is True
    assert [c.name for c in index.columns] == ["symbol", "timeframe"]
    assert index.dialect_options["sqlite"]["where"] is not None
    assert index.dialect_options["postgresql"]["where"] is not None
    assert "OPEN" in str(index.dialect_options["sqlite"]["where"])


def test_every_datetime_column_is_utc_enforcing() -> None:
    offenders: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            try:
                python_type = column.type.python_type
            except NotImplementedError:  # pragma: no cover
                continue
            if python_type is datetime and not isinstance(column.type, UTCDateTime):
                offenders.append(f"{table.name}.{column.name}")
    assert offenders == [], f"datetime columns not using UTCDateTime: {offenders}"


def test_no_float_or_numeric_money_columns() -> None:
    """Money must be exact: FixedPoint (scaled BigInteger), never REAL/NUMERIC."""
    offenders: list[str] = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, (Float, Numeric)) and not isinstance(column.type, FixedPoint):
                offenders.append(f"{table.name}.{column.name}: {column.type!r}")
    assert offenders == []

    fixed = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, FixedPoint)
    ]
    assert len(fixed) >= 30, f"expected many fixed-point columns, found {len(fixed)}"
    assert "signals.confidence" in fixed
    assert "candles.close" in fixed


def test_foreign_keys_point_the_right_way() -> None:
    def targets(table_name: str) -> set[str]:
        table = Base.metadata.tables[table_name]
        return {fk.target_fullname for column in table.columns for fk in column.foreign_keys}

    assert "signals.signal_id" in targets("orders")
    assert "orders.order_id" in targets("executions")
    assert "signals.signal_id" in targets("trades")
    assert "orders.order_id" in targets("trades")
    assert "signals.signal_id" in targets("risk_decisions")


def test_one_order_per_signal_and_purpose() -> None:
    """A signal yields at most one ENTRY and one EXIT order (idempotent retries).

    The exit belongs to the same signal because it is not a new decision: the
    stop/target levels were fixed when the entry signal was created. Reusing the
    signal id keeps the database-level "no look-ahead" CHECK on ``signals``
    untouched — an exit is settled at a level that was already known.
    """
    orders = Base.metadata.tables["orders"]
    executions = Base.metadata.tables["executions"]
    unique = [c for c in orders.constraints if c.__class__.__name__ == "UniqueConstraint"]
    assert {c.name for c in unique} == {"uq_orders_signal_purpose"}
    assert {tuple(col.name for col in c.columns) for c in unique} == {("signal_id", "purpose")}
    exec_uq = {c.name for c in executions.constraints if c.__class__.__name__ == "UniqueConstraint"}
    assert "uq_executions_order_id" in exec_uq


def test_ai_observations_have_no_foreign_keys() -> None:
    """The AI table must not be wired into the trading path."""
    table = Base.metadata.tables["ai_observations"]
    assert not any(column.foreign_keys for column in table.columns), (
        "ai_observations must stay structurally isolated from the trading path"
    )


def test_risk_decision_is_one_per_signal() -> None:
    table = Base.metadata.tables["risk_decisions"]
    assert [c.name for c in table.primary_key.columns] == ["signal_id"]


def test_snapshot_equity_identity_is_persisted() -> None:
    table = Base.metadata.tables["portfolio_snapshots"]
    names = {c.name for c in table.constraints if c.__class__.__name__ == "CheckConstraint"}
    assert "ck_snapshots_equity_identity" in names


def test_all_money_scales_are_registered() -> None:
    scales = {
        column.type.scale
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, FixedPoint)
    }
    assert scales <= {8, 10}
    assert scales == {8, 10}


def test_no_autoincrement_pk_on_financial_tables() -> None:
    """Financial rows are identified by deterministic ids, never by rowid."""
    for table_name in ("candles", "signals", "orders", "executions", "positions", "trades"):
        table = Base.metadata.tables[table_name]
        pk_columns = list(table.primary_key.columns)
        assert len(pk_columns) == 1
        assert not pk_columns[0].autoincrement or pk_columns[0].autoincrement == "auto", (
            f"{table_name} should use a deterministic string id"
        )
        assert pk_columns[0].type.python_type is str


def test_bigint_backing_for_fixed_point() -> None:
    column = Base.metadata.tables["candles"].columns["close"]
    assert isinstance(column.type, FixedPoint)
    assert isinstance(column.type.impl, type(BigInteger()))
