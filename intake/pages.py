"""The desk pages, rendered on the server as plain HTML. There is no script on any page.

Every value that comes from an enquiry, a person or a model passes through esc() before it reaches the
page; tests/test_pages.py puts markup in a message, a name and a model's answer and finds it shown as text.

The same functions serve `desk.py serve` (links under /desk) and `desk.py render --out` (relative links
between files, and never a form: a file cannot take a decision).
"""
from __future__ import annotations

import hashlib
import html
import json

from . import audit, reports, validate
from .identity import rests_on_the_phone_alone
from .reports import different_replays

STATE_WORDS = {
    "awaiting_approval": "Waiting for your approval",
    "held_two_contacts": "Held: the e-mail and the phone belong to two different contacts",
    "held_possible_duplicate": "Held: might be the same person as an existing contact",
    "apply_failed": "Writing to the CRM did not complete; it can be retried",
    "approved": "Approved; not written to the CRM yet",
    "received": "Received; not checked against the CRM yet, because the CRM could not be read",
    "applied": "Written to the CRM",
    "rejected": "Rejected by a person",
    "refused": "Not taken: it did not pass the checks at the door",
}
STATE_ORDER = tuple(STATE_WORDS)

CSS = """
:root{color-scheme:light}
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;max-width:62rem;margin:1.5rem auto;padding:0 1rem;
color:#1f2328;background:#ffffff;line-height:1.45}
h1{margin-bottom:.2rem}h2{margin-top:2rem;border-bottom:1px solid #d0d7de;padding-bottom:.2rem}
.note{color:#57606a;font-size:.92rem}
table{border-collapse:collapse;margin:.5rem 0}td,th{border:1px solid #d0d7de;padding:.3rem .6rem;text-align:left;
vertical-align:top}td.n{text-align:right;font-variant-numeric:tabular-nums}
.card{border:1px solid #d0d7de;border-radius:6px;padding:.6rem 1rem;margin:.8rem 0;background:#f6f8fa}
.card h4{margin:.2rem 0}.state{font-weight:600}.waiting{color:#9a6700}.done{color:#1a7f37}.stop{color:#57606a}
blockquote{margin:.4rem 0;padding:.4rem .8rem;background:#ffffff;border-left:4px solid #d0d7de;white-space:pre-wrap}
pre{white-space:pre-wrap;background:#ffffff;border:1px solid #d0d7de;padding:.4rem .6rem;margin:.3rem 0}
.label{display:inline-block;border:1px solid #57606a;border-radius:4px;padding:0 .4rem;font-size:.85rem}
.ok{color:#1a7f37}.bad{color:#cf222e}
form{background:#ffffff;border:1px solid #d0d7de;padding:.5rem .8rem;margin:.4rem 0}
code{font-family:ui-monospace,Consolas,monospace;font-size:.88rem}
"""


def esc(value) -> str:
    """Text for the page: every <, >, &, " and ' escaped. Not a string: shown as its JSON."""
    if value is None:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return html.escape(value, quote=True)


SLUG_HASH_DIGITS = 8


