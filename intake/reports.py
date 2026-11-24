"""The text the commands print: report, trail, plan, status and doctor. Plain lines, no colour.

Text that came from outside the desk (a sender's message, a model's answer, a CRM's error) is printed through
`printable`, so it can never move the cursor, clear the screen or start a line of its own."""
from __future__ import annotations

import json
import unicodedata

from . import audit, validate
from .doctor import PROMISES


_HIDDEN = ("Cc", "Cf", "Co", "Cs", "Zl", "Zp")


def printable(text) -> str:
    """One line a terminal shows as it is: each control, format or line-separating character as its escape."""
    return "".join(repr(c)[1:-1] if unicodedata.category(c) in _HIDDEN else c for c in str(text))


def report_lines(store) -> list:
    counts = store.state_counts()
    arrivals = store.arrival_counts()
    arrived = arrivals["first"] + arrivals["replay"] + arrivals["resend"]
    enquiries = sum(counts.values())
    lines = [
        f"arrived                     {arrived:>4}",
        f"  first arrivals            {arrivals['first']:>4}",
        f"  replays                   {arrivals['replay']:>4}",
        f"  corrected resends         {arrivals['resend']:>4}",
        "enquiries by state",
    ]
    lines += [f"  {name:<26}{n:>4}" for name, n in counts.items()]
    lines.append(f"  {'total':<26}{enquiries:>4}")
    if enquiries == arrivals["first"]:  # every first arrival is one enquiry in one state; the rest are replays
        lines.append(f"the states add up: yes: {enquiries} enquiries + {arrivals['replay']} replays"
                     f"{resends_text(arrivals['resend'])} = {arrived} arrived")
    else:
        lines.append(f"the states add up: NO: {enquiries} enquiries in their states, "
                     f"but {arrivals['first']} first arrivals (run desk.py doctor)")
    failed = store.enquiries(in_states=("apply_failed",))
    if failed:
        lines.append("apply_failed, named:")
        lines += [f"  {e.key}: {printable(e.error)}" for e in failed]
    else:
        lines.append("apply_failed: none")
    different = different_replays(store)
    if different:
        lines.append("replayed with different content (kept as replays; the first content stands):")
        for key, at, differs in different:
            verb = "differs" if len(differs) == 1 else "differ"
            lines.append(f"  {key} at {at}: {', '.join(differs)} {verb} from the first copy")
    else:
        lines.append("replayed with different content: none")
    return lines


def resends_text(n: int) -> str:
    """The corrected resends in the sentence that adds the arrivals up; nothing when there are none."""
    return f" + {n} corrected resend{'' if n == 1 else 's'}" if n else ""


def different_replays(store, key: str | None = None) -> list:
    """(key, time, {field: {"first", "this_time"}}) for every replay whose content differed from the first."""
    found = []
    for row in audit.rows(store.conn, key):
        if row[3] == "replay":
            detail = json.loads(row[4])
            if detail.get("differs"):
                found.append((row[2], row[1], detail["differs"]))
    return found


def trail_lines(store, key: str) -> list:
    enquiry = store.get_enquiry(key)
    lines = [f"trail of {key} (state now: {enquiry.state})"]
    for row in audit.rows(store.conn, key):
        row_id, at, _, event, detail, actor = row[:6]
        lines.append(f"#{row_id:<5} {at}  {printable(actor):<26} {event:<20} {_brief(json.loads(detail))}")
    return lines


def _brief(detail: dict) -> str:
    """key=value pairs in name order; a sha256 shortened to its first 12 hex digits."""
    parts = []
    for name in sorted(detail):
        value = detail[name]
        if isinstance(value, str):
            is_sha = len(value) == 64 and all(c in "0123456789abcdef" for c in value)
            text = value[:12] if is_sha else value
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        parts.append(f"{name}={printable(text)}")
    return " ".join(parts)


