"""The desk's own SQLite file: enquiries, arrivals, plans, approvals, the ledger, the trail.

Every change happens inside `transaction()`, and every change of state goes through `move()`, which
checks the state table and writes the audit row in the same transaction.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass

from . import audit, states
from .canonical import canonical_json, sha256_hex

APPROVAL_STATES = ("approved", "used", "withdrawn", "rejected")


def _schema() -> str:
    names = ", ".join(f"'{name}'" for name in states.STATES)
    approval_states = ", ".join(f"'{name}'" for name in APPROVAL_STATES)
    return f"""
CREATE TABLE IF NOT EXISTS plans (
    sha TEXT PRIMARY KEY,
    key TEXT NOT NULL,
    body TEXT NOT NULL,
    made_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enquiries (
    key TEXT PRIMARY KEY,
    first_arrived_at TEXT NOT NULL,
    via TEXT NOT NULL CHECK (via IN ('http', 'file')),
    payload TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ({names})),
    reasons TEXT NOT NULL DEFAULT '[]',
    identity TEXT,
    resolution TEXT,
    plan_sha TEXT REFERENCES plans (sha),
    draft TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS arrivals (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL REFERENCES enquiries (key),
    at TEXT NOT NULL,
    via TEXT NOT NULL CHECK (via IN ('http', 'file')),
    kind TEXT NOT NULL CHECK (kind IN ('first', 'replay', 'resend'))
);
CREATE UNIQUE INDEX IF NOT EXISTS one_first_arrival ON arrivals (key) WHERE kind = 'first';
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY,
    key TEXT NOT NULL REFERENCES enquiries (key),
    plan_sha TEXT NOT NULL REFERENCES plans (sha),
    by TEXT NOT NULL,
    at TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ({approval_states})),
    reason TEXT NOT NULL DEFAULT '',
    closed_by TEXT,
    closed_at TEXT,
    CHECK (state <> 'rejected' OR length(trim(reason)) > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_approval ON approvals (key) WHERE state = 'approved';
CREATE TABLE IF NOT EXISTS ledger (
    key TEXT NOT NULL,
    op TEXT NOT NULL,
    plan_sha TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'done')),
    crm_ref TEXT,
    how TEXT,
    at TEXT NOT NULL,
    PRIMARY KEY (key, op)
);
""" + audit.SCHEMA


class HomeError(Exception):
    """A desk or CRM file that is missing, or already there when it should not be."""


class ApplyBusy(Exception):
    """Another apply holds this desk's apply lock."""


APPLY_LOCK_SUFFIX = "-apply-lock"


@dataclass(frozen=True)
class Enquiry:
    key: str
    first_arrived_at: str
    via: str
    payload: dict
    state: str
    reasons: list
    identity: dict | None
    resolution: str | None
    plan_sha: str | None
    draft: dict | None
    error: str | None


@dataclass(frozen=True)
class Approval:
    id: int
    key: str
    plan_sha: str
    by: str
    at: str
    state: str
    reason: str
    closed_by: str | None
    closed_at: str | None


@dataclass(frozen=True)
class LedgerRow:
    key: str
    op: str
    plan_sha: str
    status: str
    crm_ref: str | None
    how: str | None
    at: str


BUSY_SECONDS = 5.0  # how long a command waits for a desk file another program holds, before it gives up


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_SECONDS)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