def slug(key: str) -> str:
    """A key as a file or path name: ":" written as "~", then "-" and 8 hex digits of the key's sha256.

    Neither a source nor a source_id may hold "~". The hash keeps two keys that differ only in letter case
    (a source_id is case-sensitive) apart on a file system that is not, as Windows' and macOS' usually are."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:SLUG_HASH_DIGITS]
    return f"{key.replace(':', '~')}-{digest}"


def unslug(name: str) -> str | None:
    """The key a slug names, or None when its hash does not fit it."""
    base, dash, digest = name.rpartition("-")
    key = base.replace("~", ":")
    return key if dash and slug(key) == name else None


class ServerLinks:
    def desk(self) -> str:
        return "/desk"

    def enquiry(self, key: str) -> str:
        return "/desk/enquiry/" + slug(key)


class FileLinks:
    """Links between rendered files: index.html, and enquiry/<key>.html beside each other."""

    def __init__(self, inside_enquiry_folder: bool):
        self.inside = inside_enquiry_folder

    def desk(self) -> str:
        return "../index.html" if self.inside else "index.html"

    def enquiry(self, key: str) -> str:
        return slug(key) + ".html" if self.inside else "enquiry/" + slug(key) + ".html"


class Decisions:
    """Approve and Reject as forms only when the server was started with --allow-decisions."""

    def __init__(self, token: str | None = None):
        self.token = token

    @property
    def active(self) -> bool:
        return self.token is not None


def _page(title: str, body: list) -> str:
    return "\n".join([
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(title)}</title><style>{CSS.strip()}</style></head><body>",
        *body,
        "</body></html>",
        "",
    ])


def _state_class(state: str) -> str:
    if state in ("awaiting_approval", "held_two_contacts", "held_possible_duplicate", "apply_failed", "received"):
        return "waiting"
    return "done" if state == "applied" else "stop"


def counts_section(store) -> list:
    counts = store.state_counts()
    arrivals = store.arrival_counts()
    enquiries = sum(counts.values())
    arrived = arrivals["first"] + arrivals["replay"] + arrivals["resend"]
    rows = [f'<tr><td>{esc(STATE_WORDS[s])}</td><td class="n">{counts[s]}</td></tr>' for s in STATE_ORDER]
    out = ["<h2>The counts</h2>", "<table><tr><th>State</th><th>Enquiries</th></tr>", *rows,
           f'<tr><th>Total</th><td class="n">{enquiries}</td></tr></table>']
    if enquiries == arrivals["first"]:
        out.append(f'<p class="ok">The counts add up: {reports.counted(enquiries, "enquiry", "enquiries")} + '
                   f'{reports.counted(arrivals["replay"], "replay", "replays")}'
                   f'{esc(reports.resends_text(arrivals["resend"]))} = {arrived} arrived. A replay is the same '
                   "enquiry sent again; it is counted and recorded, and it creates nothing.</p>")
    else:
        out.append(f'<p class="bad">The counts do not add up: {reports.counted(enquiries, "enquiry", "enquiries")}, '
                   f'but {reports.counted(arrivals["first"], "first arrival", "first arrivals")}. Run '
                   "<code>python -I desk.py doctor</code>.</p>")
    return out


def _identity(found: dict | None) -> str:
    if not found:
        return ""
    kind = found.get("kind")
    if kind == "new":
        text = "A new contact: no contact in the CRM has this e-mail or phone."
        if found.get("resolution") == "new":
            text = "A new contact, as a person decided."
    elif kind == "returning":
        ways = [w for w, k in (("e-mail", "by_email"), ("phone", "by_phone")) if found.get(k)]
        how = " and ".join(ways) if ways else "a person's decision"
        text = f"Returning: contact {found.get('contact')} (found by {how})."
    elif kind == "phone_only":
        text = (f"Only the phone matches contact {', '.join(found.get('by_phone', []))}, and the e-mails do not agree: "
                "it may be a colleague on a shared line. A person decides whether it is that contact or a new "
                "one; nothing is planned until then.")
    elif kind == "two_contacts":
        text = (f"The e-mail belongs to contact {', '.join(found.get('by_email', [])) or 'none'} and the phone to "
                f"contact {', '.join(found.get('by_phone', [])) or 'none'}. A person decides which one it is; "
                "nothing is planned until then.")
    else:
        text = (f"Contact {', '.join(found.get('by_name_and_company', []))} has the same name and company, but not "
                "this e-mail or phone. It might be the same person. A person decides; the desk never merges contacts.")
    notes = " ".join(found.get("notes", []))
    return f"<p><b>Who it is:</b> {esc(text)}{(' ' + esc(notes)) if notes else ''}</p>"


def _draft(enquiry) -> list:
    if enquiry.state == "refused":
        return ["<p><b>Model step:</b> skipped. A refused enquiry never reaches a model or the CRM.</p>"]
    draft = enquiry.draft
    if not draft:
        return ["<p><b>Model step:</b> not run yet.</p>"]
    head = f'<p><b>Model step:</b> <span class="label">{esc(draft["label"])}</span></p>'
    outcome = draft.get("outcome")
    if outcome in ("used", "stand-in"):
        rows = [("Summary", draft.get("summary")), ("Intent", draft.get("intent")), ("Urgency", draft.get("urgency"))]
        out = [head, "<table>"]
        out += [f"<tr><th>{name}</th><td>{esc(value) if value else '-'}</td></tr>" for name, value in rows]
        reply = draft.get("reply_draft")
        out.append(f"<tr><th>Reply draft</th><td>{('<pre>' + esc(reply) + '</pre>') if reply else 'none'}</td></tr>")
        out.append("</table>")
        if outcome == "used":
            out.append('<p class="note">A reply draft is only a draft: the desk never sends anything to a person '
                       "who enquired. Where it says {{name}}, the sender's name goes: the model was not given the "
                       "name, and the person who writes the real reply puts it in.</p>")
        return out
    if outcome == "refused":
        return [head, f'<p class="bad">AI draft refused: {esc(draft.get("reason"))}. The enquiry goes on with no AI '
                      "text, and its plan is the same as without it.</p>"]
    if outcome == "not sent":
        return [head, f"<p>No AI text: the request was not sent, because {esc(draft.get('reason'))}.</p>"]
    return [head, f"<p>No AI text: {esc(draft.get('reason'))}.</p>"]


def _target(contact) -> str:
    return "the new contact" if contact == "created" else f"contact {contact}"


FIELD_WORDS = (("name", "name"), ("email", "e-mail"), ("phone", "phone"), ("company", "company"))


def _values(fields: dict) -> str:
    return ", ".join(f"{word} {esc(fields[name])}" for name, word in FIELD_WORDS if name in fields)


def _operation(op: dict) -> str:
    name = op.get("op")
    if name == "create_contact":
        return f"Create a contact: {_values(op.get('fields', {}))}."
    if name == "fill_empty_fields":
        return f"Fill the empty fields of {esc(_target(op.get('contact')))}: {_values(op.get('fields', {}))}."
    if name == "add_note":
        return (f"Add a note to {esc(_target(op.get('contact')))}. Its first line is the enquiry's key, so it is "
                f"never added twice:<pre>{esc(op.get('body'))}</pre>")
    if name == "add_task":
        return f"Add a task to {esc(_target(op.get('contact')))}:<pre>{esc(op.get('body'))}</pre>"
    return f"An operation outside the allow-list: {esc(name)}. The write gate refuses it."


def _plan(store, enquiry) -> list:
    if not enquiry.plan_sha:
        return []
    plan = json.loads(store.plan_body(enquiry.plan_sha))
    said = {"applied": "exactly what was written to the CRM, and nothing else",
            "rejected": "what a person rejected"}.get(enquiry.state, "exactly what would be written to the CRM, "
                                                                    "and nothing else")
    out = [f"<p><b>The plan</b> (plan {esc(enquiry.plan_sha[:12])}): {said}.</p>", "<ol>"]
    out += [f"<li>{_operation(op)}</li>" for op in plan.get("operations", [])]
    out.append("</ol>")
    for entry in plan.get("already_written", []):
        out.append(f"<p><b>Already in the CRM, written by an apply that did not complete, and not written again:"
                   f"</b> {esc(reports.describe_write(entry.get('op'), entry.get('ref'), entry.get('contact')))}.</p>")
    if enquiry.state not in ("applied", "rejected"):
        listed = {entry.get("op") for entry in plan.get("already_written", [])}
        so_far = reports.writes_text(store.ledger_rows(enquiry.key), leave_out=listed)
        if so_far:
            out.append(f"<p><b>Written so far</b> for this enquiry: {esc(so_far)}.</p>")
    for diff in plan.get("shown", []):
        why = diff.get("not_filled") or "it is not overwritten"
        field = dict(FIELD_WORDS).get(diff["field"], diff["field"])
        out.append(f"<p><b>Shown for you, not written:</b> {esc(field)}: the CRM has "
                   f"&ldquo;{esc(diff['crm'])}&rdquo;, the enquiry says &ldquo;{esc(diff['enquiry'])}&rdquo;; "
                   f"{esc(why)}.</p>")
    return out


def _decisions(store, enquiry) -> list:
    rows = []
    for approval in store.approvals(enquiry.key):
        text = {"approved": "approved", "used": "approved, and used when it was written to the CRM",
                "withdrawn": "approved, then withdrawn", "rejected": "rejected"}[approval.state]
        line = f"{esc(approval.by)} {text} (plan {esc(approval.plan_sha[:12])}, at {esc(approval.at)})"
        if approval.state == "withdrawn":
            who = "the desk itself" if approval.closed_by == "system" else esc(approval.closed_by)
            line += f"; withdrawn by {who}"
        if approval.reason:
            line += f"; reason: {esc(approval.reason)}"
        rows.append(f"<li>{line}</li>")
    return ["<p><b>Decisions:</b></p>", "<ul>", *rows, "</ul>"] if rows else []


def _replays(store, enquiry) -> list:
    out = []
    for _, at, differs in different_replays(store, enquiry.key):
        parts = [f"{esc(field)} (first: &ldquo;{esc(_short(v['first']))}&rdquo;; this time: "
                 f"&ldquo;{esc(_short(v['this_time']))}&rdquo;)" for field, v in differs.items()]
        out.append(f"<p><b>Replayed with different content</b> at {esc(at)}: {'; '.join(parts)}. It is kept as a "
                   "replay: the first content stands, and nothing new is planned.</p>")
    return out


def _short(value, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _actions(enquiry, decisions: Decisions) -> list:
    key = enquiry.key
    if enquiry.state == "awaiting_approval" and rests_on_the_phone_alone(enquiry.identity):
        return [*_actions_awaiting(enquiry, decisions),
                "<p><b>Found by the phone alone.</b> If it is someone else on that phone, resolve it as new; it is then "
                "planned again as a new contact:</p>",
                f'<pre>python -I desk.py resolve {esc(key)} --as new --by "Your name"</pre>']
    return _actions_awaiting(enquiry, decisions) if enquiry.state == "awaiting_approval" else _actions_other(enquiry)


def _actions_awaiting(enquiry, decisions: Decisions) -> list:
    key = enquiry.key
    if decisions.active:
        hidden = (f'<input type="hidden" name="token" value="{esc(decisions.token)}">'
                  f'<input type="hidden" name="key" value="{esc(key)}">'
                  f'<input type="hidden" name="plan" value="{esc(enquiry.plan_sha)}">')
        return [
            '<form method="post" action="/desk/decide">', hidden,
            '<input type="hidden" name="decision" value="approve">',
            '<label><input type="checkbox" name="confirm" value="yes"> I have read this plan and want exactly '
            "this written to the CRM.</label><br>",
            '<label>Your name <input name="by" maxlength="80" required></label> ',
            "<button>Approve</button></form>",
            '<form method="post" action="/desk/decide">', hidden,
            '<input type="hidden" name="decision" value="reject">',
            '<label><input type="checkbox" name="confirm" value="yes"> I want nothing written for this '
            "enquiry.</label><br>",
            '<label>Your name <input name="by" maxlength="80" required></label> ',
            '<label>Reason <input name="reason" maxlength="500" required></label> ',
            "<button>Reject</button></form>",
        ]
    return ["<p><b>To decide</b> (the page takes no decisions unless the server was started with "
            "<code>--allow-decisions</code>):</p>",
            f'<pre>python -I desk.py approve {esc(key)} --by "Your name" --plan {esc(enquiry.plan_sha[:12])}\n'
            f'python -I desk.py reject {esc(key)} --by "Your name" --reason "why"</pre>']


def _actions_other(enquiry) -> list:
    key = enquiry.key
    if enquiry.state in ("held_two_contacts", "held_possible_duplicate"):
        return ["<p><b>To resolve:</b></p>",
                f'<pre>python -I desk.py resolve {esc(key)} --as new --by "Your name"\n'
                f'python -I desk.py resolve {esc(key)} --as contact:&lt;id&gt; --by "Your name"</pre>']
    if enquiry.state == "approved":
        return ["<p><b>To write it:</b></p>", f"<pre>python -I desk.py apply --key {esc(key)}</pre>"]
    if enquiry.state == "apply_failed":
        return [f"<p><b>It stopped:</b> {esc(enquiry.error)}. <b>To retry</b>, which completes what is missing and "
                "writes nothing twice:</p>", f"<pre>python -I desk.py apply --key {esc(key)}</pre>",
                f"<p><b>If a retry keeps failing</b> because the CRM changed (for example, the e-mail now belongs to "
                "another contact), withdraw the approval and plan it again against the CRM as it is now; the new "
                "plan waits for a new approval:</p>",
                f'<pre>python -I desk.py withdraw {esc(key)} --by "Your name" --reason "the CRM changed"\n'
                "python -I desk.py plan</pre>"]
    return []


def state_words(store, enquiry) -> str:
    """The enquiry's state in plain words. A rejected enquiry says what an earlier apply wrote, if anything."""
    if enquiry.state == "rejected":
        earlier = reports.written_before(store, enquiry)
        return (f"Rejected by a person {earlier}; nothing more is written" if earlier
                else "Rejected by a person; nothing written")
    return STATE_WORDS[enquiry.state]


