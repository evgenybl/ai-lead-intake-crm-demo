"""The flow of section 3: arrive, validate, identity, draft, plan; and a person's decisions on it.

Writes to the CRM are not here: they happen in intake/gate.py, and only there.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import normalise, planner, validate
from .canonical import canonical_json
from .crm.base import CRMError
from .draft import TemplateBackend
from .identity import idempotency_key

SYSTEM = "system"
MAX_NAME_CHARS = 80
MAX_REASON_CHARS = 500


class DeskError(Exception):
    """A person's command was refused; the message says why. Nothing was changed."""


@dataclass(frozen=True)
class Answer:
    """What an arrival is told: the enquiry's key and its state; for a replay, the original."""

    key: str
    state: str
    replay: bool
    first_arrived_at: str
    via: str
    reasons: tuple = ()
    same_content: bool | None = None

    def as_dict(self) -> dict:
        answer = {"key": self.key, "state": self.state, "replay": self.replay}
        if self.replay:
            answer["original"] = {"arrived_at": self.first_arrived_at, "via": self.via}
        return answer


def person(name: str) -> str:
    """A person's stated name, as the trail records it. It is what they typed, not a login."""
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise DeskError("a decision needs a person's name (--by)")
    if len(cleaned) > MAX_NAME_CHARS:
        raise DeskError(f"a name may have at most {MAX_NAME_CHARS} characters")
    if cleaned.casefold() == SYSTEM:
        raise DeskError(f"{SYSTEM!r} is the desk's own name in the trail; give a person's name")
    return cleaned


