"""Canonical hashing helpers.

Everything that must be *reproducible forever* (config hashes, dataset hashes,
deterministic ids) goes through this module so there is exactly one definition
of "canonical form".

Canonical JSON rules: sorted keys, no insignificant whitespace, UTF-8, no NaN.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

#: Separator used when hashing ordered, non-JSON payloads (e.g. id tuples).
FIELD_SEP = "|"

#: Length of the truncated digest used inside human-readable ids.
ID_DIGEST_LEN = 32


def canonicalize(obj: Any) -> Any:
    """Recursively convert a value into a canonical, JSON-safe form.

    Decimals become their normalized string form (``"1.5"``, not ``1.500``);
    enums become their values; mappings are converted to dicts (``sort_keys``
    is applied later by :func:`stable_json`).
    """
    if isinstance(obj, Decimal):
        return _decimal_repr(obj)
    if isinstance(obj, Mapping):
        return {str(k): canonicalize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [canonicalize(v) for v in obj]
    if isinstance(obj, set | frozenset):
        return [canonicalize(v) for v in sorted(obj, key=str)]
    if hasattr(obj, "value") and isinstance(obj.value, str):
        # StrEnum / Enum members
        return canonicalize(obj.value)
    return obj


def _decimal_repr(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    return "0" if text in {"-0", ""} else text


def canonical_decimal(value: Decimal | int | float | str) -> str:
    """Canonical decimal *string*: ``"1.50"`` -> ``"1.5"``, ``"200"`` -> ``"200"``.

    Used wherever a Decimal is persisted as text for hashing, so that equal
    values hash equally no matter how they were typed or which precision the
    producer used.
    """
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return _decimal_repr(decimal_value)


def stable_json(obj: Any) -> str:
    """Canonical JSON text: sorted keys, no whitespace, UTF-8, no NaN."""
    return json.dumps(
        canonicalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(payload: str) -> str:
    """Full 64-char SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def config_hash(config: Mapping[str, Any] | Any) -> str:
    """Deterministic sha256 over any config-like object."""
    if not isinstance(config, Mapping) and hasattr(config, "as_dict"):
        config = config.as_dict()  # type: ignore[union-attr]
    return sha256_hex(stable_json(config))


def digest_of_parts(parts: Iterable[Any], *, prefix: str = "") -> str:
    """Digest an ordered sequence of values joined by ``FIELD_SEP``."""
    joined = FIELD_SEP.join(stable_json(p) if not isinstance(p, str) else p for p in parts)
    return sha256_hex(prefix + joined)


def short_digest(payload: str) -> str:
    """Truncated digest used in ids (32 hex chars = 128 bits, collision-safe)."""
    return sha256_hex(payload)[:ID_DIGEST_LEN]


def dataset_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    """Order-sensitive hash of a dataset (candles, trades, ...).

    Used by the ``data_sources`` table so a backtest can prove which bytes it
    ran on. Rows are hashed in the order given: ordering is part of identity.
    """
    hasher = hashlib.sha256()
    count = 0
    for row in rows:
        hasher.update(stable_json(row).encode("utf-8"))
        hasher.update(b"\n")
        count += 1
    hasher.update(f"rows={count}".encode())
    return hasher.hexdigest()


__all__ = [
    "FIELD_SEP",
    "canonical_decimal",
    "ID_DIGEST_LEN",
    "canonicalize",
    "config_hash",
    "dataset_hash",
    "digest_of_parts",
    "sha256_hex",
    "short_digest",
    "stable_json",
]
