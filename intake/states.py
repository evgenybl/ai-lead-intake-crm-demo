"""The named states of an enquiry and the only moves between them (docs/DESIGN.md has the same table).

Every enquiry is in exactly one of STATES. A move is an (event, target) from the current state; any
other move raises TransitionError, and every move that happens is written to the audit trail with the
state it ends in.
"""
from __future__ import annotations

STATES = {
    "received": "taken; identity not checked yet, because the CRM could not be read",
    "refused": "failed validation; kept with its reasons; never reaches the model or the CRM",
    "held_two_contacts": "the e-mail and the phone point to different contacts; waits for a person",
    "held_possible_duplicate": "no exact key matches, but a contact has the same name and company; waits for a person",
    "awaiting_approval": "a plan is ready and waits for a person's approval",
    "approved": "a person approved exactly this plan; not applied yet",
    "rejected": "a person rejected the plan, with a reason; nothing is written",
    "applied": "every operation of the approved plan is in the CRM",
    "apply_failed": "a CRM call failed; the error is named; a retry completes what is missing",
}

_PLANNED = frozenset({"awaiting_approval", "held_two_contacts", "held_possible_duplicate"})

# (current state, event) -> the states that event may lead to. None is "not on the desk yet".
TRANSITIONS = {
    (None, "arrived"): frozenset({"refused", "received"}),
    ("received", "assessed"): _PLANNED,
    ("held_two_contacts", "resolved"): _PLANNED,
    ("held_possible_duplicate", "resolved"): _PLANNED,
    ("awaiting_approval", "replanned"): _PLANNED,
    ("awaiting_approval", "approved"): frozenset({"approved"}),
    ("awaiting_approval", "rejected"): frozenset({"rejected"}),
    ("approved", "withdrawn"): frozenset({"awaiting_approval"}),
    ("approved", "replanned"): _PLANNED,
    ("approved", "applied"): frozenset({"applied"}),
    ("approved", "apply_failed"): frozenset({"apply_failed"}),
    ("apply_failed", "replanned"): _PLANNED,
    ("apply_failed", "applied"): frozenset({"applied"}),
    ("apply_failed", "apply_failed"): frozenset({"apply_failed"}),
    ("apply_failed", "withdrawn"): frozenset({"awaiting_approval"}),
}

WAITING_FOR_A_PERSON = ("held_two_contacts", "held_possible_duplicate", "awaiting_approval")
FINAL = ("refused", "rejected", "applied")


class TransitionError(Exception):
    """A move the state table does not allow."""


def check(current: str | None, event: str, target: str) -> None:
    allowed = TRANSITIONS.get((current, event))
    if allowed is None:
        raise TransitionError(f"no event {event!r} from the state {current!r}")
    if target not in allowed:
        raise TransitionError(f"the event {event!r} from {current!r} cannot lead to {target!r}")
