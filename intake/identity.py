"""Section 3.3: the idempotency key, and matching a contact by exact keys only.

The key
    With a valid source and source_id: "<source>:<source_id>". Otherwise "hash:" and the first 32 hex
    digits of the sha256 of the canonical JSON of the normalised e-mail, phone digits, message and the
    UTC date of received_at. A key the desk already holds makes the arrival a replay.

Matching
    Contacts are found by the normalised e-mail and by the phone's digits, never by a similar name.
    The contacts found that way form one set:
        one contact ............ returning; but held: phone only (a person decides) when it was found by the
                                 phone alone and the e-mails do not agree: the enquiry gives an e-mail the
                                 contact does not have, or the contact has an e-mail the enquiry does not
                                 give. A shared office line must not attach a new person to someone else.
        two or more ............ held: two contacts (a person decides)
        none ................... a contact with the same normalised name AND company (both not empty)
                                 makes it held: possible duplicate; otherwise it is new.
    The system never merges contacts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import normalise, validate
from .canonical import sha256_hex

SOURCE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,80}$")
HASH_PREFIX = "hash"
HASH_HEX_DIGITS = 32


def source_key(fields: dict) -> str | None:
    """"<source>:<source_id>" when both are valid, else None. "hash" is not a valid source."""
    source = fields.get("source", "").strip().lower()
    source_id = fields.get("source_id", "").strip()
    if source == HASH_PREFIX or not SOURCE_PATTERN.match(source) or not SOURCE_ID_PATTERN.match(source_id):
        return None
    return f"{source}:{source_id}"


def key_basis(fields: dict) -> dict:
    """What the hashed key is computed over. An unreadable received_at is used as it was sent."""
    when = validate.parse_time(fields.get("received_at", ""))
    return {
        "date": when.date().isoformat() if when else fields.get("received_at", "").strip(),
        "email": normalise.email(fields.get("email", "")),
        "message": normalise.message(fields.get("message", "")),
        "phone": normalise.phone_digits(fields.get("phone", "")),
    }


def idempotency_key(fields: dict) -> str:
    return source_key(fields) or f"{HASH_PREFIX}:{sha256_hex(key_basis(fields))[:HASH_HEX_DIGITS]}"


@dataclass(frozen=True)
class Match:
    kind: str  # "new" | "returning" | "two_contacts" | "phone_only" | "possible_duplicate"
    contact: object = None  # the Contact, when returning
    by_email: tuple = ()
    by_phone: tuple = ()
    by_name: tuple = ()
    notes: tuple = field(default=())

    def describe(self) -> dict:
        """What identity found, as the desk and the plan record it."""
        found: dict = {"kind": self.kind}
        if self.contact is not None:
            found["contact"] = self.contact.id
        if self.by_email:
            found["by_email"] = list(self.by_email)
        if self.by_phone:
            found["by_phone"] = list(self.by_phone)
        if self.by_name:
            found["by_name_and_company"] = list(self.by_name)
        if self.notes:
            found["notes"] = list(self.notes)
        return found


def match_contact(fields: dict, crm) -> Match:
    """Apply the table of section 3.3 to the CRM as it is now. Reads only."""
    email = normalise.email(fields.get("email", ""))
    digits = normalise.phone_digits(fields.get("phone", ""))
    by_email = crm.find_by_email(email) if email else []
    by_phone = crm.find_by_phone(digits) if digits else []
    keyed = {c.id: c for c in by_email + by_phone}
    email_ids = tuple(sorted({c.id for c in by_email}, key=_id_order))
    phone_ids = tuple(sorted({c.id for c in by_phone}, key=_id_order))
    if len(keyed) == 1:
        (contact,) = keyed.values()
        if not email_ids and (email or normalise.email(contact.email)):
            return Match("phone_only", by_phone=phone_ids)
        return Match("returning", contact=contact, by_email=email_ids, by_phone=phone_ids)
    if len(keyed) > 1:
        return Match("two_contacts", by_email=email_ids, by_phone=phone_ids)
    name = normalise.name(fields.get("name", ""))
    company = normalise.name(fields.get("company", ""))
    if name and company:
        same = crm.find_by_name(name, company)
        if same:
            return Match("possible_duplicate", by_name=tuple(sorted({c.id for c in same}, key=_id_order)))
    return Match("new")


def _id_order(contact_id: str):
    """Numeric ids in numeric order, then any other ids as text."""
    return (0, int(contact_id), "") if contact_id.isdigit() else (1, 0, contact_id)