def card(store, enquiry, links, decisions: Decisions, heading: str = "h4") -> list:
    p = enquiry.payload
    who = " · ".join(esc(v) for v in (p.get("name"), p.get("company"), p.get("email"), p.get("phone")) if v)
    out = [f'<section class="card" id="{esc(slug(enquiry.key))}">',
           f'<{heading}><a href="{esc(links.enquiry(enquiry.key))}">{esc(enquiry.key)}</a></{heading}>',
           f'<p class="state {_state_class(enquiry.state)}">{esc(state_words(store, enquiry))}</p>',
           f"<p>From: {who or 'no name given'}. Received {esc(p.get('received_at'))} "
           f"(arrived {esc(enquiry.first_arrived_at)}, by {esc(enquiry.via)}).</p>",
           f"<blockquote>{esc(p.get('message'))}</blockquote>"]
    if enquiry.state == "refused":
        reasons = "; ".join(validate.describe(code) for code in enquiry.reasons)
        out.append(f"<p><b>Why it was not taken:</b> {esc(reasons)}. It is kept and counted.</p>")
    out.append(_identity(enquiry.identity))
    if enquiry.state == "received" and enquiry.error:
        out.append(f"<p>Not checked yet: {esc(enquiry.error)}.</p>")
    out += _draft(enquiry) + _plan(store, enquiry) + _replays(store, enquiry) + _decisions(store, enquiry)
    out += _actions(enquiry, decisions)
    out.append("</section>")
    return [line for line in out if line]