class Desk:
    def __init__(self, store, crm, clock, drafter=None):
        self.store = store
        self.crm = crm
        self.clock = clock
        self.drafter = drafter or TemplateBackend()

    # -- arrive -----------------------------------------------------------------------------------

    def receive(self, obj: dict, via: str) -> Answer:
        """One JSON object from HTTP or a file. A known key is a replay: counted, recorded, nothing queued."""
        extracted = validate.extract(obj, arrived_at=self.clock.now())
        fields = extracted.fields
        key = idempotency_key(fields)
        payload = canonical_json(fields)
        with self.store.transaction():
            existing = self.store.get_enquiry(key)
            if existing is not None:
                same = canonical_json(existing.payload) == payload
                self.store.add_replay(key, via, {"same_content": same, "state_now": existing.state})
                return Answer(key, existing.state, True, existing.first_arrived_at, existing.via,
                              tuple(existing.reasons), same)
            reasons = validate.reasons(extracted)
            state = "refused" if reasons else "received"
            detail = {"reasons": reasons}
            if extracted.ignored:
                detail["ignored_fields"] = list(extracted.ignored)
            if extracted.received_at_filled:
                detail["received_at_filled"] = True
            self.store.insert_enquiry(key, via, fields, state, reasons, detail)
            first = self.store.get_enquiry(key).first_arrived_at
        if state == "received":
            state = self.assess(key)
        return Answer(key, state, False, first, via, tuple(reasons))

    def assess(self, key: str) -> str:
        """Identity, draft and plan for an enquiry in `received`. A CRM that cannot be read leaves it there."""
        enquiry = self._get(key)
        if enquiry.state != "received":
            return enquiry.state
        try:
            assessment = planner.assess(key, enquiry.payload, enquiry.resolution, self.crm)
        except CRMError as error:
            with self.store.transaction():
                self.store.set_fields(key, error=f"identity: {error}")
                self.store.audit(key, "identity_failed", {"error": str(error), "state": "received"}, SYSTEM)
            return "received"
        draft = self.drafter.draft(enquiry.payload.get("message", ""))
        with self.store.transaction():
            self.store.set_fields(key, draft=draft, error=None)
            self.record_assessment(key, assessment, "assessed", SYSTEM, {"draft": draft["label"]})
        return assessment.state

    def record_assessment(self, key: str, assessment, event: str, actor: str, extra: dict | None = None) -> None:
        """Store an assessment and move to its state. A live approval of another plan is withdrawn."""
        with self.store.transaction():
            if assessment.plan is not None:
                self.store.save_plan(assessment.sha, key, canonical_json(assessment.plan))
            self.store.set_fields(key, identity=assessment.identity, plan_sha=assessment.sha)
            live = self.store.live_approval(key)
            if live is not None and live.plan_sha != assessment.sha:
                reason = "the plan changed: the CRM is not as it was when this plan was approved"
                self.store.close_approval(live.id, "withdrawn", SYSTEM, reason)
                self.store.audit(key, "approval_withdrawn", {"approval": live.id, "reason": reason}, SYSTEM)
            self.store.move(key, event, assessment.state, actor,
                            {**(extra or {}), "identity": assessment.identity["kind"], "plan": assessment.sha})

    def refresh(self, key: str) -> bool:
        """Plan an enquiry in `awaiting_approval` again against the CRM as it is now. True if the plan changed."""
        enquiry = self._get(key)
        if enquiry.state != "awaiting_approval":
            return False
        assessment = planner.assess(key, enquiry.payload, enquiry.resolution, self.crm)
        if assessment.sha == enquiry.plan_sha:
            return False
        self.record_assessment(key, assessment, "replanned", SYSTEM, {"why": "the CRM changed since the plan was made"})
        return True

    def prepare(self) -> list:
        """What `desk.py plan` does first: assess what could not be assessed, and re-plan what waits.

        Reads the CRM; writes nothing to it. Returns (key, what happened) for every change."""
        changes = []
        for enquiry in self.store.enquiries(in_states=("received",)):
            state = self.assess(enquiry.key)
            changes.append((enquiry.key, f"assessed: {state}" if state != "received" else "the CRM could not be read"))
        for enquiry in self.store.enquiries(in_states=("awaiting_approval",)):
            if self.refresh(enquiry.key):
                changes.append((enquiry.key, "planned again: the CRM changed"))
        return changes

    # -- a person's decisions -------------------------------------------------------------------------

    def resolve(self, key: str, as_: str, by: str) -> str:
        """Resolve a hold: `new`, or `contact:<id>` for one of the contacts identity found."""
        who = person(by)
        enquiry = self._get(key)
        if enquiry.state not in ("held_two_contacts", "held_possible_duplicate"):
            raise DeskError(f"{key} is not held; it is {enquiry.state}")
        found = enquiry.identity or {}
        candidates = sorted(set(found.get("by_email", []) + found.get("by_phone", []) + found.get("by_name_and_company", [])))
        if as_ == "new":
            email = normalise.email(enquiry.payload.get("email", ""))
            holders = self.crm.find_by_email(email) if email else []
            if holders:
                raise DeskError(
                    f"the e-mail belongs to contact {holders[0].id}; a second contact with it would be a duplicate. "
                    f"Resolve as contact:<id> instead (found: {', '.join(candidates)})"
                )
        elif as_.startswith("contact:"):
            contact_id = as_.split(":", 1)[1]
            if contact_id not in candidates:
                raise DeskError(f"contact {contact_id!r} is not one that identity found ({', '.join(candidates)})")
            if self.crm.get_contact(contact_id) is None:
                raise DeskError(f"contact {contact_id} is not in the CRM")
        else:
            raise DeskError("resolve --as takes `new` or `contact:<id>`")
        assessment = planner.assess(key, enquiry.payload, as_, self.crm)
        with self.store.transaction():
            self.store.set_fields(key, resolution=as_)
            self.store.audit(key, "resolution", {"as": as_, "candidates": candidates}, who)
            self.record_assessment(key, assessment, "resolved", who)
        return assessment.state

    def approve(self, key: str, by: str, plan_prefix: str | None = None) -> str:
        who = person(by)
        enquiry = self._get(key)
        if enquiry.state != "awaiting_approval" or not enquiry.plan_sha:
            raise DeskError(f"{key} has no plan waiting for approval; it is {enquiry.state}")
        if plan_prefix is not None:
            prefix = plan_prefix.strip().lower()
            if len(prefix) < 8 or not enquiry.plan_sha.startswith(prefix):
                raise DeskError(f"the plan waiting is {enquiry.plan_sha}, not {plan_prefix!r}: nothing approved")
        with self.store.transaction():
            approval = self.store.add_approval(key, enquiry.plan_sha, who, "approved")
            self.store.move(key, "approved", "approved", who, {"approval": approval, "plan": enquiry.plan_sha})
        return enquiry.plan_sha

    def reject(self, key: str, by: str, reason: str) -> None:
        who = person(by)
        why = " ".join((reason or "").split())
        if not why:
            raise DeskError("a rejection needs a reason (--reason)")
        if len(why) > MAX_REASON_CHARS:
            raise DeskError(f"a reason may have at most {MAX_REASON_CHARS} characters")
        enquiry = self._get(key)
        if enquiry.state != "awaiting_approval" or not enquiry.plan_sha:
            raise DeskError(f"{key} has no plan waiting for a decision; it is {enquiry.state}")
        with self.store.transaction():
            approval = self.store.add_approval(key, enquiry.plan_sha, who, "rejected", why)
            self.store.move(key, "rejected", "rejected", who, {"approval": approval, "reason": why})

    def withdraw(self, key: str, by: str, reason: str = "") -> None:
        who = person(by)
        why = " ".join((reason or "").split())[:MAX_REASON_CHARS]
        enquiry = self._get(key)
        live = self.store.live_approval(key)
        if enquiry.state not in ("approved", "apply_failed") or live is None:
            raise DeskError(f"{key} has no approval to withdraw; it is {enquiry.state}")
        with self.store.transaction():
            self.store.close_approval(live.id, "withdrawn", who, why)
            self.store.move(key, "withdrawn", "awaiting_approval", who, {"approval": live.id, "reason": why})

    # -- apply --------------------------------------------------------------------------------------

    def apply(self, key: str | None = None, dry_run: bool = False, gate=None) -> list:
        """Send approved plans through the one write gate. With a key, that enquiry only."""
        from .gate import WriteGate

        gate = gate or WriteGate(self, dry_run=dry_run)
        if key is not None:
            self._get(key)
            return [gate.apply(key)]
        keys = [e.key for e in self.store.enquiries(in_states=("approved", "apply_failed"))]
        return [gate.apply(k) for k in keys]

    # -- helpers ------------------------------------------------------------------------------------

    def _get(self, key: str):
        enquiry = self.store.get_enquiry(key)
        if enquiry is None:
            raise DeskError(f"no enquiry with the key {key!r}")
        return enquiry

    def waiting(self) -> dict:
        """The enquiries `desk.py plan` shows, by the state they wait in."""
        groups = {name: [] for name in ("received", "held_two_contacts", "held_possible_duplicate",
                                        "awaiting_approval", "approved", "apply_failed")}
        for enquiry in self.store.enquiries(in_states=tuple(groups)):
            groups[enquiry.state].append(enquiry)
        return groups
