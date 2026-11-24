"""Canonical JSON and its sha256: the identity of a plan, an audit row and a hashed idempotency key.

Canonical means: keys sorted, separators "," and ":" with no spaces, text as UTF-8 (not escaped),
and no NaN or Infinity. The same value always gives the same bytes, on every platform.
"""
from __future__ import annotations

import hashlib
import json


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_bytes(value) -> bytes:
    """Raises UnicodeEncodeError for text that UTF-8 cannot carry (a lone surrogate)."""
    return canonical_json(value).encode("utf-8")


def sha256_hex(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
