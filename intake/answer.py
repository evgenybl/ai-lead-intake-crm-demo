"""Reading a model's answer strictly, and the checks it must pass to be used.

The answer is one JSON object with exactly the keys summary, intent, urgency and reply_draft. A key
beyond the four refuses the whole answer: nothing of it is used. The object may stand alone or inside one
```json fence; any other text around it refuses it.

The checks:
- the shape and the limits: a summary of 1 to 40 words and at most 400 characters; an intent and an
  urgency from the fixed lists; a reply draft of at most 120 words and 1,200 characters, or null;
- no control characters, and no characters that reorder or hide text (bidi controls, zero width), in any
  of the four; the reply alone may hold line breaks (LF);
- every number written in digits in the summary and the reply appears in the message the model saw
  (1,200 and 1200 are one number; 02 and 2 are one number);
- the reply holds the placeholder {{name}}, and no e-mail address, phone number or web address, with or
  without its scheme (evil.example/pay counts, and so does javascript:);
- neither text holds the sender's own name, e-mail or phone.
An answer that fails any check is not used. The reasons say which check, never a personal value; where a
reason names something from the answer (a key, a number), it is quoted and cut to a fixed length.
"""
from __future__ import annotations

import json
import re
import unicodedata

from . import normalise
from .request import EMAIL_SHAPE, INTENTS, PLACEHOLDER, URGENCIES, name_spans, own_phone_spans, phone_spans

KEYS = ("summary", "intent", "urgency", "reply_draft")
SUMMARY_MAX_WORDS = 40
REPLY_MAX_WORDS = 120
SUMMARY_MAX_CHARS = 400
REPLY_MAX_CHARS = 1200
SHOWN_MAX_CHARS = 40  # how much of a key or a number from the answer a reason may quote

_FENCE = re.compile(r"^```(?:json)?[ \t]*\n(.*)\n```$", re.DOTALL)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WEB = re.compile(
    r"[a-z][a-z0-9+.-]*://|www\.|\b(?:javascript|vbscript|data|file|mailto|tel|sms):"
    r"|(?<![\w@.-])(?:[\w-]+\.)+[^\W\d_]{2,63}(?![\w@-])",  # a domain with no scheme: evil.example, bit.ly/x
    re.IGNORECASE,
)
_HIDDEN = ("Cc", "Cf", "Co", "Cs", "Zl", "Zp")  # control, format (bidi, zero width), private, surrogate, separators


def shown(value) -> str:
    """Something from the answer, as a reason may name it: quoted, escaped, cut to a fixed length."""
    text = str(value)
    return repr(text[:SHOWN_MAX_CHARS]) + ("..." if len(text) > SHOWN_MAX_CHARS else "")


def hidden_characters(text: str, allow: str = "") -> bool:
    return any(unicodedata.category(c) in _HIDDEN and c not in allow for c in text)


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
        return None, f"the key {shown(dup)} appears twice"
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
        more = f" and {len(extra) - 3} more" if len(extra) > 3 else ""
        return [f"the answer has keys beyond the four: {', '.join(shown(k) for k in extra[:3])}{more}; "
                "it is refused whole"]
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
    elif len(summary) > SUMMARY_MAX_CHARS:
        reasons.append(f"the summary has more than {SUMMARY_MAX_CHARS} characters")
    if summary and hidden_characters(summary):
        reasons.append("the summary holds control characters")
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
        elif len(reply) > REPLY_MAX_CHARS:
            reasons.append(f"the reply draft has more than {REPLY_MAX_CHARS} characters")
        if hidden_characters(reply, allow="\n"):
            reasons.append("the reply draft holds control characters other than a line break")
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
        more = f" and {len(invented) - 5} more" if len(invented) > 5 else ""
        reasons.append(f"a number that is not in the message: {', '.join(shown(n) for n in invented[:5])}{more}")
    own_email = normalise.email(fields.get("email", ""))
    for label, text in (("summary", summary), ("reply draft", reply or "")):
        if own_email and own_email in text.casefold():
            reasons.append(f"the {label} holds the sender's e-mail")
        if own_phone_spans(text, fields.get("phone", "")):
            reasons.append(f"the {label} holds the sender's phone")
        if name_spans(text, fields.get("name", "")):
            reasons.append(f"the {label} holds the sender's name")
    return reasons
