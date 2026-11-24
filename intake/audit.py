"""The audit trail, append-only by the database itself.

Three triggers stand guard:
    audit_refuses_update ... no row is ever changed
    audit_refuses_delete ... no row is ever removed
    audit_refuses_unchained  a new row must name the last row's hash, and may not reuse a row number
                             (this also stops INSERT OR REPLACE, which skips delete triggers)

Each row also carries the sha256 of its own content and of the row before it. If the triggers are
dropped, or replaced, and a row is changed, `desk.py doctor` names the trigger and the row.

What this guards against: mistakes, bugs, and plain edits of the file by the program or by hand. What it
does not: a person who can open the database file can drop the triggers, change rows, recompute every
hash and put the triggers back as written. Nothing inside one file can tell that rewrite apart.
"""
from __future__ import annotations

import json
import sqlite3

from .canonical import canonical_json, sha256_hex

GENESIS = "0" * 64
TRIGGERS = ("audit_refuses_update", "audit_refuses_delete", "audit_refuses_unchained")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    key TEXT NOT NULL,
    event TEXT NOT NULL,
    detail TEXT NOT NULL,
    actor TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    row_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS audit_by_key ON audit (key, id);
CREATE TRIGGER IF NOT EXISTS audit_refuses_update BEFORE UPDATE ON audit
BEGIN
    SELECT RAISE(ABORT, 'the audit trail is append-only: a row cannot be changed');
END;
CREATE TRIGGER IF NOT EXISTS audit_refuses_delete BEFORE DELETE ON audit
BEGIN
    SELECT RAISE(ABORT, 'the audit trail is append-only: a row cannot be removed');
END;
CREATE TRIGGER IF NOT EXISTS audit_refuses_unchained BEFORE INSERT ON audit
WHEN NEW.prev_hash <> coalesce((SELECT row_hash FROM audit ORDER BY id DESC LIMIT 1), '{GENESIS}')
  OR EXISTS (SELECT 1 FROM audit WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'the audit trail is append-only: a new row must follow the last one');
END;
"""


def row_hash(prev_hash: str, at: str, key: str, event: str, detail: str, actor: str) -> str:
    return sha256_hex({"prev": prev_hash, "at": at, "key": key, "event": event, "detail": detail, "actor": actor})


def append(conn, at: str, key: str, event: str, detail: dict, actor: str) -> int:
    """Add one row. The caller holds the transaction, so the last hash cannot move underneath."""
    last = conn.execute("SELECT row_hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    prev = last[0] if last else GENESIS
    text = canonical_json(detail)
    digest = row_hash(prev, at, key, event, text, actor)
    cur = conn.execute(
        "INSERT INTO audit (at, key, event, detail, actor, prev_hash, row_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (at, key, event, text, actor, prev, digest),
    )
    return cur.lastrowid


def rows(conn, key: str | None = None) -> list:
    sql = "SELECT id, at, key, event, detail, actor, prev_hash, row_hash FROM audit"
    if key is None:
        return conn.execute(sql + " ORDER BY id").fetchall()
    return conn.execute(sql + " WHERE key = ? ORDER BY id", (key,)).fetchall()


def detail_of(row) -> dict:
    return json.loads(row[4])


def chain_problems(conn) -> list:
    """Every row whose hash or link does not add up. The removal of the very last row leaves no gap."""
    problems = []
    prev = GENESIS
    for row in rows(conn):
        row_id, at, key, event, detail, actor, prev_hash, digest = row
        if prev_hash != prev:
            problems.append(f"row {row_id} does not follow the row before it (a row was removed or changed)")
        if row_hash(prev_hash, at, key, event, detail, actor) != digest:
            problems.append(f"row {row_id} was changed after it was written")
        prev = digest
    return problems


def missing_triggers(conn) -> list:
    present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    return [name for name in TRIGGERS if name not in present]


def _written_triggers() -> dict:
    """Each trigger's SQL as SQLite stores it when SCHEMA creates it."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA)
        return dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"))
    finally:
        conn.close()


def trigger_problems(conn) -> list:
    """A trigger that is missing, or whose SQL is not the SQL this module writes (one that does nothing, say)."""
    present = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"))
    problems = []
    for name, sql in _written_triggers().items():
        if name not in present:
            problems.append(f"the trigger {name} is missing: the trail can be changed")
        elif present[name] != sql:
            problems.append(f"the trigger {name} is not as written: the trail may be changed")
    return problems
