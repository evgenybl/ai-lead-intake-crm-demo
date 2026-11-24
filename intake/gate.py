"""The one write gate (section 3.5). Every CRM write in this project passes through WriteGate.apply.

Before anything leaves, the gate refuses:
  1. an operation outside the allow-list (intake/ops.py), or one that appears twice;
  2. a plan with no approval in the state approved;
  3. an approval for another plan's sha256. On a first attempt the plan is computed again from the CRM as
     it is now; if the CRM changed since the approval, the plan is no longer the one the person saw: the
     approval is withdrawn by the system, the enquiry is planned again and waits again;
  4. any write when the run is a dry run.

Before each CRM write, the gate records its intent: the ledger row `started` and the audit row
`operation_started`, in one transaction. If the process is killed between the write and its record, the
desk still shows that the step began; `desk.py doctor` names the ledger row left at `started`, and the
next apply continues the same plan under the same approval.

Writes are idempotent at the CRM too. Before each write the gate looks, in this order: in the local
ledger; then in the contact's own notes (or tasks) by their first line, which is the enquiry's key; then,
for a note, in a search for the key, confirmed by the first line. A contact the gate created is found
again by its origin key. A step that was written but not recorded is therefore found, not written twice.

A failed CRM call leaves the enquiry in `apply_failed` with the error. A retry completes the approved
plan step by step and skips what is already there. It does not plan again once a step of the plan has
started: the CRM now holds this plan's own first writes, which are not a change by anyone else.

One apply runs at a time on a desk: `Desk.apply` holds the desk's apply lock for the whole run, and a
second apply started meanwhile is refused before it reads anything (see `DeskStore.apply_lock`).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import planner
from .canonical import sha256_hex
from .crm.base import CRMError
from .ops import ALLOWED_OPS, operations_outside, repeated_operations

SYSTEM = "system"


class GateRefusal(Exception):
    """The gate refused before anything left. The message names the rule."""


@dataclass
class GateResult:
    key: str
    outcome: str  # applied | apply_failed | refused | replanned | dry_run
    message: str
    steps: list = field(default_factory=list)


class WriteGate:
    def __init__(self, desk, allowed_ops=ALLOWED_OPS, dry_run: bool = False):
        self.desk = desk
        self.store = desk.store
        self.crm = desk.crm
        self.allowed_ops = tuple(allowed_ops)
        self.dry_run = dry_run

    # -- the checks, in the order of section 3.5 --------------------------------------------------

    def _check_allow_list(self, plan: dict) -> None:
        outside = operations_outside(plan, self.allowed_ops)
        if outside:
            raise GateRefusal(f"an operation outside the allow-list: {', '.join(map(str, outside))}")
        repeated = repeated_operations(plan)
        if repeated:
            raise GateRefusal(f"an operation that appears more than once: {', '.join(repeated)}")

    def _check_approval(self, enquiry):
        """The approval in the state approved, for exactly the stored plan."""
        live = self.store.live_approval(enquiry.key)
        if live is None:
            raise GateRefusal("no approval in the state approved for this plan")
        if live.plan_sha != enquiry.plan_sha:
            raise GateRefusal("the approval is for another plan")
        return live

    def _write(self, method, *args):
        """The only place a CRM write is called from. A dry run never passes it."""
        if self.dry_run:
            raise GateRefusal("dry run: no write leaves the gate")
        return method(*args)

    # -- apply one enquiry --------------------------------------------------------------------------

    def apply(self, key: str) -> GateResult:
        enquiry = self.store.get_enquiry(key)
        if enquiry is None:
            return GateResult(key, "refused", "no enquiry with this key")
        if not enquiry.plan_sha:
            return self._refuse(enquiry, f"no plan to apply: the enquiry is {enquiry.state}")
        plan = json.loads(self.store.plan_body(enquiry.plan_sha))
        try:
            if sha256_hex(plan) != enquiry.plan_sha:
                raise GateRefusal("the stored plan does not match its sha256")
            self._check_allow_list(plan)
            approval = self._check_approval(enquiry)
        except GateRefusal as refusal:
            return self._refuse(enquiry, str(refusal))

        started = [row for row in self.store.ledger_rows(key) if row.plan_sha == enquiry.plan_sha]
        if not started:  # a first attempt at this plan: check it is still the plan the person approved
            try:
                current = planner.assess(key, enquiry.payload, enquiry.resolution, self.crm)
            except CRMError as error:
                if self.dry_run:
                    return GateResult(key, "dry_run", f"dry run: the CRM could not be read ({error}); nothing written")
                return self._fail(enquiry, "checking the plan against the CRM", error, [])
            if approval is not None and current.sha != approval.plan_sha:
                why = "the approval is for another plan: the CRM changed since the approval"
                if self.dry_run:
                    return GateResult(key, "dry_run", f"dry run: refused, {why}; nothing written")
                self.store.audit(key, "apply_refused", {"reason": why}, SYSTEM)
                self.desk.record_assessment(key, current, "replanned", SYSTEM, {"why": why})
                return GateResult(key, "replanned", f"refused, {why}; planned again, it waits for a new approval")

        if self.dry_run:
            return GateResult(key, "dry_run", "dry run: nothing written", [_describe(op) for op in plan["operations"]])

        refs: dict = {}
        steps = []
        for number, op in enumerate(plan["operations"], start=1):
            try:
                ref, how = self._step(key, enquiry.plan_sha, op, refs)
            except CRMError as error:
                return self._fail(enquiry, f"step {number} {op['op']}", error, steps)
            refs[op["op"]] = ref
            steps.append(f"{op['op']} {how} ({ref})")
        with self.store.transaction():
            if approval is not None:
                self.store.close_approval(approval.id, "used", SYSTEM)
            self.store.set_fields(key, error=None)
            self.store.move(key, "applied", "applied", SYSTEM, {"plan": enquiry.plan_sha, "operations": steps})
        return GateResult(key, "applied", "applied", steps)

    def _step(self, key: str, plan_sha: str, op: dict, refs: dict) -> tuple:
        name = op["op"]
        row = self.store.ledger_get(key, name)
        if row is not None and row.status == "done" and (name != "fill_empty_fields" or row.plan_sha == plan_sha):
            return row.crm_ref, "already done (ledger)"
        handler = getattr(self, "_do_" + name, None)
        if handler is None:
            raise RuntimeError(f"the gate has no way to perform {name!r}")
        with self.store.transaction():  # the intent, on the record before the CRM is called
            self.store.ledger_start(key, name, plan_sha)
            self.store.audit(key, "operation_started", {"op": name, "plan": plan_sha}, SYSTEM)
        ref, how = handler(key, op, refs)
        with self.store.transaction():
            self.store.ledger_done(key, name, ref, how)
            self.store.audit(key, "operation", {"op": name, "ref": ref, "how": how}, SYSTEM)
        return ref, how

    # -- the four operations ---------------------------------------------------------------------------

    def _do_create_contact(self, key: str, op: dict, refs: dict) -> tuple:
        found = self.crm.find_by_origin_key(key)
        if found:
            return found[0].id, "found by its origin key"
        return self._write(self.crm.create_contact, op["fields"], key).id, "written"

    def _do_fill_empty_fields(self, key: str, op: dict, refs: dict) -> tuple:
        contact_id = self._contact(key, op, refs)
        result = self._write(self.crm.fill_empty_fields, contact_id, op["fields"])
        return contact_id, f"written (filled: {', '.join(result['filled']) or 'none'})"

    def _do_add_note(self, key: str, op: dict, refs: dict) -> tuple:
        contact_id = self._contact(key, op, refs)
        note = self._find_note(contact_id, key)
        if note is not None:
            return note.id, "found"
        return self._write(self.crm.add_note, contact_id, op["body"]).id, "written"

    def _do_add_task(self, key: str, op: dict, refs: dict) -> tuple:
        contact_id = self._contact(key, op, refs)
        for task in self.crm.list_tasks(contact_id):
            if task.first_line == key:
                return task.id, "found"
        return self._write(self.crm.add_task, contact_id, op["body"]).id, "written"

    def _find_note(self, contact_id: str, key: str):
        """The contact's own notes first (they do not lag), then a search, each confirmed by the first line."""
        for note in self.crm.list_notes(contact_id):
            if note.first_line == key:
                return note
        for note in self.crm.search_notes(key):
            if note.first_line == key:
                return note
        return None

    def _contact(self, key: str, op: dict, refs: dict) -> str:
        target = op["contact"]
        if target != planner.CREATED:
            return target
        if "create_contact" in refs:
            return refs["create_contact"]
        row = self.store.ledger_get(key, "create_contact")
        if row is not None and row.status == "done":
            return row.crm_ref
        raise CRMError("the plan's new contact is not in the CRM yet")

    # -- outcomes ----------------------------------------------------------------------------------------

    def _refuse(self, enquiry, reason: str) -> GateResult:
        if not self.dry_run:
            self.store.audit(enquiry.key, "apply_refused", {"reason": reason}, SYSTEM)
        return GateResult(enquiry.key, "refused", f"refused: {reason}; nothing written")

    def _fail(self, enquiry, where: str, error: Exception, steps: list) -> GateResult:
        message = f"{where}: {error}"
        with self.store.transaction():
            self.store.set_fields(enquiry.key, error=message)
            self.store.move(enquiry.key, "apply_failed", "apply_failed", SYSTEM, {"error": message, "operations": steps})
        return GateResult(enquiry.key, "apply_failed", message, steps)


def _describe(op: dict) -> str:
    target = op.get("contact")
    return f"would {op.get('op')}" + (f" on contact {target}" if target else "")
