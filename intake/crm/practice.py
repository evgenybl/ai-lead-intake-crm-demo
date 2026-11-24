"""The practice CRM: contacts, notes and tasks in a second SQLite file. Local: it really runs.

One contact per normalised e-mail is held by a unique index (`contacts_one_per_email`). The index is
the CRM's own rule; the desk's identity rules and the write gate never rely on it alone.
"""
from __future__ import annotations

import functools
import os
import sqlite3

from .. import normalise
from .base import CONTACT_FIELDS, CRM, Contact, CRMConflict, CRMError, Note, Task

UNIQUE_EMAIL_INDEX = "contacts_one_per_email"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    origin_key TEXT NOT NULL DEFAULT '',
    email_norm TEXT,
    phone_digits TEXT NOT NULL DEFAULT '',
    name_norm TEXT NOT NULL DEFAULT '',
    company_norm TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS {UNIQUE_EMAIL_INDEX} ON contacts (email_norm) WHERE email_norm IS NOT NULL;
CREATE INDEX IF NOT EXISTS contacts_by_phone ON contacts (phone_digits);
CREATE INDEX IF NOT EXISTS contacts_by_name ON contacts (name_norm, company_norm);
CREATE INDEX IF NOT EXISTS contacts_by_origin ON contacts (origin_key);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts (id),
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts (id),
    body TEXT NOT NULL
);
"""

_CONTACT_COLUMNS = "id, name, email, phone, company, origin_key"


def _derived(values: dict) -> dict:
    email = normalise.email(values.get("email", ""))
    return {
        "email_norm": email or None,
        "phone_digits": normalise.phone_digits(values.get("phone", "")),
        "name_norm": normalise.name(values.get("name", "")),
        "company_norm": normalise.name(values.get("company", "")),
    }


def _contact(row) -> Contact:
    return Contact(str(row[0]), row[1], row[2], row[3], row[4], row[5])


def _as_crm_error(method):
    """A file that is locked by another program, or cannot be read, is a CRM that did not answer: CRMError."""
    @functools.wraps(method)
    def wrapped(self, *args):
        try:
            return method(self, *args)
        except sqlite3.Error as error:
            raise CRMError(f"the practice CRM file could not be used ({error})") from None
    return wrapped


def _row_id(contact_id) -> int | None:
    """The practice CRM's ids are numbers; the adapter interface passes them as text."""
    text = str(contact_id)
    return int(text) if text.isdigit() else None


