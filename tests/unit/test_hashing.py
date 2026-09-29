"""Canonical hashing: ordering, decimal normalisation, dataset identity."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.common.enums import Timeframe
from app.common.hashing import (
    canonicalize,
    config_hash,
    dataset_hash,
    digest_of_parts,
    sha256_hex,
    stable_json,
)


def test_sha256_known_vector() -> None:
    # Standard test vector for SHA-256("abc").
    assert sha256_hex("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_stable_json_sorts_keys_and_strips_whitespace() -> None:
    assert stable_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert " " not in stable_json({"a": [1, 2, 3]})


def test_stable_json_is_utf8_not_ascii_escaped() -> None:
    assert "المحفظة" in stable_json({"note": "المحفظة"})


def test_key_order_does_not_affect_hash() -> None:
    assert config_hash({"a": 1, "b": 2}) == config_hash({"b": 2, "a": 1})


def test_decimal_canonicalisation() -> None:
    assert canonicalize(Decimal("1.500")) == "1.5"
    assert canonicalize(Decimal("0.1000")) == "0.1"
    assert canonicalize(Decimal("10")) == "10"
    assert config_hash({"x": Decimal("1.50")}) == config_hash({"x": Decimal("1.5")})


def test_enum_values_are_canonicalised() -> None:
    assert config_hash({"tf": Timeframe.H4}) == config_hash({"tf": "4h"})


def test_nan_is_rejected() -> None:
    with pytest.raises(ValueError):
        stable_json({"x": float("nan")})


def test_digest_of_parts_is_order_sensitive() -> None:
    assert digest_of_parts(["a", "b"]) != digest_of_parts(["b", "a"])
    assert digest_of_parts(["a", "b"]) == digest_of_parts(["a", "b"])


def test_dataset_hash_is_order_sensitive_and_stable() -> None:
    rows = [
        {"open_time": "2026-01-01T00:00:00Z", "close": "2500.10"},
        {"open_time": "2026-01-01T04:00:00Z", "close": "2511.00"},
    ]
    assert dataset_hash(rows) == dataset_hash(list(rows))
    assert dataset_hash(rows) != dataset_hash(list(reversed(rows)))


def test_dataset_hash_detects_a_single_changed_value() -> None:
    base = [{"open_time": "t1", "close": "2500.1"}, {"open_time": "t2", "close": "2501.1"}]
    tampered = [{"open_time": "t1", "close": "2500.1"}, {"open_time": "t2", "close": "2501.2"}]
    assert dataset_hash(base) != dataset_hash(tampered)


def test_dataset_hash_of_empty_dataset_is_stable() -> None:
    assert dataset_hash([]) == dataset_hash([])
