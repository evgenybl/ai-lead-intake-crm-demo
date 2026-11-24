"""What an enquiry would write, as an exact list of operations (the plan).

A plan is canonical JSON; its sha256 is its identity, and an approval names that sha256. The plan holds
everything the person approves: what identity found, each operation with its values, and the
differences shown and not written. It is computed from the CRM as it is now, so the sha256 changes when
the CRM changes in a way that matters (the contact now exists, a field was filled meanwhile), and an
approval of the older plan no longer fits it.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import identity, normalise
from .canonical import sha256_hex
from .crm.base import CONTACT_FIELDS

CREATED = "created"  # the contact this plan's create_contact makes

_SAME = {
    "name": normalise.name,
    "company": normalise.name,
    "email": normalise.email,
    "phone": normalise.phone_digits,
}

HOLDS = {"two_contacts": "held_two_contacts", "phone_only": "held_possible_duplicate",
         "possible_duplicate": "held_possible_duplicate"}


@dataclass(frozen=True)
class Assessment:
    state: str  # awaiting_approval | held_two_contacts | held_possible_duplicate
    identity: dict
    plan: dict | None
    sha: str | None


def assess(key: str, fields: dict, resolution: str | None, crm) -> Assessment:
    """Identity, then the plan. Reads the CRM; never writes to it."""
    match = identify(fields, resolution, crm)
    found = match.describe()
    if resolution:
        found["resolution"] = resolution
    if match.kind in HOLDS:
        return Assessment(HOLDS[match.kind], found, None, None)
    plan = build_plan(key, fields, match, found, crm)
    return Assessment("awaiting_approval", found, plan, sha256_hex(plan))


def identify(fields: dict, resolution: str | None, crm) -> identity.Match:
    """The matching rules of intake/identity.py, after a person's resolution when there is one that still applies."""
    notes = []
    if resolution == "new":
        email = normalise.email(fields.get("email", ""))
        holders = crm.find_by_email(email) if email else []
        if not holders:
            return identity.Match("new", notes=("a person resolved it as new",))
        notes.append(f"the resolution 'new' no longer applies: the e-mail now belongs to contact {holders[0].id}")
    elif resolution and resolution.startswith("contact:"):
        contact_id = resolution.split(":", 1)[1]
        contact = crm.get_contact(contact_id)
        if contact is not None:
            return identity.Match("returning", contact=contact, notes=(f"a person resolved it as contact {contact_id}",))
        notes.append(f"the resolution no longer applies: contact {contact_id} is not in the CRM")
    match = identity.match_contact(fields, crm)
    if notes:
        return identity.Match(match.kind, match.contact, match.by_email, match.by_phone, match.by_name, tuple(notes))
    return match


def contact_values(fields: dict) -> dict:
    return {
        "name": fields.get("name", "").strip(),
        "email": normalise.email(fields.get("email", "")),
        "phone": fields.get("phone", "").strip(),
        "company": fields.get("company", "").strip(),
    }


def note_body(key: str, fields: dict) -> str:
    """The note's first line is the enquiry's key: the write gate finds it again by that line."""
    source = fields.get("source", "").strip() or "an unnamed source"
    received = fields.get("received_at", "").strip()
    return "\n".join([key, f"Enquiry from {source}, received {received}.", "", fields.get("message", "").strip()])


def task_body(key: str, fields: dict) -> str:
    return "\n".join([key, f"Answer the enquiry from {fields.get('name', '').strip()}."])


def compare(contact, values: dict, crm) -> tuple:
    """Fields to fill (the contact's are empty) and differences to show (never overwritten)."""
    fill, shown = {}, []
    for name in CONTACT_FIELDS:
        new = values[name]
        if not new:
            continue
        old = getattr(contact, name)
        if not old.strip():
            if name == "email":
                others = [c.id for c in crm.find_by_email(new) if c.id != contact.id]
                if others:
                    shown.append({"field": name, "crm": old, "enquiry": new,
                                  "not_filled": f"the e-mail belongs to contact {others[0]}"})
                    continue
            fill[name] = new
        elif _SAME[name](old) != _SAME[name](new):
            shown.append({"field": name, "crm": old, "enquiry": new})
    return fill, shown


def build_plan(key: str, fields: dict, match: identity.Match, found: dict, crm) -> dict:
    values = contact_values(fields)
    operations, shown = [], []
    if match.kind == "new":
        target = CREATED
        operations.append({"op": "create_contact", "fields": {k: v for k, v in values.items() if v}})
    else:
        target = match.contact.id
        fill, shown = compare(match.contact, values, crm)
        if fill:
            operations.append({"op": "fill_empty_fields", "contact": target, "fields": fill})
    operations.append({"op": "add_note", "contact": target, "body": note_body(key, fields)})
    operations.append({"op": "add_task", "contact": target, "body": task_body(key, fields)})
    return {"enquiry": key, "identity": found, "operations": operations, "shown": shown}