class DeskStore:
    def __init__(self, conn: sqlite3.Connection, clock, path: str):
        self.conn = conn
        self.clock = clock
        self.path = path
        self._depth = 0

    @classmethod
    def create(cls, path: str, clock) -> "DeskStore":
        if os.path.exists(path):
            raise HomeError(f"a desk is already at {path}")
        conn = _connect(path)
        conn.executescript(_schema())
        return cls(conn, clock, path)

    @classmethod
    def open(cls, path: str, clock) -> "DeskStore":
        if not os.path.exists(path):
            raise HomeError(f"no desk at {path}: run `desk.py init` first")
        return cls(_connect(path), clock, path)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def apply_lock(self):
        """One apply at a time on this desk. The lock is an exclusive SQLite transaction on a file of its own
        beside the desk file, held for the whole run; a second apply finds it taken and stops before it reads
        anything. The operating system releases it when the process ends, so a killed apply never leaves it
        held. It does not stop another program, or a second desk with files of its own, from writing the CRM."""
        conn = sqlite3.connect(self.path + APPLY_LOCK_SUFFIX, timeout=0, isolation_level=None)
        try:
            try:
                conn.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError:
                raise ApplyBusy(self.path) from None
            try:
                yield
            finally:
                conn.execute("ROLLBACK")
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """One SQLite write transaction; nested use joins the outer one."""
        if self._depth:
            self._depth += 1
            try:
                yield
            finally:
                self._depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._depth = 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._depth = 0

    # -- the trail ---------------------------------------------------------------------------

    def audit(self, key: str, event: str, detail: dict, actor: str) -> int:
        with self.transaction():
            return audit.append(self.conn, self.clock.now(), key, event, detail, actor)

    def trail(self, key: str) -> list:
        return audit.rows(self.conn, key)

    def move(self, key: str, event: str, target: str, actor: str, detail: dict | None = None) -> str:
        """Change an enquiry's state through the state table, with its audit row. Returns the old state."""
        with self.transaction():
            row = self.conn.execute("SELECT state FROM enquiries WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise states.TransitionError(f"no enquiry {key!r}")
            current = row[0]
            states.check(current, event, target)
            self.conn.execute("UPDATE enquiries SET state = ? WHERE key = ?", (target, key))
            audit.append(
                self.conn, self.clock.now(), key, event,
                {**(detail or {}), "from": current, "state": target}, actor,
            )
            return current

    # -- enquiries and arrivals --------------------------------------------------------------

    def insert_enquiry(self, key: str, via: str, payload: dict, state: str, reasons: list, detail: dict) -> None:
        """A first arrival: the enquiry, its arrival row and its 'arrived' audit row, together."""
        states.check(None, "arrived", state)
        with self.transaction():
            now = self.clock.now()
            self.conn.execute(
                "INSERT INTO enquiries (key, first_arrived_at, via, payload, state, reasons) VALUES (?, ?, ?, ?, ?, ?)",
                (key, now, via, canonical_json(payload), state, json.dumps(reasons)),
            )
            self.conn.execute(
                "INSERT INTO arrivals (key, at, via, kind) VALUES (?, ?, ?, 'first')", (key, now, via)
            )
            audit.append(self.conn, now, key, "arrived",
                         {**detail, "via": via, "state": state, "payload_sha256": sha256_hex(payload)}, "system")

    def resend(self, key: str, via: str, payload: dict, detail: dict) -> None:
        """A corrected copy of a refused enquiry: it becomes the enquiry's content, and the enquiry moves from
        refused to received. The arrival is counted as a resend; the trail keeps the earlier reasons."""
        with self.transaction():
            now = self.clock.now()
            self.conn.execute("UPDATE enquiries SET payload = ?, reasons = '[]' WHERE key = ?",
                              (canonical_json(payload), key))
            self.conn.execute(
                "INSERT INTO arrivals (key, at, via, kind) VALUES (?, ?, ?, 'resend')", (key, now, via)
            )
            self.move(key, "resent", "received", "system",
                      {**detail, "via": via, "payload_sha256": sha256_hex(payload)})

    def add_replay(self, key: str, via: str, detail: dict) -> None:
        with self.transaction():
            now = self.clock.now()
            self.conn.execute(
                "INSERT INTO arrivals (key, at, via, kind) VALUES (?, ?, ?, 'replay')", (key, now, via)
            )
            audit.append(self.conn, now, key, "replay", {**detail, "via": via}, "system")

    def get_enquiry(self, key: str) -> Enquiry | None:
        row = self.conn.execute(
            "SELECT key, first_arrived_at, via, payload, state, reasons, identity, resolution, plan_sha, draft, error "
            "FROM enquiries WHERE key = ?",
            (key,),
        ).fetchone()
        return _enquiry(row) if row else None

    def enquiries(self, in_states=None) -> list:
        sql = (
            "SELECT key, first_arrived_at, via, payload, state, reasons, identity, resolution, plan_sha, draft, error "
            "FROM enquiries"
        )
        found = [_enquiry(r) for r in self.conn.execute(sql + " ORDER BY rowid")]
        if in_states is None:
            return found
        return [e for e in found if e.state in in_states]

    def set_fields(self, key: str, **values) -> None:
        allowed = {"identity", "resolution", "plan_sha", "draft", "error"}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"not a settable field: {sorted(unknown)}")
        encoded = {
            name: (canonical_json(v) if name in ("identity", "draft") and v is not None else v)
            for name, v in values.items()
        }
        assignments = ", ".join(f"{name} = ?" for name in encoded)
        with self.transaction():
            self.conn.execute(f"UPDATE enquiries SET {assignments} WHERE key = ?", (*encoded.values(), key))

    def arrival_counts(self) -> dict:
        counts = {"first": 0, "replay": 0, "resend": 0}
        for kind, n in self.conn.execute("SELECT kind, count(*) FROM arrivals GROUP BY kind"):
            counts[kind] = n
        return counts

    def state_counts(self) -> dict:
        counts = {name: 0 for name in states.STATES}
        for state, n in self.conn.execute("SELECT state, count(*) FROM enquiries GROUP BY state"):
            counts[state] = counts.get(state, 0) + n
        return counts

    # -- plans ----------------------------------------------------------------------------------

    def save_plan(self, sha: str, key: str, body: str) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO plans (sha, key, body, made_at) VALUES (?, ?, ?, ?)",
                (sha, key, body, self.clock.now()),
            )

    def plan_body(self, sha: str) -> str | None:
        row = self.conn.execute("SELECT body FROM plans WHERE sha = ?", (sha,)).fetchone()
        return row[0] if row else None

    # -- approvals ------------------------------------------------------------------------------

    def add_approval(self, key: str, plan_sha: str, by: str, state: str, reason: str = "") -> int:
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO approvals (key, plan_sha, by, at, state, reason) VALUES (?, ?, ?, ?, ?, ?)",
                (key, plan_sha, by, self.clock.now(), state, reason),
            )
            return cur.lastrowid

    def close_approval(self, approval_id: int, state: str, by: str, reason: str = "") -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE approvals SET state = ?, closed_by = ?, closed_at = ?, "
                "reason = CASE WHEN ? <> '' THEN ? ELSE reason END WHERE id = ?",
                (state, by, self.clock.now(), reason, reason, approval_id),
            )

    def live_approval(self, key: str) -> Approval | None:
        row = self.conn.execute(
            "SELECT id, key, plan_sha, by, at, state, reason, closed_by, closed_at FROM approvals "
            "WHERE key = ? AND state = 'approved'",
            (key,),
        ).fetchone()
        return Approval(*row) if row else None

    def approvals(self, key: str | None = None) -> list:
        sql = "SELECT id, key, plan_sha, by, at, state, reason, closed_by, closed_at FROM approvals"
        if key is None:
            return [Approval(*r) for r in self.conn.execute(sql + " ORDER BY id")]
        return [Approval(*r) for r in self.conn.execute(sql + " WHERE key = ? ORDER BY id", (key,))]

    # -- the ledger -----------------------------------------------------------------------------

    def ledger_get(self, key: str, op: str) -> LedgerRow | None:
        row = self.conn.execute(
            "SELECT key, op, plan_sha, status, crm_ref, how, at FROM ledger WHERE key = ? AND op = ?", (key, op)
        ).fetchone()
        return LedgerRow(*row) if row else None

    def ledger_rows(self, key: str | None = None) -> list:
        sql = "SELECT key, op, plan_sha, status, crm_ref, how, at FROM ledger"
        if key is None:
            return [LedgerRow(*r) for r in self.conn.execute(sql + " ORDER BY key, op")]
        return [LedgerRow(*r) for r in self.conn.execute(sql + " WHERE key = ? ORDER BY op", (key,))]

    def ledger_start(self, key: str, op: str, plan_sha: str) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO ledger (key, op, plan_sha, status, at) VALUES (?, ?, ?, 'started', ?) "
                "ON CONFLICT (key, op) DO UPDATE SET plan_sha = excluded.plan_sha, status = 'started', "
                "crm_ref = NULL, how = NULL, at = excluded.at",
                (key, op, plan_sha, self.clock.now()),
            )

    def ledger_done(self, key: str, op: str, crm_ref: str, how: str) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE ledger SET status = 'done', crm_ref = ?, how = ?, at = ? WHERE key = ? AND op = ?",
                (crm_ref, how, self.clock.now(), key, op),
            )


def _enquiry(row) -> Enquiry:
    key, first, via, payload, state, reasons, identity, resolution, plan_sha, draft, error = row
    return Enquiry(
        key=key,
        first_arrived_at=first,
        via=via,
        payload=json.loads(payload),
        state=state,
        reasons=json.loads(reasons),
        identity=json.loads(identity) if identity else None,
        resolution=resolution,
        plan_sha=plan_sha,
        draft=json.loads(draft) if draft else None,
        error=error,
    )