def draft_line(draft: dict) -> str:
    """The model step in one line: what it suggested, or why there is no AI text. A reply draft is never sent."""
    outcome = draft.get("outcome")
    label = printable(draft.get("label"))
    if outcome == "used":
        return (f"draft ({label}; intent {printable(draft.get('intent'))}, urgency {printable(draft.get('urgency'))}):"
                f" {printable(draft.get('summary'))}")
    if outcome == "stand-in":
        return f"draft ({label}): {printable(draft.get('summary'))}"
    if outcome == "refused":
        return f"draft ({label}): AI draft refused: {printable(draft.get('reason'))}"
    return f"draft ({label}): no AI text: {printable(draft.get('reason'))}"


def identity_text(found: dict) -> str:
    """What identity found, in plain words, as `plan` prints it."""
    kind = found.get("kind")
    if kind == "new":
        text = "new: a person decided so" if found.get("resolution") == "new" else \
            "new: no contact has this e-mail or phone"
    elif kind == "returning":
        ways = [w for w, k in (("e-mail", "by_email"), ("phone", "by_phone")) if found.get(k)]
        how = " and ".join(ways) if ways else "a person's decision"
        text = f"returning: contact {found.get('contact')}, found by {how}"
    elif kind == "two_contacts":
        text = (f"held: the e-mail belongs to contact {', '.join(found.get('by_email', [])) or 'none'}, the phone "
                f"to contact {', '.join(found.get('by_phone', [])) or 'none'}")
    elif kind == "phone_only":
        text = f"held: only the phone matches contact {', '.join(found.get('by_phone', []))}, and the e-mails differ"
    elif kind == "possible_duplicate":
        text = f"held: contact {', '.join(found.get('by_name_and_company', []))} has the same name and company"
    else:
        text = _brief(found)
    notes = "; ".join(found.get("notes", []))
    return printable(text + (f" ({notes})" if notes else ""))


def enquiry_lines(store, enquiry, crm_note: str = "", full: bool = False) -> list:
    lines = [f"{enquiry.key}  [{enquiry.state}]"]
    found = enquiry.identity or {}
    if found:
        lines.append(f"  identity: {identity_text(found)}")
    if enquiry.draft:
        lines.append("  " + draft_line(enquiry.draft))
    if enquiry.state in ("held_two_contacts", "held_possible_duplicate"):
        candidates = sorted(set(found.get("by_email", []) + found.get("by_phone", []) + found.get("by_name_and_company", [])))
        lines.append(f"  waits for a person: desk.py resolve {enquiry.key} --as new|contact:<id> --by <name>"
                     f"  (contacts found: {', '.join(candidates)})")
    if enquiry.state == "received" and enquiry.error:
        lines.append(f"  not assessed yet: {printable(enquiry.error)}")
    if enquiry.plan_sha and enquiry.state in ("awaiting_approval", "approved", "apply_failed"):
        plan = json.loads(store.plan_body(enquiry.plan_sha))
        lines.append(f"  plan {enquiry.plan_sha}")
        for number, op in enumerate(plan["operations"], start=1):
            if full and "body" in op:
                lines.append(f"    {number}. {op['op']} on contact {op['contact']}, exactly this text:")
                lines += [("         " + printable(line)).rstrip() for line in op["body"].split("\n")]
            else:
                lines.append(f"    {number}. {_operation(op)}")
        for diff in plan["shown"]:
            extra = f" ({diff['not_filled']})" if "not_filled" in diff else ", not overwritten"
            lines.append(f"    shown for a person: {diff['field']}: the CRM has {diff['crm']!r}, "
                         f"the enquiry says {diff['enquiry']!r}{extra}")
        for entry in plan.get("already_written", []):
            lines.append("    already in the CRM, written by an apply that did not complete, not written again: "
                         + describe_write(entry.get("op"), entry.get("ref"), entry.get("contact")))
        listed = {entry.get("op") for entry in plan.get("already_written", [])}
        so_far = writes_text(store.ledger_rows(enquiry.key), leave_out=listed)
        if so_far:
            lines.append(f"  written so far for this enquiry: {so_far}")
        if enquiry.state == "awaiting_approval":
            lines.append(f"  to approve: desk.py approve {enquiry.key} --by <name> --plan {enquiry.plan_sha[:12]}")
    if enquiry.state == "apply_failed":
        lines.append(f"  apply_failed: {printable(enquiry.error)}")
        lines.append(f"  to retry: desk.py apply --key {enquiry.key} (it completes what is missing, writes nothing twice)")
        lines.append(f"  if a retry keeps failing because the CRM changed (the e-mail now belongs to another contact): "
                     f"desk.py withdraw {enquiry.key} --by <name>, then desk.py plan: it is planned again and "
                     "waits for a new approval")
    if crm_note:
        lines.append(f"  {crm_note}")
    return lines


