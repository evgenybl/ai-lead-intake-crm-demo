"""The flow of an enquiry: arrive, validate, identity, draft, plan; and a person's decisions on it.

Writes to the CRM are not here: they happen in intake/gate.py, and only there.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from . import normalise, planner, validate
from .canonical import canonical_json
from .crm.base import CRMError
from .draft import RecordedBackend
from .identity import idempotency_key, rests_on_the_phone_alone
from .store import ApplyBusy

SYSTEM = "system"
MAX_NAME_CHARS = 80
MAX_REASON_CHARS = 500
MIN_PLAN_PREFIX = 8  # hex digits of the plan's sha256 an approval must name


class DeskError(Exception):
    """A person's command was refused; the message says why. Nothing was changed.

    `for_page` is the same reason worded for the desk page's form, which has no command and no option: where the
    message names one (--by, desk.py plan), the page's wording does not."""

    def __init__(self, message: str, for_page: str | None = None):
        super().__init__(message)
        self.for_page = for_page or message


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
    differs: tuple = ()  # for a replay with different content: the fields that differ
    resent: bool = False  # a copy that passes, under the key of an enquiry that was refused

    def as_dict(self) -> dict:
        answer = {"key": self.key, "state": self.state, "replay": self.replay}
        if self.replay:
            answer["original"] = {"arrived_at": self.first_arrived_at, "via": self.via}
        if self.resent:
            answer["earlier_copy_refused"] = True
        return answer


def person(name: str) -> str:
    """A person's stated name, as the trail records it. It is what they typed, not a login."""
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise DeskError("a decision needs a person's name (--by)", "a decision needs a person's name")
    if validate.starts_like_formula(name or ""):
        raise DeskError("a name may not start with =, +, -, @, a tab or a carriage return")
    if validate.has_control_characters(cleaned):
        raise DeskError("a name may not hold control characters")
    if len(cleaned) > MAX_NAME_CHARS:
        raise DeskError(f"a name may have at most {MAX_NAME_CHARS} characters")
    if cleaned.casefold() == SYSTEM:
        raise DeskError(f"{SYSTEM!r} is the desk's own name in the trail; give a person's name")
    return cleaned


def replay_differences(first: dict, this_time: dict) -> dict:
    """What a replay changes against the first arrival, field by field. The time it was sent is not content."""
    differs = {}
    for name in validate.FIELDS:
        if name != "received_at" and first.get(name) != this_time.get(name):
            differs[name] = {"first": first.get(name), "this_time": this_time.get(name)}
    return differs