class PracticeCRM(CRM):
    name = "practice"
    label = "Local: it really runs"

    def __init__(self, path: str, create: bool = False):
        if create and os.path.exists(path):
            raise CRMError(f"a practice CRM is already at {path}")
        if not create and not os.path.exists(path):
            raise CRMError(f"no practice CRM at {path}: run `desk.py init` first")
        self.path = path
        try:
            self.conn = sqlite3.connect(path, isolation_level=None)
            self.conn.execute("PRAGMA foreign_keys = ON")
            if create:
                self.conn.executescript(SCHEMA)
        except sqlite3.Error as error:
            raise CRMError(f"the practice CRM file could not be used ({error})") from None

    def close(self) -> None:
        self.conn.close()

    # -- reads --
    def _contacts(self, where: str, args: tuple) -> list:
        sql = f"SELECT {_CONTACT_COLUMNS} FROM contacts WHERE {where} ORDER BY id"
        return [_contact(r) for r in self.conn.execute(sql, args)]

    @_as_crm_error
    def find_by_email(self, email: str) -> list:
        return self._contacts("email_norm = ?", (normalise.email(email),))

    @_as_crm_error
    def find_by_phone(self, digits: str) -> list:
        return self._contacts("phone_digits = ? AND phone_digits <> ''", (digits,))

    @_as_crm_error
    def find_by_name(self, name_norm: str, company_norm: str) -> list:
        return self._contacts(
            "name_norm = ? AND company_norm = ? AND name_norm <> '' AND company_norm <> ''", (name_norm, company_norm)
        )

    @_as_crm_error
    def find_by_origin_key(self, key: str) -> list:
        return self._contacts("origin_key = ? AND origin_key <> ''", (key,))

    @_as_crm_error
    def get_contact(self, contact_id: str):
        found = self._contacts("id = ?", (_row_id(contact_id),))
        return found[0] if found else None

    @_as_crm_error
    def list_notes(self, contact_id: str) -> list:
        rows = self.conn.execute("SELECT id, contact_id, body FROM notes WHERE contact_id = ? ORDER BY id",
                                 (_row_id(contact_id),))
        return [Note(str(r[0]), str(r[1]), r[2]) for r in rows]

    @_as_crm_error
    def search_notes(self, text: str) -> list:
        rows = self.conn.execute("SELECT id, contact_id, body FROM notes WHERE instr(body, ?) > 0 ORDER BY id", (text,))
        return [Note(str(r[0]), str(r[1]), r[2]) for r in rows]

    @_as_crm_error
    def list_tasks(self, contact_id: str) -> list:
        rows = self.conn.execute("SELECT id, contact_id, body FROM tasks WHERE contact_id = ? ORDER BY id",
                                 (_row_id(contact_id),))
        return [Task(str(r[0]), str(r[1]), r[2]) for r in rows]

    # -- writes: called by intake/gate.py only --
    @_as_crm_error
    def create_contact(self, fields: dict, origin_key: str) -> Contact:
        values = {name: str(fields.get(name, "")).strip() for name in CONTACT_FIELDS}
        values["email"] = normalise.email(values["email"])
        derived = _derived(values)
        try:
            cur = self.conn.execute(
                "INSERT INTO contacts (name, email, phone, company, origin_key, email_norm, phone_digits, name_norm, "
                "company_norm) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (values["name"], values["email"], values["phone"], values["company"], origin_key,
                 derived["email_norm"], derived["phone_digits"], derived["name_norm"], derived["company_norm"]),
            )
        except sqlite3.IntegrityError:
            raise CRMConflict("the practice CRM already has a contact with this e-mail") from None
        return self.get_contact(str(cur.lastrowid))

    @_as_crm_error
    def fill_empty_fields(self, contact_id: str, fields: dict) -> dict:
        unknown = sorted(set(fields) - set(CONTACT_FIELDS))
        if unknown:
            raise CRMError(f"not a contact field: {unknown}")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            contact = self.get_contact(contact_id)
            if contact is None:
                raise CRMError(f"no contact {contact_id}")
            current = {name: getattr(contact, name) for name in CONTACT_FIELDS}
            filled, kept = [], []
            for name in sorted(fields):
                value = normalise.email(fields[name]) if name == "email" else str(fields[name]).strip()
                if current[name].strip():
                    kept.append(name)
                elif value:
                    current[name] = value
                    filled.append(name)
            derived = _derived(current)
            self.conn.execute(
                "UPDATE contacts SET name = ?, email = ?, phone = ?, company = ?, email_norm = ?, phone_digits = ?, "
                "name_norm = ?, company_norm = ? WHERE id = ?",
                (current["name"], current["email"], current["phone"], current["company"], derived["email_norm"],
                 derived["phone_digits"], derived["name_norm"], derived["company_norm"], _row_id(contact_id)),
            )
        except sqlite3.IntegrityError:
            self.conn.execute("ROLLBACK")
            raise CRMConflict("the practice CRM already has a contact with this e-mail") from None
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        return {"filled": filled, "kept": kept}

    @_as_crm_error
    def add_note(self, contact_id: str, body: str) -> Note:
        if self.get_contact(contact_id) is None:
            raise CRMError(f"no contact {contact_id}")
        cur = self.conn.execute("INSERT INTO notes (contact_id, body) VALUES (?, ?)", (_row_id(contact_id), body))
        return Note(str(cur.lastrowid), str(contact_id), body)

    @_as_crm_error
    def add_task(self, contact_id: str, body: str) -> Task:
        if self.get_contact(contact_id) is None:
            raise CRMError(f"no contact {contact_id}")
        cur = self.conn.execute("INSERT INTO tasks (contact_id, body) VALUES (?, ?)", (_row_id(contact_id), body))
        return Task(str(cur.lastrowid), str(contact_id), body)

    # -- setup and inspection (not writes of the desk) --
    def load_starting_contacts(self, contacts: list) -> int:
        """Fill a new practice CRM with its starting set: the contacts a business already had."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for contact in contacts:
                values = {name: str(contact.get(name, "")).strip() for name in CONTACT_FIELDS}
                values["email"] = normalise.email(values["email"])
                derived = _derived(values)
                self.conn.execute(
                    "INSERT INTO contacts (name, email, phone, company, email_norm, phone_digits, name_norm, "
                    "company_norm) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (values["name"], values["email"], values["phone"], values["company"], derived["email_norm"],
                     derived["phone_digits"], derived["name_norm"], derived["company_norm"]),
                )
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")
        return len(contacts)

    def counts(self) -> dict:
        return {
            table: self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("contacts", "notes", "tasks")
        }

    def everything(self) -> dict:
        """Every contact, note and task, for `desk.py doctor`."""
        return {
            "contacts": self._contacts("1 = 1", ()),
            "notes": [Note(str(r[0]), str(r[1]), r[2]) for r in self.conn.execute("SELECT id, contact_id, body FROM notes ORDER BY id")],
            "tasks": [Task(str(r[0]), str(r[1]), r[2]) for r in self.conn.execute("SELECT id, contact_id, body FROM tasks ORDER BY id")],
        }

    def check(self) -> tuple:
        try:
            counts = self.counts()
        except sqlite3.Error as error:
            return "error", f"the file cannot be read ({error})"
        return "opens", f"{counts['contacts']} contacts, {counts['notes']} notes, {counts['tasks']} tasks"
