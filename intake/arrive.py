"""Reading what arrives. One JSON object per HTTP body, or per line of a .jsonl file.

What is not one JSON object never becomes an enquiry: the HTTP answer is 400, and `desk.py ingest`
names the line and takes the others. A key that appears twice in one object, NaN or Infinity, and text
that UTF-8 cannot carry are refused the same way, because the desk could not store them faithfully.
"""
from __future__ import annotations

import json

from .canonical import canonical_bytes


class _Duplicate(ValueError):
    pass


def _no_duplicates(pairs):
    seen = {}
    for name, value in pairs:
        if name in seen:
            raise _Duplicate(name)
        seen[name] = value
    return seen


def _no_constants(name):
    raise ValueError(f"{name} is not allowed")


def parse_object(raw: bytes) -> tuple:
    """(the object, None) or (None, why it is not one JSON object)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "not UTF-8 text"
    if text.startswith("\ufeff"):  # a byte-order mark
        text = text[1:]
    try:
        obj = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except _Duplicate as dup:
        return None, f"the key {str(dup)!r} appears twice"
    except json.JSONDecodeError as error:
        return None, f"not JSON ({error.msg} at column {error.colno})"
    except (ValueError, RecursionError) as error:
        return None, f"not JSON ({error})"
    if not isinstance(obj, dict):
        return None, "not one JSON object"
    try:
        canonical_bytes(obj)
    except (UnicodeEncodeError, ValueError):
        return None, "holds text that cannot be stored as UTF-8"
    return obj, None


def read_lines(raw: bytes):
    """(line number, the object or None, why not) for every line that is not blank."""
    for number, line in enumerate(raw.split(b"\n"), start=1):
        line = line.rstrip(b"\r")
        if not line.strip():
            continue
        obj, problem = parse_object(line)
        yield number, obj, problem