class Desk:
    def __init__(self, store, crm, clock, drafter=None):
        self.store = store
        self.crm = crm
        self.clock = clock
        self.drafter = drafter or RecordedBackend()

    # -- arrive -----------------------------------------------------------------------------------

    def receive(self, obj: dict, via: str) -> Answer:
        """One JSON object from HTTP or a file. A known key is a replay: counted, recorded, nothing queued.

        One exception: a copy that passes validation, under the key of an enquiry that was refused, is a
        corrected resend. It becomes the enquiry, and the trail keeps why the earlier copy was refused."""
        extracted = validate.extract(obj, arrived_at=self.clock.now())
        fields = extracted.fields
        key = idempotency_key(fields)
        reasons = validate.reasons(extracted)
        detail = {"reasons": reasons}
        if extracted.ignored:
            detail["ignored_fields"] = list(extracted.ignored)
        if extracted.received_at_filled:
            detail["received_at_filled"] = True
        with self.store.transaction():
            existing = self.store.get_enquiry(key)
            if existing is not None and existing.state == "refused" and not reasons:
                detail.update({"earlier_copy_refused": existing.reasons,
                               "fields_changed": sorted(replay_differences(existing.payload, fields)),
                               "received_at": fields["received_at"]})
                self.store.resend(key, via, fields, detail)
                state, resent = "received", True
            elif existing is not None:
                differs = replay_differences(existing.payload, fields)
                detail = {"same_content": not differs, "state_now": existing.state,
                          "received_at": fields["received_at"]}
                if differs:
                    detail["differs"] = differs
                self.store.add_replay(key, via, detail)
                return Answer(key, existing.state, True, existing.first_arrived_at, existing.via,
                              tuple(existing.reasons), not differs, tuple(differs))
            else:
                state, resent = ("refused" if reasons else "received"), False
                self.store.insert_enquiry(key, via, fields, state, reasons, detail)
            first = self.store.get_enquiry(key).first_arrived_at
        if state == "received":
            state = self.assess(key)
        return Answer(key, state, False, first, via, tuple(reasons), resent=resent)

    def assess(self, key: str) -> str:
        """Identity, draft and plan for an enquiry in `received`. A CRM that cannot be read leaves it there."""
        enquiry = self._get(key)
        if enquiry.state != "received":
            return enquiry.state
        try:
            assessment = planner.assess(key, enquiry.payload, enquiry.resolution, self.crm, self.store.written(key))
        except CRMError as error:
            with self.store.transaction():
                self.store.set_fields(key, error=f"identity: {error}")
                self.store.audit(key, "identity_failed", {"error": str(error), "state": "received"}, SYSTEM)
            return "received"
        draft = self.drafter.draft(enquiry.payload)
        with self.store.transaction():
            self.store.set_fields(key, draft=draft, error=None)
            noted = {"label": draft["label"], "outcome": draft["outcome"], "request": draft.get("request", "")}
            if draft.get("reason"):
                noted["reason"] = draft["reason"]
            self.store.audit(key, "draft", noted, SYSTEM)
            self.record_assessment(key, assessment, "assessed", SYSTEM, {"draft": draft["outcome"]})
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
        assessment = planner.assess(key, enquiry.payload, enquiry.resolution, self.crm, self.store.written(key))
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
        """Resolve a hold: `new`, or `contact:<id>` for one of the contacts identity found. An enquiry waiting for
        approval whose contact was found by the phone alone may be resolved as `new` too: the phone may be shared."""
        who = person(by)
        enquiry = self._get(key)
        phone_alone = enquiry.state == "awaiting_approval" and rests_on_the_phone_alone(enquiry.identity)
        if enquiry.state not in ("held_two_contacts", "held_possible_duplicate") and not phone_alone:
            raise DeskError(f"{key} is not held; it is {enquiry.state}")
        if phone_alone and as_ != "new":
            raise DeskError(f"{key} was matched to contact {enquiry.identity.get('contact')} by its phone alone; it "
                            "can be resolved as new, or approved as it is")
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
        assessment = planner.assess(key, enquiry.payload, as_, self.crm, self.store.written(key))
        with self.store.transaction():
            self.store.set_fields(key, resolution=as_)
            self.store.audit(key, "resolution", {"as": as_, "candidates": candidates}, who)
            self.record_assessment(key, assessment, "resolved", who)
        return assessment.state

    def approve(self, key: str, by: str, plan: str) -> str:
        """Approve the waiting plan, named by its sha256 or its first 8 or more hex digits.

        The plan must be named: the person approves the plan they saw, and if the desk planned again since
        (the CRM changed), the name no longer fits and nothing is approved."""
        who = person(by)
        enquiry = self._get(key)
        if enquiry.state != "awaiting_approval" or not enquiry.plan_sha:
            raise DeskError(f"{key} has no plan waiting for approval; it is {enquiry.state}")
        prefix = (plan or "").strip().lower() if isinstance(plan, str) else ""
        if len(prefix) < MIN_PLAN_PREFIX or not all(c in "0123456789abcdef" for c in prefix):
            named = f"an approval names the plan it approves: its sha256, or its first {MIN_PLAN_PREFIX} or more hex digits"
            raise DeskError(f"{named}, as `desk.py plan` printed it; nothing approved",
                            f"{named}, as the enquiry's page shows it; nothing approved")
        if not enquiry.plan_sha.startswith(prefix):
            waiting = f"the plan waiting is {enquiry.plan_sha[:12]}, not the one named ({prefix[:12]})"
            if not any(sha.startswith(prefix) for sha in self.store.plan_shas(key)):
                raise DeskError(f"{waiting}, which is not a plan of {key}; read the plan with `desk.py plan`; nothing "
                                "approved", f"{waiting}, which is not a plan of {key}; nothing approved")
            raise DeskError(f"{waiting}: the desk planned again since; read it with `desk.py plan`; nothing approved",
                            f"{waiting}: the desk planned again since; read the plan as it is now on the enquiry's "
                            "page; nothing approved")
        with self.store.transaction():
            approval = self.store.add_approval(key, enquiry.plan_sha, who, "approved")
            self.store.move(key, "approved", "approved", who, {"approval": approval, "plan": enquiry.plan_sha})
        return enquiry.plan_sha

    def reject(self, key: str, by: str, reason: str) -> list:
        who = person(by)
        why = " ".join((reason or "").split())
        if not why:
            raise DeskError("a rejection needs a reason (--reason)", "a rejection needs a reason")
        if len(why) > MAX_REASON_CHARS:
            raise DeskError(f"a reason may have at most {MAX_REASON_CHARS} characters")
        enquiry = self._get(key)
        if enquiry.state != "awaiting_approval" or not enquiry.plan_sha:
            raise DeskError(f"{key} has no plan waiting for a decision; it is {enquiry.state}")
        with self._settled(key) as found:
            with self.store.transaction():
                self._record_settled(key, found)
                approval = self.store.add_approval(key, enquiry.plan_sha, who, "rejected", why)
                self.store.move(key, "rejected", "rejected", who, {"approval": approval, "reason": why})
        return found

    def withdraw(self, key: str, by: str, reason: str = "") -> list:
        """Withdraw an approval not used yet. A step that an apply began and did not record (it failed, or the
        process stopped) is looked for in the CRM first, and the desk records whether it was written.

        Returns (ledger row, what the CRM holds or None) for each such step."""
        who = person(by)
        why = " ".join((reason or "").split())[:MAX_REASON_CHARS]
        enquiry = self._get(key)
        live = self.store.live_approval(key)
        if enquiry.state not in ("approved", "apply_failed") or live is None:
            raise DeskError(f"{key} has no approval to withdraw; it is {enquiry.state}")
        with self._settled(key, alone=True) as found:
            # No apply runs now, and none starts before this ends. One that ran a moment ago may have used the
            # approval, so the state is read again.
            enquiry = self._get(key)
            live = self.store.live_approval(key)
            if enquiry.state not in ("approved", "apply_failed") or live is None:
                raise DeskError(f"{key} has no approval to withdraw; it is {enquiry.state}")
            with self.store.transaction():
                self._record_settled(key, found)
                self.store.close_approval(live.id, "withdrawn", who, why)
                self.store.move(key, "withdrawn", "awaiting_approval", who, {"approval": live.id, "reason": why})
        return found

    @contextmanager
    def _settled(self, key: str, alone: bool = False):
        """What the CRM holds for each step of this enquiry left `started`, looked up while no apply runs.

        With `alone`, the apply lock is held even when no step is left started: a withdrawal takes an approval
        back, and an apply that runs meanwhile would write on it after it was withdrawn."""
        from .gate import WriteGate

        if not alone and not any(row.status == "started" for row in self.store.ledger_rows(key)):
            yield []
            return
        try:
            with self.store.apply_lock():
                rows = [row for row in self.store.ledger_rows(key) if row.status == "started"]
                try:
                    found = WriteGate(self).settle(key, rows) if rows else []
                except CRMError as error:
                    raise DeskError(f"an apply of {key} began a step it did not record, and the CRM could not be "
                                    f"read to see whether it was written ({error}); nothing was changed. Try again "
                                    "when the CRM answers") from None
                yield found
        except ApplyBusy:
            raise DeskError("another `desk.py apply` is running on this desk; nothing was changed. Try again when "
                            "that one has finished", "an apply is running on this desk just now; nothing was changed. "
                            "Try again in a moment") from None

    def _record_settled(self, key: str, found: list) -> None:
        for row, there in found:
            if there is None:
                self.store.ledger_drop(key, row.op)
                self.store.audit(key, "operation_not_written", {"op": row.op}, SYSTEM)
            else:
                ref, how, contact = there
                self.store.ledger_done(key, row.op, ref, how, contact)
                self.store.audit(key, "operation", {"op": row.op, "ref": ref, "how": how}, SYSTEM)

    # -- apply --------------------------------------------------------------------------------------

    def apply(self, key: str | None = None, dry_run: bool = False, gate=None) -> list:
        """Send approved plans through the one write gate. With a key, that enquiry only."""
        from .gate import WriteGate

        gate = gate or WriteGate(self, dry_run=dry_run)
        if key is not None:
            self._get(key)
        if gate.dry_run:
            return self._apply_each(gate, key)
        try:
            with self.store.apply_lock():
                return self._apply_each(gate, key)
        except ApplyBusy:
            raise DeskError("another `desk.py apply` is running on this desk; this one wrote nothing. Run it "
                            "again when that one has finished") from None

    def _apply_each(self, gate, key: str | None) -> list:
        if key is not None:
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