def desk_page(store, links, decisions: Decisions) -> str:
    body = ["<h1>Enquiries desk</h1>",
            '<p class="note">All people, companies and enquiries here are invented. Nothing is ever sent to a person '
            "who enquired, and nothing is written to the CRM without a person's approval of the exact plan.</p>"]
    body += counts_section(store)
    groups = {state: [] for state in STATE_ORDER}
    for enquiry in store.enquiries():
        groups[enquiry.state].append(enquiry)
    body.append("<h2>Enquiries</h2>")
    for state in STATE_ORDER:
        if groups[state]:
            body.append(f"<h3>{esc(STATE_WORDS[state])} ({len(groups[state])})</h3>")
            for enquiry in groups[state]:
                body += card(store, enquiry, links, decisions)
    return _page("Enquiries desk", body)


def enquiry_page(store, key: str, links, decisions: Decisions) -> str | None:
    enquiry = store.get_enquiry(key)
    if enquiry is None:
        return None
    body = [f'<p><a href="{esc(links.desk())}">Back to the desk</a></p>', f"<h1>Enquiry {esc(key)}</h1>"]
    body += card(store, enquiry, links, decisions, heading="h2")
    body += ["<h2>Its trail</h2>",
             '<p class="note">Every step, as the audit trail recorded it. The database refuses to change or remove a '
             "row, and each row carries a hash of the one before it. That guards against mistakes, bugs and plain "
             "edits of the file; it does not stop a person who can open the database file, take the refusal away "
             "and recompute every hash.</p>",
             "<table><tr><th>#</th><th>Time</th><th>Who</th><th>Event</th><th>Detail</th></tr>"]
    for row in audit.rows(store.conn, key):
        detail = json.loads(row[4])
        text = "; ".join(f"{name}: {_short(value, 120)}" for name, value in sorted(detail.items()))
        body.append(f"<tr><td>{row[0]}</td><td>{esc(row[1])}</td><td>{esc(row[5])}</td><td>{esc(row[3])}</td>"
                    f"<td>{esc(text)}</td></tr>")
    body.append("</table>")
    return _page(f"Enquiry {key}", body)


def message_page(title: str, text: str, links) -> str:
    return _page(title, [f'<p><a href="{esc(links.desk())}">Back to the desk</a></p>', f"<h1>{esc(title)}</h1>",
                         f"<p>{esc(text)}</p>"])
