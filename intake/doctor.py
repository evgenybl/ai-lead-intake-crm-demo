"""`desk.py doctor`: the invariants of the four promises, run on the desk file and the practice CRM file.

It reads both files and changes nothing. Each check belongs to one promise:

  1 No lead is lost ................ every enquiry in exactly one named state; the arrivals add up and agree
                                     with the trail; every key in the trail is on the desk; every failure
                                     names its error
  2 No duplicate is created ........ one contact per normalised e-mail; no two notes, tasks or created
                                     contacts with the same enquiry key
  3 Nothing is written before a person approves
                                   . every applied plan has a used approval; every approval row matches the
                                     decision the trail records (who, which plan); every ledger row and every
                                     CRM record that carries an enquiry key has an approval; every operation
                                     is in the allow-list; every stored plan matches its sha256
  4 Every step is on the record .... the audit triggers are in place as written; the hash chain is unbroken;
                                     every state move follows the one before it; each enquiry's state, stored
                                     content and plan are the ones its trail last records; every operation in
                                     the trail has its ledger row, and no step is left half recorded

What it can and cannot find: the trail guards against mistakes, bugs and plain edits of the files. A person
who can open the desk file can drop the triggers, change rows, recompute every hash and put the triggers
back; such a rewrite, done consistently, is not found. docs/DESIGN.md lists the cases.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from . import audit, normalise, states
from .canonical import sha256_hex
from .ops import ALLOWED_OPS, operations_outside

PROMISES = {
    1: "No lead is lost",
    2: "No duplicate is created",
    3: "Nothing is written before a person approves",
    4: "Every step is on the record",
}

# The shape of an enquiry key, for CRM records that carry one in their first line.
KEY_SHAPE = re.compile(r"^[a-z][a-z0-9_-]{0,39}:[A-Za-z0-9_-]{6,80}$")
AUTHORISING = ("approved", "used", "withdrawn")


@dataclass
class Check:
    promise: int
    name: str
    problems: list = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


def _read_only(path: str) -> sqlite3.Connection:
    return sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def examine(desk_path: str, crm_path: str | None) -> list:
    """Every check, in promise order. A CRM path of None skips the CRM checks and says so."""
    desk = _read_only(desk_path)
    try:
        crm = _crm_records(crm_path) if crm_path else None
        checks = (
            _promise_1(desk)
            + _promise_2(crm)
            + _promise_3(desk, crm)
            + _promise_4(desk)
        )
    finally:
        desk.close()
    return checks


def _crm_records(path: str) -> dict:
    conn = _read_only(path)
    try:
        return {
            "contacts": conn.execute("SELECT id, email, origin_key FROM contacts ORDER BY id").fetchall(),
            "notes": conn.execute("SELECT id, contact_id, body FROM notes ORDER BY id").fetchall(),
            "tasks": conn.execute("SELECT id, contact_id, body FROM tasks ORDER BY id").fetchall(),
        }
    finally:
        conn.close()


def _first_line(body: str) -> str:
    return body.split("\n", 1)[0].strip()


# -- promise 1 ----------------------------------------------------------------------------------------

def _promise_1(desk) -> list:
    enquiries = desk.execute("SELECT key, state, error FROM enquiries").fetchall()
    keys = {row[0] for row in enquiries}

    named = Check(1, "every enquiry is in exactly one named state")
    for key, state, _ in enquiries:
        if state not in states.STATES:
            named.problems.append(f"{key} is in {state!r}, which is not a named state")

    arrivals = desk.execute("SELECT key, kind FROM arrivals").fetchall()
    first = Counter(key for key, kind in arrivals if kind == "first")
    kinds = Counter(kind for _, kind in arrivals)
    add_up = Check(1, "the arrivals add up, and agree with the trail")
    for key in sorted(keys):
        if first[key] != 1:
            add_up.problems.append(f"{key} has {first[key]} first arrivals, not 1")
    for key in sorted({k for k, _ in arrivals} - keys):
        add_up.problems.append(f"an arrival names {key}, which is not on the desk")
    resends = f" + {kinds['resend']} corrected resends" if kinds["resend"] else ""
    add_up.note = f"{len(arrivals)} arrived = {len(keys)} enquiries + {kinds['replay']} replays{resends}"
    if len(arrivals) != len(keys) + kinds["replay"] + kinds["resend"]:
        add_up.problems.append(f"{len(arrivals)} arrived, but {len(keys)} enquiries + {kinds['replay']} replays{resends}")
    events = Counter(event for (event,) in desk.execute(
        "SELECT event FROM audit WHERE event IN ('arrived', 'replay', 'resent')"))
    for event, held, what in (("arrived", len(keys), "enquiries on the desk"), ("replay", kinds["replay"], "replays"),
                              ("resent", kinds["resend"], "corrected resends")):
        if events[event] != held:
            add_up.problems.append(f"the trail records {events[event]} '{event}' events, but there are {held} {what}")

    trail = Check(1, "every key in the trail is an enquiry on the desk")
    for (key,) in desk.execute("SELECT DISTINCT key FROM audit ORDER BY key"):
        if key not in keys:
            trail.problems.append(f"the trail records {key}, which is not on the desk: a lost lead")

    errors = Check(1, "every apply_failed enquiry names its error")
    for key, state, error in enquiries:
        if state == "apply_failed" and not (error or "").strip():
            errors.problems.append(f"{key} is apply_failed with no error named")
    return [named, add_up, trail, errors]


def _trail_by_key(desk) -> dict:
    by_key: dict = {}
    for row in audit.rows(desk):
        by_key.setdefault(row[2], []).append((row[0], row[3], audit.detail_of(row), row[5]))
    return by_key


# -- promise 2 ----------------------------------------------------------------------------------------

def _not_checked(promise: int, name: str) -> Check:
    return Check(promise, name, note="not checked: no practice CRM file (the doctor cannot list an HTTP CRM)")


def _promise_2(crm) -> list:
    names = (
        "one contact per normalised e-mail",
        "no two notes with the same enquiry key",
        "no two tasks with the same enquiry key",
        "no two contacts created by the same enquiry",
    )
    if crm is None:
        return [_not_checked(2, name) for name in names]
    one_email = Check(2, names[0])
    emails = Counter(normalise.email(email) for _, email, _ in crm["contacts"] if normalise.email(email))
    for email, n in sorted(emails.items()):
        if n > 1:
            one_email.problems.append(f"{n} contacts share one e-mail ({_mask(email)})")
    checks = [one_email]
    for name, rows in ((names[1], crm["notes"]), (names[2], crm["tasks"])):
        check = Check(2, name)
        lines = Counter(_first_line(body) for _, _, body in rows if KEY_SHAPE.match(_first_line(body)))
        for key, n in sorted(lines.items()):
            if n > 1:
                check.problems.append(f"{n} records carry {key}")
        checks.append(check)
    origin = Check(2, names[3])
    origins = Counter(key for _, _, key in crm["contacts"] if key)
    for key, n in sorted(origins.items()):
        if n > 1:
            origin.problems.append(f"{n} contacts were created by {key}")
    checks.append(origin)
    return checks


def _mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}...@{domain}"


# -- promise 3 ----------------------------------------------------------------------------------------

def _promise_3(desk, crm) -> list:
    approvals = desk.execute("SELECT key, plan_sha, state FROM approvals").fetchall()
    enquiries = desk.execute("SELECT key, state, plan_sha FROM enquiries").fetchall()
    on_desk = {key for key, _, _ in enquiries}
    authorised = {(key, sha) for key, sha, state in approvals if state in AUTHORISING}
    authorised_keys = {key for key, _ in authorised}

    used = Check(3, "every applied enquiry has a used approval for its plan")
    used_pairs = {(key, sha) for key, sha, state in approvals if state == "used"}
    for key, state, sha in enquiries:
        if state == "applied" and (key, sha) not in used_pairs:
            used.problems.append(f"{key} is applied, but no approval of its plan is used")

    decided = Check(3, "every approval matches the decision its trail records")
    decisions = {}
    for row in audit.rows(desk):
        if row[3] in ("approved", "rejected"):
            detail = audit.detail_of(row)
            decisions[detail.get("approval")] = (row[0], row[2], row[3], row[5], detail.get("plan"))
    rows = desk.execute("SELECT id, key, plan_sha, by, state FROM approvals ORDER BY id").fetchall()
    for approval_id, key, sha, by, state in rows:
        want = "rejected" if state == "rejected" else "approved"
        found = decisions.get(approval_id)
        if found is None:
            decided.problems.append(f"approval {approval_id} for {key} has no '{want}' event in the trail: "
                                    "it was not made through the desk")
        elif found[1:4] != (key, want, by) or (want == "approved" and found[4] != sha):
            decided.problems.append(f"approval {approval_id} for {key} does not match the '{want}' event in row "
                                    f"{found[0]} of the trail (who, or which plan)")
    for approval_id in sorted(set(decisions) - {row[0] for row in rows}, key=str):
        decided.problems.append(f"the trail records approval {approval_id}, which is not on the desk")

    ledger = Check(3, "every ledger row has an approval of its plan")
    for key, op, sha in desk.execute("SELECT key, op, plan_sha FROM ledger ORDER BY key, op"):
        if (key, sha) not in authorised:
            ledger.problems.append(f"the ledger records {op} for {key} with no approval of that plan")

    records = Check(3, "every CRM record that carries an enquiry key has an approval")
    if crm is None:
        records.note = "not checked: no practice CRM file (the doctor cannot list an HTTP CRM)"
    else:
        carried = [("contact", cid, key) for cid, _, key in crm["contacts"] if key]
        carried += [("note", nid, _first_line(body)) for nid, _, body in crm["notes"]]
        carried += [("task", tid, _first_line(body)) for tid, _, body in crm["tasks"]]
        for kind, record_id, key in carried:
            if key in authorised_keys:
                continue
            if key in on_desk:
                records.problems.append(f"{kind} {record_id} carries {key}, which has no approval: a write with no approval")
            elif KEY_SHAPE.match(key):
                records.problems.append(f"{kind} {record_id} carries {key}, which is not on the desk")

    allowed = Check(3, "every operation is in the allow-list")
    for sha, key, body in desk.execute("SELECT sha, key, body FROM plans ORDER BY made_at, sha"):
        for name in operations_outside(json.loads(body), ALLOWED_OPS):
            allowed.problems.append(f"the plan {sha[:12]} for {key} holds {name!r}, outside the allow-list")
    for key, op in desk.execute("SELECT key, op FROM ledger ORDER BY key, op"):
        if op not in ALLOWED_OPS:
            allowed.problems.append(f"the ledger records {op!r} for {key}: a foreign operation reached the CRM")

    intact = Check(3, "every stored plan matches its sha256")
    for sha, body in desk.execute("SELECT sha, body FROM plans ORDER BY made_at, sha"):
        if sha256_hex(json.loads(body)) != sha:
            intact.problems.append(f"the plan stored as {sha[:12]} does not match its sha256")
    return [used, decided, ledger, records, allowed, intact]


# -- promise 4 ----------------------------------------------------------------------------------------

def _promise_4(desk) -> list:
    triggers = Check(4, "the audit triggers are in place, as written")
    triggers.problems.extend(audit.trigger_problems(desk))

    chain = Check(4, "the audit chain is unbroken")
    chain.problems.extend(audit.chain_problems(desk))

    trail = _trail_by_key(desk)
    moves = Check(4, "every state move follows the one before it")
    for key, rows in sorted(trail.items()):
        before = None
        for row_id, event, detail, _ in rows:
            if "state" not in detail:
                continue
            if "from" in detail:
                allowed = states.TRANSITIONS.get((detail["from"], event), frozenset())
                if detail["from"] != before:
                    moves.problems.append(f"{key}: row {row_id} ({event}) moves from {detail['from']}, but the rows "
                                          f"before it leave it in {before}")
                elif detail["state"] not in allowed:
                    moves.problems.append(f"{key}: row {row_id} ({event}) from {detail['from']} to "
                                          f"{detail['state']} is not a move the state table allows")
            elif event == "arrived":
                if before is not None or detail["state"] not in states.TRANSITIONS[(None, "arrived")]:
                    moves.problems.append(f"{key}: row {row_id} records a second arrival, or an arrival in "
                                          f"{detail['state']}")
            elif detail["state"] != before:
                moves.problems.append(f"{key}: row {row_id} ({event}) records {detail['state']}, but the rows "
                                      f"before it leave it in {before}")
            before = detail["state"]

    last = Check(4, "each enquiry's state, content and plan are the ones its trail last records")
    for key, state, payload, plan_sha in desk.execute(
            "SELECT key, state, payload, plan_sha FROM enquiries ORDER BY rowid"):
        recorded = {"state": None, "payload_sha256": None, "plan": None}
        for _, _, detail, _ in trail.get(key, []):
            for name in recorded:
                if name in detail:
                    recorded[name] = detail[name]
        if recorded["state"] != state:
            last.problems.append(f"{key} is {state}, but its trail last records {recorded['state']!r}")
        if recorded["payload_sha256"] != hashlib.sha256(payload.encode("utf-8")).hexdigest():
            last.problems.append(f"{key}: the stored content does not match the sha256 its trail records")
        if recorded["plan"] != plan_sha:
            last.problems.append(f"{key}: its plan is {(plan_sha or 'none')[:12]}, but its trail last records "
                                 f"{(recorded['plan'] or 'none')[:12]}")

    steps = Check(4, "every operation in the trail has its ledger row; no step is left half recorded")
    ledger_rows = desk.execute("SELECT key, op, status, plan_sha FROM ledger").fetchall()
    ledger = {(key, op): status for key, op, status, _ in ledger_rows}
    current = {key: (state, plan) for key, state, plan in desk.execute("SELECT key, state, plan_sha FROM enquiries")}
    for key, rows in sorted(trail.items()):
        for row_id, event, detail, _ in rows:
            if event == "operation" and (key, detail.get("op")) not in ledger:
                steps.problems.append(f"{key}: row {row_id} records {detail.get('op')}, but the ledger has no row "
                                      "for it")
    for key, op, status, plan in sorted(ledger_rows):
        # A step of the enquiry's current plan that began, while the enquiry is not apply_failed (which names
        # its error). A step of an earlier plan is history: the plan was made again from the CRM as it was.
        state, current_plan = current.get(key, (None, None))
        if status == "started" and plan == current_plan and state != "apply_failed":
            steps.problems.append(f"{key}: the step {op} began and its end is not recorded (the process stopped "
                                  "between the CRM call and its record); `desk.py apply` completes the plan and "
                                  "finds what is already written")
    return [triggers, chain, moves, last, steps]
