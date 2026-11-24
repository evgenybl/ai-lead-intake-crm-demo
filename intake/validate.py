"""What is refused, and why.

A refused enquiry is a state, not an error: it is kept with every reason that applies, counted in the
report and shown in its trail. It never reaches the model or the CRM.
"""
from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from dataclasses import dataclass

from . import normalise

FIELDS = ("source", "source_id", "received_at", "name", "email", "phone", "company", "message", "consent", "website")
TEXT_FIELDS = ("source", "source_id", "received_at", "name", "email", "phone", "company", "message")
MAX_MESSAGE_CHARS = 5000
# A value that starts so could be read as a formula by a spreadsheet that opens an export of the CRM or the trail.
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
# The four contact fields the desk writes to the CRM. A phone may start with a + that a digit follows (+44 ...);
# any other character may stand in a phone, except a control character: only its digits are compared.
SPREADSHEET_FIELDS = ("name", "company", "email", "phone")

REASONS = {
    "no_name": "no name",
    "no_message": "no message",
    "no_way_to_answer": "neither e-mail nor phone",
    "email_invalid": "an e-mail that is not an address",
    "phone_invalid": "a phone that is not a phone number (7 to 15 digits)",
    "no_consent": "consent is not true",
    "honeypot_filled": "the honeypot field is filled",
    "message_too_long": "a message above 5,000 characters",
    "received_at_invalid": "received_at is not a date and time",
    "not_text": "a field that must be text is not text",
    "formula_start": "a field that starts like a spreadsheet formula (=, +, -, @, a tab or a carriage return; a phone "
                     "may start with + and a digit)",
    "control_characters": "a field that holds control characters",
}

_STAMP = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(Z|[+-]\d{2}:\d{2})?$"
)


@dataclass(frozen=True)
class Extracted:
    fields: dict  # every field of FIELDS; text fields as text ("" when absent)
    problems: tuple  # "not_text:<field>" for a text field that was another JSON type
    ignored: tuple  # names of keys that are not fields; their values are not kept
    received_at_filled: bool  # True when received_at was absent and the arrival time was used


def extract(obj: dict, arrived_at: str) -> Extracted:
    """Take the known fields of one JSON object. Never raises for any JSON object."""
    fields: dict = {}
    problems = []
    for name in TEXT_FIELDS:
        value = obj.get(name)
        if value is None:
            fields[name] = ""
        elif isinstance(value, str):
            fields[name] = value
        else:
            fields[name] = ""
            problems.append(f"not_text:{name}")
    fields["consent"] = obj.get("consent")
    fields["website"] = obj.get("website")
    filled = not fields["received_at"].strip() and "not_text:received_at" not in problems
    if filled:
        fields["received_at"] = arrived_at
    ignored = tuple(sorted(k for k in obj if k not in FIELDS))
    return Extracted(fields, tuple(problems), ignored, filled)


def parse_time(text) -> _dt.datetime | None:
    """An ISO-8601 date and time, read the same way on every Python version; UTC when no offset is given."""
    if not isinstance(text, str):
        return None
    m = _STAMP.match(text.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, fraction, zone = m.groups()
    if zone in (None, "Z"):
        tz = _dt.timezone.utc
    else:
        sign = 1 if zone[0] == "+" else -1
        offset = _dt.timedelta(hours=int(zone[1:3]), minutes=int(zone[4:6]))
        if offset >= _dt.timedelta(hours=24):
            return None
        tz = _dt.timezone(sign * offset)
    try:
        value = _dt.datetime(
            int(year), int(month), int(day), int(hour), int(minute), int(second or 0),
            int((fraction or "0").ljust(6, "0")), tzinfo=tz,
        )
    except ValueError:
        return None
    return value.astimezone(_dt.timezone.utc)


def honeypot_filled(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True  # a number, a list, true: a person's browser sends none of these


def starts_like_formula(value: str) -> bool:
    """True when the value, as sent or with its outer spaces removed, starts with a formula sign."""
    return bool(value) and (value[:1] in FORMULA_STARTS or value.strip()[:1] in FORMULA_STARTS)


def phone_starts_like_formula(value: str) -> bool:
    """As starts_like_formula, but a + that a digit follows is how a phone number with a country code starts."""
    for text in (value, value.strip()):
        if text[:1] == "+" and text[1:2] in tuple("0123456789"):
            continue
        if text[:1] in FORMULA_STARTS:
            return True
    return False


def has_control_characters(value: str) -> bool:
    """True for a C0 or C1 control character, or a character that reorders or hides text (bidi, zero width)."""
    return any(unicodedata.category(c) in ("Cc", "Cf") for c in value)


def reasons(extracted: Extracted) -> list:
    """Every reason that applies, in a fixed order. An empty list means the enquiry is taken."""
    f = extracted.fields
    found = []
    message = f["message"].strip()
    if not f["name"].strip():
        found.append("no_name")
    if not message:
        found.append("no_message")
    email = normalise.email(f["email"])
    digits = normalise.phone_digits(f["phone"])
    if not email and not digits:
        found.append("no_way_to_answer")
    if email and not normalise.is_email(email):
        found.append("email_invalid")
    if digits and not normalise.is_phone(digits):
        found.append("phone_invalid")
    if f["consent"] is not True:
        found.append("no_consent")
    if honeypot_filled(f["website"]):
        found.append("honeypot_filled")
    if len(message) > MAX_MESSAGE_CHARS:
        found.append("message_too_long")
    if f["received_at"].strip() and parse_time(f["received_at"]) is None:
        found.append("received_at_invalid")
    for name in SPREADSHEET_FIELDS:
        if (phone_starts_like_formula if name == "phone" else starts_like_formula)(f[name]):
            found.append(f"formula_start:{name}")
        if has_control_characters(f[name]):
            found.append(f"control_characters:{name}")
    found.extend(extracted.problems)
    return found


def describe(code: str) -> str:
    base, _, field = code.partition(":")
    text = REASONS.get(base, base)
    return f"{text}: {field}" if field else text
