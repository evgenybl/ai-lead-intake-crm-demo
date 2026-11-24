"""`desk.py doctor`: the invariants of the four promises, run on the desk file and the practice CRM file.

It reads both files and changes nothing. Each check belongs to one promise of section 2:

  1 No lead is lost ................ every enquiry in exactly one named state; the arrivals add up;
                                     every key in the trail is on the desk; every failure names its error
  2 No duplicate is created ........ one contact per normalised e-mail; no two notes, tasks or created
                                     contacts with the same enquiry key
  3 Nothing is written before a person approves
                                   . every applied plan has a used approval; every ledger row and every
                                     CRM record that carries an enquiry key has an approval; every
                                     operation is in the allow-list; every stored plan matches its sha256
  4 Every step is on the record .... the audit triggers are in place; the hash chain is unbroken; each
                                     enquiry's state is the last state its trail records
"""
from __future__ import annotations

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
    replays = sum(1 for _, kind in arrivals if kind == "replay")
    add_up = Check(1, "the arrivals add up")
    for key in sorted(keys):
        if first[key] != 1:
            add_up.problems.append(f"{key} has {first[key]} first arrivals, not 1")
    for key in sorted({k for k, _ in arrivals} - keys):
        add_up.problems.append(f"an arrival names {key}, which is not on the desk")
    add_up.note = f"{len(arrivals)} arrived = {len(keys)} enquiries + {replays} replays"
    if len(arrivals) != len(keys) + replays:
        add_up.problems.append(f"{len(arrivals)} arrived, but {len(keys)} enquiries + {replays} replays")

    trail = Check(1, "every key in the trail is an enquiry on the desk")
    for (key,) in desk.execute("SELECT DISTINCT key FROM audit ORDER BY key"):
        if key not in keys:
            trail.problems.append(f"the trail records {key}, which is not on the desk: a lost lead")

    errors = Check(1, "every apply_failed enquiry names its error")
    for key, state, error in enquiries:
        if state == "apply_failed" and not (error or "").strip():
            errors.problems.append(f"{key} is apply_failed with no error named")
    return [named, add_up, trail, errors]


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
    return [used, ledger, records, allowed, intact]


# -- promise 4 ----------------------------------------------------------------------------------------

def _promise_4(desk) -> list:
    triggers = Check(4, "the audit triggers are in place")
    for name in audit.missing_triggers(desk):
        triggers.problems.append(f"the trigger {name} is missing: the trail can be changed")

    chain = Check(4, "the audit chain is unbroken")
    chain.problems.extend(audit.chain_problems(desk))

    last = Check(4, "each enquiry's state is the last state its trail records")
    recorded = {}
    for row in audit.rows(desk):
        detail = audit.detail_of(row)
        if "state" in detail:
            recorded[row[2]] = detail["state"]
    for key, state in desk.execute("SELECT key, state FROM enquiries ORDER BY rowid"):
        if recorded.get(key) != state:
            last.problems.append(f"{key} is {state}, but its trail last records {recorded.get(key)!r}")
    return [triggers, chain, last]
