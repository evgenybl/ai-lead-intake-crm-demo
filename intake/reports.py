"""The text the commands print: report, trail, plan, status and doctor. Plain lines, no colour."""
from __future__ import annotations

import json

from . import audit, validate
from .doctor import PROMISES


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
        lines += [f"  {e.key}: {e.error}" for e in failed]
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
        lines.append(f"#{row_id:<5} {at}  {actor:<26} {event:<20} {_brief(json.loads(detail))}")
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
        parts.append(f"{name}={text}")
    return " ".join(parts)


def draft_line(draft: dict) -> str:
    """The model step in one line: what it suggested, or why there is no AI text. A reply draft is never sent."""
    outcome = draft.get("outcome")
    if outcome == "used":
        return (f"draft ({draft['label']}; intent {draft.get('intent')}, urgency {draft.get('urgency')}): "
                f"{draft.get('summary')}")
    if outcome == "stand-in":
        return f"draft ({draft['label']}): {draft.get('summary')}"
    if outcome == "refused":
        return f"draft ({draft['label']}): AI draft refused: {draft.get('reason')}"
    return f"draft ({draft['label']}): no AI text: {draft.get('reason')}"


def enquiry_lines(store, enquiry, crm_note: str = "") -> list:
    lines = [f"{enquiry.key}  [{enquiry.state}]"]
    found = enquiry.identity or {}
    if found:
        lines.append(f"  identity: {_brief(found)}")
    if enquiry.draft:
        lines.append("  " + draft_line(enquiry.draft))
    if enquiry.state in ("held_two_contacts", "held_possible_duplicate"):
        candidates = sorted(set(found.get("by_email", []) + found.get("by_phone", []) + found.get("by_name_and_company", [])))
        lines.append(f"  waits for a person: desk.py resolve {enquiry.key} --as new|contact:<id> --by <name>"
                     f"  (contacts found: {', '.join(candidates)})")
    if enquiry.state == "received" and enquiry.error:
        lines.append(f"  not assessed yet: {enquiry.error}")
    if enquiry.plan_sha and enquiry.state in ("awaiting_approval", "approved", "apply_failed"):
        plan = json.loads(store.plan_body(enquiry.plan_sha))
        lines.append(f"  plan {enquiry.plan_sha}")
        for number, op in enumerate(plan["operations"], start=1):
            lines.append(f"    {number}. {_operation(op)}")
        for diff in plan["shown"]:
            extra = f" ({diff['not_filled']})" if "not_filled" in diff else ", not overwritten"
            lines.append(f"    shown for a person: {diff['field']}: the CRM has {diff['crm']!r}, "
                         f"the enquiry says {diff['enquiry']!r}{extra}")
        if enquiry.state == "awaiting_approval":
            lines.append(f"  to approve: desk.py approve {enquiry.key} --by <name> --plan {enquiry.plan_sha[:12]}")
    if enquiry.state == "apply_failed":
        lines.append(f"  apply_failed: {enquiry.error}")
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
