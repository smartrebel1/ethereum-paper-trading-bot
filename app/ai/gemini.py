"""Isolated Gemini market observer. Its output is never consumed by trading code."""
from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.models.ai_observation import AIObservation
from app.models.candle import Candle
from app.strategy.indicators import atr, ema

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "bias": {"type": "string", "enum": ["buy", "sell", "neutral"]},
        "confidence": {"type": "number"},
        "summary_ar": {"type": "string"},
        "risks_ar": {"type": "string"},
    },
    "required": ["bias", "confidence", "summary_ar", "risks_ar"],
}


def _observation_id(symbol: str, timeframe: str, at: datetime) -> str:
    return hashlib.sha256(
        f"gemini|{symbol}|{timeframe}|{at.isoformat()}".encode()
    ).hexdigest()


def _context(candles: list[Candle], settings: Settings) -> dict[str, Any]:
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    emas = ema(closes, settings.ema_period)
    atrs = atr(highs, lows, closes, settings.atr_period)
    latest = candles[-1]
    ohlcv = [
        {
            "open_time": c.open_time.isoformat(),
            "open": str(c.open),
            "high": str(c.high),
            "low": str(c.low),
            "close": str(c.close),
            "volume": str(c.volume),
        }
        for c in candles[-8:]
    ]
    return {
        "symbol": latest.symbol,
        "timeframe": latest.timeframe,
        "candle_open_time": latest.open_time.isoformat(),
        "ohlcv": ohlcv,
        "indicators": {
            "ema": None if emas[-1] is None else str(emas[-1]),
            "ema_previous": (
                None
                if len(emas) < 2 or emas[-2] is None
                else str(emas[-2])
            ),
            "atr": None if atrs[-1] is None else str(atrs[-1]),
        },
    }


def _parse(text: str) -> dict[str, Any]:
    data = json.loads(text.strip())
    if not isinstance(data, dict):
        raise ValueError("Gemini response must be a JSON object")
    bias = str(data.get("bias", "neutral")).lower()
    if bias not in {"buy", "sell", "neutral"}:
        bias = "neutral"
    return {
        "bias": bias,
        "confidence": max(
            0.0, min(1.0, float(data.get("confidence", 0)))
        ),
        "summary_ar": str(data.get("summary_ar", "")),
        "risks_ar": str(data.get("risks_ar", "")),
    }


def observe_latest(
    session: Session,
    settings: Settings,
    *,
    refresh: bool = False,
) -> AIObservation:
    if not settings.ai_enabled or settings.ai_provider != "gemini":
        raise ValueError("Gemini shadow is disabled")
    candles = list(
        session.execute(
            select(Candle)
            .where(
                Candle.symbol == settings.symbol,
                Candle.timeframe == settings.timeframe.value,
                Candle.is_complete.is_(True),
            )
            .order_by(desc(Candle.open_time))
            .limit(
                max(
                    8,
                    settings.ema_period + 2,
                    settings.atr_period + 2,
                )
            )
        ).scalars()
    )[::-1]
    if not candles:
        raise ValueError("No completed market candles available")
    latest = candles[-1]
    oid = _observation_id(
        settings.symbol,
        settings.timeframe.value,
        latest.open_time,
    )
    existing = session.get(AIObservation, oid)
    if existing is not None and not refresh:
        return existing
    context = _context(candles, settings)
    prompt = (
        "أنت مراقب سوق فقط داخل بوت تداول تجريبي. اقرأ بيانات السوق فقط. "
        "لا تدخل في قرار البوت ولا تقترح تنفيذ أمر. أرجع JSON فقط بالمفاتيح: "
        "bias (buy/sell/neutral), confidence (0..1), summary_ar, risks_ar. "
        "كن حذرًا ولا تدّعي يقينًا.\n\n"
        + json.dumps(context, ensure_ascii=False)
    )
    started = time.perf_counter()
    raw: dict[str, Any] = {}
    parsed: dict[str, Any] = {}
    error: str | None = None
    try:
        url = GEMINI_URL.format(model=settings.gemini_model)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": GEMINI_RESPONSE_SCHEMA,
            },
        }
        last_response = None
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = httpx.post(
                    url,
                    headers={"x-goog-api-key": settings.gemini_api_key},
                    json=payload,
                    timeout=max(30.0, settings.ai_timeout_seconds),
                )
                last_response = response
                if (
                    response.status_code not in {408, 429, 500, 502, 503, 504}
                    or attempt == 3
                ):
                    break
                delay = min(8.0, 1.0 * (2**attempt)) + random.uniform(0, 0.5)
                print(
                    f"Gemini transient HTTP {response.status_code}; "
                    f"retrying in {delay:.1f}s ({attempt + 1}/3)"
                )
                time.sleep(delay)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt == 3:
                    break
                delay = min(8.0, 1.0 * (2**attempt)) + random.uniform(0, 0.5)
                print(
                    f"Gemini network error; retrying in "
                    f"{delay:.1f}s ({attempt + 1}/3)"
                )
                time.sleep(delay)
        if last_response is None:
            raise last_error or RuntimeError(
                "Gemini request failed without a response"
            )
        assert last_response is not None
        if last_response.is_error:
            try:
                detail = last_response.json().get("error", {}).get("message", "")
            except Exception:
                detail = ""
            raise ValueError(
                f"Gemini HTTP {last_response.status_code}: "
                f"{detail or last_response.text[:200]}"
            )
        raw = last_response.json()
        candidates = raw.get("candidates") or []
        if not candidates:
            feedback = raw.get("promptFeedback") or {}
            raise ValueError(
                f"Gemini returned no candidates; promptFeedback={feedback}"
            )
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])
        response_text = "".join(
            str(p.get("text", ""))
            for p in parts
            if isinstance(p, dict)
        )
        if not response_text.strip():
            finish_reason = candidate.get("finishReason", "UNKNOWN")
            safety = candidate.get("safetyRatings", [])
            raise ValueError(
                "Gemini returned no text; "
                f"finishReason={finish_reason}; safetyRatings={safety}"
            )
        parsed = _parse(response_text)
    except Exception as exc:
        error = str(exc)[:256]
    observation = existing or AIObservation(
        observation_id=oid,
        provider="gemini",
        model=settings.gemini_model,
        symbol=settings.symbol,
        timeframe=settings.timeframe.value,
        at_candle_open_time=latest.open_time,
        input_context=context,
        raw_response=raw,
        parsed_summary=parsed,
        created_at=datetime.now(UTC),
    )
    observation.model = settings.gemini_model
    observation.input_context = context
    observation.raw_response = raw
    observation.parsed_summary = parsed
    observation.latency_ms = int((time.perf_counter() - started) * 1000)
    observation.error = error
    session.add(observation)
    session.commit()
    session.refresh(observation)
    return observation


__all__ = ["observe_latest"]