def _operation(op: dict) -> str:
    name = op["op"]
    if name == "create_contact":
        return "create_contact " + ", ".join(f"{k}={v!r}" for k, v in sorted(op["fields"].items()))
    if name == "fill_empty_fields":
        return f"fill_empty_fields on contact {op['contact']}: " + ", ".join(
            f"{k}={v!r}" for k, v in sorted(op["fields"].items()))
    first, _, rest = op["body"].partition("\n")
    more = len(rest.splitlines())
    return f"{name} on contact {op['contact']}: first line {first!r}, then {more} more line{'' if more == 1 else 's'}"


WRITE_ORDER = ("create_contact", "fill_empty_fields", "add_note", "add_task")


def describe_write(op: str, ref, contact) -> str:
    """One write the desk made, and where: "contact 11 created", "note 5 on contact 11"."""
    if op == "create_contact":
        return f"contact {contact or ref} created"
    if op == "fill_empty_fields":
        return f"empty fields of contact {contact or ref} filled"
    noun = {"add_note": "note", "add_task": "task"}.get(op, op)
    return f"{noun} {ref}" + (f" on contact {contact}" if contact else "")


def writes_text(ledger_rows, leave_out=()) -> str:
    """Every write the ledger records as done, in the plan's order; empty when there is none."""
    done = [row for row in ledger_rows if row.status == "done" and row.op not in leave_out]
    done.sort(key=lambda row: WRITE_ORDER.index(row.op) if row.op in WRITE_ORDER else len(WRITE_ORDER))
    return "; ".join(describe_write(row.op, row.crm_ref, row.contact) for row in done)


def written_before(store, enquiry) -> str:
    """For an enquiry that was not applied: what an apply that did not complete wrote, as part of a sentence
    ("after 1 of 3 steps had been written: contact 11 created"); empty when it wrote nothing."""
    rows = [row for row in store.ledger_rows(enquiry.key) if row.status == "done"]
    if not rows:
        return ""
    what = writes_text(rows)
    if enquiry.plan_sha and all(row.plan_sha == enquiry.plan_sha for row in rows):
        total = len(json.loads(store.plan_body(enquiry.plan_sha)).get("operations", []))
        return f"after {len(rows)} of {total} steps had been written: {what}"
    return f"after an apply of an earlier plan had written: {what}"


def refusal_text(reasons) -> str:
    return "; ".join(validate.describe(code) for code in reasons)


def doctor_lines(checks: list) -> list:
    lines = []
    for promise, title in PROMISES.items():
        lines.append(f"promise {promise}: {title}")
        for check in (c for c in checks if c.promise == promise):
            if check.problems:
                lines.append(f"  FAIL  {check.name}")
                lines += [f"          {problem}" for problem in check.problems]
            elif check.note.startswith("not checked"):
                lines.append(f"  --    {check.name}: {check.note}")
            else:
                lines.append(f"  ok    {check.name}" + (f" ({check.note})" if check.note else ""))
    failed = [c for c in checks if c.problems]
    run = [c for c in checks if not c.note.startswith("not checked")]
    lines.append(f"doctor: {len(run)} of {len(run)} checks passed" if not failed
                 else f"doctor: {len(failed)} of {len(run)} checks FAILED")
    return lines
