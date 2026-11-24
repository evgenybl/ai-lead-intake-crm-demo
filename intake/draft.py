"""The draft step (section 3.4): a summary, an intent, an urgency and a reply draft, used only if they pass.

The CRM path never depends on this step: its result is shown on the desk and is never part of a plan.

Three backends, each with a label it earns from what actually happened:
- `recorded`: an answer found by the request's sha256 under recorded/. Labelled "Recorded" only when the
  answer sits in recorded/answers/, its meta file says it was recorded, and the request file and the
  answer file both match the sha256s the meta file names. An answer in recorded/constructed/ is labelled
  "Constructed answer, not from a model". No answer: no AI text, "no recording for this request".
- `template`: a stand-in that is not a model: the first sentence of the message, intent "other", no
  urgency, no reply draft. Labelled "stand-in, no model".
- `anthropic`: the Messages API, the key from ANTHROPIC_API_KEY. Labelled "Live path", or "Live path
  (custom endpoint: <host>)" when ANTHROPIC_BASE_URL is not the default. One retry carrying the reasons is
  allowed when an answer fails the checks. In this repository it has been run against a local mock only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from . import answer, request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RECORDED_DIR = os.path.join(ROOT, "recorded")
SOURCES = {"answers": "recorded", "constructed": "constructed"}  # folder -> the origin its answers must state
LABELS = {
    "recorded": "Recorded",
    "constructed": "Constructed answer, not from a model",
    "template": "stand-in, no model",
}
SUMMARY_MAX_WORDS = 40
DRAFT_ENV = "INTAKE_DRAFT"
BACKENDS = ("recorded", "template", "anthropic")

KEY_ENV = "ANTHROPIC_API_KEY"
BASE_ENV = "ANTHROPIC_BASE_URL"
MODEL_ENV = "INTAKE_MODEL"
DEFAULT_BASE = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-opus-5-5"
API_VERSION = "2026-10-21"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000

_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


class DraftConfigError(Exception):
    """A backend that cannot be used as configured; the message says what to set."""


def first_sentence(text: str, max_words: int = SUMMARY_MAX_WORDS) -> str:
    flat = " ".join(text.split())
    end = _SENTENCE_END.search(flat)
    sentence = flat[: end.end()] if end else flat
    words = sentence.split()
    if len(words) > max_words:
        sentence = " ".join(words[:max_words]) + " ..."
    return sentence


def _result(backend: str, label: str, outcome: str, reason: str = "", request_sha: str = "", values=None,
            **extra) -> dict:
    """The draft as the desk keeps it. outcome: used | refused | none | not sent | stand-in."""
    values = values or {}
    record = {"backend": backend, "label": label, "outcome": outcome, "reason": reason, "request": request_sha}
    for key in answer.KEYS:
        record[key] = values.get(key) if outcome in ("used", "stand-in") else None
    record.update(extra)
    return record


def _evaluate(text: str, req, fields: dict) -> tuple:
    obj, problem = answer.parse(text)
    if problem:
        return None, [problem]
    reasons = answer.check(obj, req.message, fields)
    return (obj, []) if not reasons else (None, reasons)


class TemplateBackend:
    name = "template"
    label = LABELS["template"]

    def draft(self, fields: dict) -> dict:
        return _result(self.name, self.label, "stand-in", values={
            "summary": first_sentence(fields.get("message", "")), "intent": "other",
            "urgency": None, "reply_draft": None})

    def check(self) -> tuple:
        return "connected", "a stand-in that runs locally, no model"


class RecordedBackend:
    name = "recorded"

    def __init__(self, root: str = RECORDED_DIR, sources: dict | None = None):
        self.root = root
        self.sources = dict(SOURCES if sources is None else sources)

    def find(self, req) -> tuple:
        """(label, answer text) of the first source that holds a matching answer, or (None, why not)."""
        for folder, origin in self.sources.items():
            meta_path = os.path.join(self.root, folder, req.sha256 + ".json")
            if not os.path.exists(meta_path):
                continue
            try:
                with open(meta_path, encoding="utf-8") as handle:
                    meta = json.load(handle)
                with open(os.path.join(self.root, "requests", req.sha256 + ".txt"), "rb") as handle:
                    shipped_request = handle.read()
                with open(os.path.join(self.root, folder, meta["answer_file"]), "rb") as handle:
                    raw = handle.read()
                text = raw.decode("utf-8")
            except (OSError, KeyError, TypeError, ValueError):
                return None, f"the recording in {folder}/ cannot be read whole"
            matches = (
                meta.get("origin") == origin
                and meta.get("request_sha256") == req.sha256
                and shipped_request == req.text.encode("utf-8")
                and meta.get("answer_sha256") == hashlib.sha256(raw).hexdigest()
            )
            if not matches:
                return None, f"the recording in {folder}/ does not match its request or its answer file"
            return LABELS[origin], text
        return None, "no recording for this request"

    def draft(self, fields: dict) -> dict:
        req = request.build(fields)
        held = request.guard(req.text, fields)
        if held:
            return _result(self.name, "not sent", "not sent", "the request still held " + ", ".join(held), req.sha256)
        label, found = self.find(req)
        if label is None:
            return _result(self.name, "no recording", "none", found, req.sha256)
        obj, reasons = _evaluate(found, req, fields)
        if reasons:
            return _result(self.name, label, "refused", "; ".join(reasons), req.sha256)
        return _result(self.name, label, "used", request_sha=req.sha256, values=obj)

    def counts(self) -> dict:
        found = {}
        for folder in ("requests", *self.sources):
            path = os.path.join(self.root, folder)
            suffix = ".txt" if folder == "requests" else ".json"
            found[folder] = len([n for n in os.listdir(path) if n.endswith(suffix)]) if os.path.isdir(path) else 0
        return found

    def check(self) -> tuple:
        counts = self.counts()
        return "connected", (f"{counts['requests']} requests, {counts.get('answers', 0)} recorded answers, "
                             f"{counts.get('constructed', 0)} constructed answers")


class AnthropicBackend:
    """The live path. Only ever called when a person chose it; in this repository, only against a local mock."""

    name = "anthropic"

    def __init__(self, key: str, base_url: str = DEFAULT_BASE, model: str = DEFAULT_MODEL, timeout: float = 120.0):
        parts = urllib.parse.urlsplit(base_url or "")
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise DraftConfigError(f"{BASE_ENV} must be an http or https address")
        if not key:
            raise DraftConfigError(f"set {KEY_ENV} to use the anthropic backend")
        self._key = key
        self._url = base_url.rstrip("/") + "/v1/messages"
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        self.host = parts.netloc
        default = urllib.parse.urlsplit(DEFAULT_BASE)
        is_default = parts.scheme == default.scheme and parts.netloc == default.netloc
        self.label = "Live path" if is_default else f"Live path (custom endpoint: {parts.netloc})"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def _call(self, messages: list) -> tuple:
        body = {"model": self.model, "max_tokens": MAX_TOKENS, "messages": messages, "fallbacks": "default"}
        headers = {"content-type": "application/json", "x-api-key": self._key,
                   "anthropic-version": API_VERSION, "anthropic-beta": FALLBACK_BETA}
        req = urllib.request.Request(self._url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                reply = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            error.close()
            raise _CallError(f"HTTP {error.code}") from None
        except (OSError, ValueError) as error:
            raise _CallError(f"no usable answer ({type(error).__name__})") from None
        content = reply.get("content") if isinstance(reply, dict) else None
        if not isinstance(content, list):
            raise _CallError("the answer has no content list")
        text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        named = reply.get("model")  # kept only when it is a plain model name, never any text the answer carries
        model = named if isinstance(named, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", named) else self.model
        return content, text, reply.get("stop_reason"), model

    def draft(self, fields: dict) -> dict:
        req = request.build(fields)
        held = request.guard(req.text, fields)
        if held:
            return _result(self.name, "not sent", "not sent", "the request still held " + ", ".join(held), req.sha256)
        messages = [{"role": "user", "content": req.text}]
        try:
            content, text, stop, model = self._call(messages)
            if stop == "refusal":
                return _result(self.name, self.label, "none", "the model declined to answer", req.sha256, model=model)
            obj, reasons = _evaluate(text, req, fields)
            if reasons:  # one retry, carrying the reasons (which never hold a personal value)
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "That answer cannot be used: " + "; ".join(reasons)
                     + ". Answer again with one JSON object as described, and nothing else."},
                ]
                content, text, stop, model = self._call(messages)
                if stop == "refusal":
                    return _result(self.name, self.label, "none", "the model declined to answer", req.sha256, model=model)
                obj, again = _evaluate(text, req, fields)
                if again:
                    return _result(self.name, self.label, "refused", "after one retry: " + "; ".join(again),
                                   req.sha256, model=model)
        except _CallError as error:
            return _result(self.name, self.label, "none", f"the model could not be reached: {error}", req.sha256)
        return _result(self.name, self.label, "used", request_sha=req.sha256, values=obj, model=model)

    def check(self) -> tuple:
        return "connected", f"configured: {self.model} at {self.host}; status makes no call, which would be billed"


class _CallError(Exception):
    pass


def make_backend(choice: str | None, environ):
    chosen = choice or environ.get(DRAFT_ENV) or "recorded"
    if chosen == "recorded":
        return RecordedBackend()
    if chosen == "template":
        return TemplateBackend()
    if chosen == "anthropic":
        return AnthropicBackend(environ.get(KEY_ENV, ""), environ.get(BASE_ENV) or DEFAULT_BASE,
                                environ.get(MODEL_ENV) or DEFAULT_MODEL)
    raise DraftConfigError(f"{DRAFT_ENV} must be one of {', '.join(BACKENDS)}, not {chosen!r}")
