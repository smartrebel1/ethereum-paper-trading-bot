"""Real-time paper trader driven by Binance public WebSocket market data.

This module is deliberately separate from the deterministic candle replay engine.
The replay engine remains the historical research baseline; this engine is the
live-paper path used to observe what the same baseline does with a real-time
price feed.

Policy:
* public market data only; no Binance credentials and no Binance order endpoint;
* the 4h EMA/ATR strategy is evaluated only on a newly closed 4h candle;
* an approved BUY is armed for a short window and filled on the next live trade;
* SL/TP are checked on every live trade, not on 4h candle closes;
* state is persisted atomically to a local JSON file so a process restart can
  recover the virtual cash, position and trade history;
* drawdown/day-loss guards can stop new entries while an open position is still
  managed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import websockets

from app.common.time_utils import ensure_aware_utc
from app.config.settings import get_settings
from app.config.strategy_config import load_frozen_strategy_config
from app.market_data.binance import BinanceRESTProvider
from app.strategy.ema_atr import EMAAtrStrategy

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://stream.binance.com:9443/stream"
STATE_VERSION = 1
ENTRY_ARM_SECONDS = 120
SLIPPAGE_BPS = Decimal("2")
MAX_DRAWDOWN_PCT = Decimal("0.05")
MAX_DAILY_LOSS_PCT = Decimal("0.03")


@dataclass
class LivePosition:
    quantity: Decimal
    entry_price: Decimal
    entry_fee: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    signal_candle_open_time: str
    signal_confidence: Decimal
    opened_at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "quantity": str(self.quantity),
            "entry_price": str(self.entry_price),
            "entry_fee": str(self.entry_fee),
            "stop_loss": str(self.stop_loss),
            "take_profit": str(self.take_profit),
            "signal_candle_open_time": self.signal_candle_open_time,
            "signal_confidence": str(self.signal_confidence),
            "opened_at": self.opened_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> LivePosition:
        return cls(
            quantity=Decimal(value["quantity"]),
            entry_price=Decimal(value["entry_price"]),
            entry_fee=Decimal(value["entry_fee"]),
            stop_loss=Decimal(value["stop_loss"]),
            take_profit=Decimal(value["take_profit"]),
            signal_candle_open_time=str(value["signal_candle_open_time"]),
            signal_confidence=Decimal(value["signal_confidence"]),
            opened_at=str(value["opened_at"]),
        )


class LivePaperState:
    def __init__(self, path: Path, starting_balance: Decimal, symbol: str) -> None:
        self.path = path
        self.starting_balance = starting_balance
        self.symbol = symbol
        self.cash = starting_balance
        self.position: LivePosition | None = None
        self.trades: list[dict[str, Any]] = []
        self.last_price: Decimal | None = None
        self.last_price_at: str | None = None
        self.last_closed_4h: str | None = None
        self.last_signal: dict[str, Any] | None = None
        self.armed: dict[str, Any] | None = None
        self.peak_equity = starting_balance
        self.status = "STARTING"
        self.connected_at: str | None = None
        self.updated_at: str | None = None
        self.last_error: str | None = None
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != STATE_VERSION:
                raise ValueError("unsupported live paper state version")
            self.cash = Decimal(payload["cash"])
            self.peak_equity = Decimal(payload.get("peak_equity", payload["cash"]))
            self.last_price = Decimal(payload["last_price"]) if payload.get("last_price") else None
            self.last_price_at = payload.get("last_price_at")
            self.last_closed_4h = payload.get("last_closed_4h")
            self.last_signal = payload.get("last_signal")
            self.armed = payload.get("armed")
            self.position = LivePosition.from_dict(payload["position"]) if payload.get("position") else None
            self.trades = list(payload.get("trades", []))
            self.status = payload.get("status", "RECOVERED")
            self.connected_at = payload.get("connected_at")
            self.updated_at = payload.get("updated_at")
            self.last_error = payload.get("last_error")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"cannot load live paper state {self.path}: {exc}") from exc

    @property
    def equity(self) -> Decimal:
        if self.last_price is None or self.position is None:
            return self.cash
        return self.cash + self.position.quantity * self.last_price

    @property
    def unrealized_pnl(self) -> Decimal:
        if self.last_price is None or self.position is None:
            return Decimal(0)
        return (self.last_price - self.position.entry_price) * self.position.quantity - self.position.entry_fee

    @property
    def net_pnl(self) -> Decimal:
        return self.equity - self.starting_balance

    @property
    def drawdown_pct(self) -> Decimal:
        if self.peak_equity <= 0:
            return Decimal(0)
        return max(Decimal(0), (self.peak_equity - self.equity) / self.peak_equity)

    def daily_realized_pnl(self, now: datetime) -> Decimal:
        day = ensure_aware_utc(now).date().isoformat()
        return sum(
            (Decimal(str(t["net_pnl"])) for t in self.trades if str(t["exit_at"]).startswith(day)),
            Decimal(0),
        )

    def can_open(self, now: datetime) -> tuple[bool, str]:
        if self.position is not None:
            return False, "POSITION_ALREADY_OPEN"
        if self.drawdown_pct >= MAX_DRAWDOWN_PCT:
            return False, "MAX_DRAWDOWN_GUARD"
        daily_loss = -self.daily_realized_pnl(now)
        if daily_loss >= self.starting_balance * MAX_DAILY_LOSS_PCT:
            return False, "MAX_DAILY_LOSS_GUARD"
        return True, "OK"

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": STATE_VERSION,
            "symbol": self.symbol,
            "starting_balance": str(self.starting_balance),
            "cash": str(self.cash),
            "equity": str(self.equity),
            "net_pnl": str(self.net_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "peak_equity": str(self.peak_equity),
            "drawdown_pct": str(self.drawdown_pct),
            "last_price": str(self.last_price) if self.last_price is not None else None,
            "last_price_at": self.last_price_at,
            "last_closed_4h": self.last_closed_4h,
            "last_signal": self.last_signal,
            "armed": self.armed,
            "position": self.position.as_dict() if self.position else None,
            "trades": self.trades[-200:],
            "status": self.status,
            "connected_at": self.connected_at,
            "updated_at": self.updated_at,
            "last_error": self.last_error,
        }
        fd, temp_name = tempfile.mkstemp(prefix="live-paper-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


class LivePaperEngine:
    """Long-only real-time paper trader for ETHUSDT."""

    def __init__(
        self,
        *,
        state_path: str | Path | None = None,
        websocket_url: str = DEFAULT_WS_URL,
        rest_provider: BinanceRESTProvider | None = None,
    ) -> None:
        settings = get_settings()
        self.settings = settings
        self.config = load_frozen_strategy_config()
        self.strategy = EMAAtrStrategy(self.config)
        self.symbol = settings.symbol
        self.websocket_url = websocket_url.rstrip("/")
        self.rest = rest_provider or BinanceRESTProvider()
        self.owns_rest = rest_provider is None
        self.state = LivePaperState(
            Path(state_path or "data/live_paper_state.json"),
            settings.starting_balance,
            self.symbol,
        )
        self._stop = asyncio.Event()

    async def run_forever(self) -> None:
        """Keep the real-time paper trader connected with bounded reconnects."""
        backoff = 1
        try:
            while not self._stop.is_set():
                try:
                    await self._run_connection()
                    backoff = 1
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.state.status = "RECONNECTING"
                    self.state.last_error = f"{type(exc).__name__}: {exc}"
                    self.state.updated_at = datetime.now(tz=UTC).isoformat()
                    self.state.save()
                    logger.exception("live paper websocket loop failed")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
        finally:
            self.state.status = "STOPPED"
            self.state.updated_at = datetime.now(tz=UTC).isoformat()
            self.state.save()
            self.close()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        if self.owns_rest:
            self.rest.close()

    async def _run_connection(self) -> None:
        streams = f"{self.symbol.lower()}@trade/{self.symbol.lower()}@kline_4h"
        url = f"{self.websocket_url}?streams={streams}"
        logger.info("connecting live paper stream: %s", url)
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_queue=2000,
        ) as websocket:
            now = datetime.now(tz=UTC)
            self.state.status = "RUNNING"
            self.state.connected_at = now.isoformat()
            self.state.last_error = None
            self.state.updated_at = now.isoformat()
            self.state.save()

            if self.state.last_closed_4h is None:
                candles = self.rest.fetch_candles(self.symbol, "4h", include_incomplete=False)
                if candles:
                    self.state.last_closed_4h = candles[-1].open_time.isoformat()
                    self.state.save()

            async for message in websocket:
                if isinstance(message, bytes):
                    message = message.decode("utf-8")
                payload = json.loads(message)
                data = payload.get("data", payload)
                event_type = data.get("e")
                if event_type == "trade":
                    await self._on_trade(Decimal(str(data["p"])), self._event_time(data.get("E")))
                elif event_type == "kline" and data.get("k", {}).get("x"):
                    await self._on_closed_4h(data["k"])

    @staticmethod
    def _event_time(value: object) -> datetime:
        if value is None:
            return datetime.now(tz=UTC)
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)

    async def _on_trade(self, price: Decimal, at: datetime) -> None:
        price = price.quantize(Decimal("0.00000001"))
        self.state.last_price = price
        self.state.last_price_at = at.isoformat()
        self.state.peak_equity = max(self.state.peak_equity, self.state.equity)

        if self.state.armed is not None:
            expires = datetime.fromisoformat(self.state.armed["expires_at"])
            if at <= expires and self.state.position is None:
                allowed, reason = self.state.can_open(at)
                if allowed:
                    self._open(price, at)
                else:
                    if self.state.last_signal is not None:
                        self.state.last_signal["risk_gate"] = reason
                    self.state.armed = None

        if self.state.position is not None:
            if price <= self.state.position.stop_loss:
                self._close(price, at, "SL_HIT")
            elif price >= self.state.position.take_profit:
                self._close(price, at, "TP_HIT")

        self.state.updated_at = at.isoformat()
        self.state.save()

    async def _on_closed_4h(self, kline: dict[str, Any]) -> None:
        open_time = datetime.fromtimestamp(int(kline["t"]) / 1000, tz=UTC)
        marker = open_time.isoformat()
        if marker == self.state.last_closed_4h:
            return

        candles = self.rest.fetch_candles(self.symbol, "4h", include_incomplete=False)
        if not candles:
            return
        window = candles[-self.strategy.warmup_candles() :]
        decision = self.strategy.evaluate(window)
        self.state.last_closed_4h = marker
        self.state.last_signal = {
            "candle_open_time": marker,
            "action": decision.action.value,
            "confidence": str(decision.confidence) if decision.confidence is not None else None,
            "reason": decision.reason,
            "atr": str(decision.snapshot.atr) if decision.snapshot.atr is not None else None,
            "evaluated_at": datetime.now(tz=UTC).isoformat(),
        }
        self.state.armed = None

        if (
            decision.is_entry
            and decision.confidence is not None
            and decision.snapshot.atr is not None
        ):
            allowed, reason = self.state.can_open(open_time + timedelta(seconds=1))
            if allowed:
                self.state.armed = {
                    "signal_candle_open_time": marker,
                    "confidence": str(decision.confidence),
                    "atr": str(decision.snapshot.atr),
                    "expires_at": (open_time + timedelta(seconds=ENTRY_ARM_SECONDS)).isoformat(),
                }
            else:
                self.state.last_signal["risk_gate"] = reason

        self.state.updated_at = datetime.now(tz=UTC).isoformat()
        self.state.save()
        logger.info("new 4h decision: %s", self.state.last_signal)

    def _open(self, price: Decimal, at: datetime) -> None:
        if self.state.armed is None:
            return
        equity = self.state.equity
        cap = equity * self.config.max_position_pct_decimal
        budget = min(cap, self.state.cash / (Decimal(1) + self.config.fee_rate_decimal))
        quantity = (budget / price).quantize(Decimal("0.000001"))
        if quantity <= 0:
            if self.state.last_signal is not None:
                self.state.last_signal["risk_gate"] = "ZERO_QUANTITY"
            self.state.armed = None
            return

        notional = quantity * price
        fee = notional * self.config.fee_rate_decimal
        atr_value = Decimal(self.state.armed["atr"])
        stop_loss, take_profit = self.strategy.stop_and_target(
            entry_price=price,
            atr_value=atr_value,
            side=self._buy_side(),
        )
        self.state.cash -= notional + fee
        self.state.position = LivePosition(
            quantity=quantity,
            entry_price=price,
            entry_fee=fee,
            stop_loss=stop_loss,
            take_profit=take_profit,
            signal_candle_open_time=self.state.armed["signal_candle_open_time"],
            signal_confidence=Decimal(self.state.armed["confidence"]),
            opened_at=at.isoformat(),
        )
        if self.state.last_signal is not None:
            self.state.last_signal["entry_price"] = str(price)
            self.state.last_signal["entry_at"] = at.isoformat()
        self.state.armed = None

    def _close(self, price: Decimal, at: datetime, reason: str) -> None:
        position = self.state.position
        if position is None:
            return
        notional = position.quantity * price
        exit_fee = notional * self.config.fee_rate_decimal
        self.state.cash += notional - exit_fee
        gross_pnl = (price - position.entry_price) * position.quantity
        net_pnl = gross_pnl - position.entry_fee - exit_fee
        self.state.trades.append(
            {
                "entry_price": str(position.entry_price),
                "exit_price": str(price),
                "quantity": str(position.quantity),
                "entry_fee": str(position.entry_fee),
                "exit_fee": str(exit_fee),
                "gross_pnl": str(gross_pnl),
                "net_pnl": str(net_pnl),
                "exit_reason": reason,
                "entry_at": position.opened_at,
                "exit_at": at.isoformat(),
                "signal_candle_open_time": position.signal_candle_open_time,
                "signal_confidence": str(position.signal_confidence),
            }
        )
        self.state.position = None
        self.state.peak_equity = max(self.state.peak_equity, self.state.equity)

    @staticmethod
    def _buy_side():
        from app.common.enums import Side

        return Side.BUY


__all__ = [
    "ENTRY_ARM_SECONDS",
    "LivePaperEngine",
    "LivePaperState",
    "LivePosition",
    "MAX_DAILY_LOSS_PCT",
    "MAX_DRAWDOWN_PCT",
    "SLIPPAGE_BPS",
]
