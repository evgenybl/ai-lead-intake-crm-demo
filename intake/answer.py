"""Section 3.4: reading a model's answer strictly, and the checks it must pass to be used.

The answer is one JSON object with exactly the keys summary, intent, urgency and reply_draft. A key
beyond the four refuses the whole answer: nothing of it is used. The object may stand alone or inside one
```json fence; any other text around it refuses it.

The checks:
- the shape and the limits: a summary of 1 to 40 words; an intent and an urgency from the fixed lists;
  a reply draft of at most 120 words, or null;
- every number written in digits in the summary and the reply appears in the message the model saw
  (1,200 and 1200 are one number; 02 and 2 are one number);
- the reply holds the placeholder {{name}}, and no e-mail address, phone number or web address;
- neither text holds the sender's own name, e-mail or phone.
An answer that fails any check is not used. The reasons say which check, never a personal value.
"""
from __future__ import annotations

import json
import re

from . import normalise
from .request import EMAIL_SHAPE, INTENTS, PLACEHOLDER, URGENCIES, name_words, phone_spans

KEYS = ("summary", "intent", "urgency", "reply_draft")
SUMMARY_MAX_WORDS = 40
REPLY_MAX_WORDS = 120

_FENCE = re.compile(r"^```(?:json)?[ \t]*\n(.*)\n```$", re.DOTALL)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WEB = re.compile(
    r"https?://|www\.|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|info|biz|app|dev|uk|us|de|fr|il|eu)\b",
    re.IGNORECASE,
)


class _Duplicate(ValueError):
    pass


def _no_duplicates(pairs):
    seen = {}
    for name, value in pairs:
        if name in seen:
            raise _Duplicate(name)
        seen[name] = value
    return seen


def parse(text: str) -> tuple:
    """(the object, None) or (None, why it is not one JSON object)."""
    body = (text or "").strip()
    fenced = _FENCE.match(body)
    if fenced:
        body = fenced.group(1).strip()
    try:
        obj = json.loads(body, object_pairs_hook=_no_duplicates)
    except _Duplicate as dup:
        return None, f"the key {str(dup)!r} appears twice"
    except ValueError:
        return None, "the answer is not one JSON object"
    if not isinstance(obj, dict):
        return None, "the answer is not one JSON object"
    return obj, None


def _numbers(text: str) -> set:
    found = set()
    for raw in _NUMBER.findall(text or ""):
        value = raw.replace(",", "")
        whole, dot, fraction = value.partition(".")
        whole = whole.lstrip("0") or "0"
        found.add(whole + (dot + fraction.rstrip("0") if fraction.rstrip("0") else ""))
    return found


def _words(text: str) -> int:
    return len(text.split())


def check(obj: dict, seen_message: str, fields: dict) -> list:
    """Every reason the answer may not be used. Empty means it passes."""
    keys = set(obj)
    extra = sorted(keys - set(KEYS))
    if extra:
        return [f"the answer has keys beyond the four: {', '.join(extra)}; it is refused whole"]
    missing = [k for k in KEYS if k not in keys]
    if missing:
        return [f"the answer lacks {', '.join(missing)}"]
    reasons = []
    summary, intent, urgency, reply = (obj[k] for k in KEYS)
    if not isinstance(summary, str) or not summary.strip():
        reasons.append("the summary is not text")
        summary = ""
    elif _words(summary) > SUMMARY_MAX_WORDS:
        reasons.append(f"the summary has more than {SUMMARY_MAX_WORDS} words")
    if intent not in INTENTS:
        reasons.append("the intent is not one of the fixed list")
    if urgency not in URGENCIES:
        reasons.append("the urgency is not low, normal or high")
    if reply is not None and not isinstance(reply, str):
        reasons.append("the reply draft is not text")
        reply = None
    texts = [summary] + ([reply] if reply else [])
    if reply is not None:
        if _words(reply) > REPLY_MAX_WORDS:
            reasons.append(f"the reply draft has more than {REPLY_MAX_WORDS} words")
        if PLACEHOLDER not in reply:
            reasons.append("the reply draft does not hold the placeholder " + PLACEHOLDER)
        if EMAIL_SHAPE.search(reply):
            reasons.append("the reply draft holds an e-mail address")
        if phone_spans(reply):
            reasons.append("the reply draft holds a phone number")
        if _WEB.search(reply):
            reasons.append("the reply draft holds a web address")
    allowed = _numbers(seen_message)
    invented = sorted(set().union(*(_numbers(t) for t in texts)) - allowed)
    if invented:
        reasons.append(f"a number that is not in the message: {', '.join(invented)}")
    own_email = normalise.email(fields.get("email", ""))
    own_digits = normalise.phone_digits(fields.get("phone", ""))
    for label, text in (("summary", summary), ("reply draft", reply or "")):
        if own_email and own_email in text.casefold():
            reasons.append(f"the {label} holds the sender's e-mail")
        if own_digits and any(normalise.phone_digits(text[s:e]) == own_digits for s, e in phone_spans(text)):
            reasons.append(f"the {label} holds the sender's phone")
        if any(re.search(r"(?<!\w)" + re.escape(w) + r"(?!\w)", text, re.IGNORECASE)
               for w in name_words(fields.get("name", ""))):
            reasons.append(f"the {label} holds the sender's name")
    return reasons
