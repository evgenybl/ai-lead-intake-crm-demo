"""The draft step (section 3.4), phase 1: the `template` backend only.

`template` is a stand-in, not a model: the first sentence of the message as the summary, intent
"other", no urgency and no reply draft. It is labelled "stand-in, no model" wherever it is shown.
The draft never enters a plan: the CRM path does not depend on it.
"""
from __future__ import annotations

import re

SUMMARY_MAX_WORDS = 40
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def first_sentence(text: str, max_words: int = SUMMARY_MAX_WORDS) -> str:
    flat = " ".join(text.split())
    end = _SENTENCE_END.search(flat)
    sentence = flat[: end.end()] if end else flat
    words = sentence.split()
    if len(words) > max_words:
        sentence = " ".join(words[:max_words]) + " ..."
    return sentence


class TemplateBackend:
    name = "template"
    label = "stand-in, no model"

    def draft(self, message: str) -> dict:
        return {
            "backend": self.name,
            "label": self.label,
            "summary": first_sentence(message),
            "intent": "other",
            "urgency": None,
            "reply_draft": None,
        }

    def check(self) -> tuple:
        return "connected", "a stand-in that runs locally, no model"
