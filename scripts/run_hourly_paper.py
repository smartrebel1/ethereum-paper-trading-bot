#!/usr/bin/env python
"""Run one hourly stateful paper-trading cycle from public Binance data.

This is the GitHub-hosted alternative to the always-on WebSocket process.
Each run restores a JSON state, processes newly completed 4h candles, updates
an open virtual position, evaluates new 4h signals, and persists the state.
No credentials and no real orders are used.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "runtime" / "hourly_paper_state.json"
os.environ["TRADING_MODE"] = "paper"
os.environ["ENABLE_LIVE_TRADING"] = "false"

if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from app.common.enums import Side  # noqa: E402
from app.common.safety import enforce_paper_only  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.config.strategy_config import load_frozen_strategy_config  # noqa: E402
from app.market_data.binance import BinanceRESTProvider  # noqa: E402
from app.strategy.ema_atr import EMAAtrStrategy  # noqa: E402

VERSION = 1
MAX_DRAWDOWN = Decimal("0.05")
MAX_DAILY_LOSS = Decimal("0.03")


def dec(value: object) -> Decimal:
    return Decimal(str(value))


def atomic_write(payload: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="hourly-paper-",
        suffix=".json",
        dir=STATE_PATH.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Path(temp_name).replace(STATE_PATH)
    finally:
        temp = Path(temp_name)
        if temp.exists():
            temp.unlink()


def load_state(starting_balance: Decimal, symbol: str) -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "version": VERSION,
            "symbol": symbol,
            "starting_balance": str(starting_balance),
            "cash": str(starting_balance),
            "position": None,
            "trades": [],
            "last_processed_4h": None,
            "last_signal": None,
            "last_price": None,
            "last_price_at": None,
            "peak_equity": str(starting_balance),
            "status": "INITIALIZED",
            "updated_at": None,
            "last_error": None,
        }

    payload = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if payload.get("version") != VERSION or payload.get("symbol") != symbol:
        raise RuntimeError("hourly paper state is incompatible with current configuration")
    return payload


def equity(state: dict[str, Any]) -> Decimal:
    position = state.get("position")
    price = state.get("last_price")
    if not position or price is None:
        return dec(state["cash"])
    return dec(state["cash"]) + dec(position["quantity"]) * dec(price)


def daily_realized_loss(state: dict[str, Any], now: datetime) -> Decimal:
    day = now.date().isoformat()
    pnl = sum(
        (
            dec(t["net_pnl"])
            for t in state["trades"]
            if str(t["exit_at"]).startswith(day)
        ),
        Decimal(0),
    )
    return max(Decimal(0), -pnl)


def can_open(state: dict[str, Any], now: datetime) -> tuple[bool, str]:
    if state.get("position") is not None:
        return False, "POSITION_ALREADY_OPEN"

    eq = equity(state)
    peak = dec(state.get("peak_equity", state["starting_balance"]))
    if peak > 0 and (peak - eq) / peak >= MAX_DRAWDOWN:
        return False, "MAX_DRAWDOWN_GUARD"

    daily_limit = dec(state["starting_balance"]) * MAX_DAILY_LOSS
    if daily_realized_loss(state, now) >= daily_limit:
        return False, "MAX_DAILY_LOSS_GUARD"

    return True, "OK"


def close_position(
    state: dict[str, Any],
    price: Decimal,
    at: datetime,
    reason: str,
) -> None:
    position = state["position"]
    if position is None:
        return

    quantity = dec(position["quantity"])
    entry = dec(position["entry_price"])
    entry_fee = dec(position["entry_fee"])
    exit_notional = quantity * price
    exit_fee = exit_notional * dec(position["fee_rate"])
    gross = (price - entry) * quantity
    net = gross - entry_fee - exit_fee

    state["cash"] = str(dec(state["cash"]) + exit_notional - exit_fee)
    state["trades"].append(
        {
            "entry_price": str(entry),
            "exit_price": str(price),
            "quantity": str(quantity),
            "entry_fee": str(entry_fee),
            "exit_fee": str(exit_fee),
            "gross_pnl": str(gross),
            "net_pnl": str(net),
            "exit_reason": reason,
            "entry_at": position["opened_at"],
            "exit_at": at.isoformat(),
            "signal_candle_open_time": position["signal_candle_open_time"],
            "signal_confidence": position["signal_confidence"],
        }
    )
    state["position"] = None


def open_position(
    state: dict[str, Any],
    price: Decimal,
    candle: Any,
    confidence: Decimal,
    atr: Decimal,
    strategy: EMAAtrStrategy,
    config: Any,
) -> None:
    eq = equity(state)
    fee_multiplier = Decimal(1) + config.fee_rate_decimal
    budget = min(
        eq * config.max_position_pct_decimal,
        dec(state["cash"]) / fee_multiplier,
    )
    quantity = (budget / price).quantize(Decimal("0.000001"))
    if quantity <= 0:
        return

    notional = quantity * price
    fee = notional * config.fee_rate_decimal
    sl, tp = strategy.stop_and_target(
        entry_price=price,
        atr_value=atr,
        side=Side.BUY,
    )
    state["cash"] = str(dec(state["cash"]) - notional - fee)
    state["position"] = {
        "quantity": str(quantity),
        "entry_price": str(price),
        "entry_fee": str(fee),
        "fee_rate": str(config.fee_rate_decimal),
        "stop_loss": str(sl),
        "take_profit": str(tp),
        "signal_candle_open_time": candle.open_time.isoformat(),
        "signal_confidence": str(confidence),
        "opened_at": candle.close_time.isoformat(),
    }


def main() -> int:
    settings = get_settings()
    enforce_paper_only(settings)
    config = load_frozen_strategy_config()
    strategy = EMAAtrStrategy(config)
    state = load_state(settings.starting_balance, settings.symbol)
    provider = BinanceRESTProvider()

    try:
        candles = provider.fetch_candles(
            settings.symbol,
            settings.timeframe,
            include_incomplete=False,
        )
        if len(candles) < strategy.warmup_candles():
            raise RuntimeError("not enough completed candles for strategy warmup")
        current_price, current_at = provider.fetch_current_price(settings.symbol)
    finally:
        provider.close()

    state["last_price"] = str(current_price)
    state["last_price_at"] = current_at.isoformat()
    state["last_error"] = None

    completed = candles[-500:]
    last_processed = state.get("last_processed_4h")
    if last_processed is None:
        state["last_processed_4h"] = completed[-1].open_time.isoformat()
        new_candles = []
    else:
        new_candles = [
            candle
            for candle in completed
            if candle.open_time.isoformat() > last_processed
        ]

    for candle in new_candles:
        position = state.get("position")
        if position is not None:
            stop_loss = dec(position["stop_loss"])
            take_profit = dec(position["take_profit"])
            if candle.low <= stop_loss:
                close_position(state, stop_loss, candle.open_time, "SL_HIT")
            elif candle.high >= take_profit:
                close_position(state, take_profit, candle.open_time, "TP_HIT")

        window = [
            item
            for item in completed
            if item.open_time <= candle.open_time
        ][-strategy.warmup_candles():]

        if len(window) >= strategy.warmup_candles() and state.get("position") is None:
            decision = strategy.evaluate(window)
            state["last_signal"] = {
                "candle_open_time": candle.open_time.isoformat(),
                "action": decision.action.value,
                "confidence": (
                    str(decision.confidence)
                    if decision.confidence is not None
                    else None
                ),
                "reason": decision.reason,
                "suppressed_reason": decision.suppressed_reason,
                "atr": (
                    str(decision.snapshot.atr)
                    if decision.snapshot.atr is not None
                    else None
                ),
                "evaluated_at": datetime.now(tz=UTC).isoformat(),
            }

            if (
                decision.is_entry
                and decision.confidence is not None
                and decision.snapshot.atr is not None
            ):
                allowed, reason = can_open(state, candle.close_time)
                if allowed:
                    next_candle = next(
                        (
                            item
                            for item in completed
                            if item.open_time
                            == decision.target_execution_open_time
                        ),
                        None,
                    )
                    if next_candle is not None:
                        entry_price = next_candle.open
                        entry_at = next_candle.open_time
                        entry_candle = next_candle
                        execution_mode = "TARGET_CANDLE_OPEN"
                    else:
                        # The GitHub job may start after the target 4h candle
                        # has opened. Never invent its historical fill; use the
                        # current public price as a delayed, conservative fill.
                        entry_price = current_price
                        entry_at = current_at
                        entry_candle = candle
                        execution_mode = "CURRENT_PRICE_DELAYED"

                    open_position(
                        state,
                        entry_price,
                        entry_candle,
                        decision.confidence,
                        decision.snapshot.atr,
                        strategy,
                        config,
                    )
                    state["last_signal"]["entry_price"] = str(entry_price)
                    state["last_signal"]["entry_at"] = entry_at.isoformat()
                    state["last_signal"]["execution_mode"] = execution_mode
                else:
                    state["last_signal"]["risk_gate"] = reason

        state["last_processed_4h"] = candle.open_time.isoformat()

    position = state.get("position")
    if position is not None:
        stop_loss = dec(position["stop_loss"])
        take_profit = dec(position["take_profit"])
        if current_price <= stop_loss:
            close_position(state, stop_loss, current_at, "LIVE_PRICE_SL")
        elif current_price >= take_profit:
            close_position(state, take_profit, current_at, "LIVE_PRICE_TP")

    eq = equity(state)
    peak = dec(state.get("peak_equity", state["starting_balance"]))
    state["peak_equity"] = str(max(peak, eq))
    state["status"] = "IDLE"
    state["updated_at"] = current_at.isoformat()
    atomic_write(state)

    print(
        f"[hourly-paper] price={current_price} equity={eq} "
        f"new_4h={len(new_candles)} trades={len(state['trades'])} "
        f"position={'OPEN' if state.get('position') else 'NONE'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
