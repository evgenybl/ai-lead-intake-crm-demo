"""What the desk needs from a CRM, and nothing more.

Reads are used by identity, the planner and the write gate's look-ups. The four writes are called by
intake/gate.py only; tests/test_rules.py fails if any other module of intake/ calls one.
"""
from __future__ import annotations

from dataclasses import dataclass

CONTACT_FIELDS = ("name", "email", "phone", "company")

CAPABILITIES = (
    "find a contact by e-mail",
    "find a contact by phone",
    "find a contact by name and company",
    "find a contact by origin key",
    "create a contact",
    "fill empty fields",
    "list, search and add notes",
    "list and add tasks",
)


@dataclass(frozen=True)
class Contact:
    id: str
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    origin_key: str = ""  # the key of the enquiry that created it; "" for a contact made elsewhere


@dataclass(frozen=True)
class Note:
    id: str
    contact_id: str
    body: str

    @property
    def first_line(self) -> str:
        return self.body.split("\n", 1)[0].strip()


@dataclass(frozen=True)
class Task:
    id: str
    contact_id: str
    body: str

    @property
    def first_line(self) -> str:
        return self.body.split("\n", 1)[0].strip()


class CRMError(Exception):
    """A CRM call failed. Nothing is assumed about whether it took effect. The message never holds a credential."""


class CRMConflict(CRMError):
    """The CRM refused a write that would break its own rule of one contact per e-mail."""


class CRM:
    name = ""
    label = ""

    # -- reads --
    def find_by_email(self, email: str) -> list:
        raise NotImplementedError

    def find_by_phone(self, digits: str) -> list:
        raise NotImplementedError

    def find_by_name(self, name_norm: str, company_norm: str) -> list:
        """Contacts whose normalised name AND normalised company equal the two given."""
        raise NotImplementedError

    def find_by_origin_key(self, key: str) -> list:
        raise NotImplementedError

    def get_contact(self, contact_id: str):
        raise NotImplementedError

    def list_notes(self, contact_id: str) -> list:
        raise NotImplementedError

    def search_notes(self, text: str) -> list:
        """A search may lag behind writes; the gate confirms every hit by the note's first line."""
        raise NotImplementedError

    def list_tasks(self, contact_id: str) -> list:
        raise NotImplementedError

    # -- writes: called by intake/gate.py only --
    def create_contact(self, fields: dict, origin_key: str) -> Contact:
        raise NotImplementedError

    def fill_empty_fields(self, contact_id: str, fields: dict) -> dict:
        """Write each field only where the contact's field is empty. Returns {"filled": [...], "kept": [...]}."""
        raise NotImplementedError

    def add_note(self, contact_id: str, body: str) -> Note:
        raise NotImplementedError

    def add_task(self, contact_id: str, body: str) -> Task:
        raise NotImplementedError

    # -- health --
    def check(self) -> tuple:
        """(what was checked, a short detail): "opens" for a local file read, "connected" for a server that
        answered, or "error"."""
        raise NotImplementedError

    def close(self) -> None:
        pass
