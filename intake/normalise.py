"""What "normalised" means for an e-mail, a phone, a name and a message. docs/DESIGN.md states the same.

- e-mail: trimmed and lowercased; it is an address when it has one "@", no spaces, and a dot after the "@".
- phone: its ASCII digits only; it is a phone number when it has 7 to 15 of them. No country codes are known.
- name (and company): NFKC, case-folded, every run of characters that are not letters or digits made one
  space, trimmed. "Ada  O'Neil" and "ada o neil" are the same; "Zoe" and "Zoë" are not.
- message (for the hashed key only): every run of whitespace made one space, trimmed.
"""
from __future__ import annotations

import re
import unicodedata

PHONE_MIN_DIGITS = 7
PHONE_MAX_DIGITS = 15
EMAIL_MAX_CHARS = 254

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_WORD = re.compile(r"[^\W_]+")
_DIGITS = frozenset("0123456789")


def email(raw) -> str:
    return raw.strip().lower() if isinstance(raw, str) else ""


def is_email(normalised: str) -> bool:
    return len(normalised) <= EMAIL_MAX_CHARS and bool(_EMAIL.match(normalised))


def phone_digits(raw) -> str:
    if not isinstance(raw, str):
        return ""
    return "".join(ch for ch in raw if ch in _DIGITS)


def is_phone(digits: str) -> bool:
    return PHONE_MIN_DIGITS <= len(digits) <= PHONE_MAX_DIGITS


def name(raw) -> str:
    if not isinstance(raw, str):
        return ""
    folded = unicodedata.normalize("NFKC", raw).casefold()
    return " ".join(_WORD.findall(folded))


def message(raw) -> str:
    return " ".join(raw.split()) if isinstance(raw, str) else ""
