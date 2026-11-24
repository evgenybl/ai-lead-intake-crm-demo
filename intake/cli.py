"""`desk.py`: the command line of the desk. `main(argv)` is what the end-to-end tests drive.

Exit codes: 0 done; 1 refused, failed, or a doctor check failed; 2 a usage error or a missing desk.
"""
from __future__ import annotations

import argparse
import os
import sys

import secrets

from . import doctor, pages, planner, reports
from .arrive import read_lines
from .clock import ClockError, make_clock
from .crm.base import CAPABILITIES, CRMError
from .crm.http_crm import TOKEN_ENV, URL_ENV, HttpCRM
from .crm.practice import PracticeCRM
from .draft import (BACKENDS, DEFAULT_BASE, DEFAULT_MODEL, DRAFT_ENV, KEY_ENV, AnthropicBackend, DraftConfigError,
                    RecordedBackend, TemplateBackend, make_backend)
from .flow import DeskError
from .home import adapter_choice, crm_path, desk_path, open_crm, open_desk, resolve_home
from .server import SECRET_ENV, IntakeServer, secret_from
from .states import TransitionError
from .store import DeskStore, HomeError

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STARTING_CONTACTS = ("data", "crm-start.synthetic.jsonl")
DEFAULT_PORT = 8765


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", help="the folder with desk.sqlite3 and practice-crm.sqlite3 "
                                       "(default: $INTAKE_HOME, else ./work)")
    common.add_argument("--at", help="fix the clock, e.g. 2026-01-05T09:00:00Z (default: $INTAKE_NOW, else the system clock)")
    common.add_argument("--crm", choices=("practice", "http"),
                        help="the CRM adapter (default: $INTAKE_CRM, else practice)")
    common.add_argument("--draft", choices=BACKENDS,
                        help="the model step's backend (default: $INTAKE_DRAFT, else recorded)")

    parser = argparse.ArgumentParser(prog="desk.py", description="A lead-intake desk: nothing is written to the CRM "
                                     "without a recorded approval. All data here is invented.")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    p = sub.add_parser("init", parents=[common], help="create the desk and the practice CRM with its starting contacts")
    p.add_argument("--empty-crm", action="store_true", help="create the practice CRM with no contacts")
    p = sub.add_parser("ingest", parents=[common], help="take enquiries from a .jsonl file, one JSON object per line")
    p.add_argument("file")
    p = sub.add_parser("listen", parents=[common], help="offer POST /intake on 127.0.0.1 (the secret comes from INTAKE_SECRET)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"0 lets the system choose (default {DEFAULT_PORT})")
    p = sub.add_parser("serve", parents=[common], help="POST /intake and the desk pages at /desk, on 127.0.0.1")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"0 lets the system choose (default {DEFAULT_PORT})")
    p.add_argument("--allow-decisions", action="store_true",
                   help="let the desk page take Approve and Reject, each with a ticked box and a name")
    p = sub.add_parser("render", parents=[common], help="write the desk pages as files, for a reader with no server")
    p.add_argument("--out", required=True, help="the folder to write index.html and enquiry/*.html into")
    sub.add_parser("plan", parents=[common], help="the dry run: print every waiting plan; writes nothing to the CRM")
    p = sub.add_parser("approve", parents=[common], help="approve the waiting plan of one enquiry")
    p.add_argument("key")
    p.add_argument("--by", required=True, help="the name of the person who decides")
    p.add_argument("--plan", required=True,
                   help="the plan's sha256, or its first 8 or more hex digits, as `plan` printed it")
    p = sub.add_parser("reject", parents=[common], help="reject the waiting plan of one enquiry, with a reason")
    p.add_argument("key")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("withdraw", parents=[common], help="withdraw an approval that is not used yet")
    p.add_argument("key")
    p.add_argument("--by", required=True)
    p.add_argument("--reason", default="")
    p = sub.add_parser("resolve", parents=[common], help="resolve a held enquiry as new or as one found contact")
    p.add_argument("key")
    p.add_argument("--as", dest="as_", required=True, metavar="new|contact:<id>")
    p.add_argument("--by", required=True)
    p = sub.add_parser("apply", parents=[common], help="send approved plans through the write gate")
    p.add_argument("--key", help="apply this enquiry only")
    p.add_argument("--dry-run", action="store_true", help="run the gate's checks and write nothing")
    p = sub.add_parser("trail", parents=[common], help="print one enquiry's life from the audit trail")
    p.add_argument("key")
    sub.add_parser("report", parents=[common], help="the counts by state, and whether they add up")
    sub.add_parser("doctor", parents=[common], help="run the invariants of the four promises on both files")
    sub.add_parser("status", parents=[common], help="each adapter and capability: connected, not configured or error")
    return parser


def main(argv=None, out=None, environ=None) -> int:
    out = out or sys.stdout
    environ = os.environ if environ is None else environ
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as stop:
        return int(stop.code or 0)

    def say(line: str = "") -> None:
        print(line, file=out)

    try:
        clock = make_clock(args.at, environ)
        home = resolve_home(args.home, environ)
        choice = adapter_choice(args.crm, environ)
        command = COMMANDS[args.command]
        return command(args, say, home, choice, clock, environ, out)
    except (ClockError, HomeError, DraftConfigError) as error:
        say(f"error: {error}")
        return 2
    except (DeskError, TransitionError) as error:
        say(f"refused: {error}")
        return 1
    except CRMError as error:
        say(f"the CRM did not answer as expected: {error}")
        return 1


# -- the commands -----------------------------------------------------------------------------------

def cmd_init(args, say, home, choice, clock, environ, out) -> int:
    desk_file, crm_file = desk_path(home), crm_path(home)
    for path in (desk_file, crm_file):
        if os.path.exists(path):
            raise HomeError(f"{path} is already there; nothing was changed")
    os.makedirs(home, exist_ok=True)
    DeskStore.create(desk_file, clock).close()
    say(f"created {desk_file}")
    crm = PracticeCRM(crm_file, create=True)
    try:
        if args.empty_crm:
            say(f"created {crm_file} with no contacts")
            return 0
        raw = _read_file(os.path.join(REPO_ROOT, *STARTING_CONTACTS))
        contacts = [obj for _, obj, problem in read_lines(raw) if not problem]
        loaded = crm.load_starting_contacts(contacts)
        say(f"created {crm_file} with the {loaded} starting contacts of {'/'.join(STARTING_CONTACTS)}")
    finally:
        crm.close()
    return 0


def cmd_ingest(args, say, home, choice, clock, environ, out) -> int:
    raw = _read_file(args.file)
    new = replays = not_taken = 0
    with open_desk(home, choice, environ, clock, make_backend(args.draft, environ)) as desk:
        for number, obj, problem in read_lines(raw):
            if problem:
                not_taken += 1
                say(f"line {number}: not taken: {problem}")
                continue
            answer = desk.receive(obj, via="file")
            if answer.replay:
                replays += 1
                changed = f"; replayed with different content: {', '.join(answer.differs)}" if answer.differs else ""
                say(f"line {number}: {answer.key} -> replay of the enquiry first arrived at "
                    f"{answer.first_arrived_at} (state now: {answer.state}); nothing new is queued{changed}")
            else:
                new += 1
                why = f": {reports.refusal_text(answer.reasons)}" if answer.state == "refused" else ""
                say(f"line {number}: {answer.key} -> {answer.state}{why}")
    say(f"ingest: {new} new enquiries, {replays} replays, {not_taken} lines not taken")
    return 1 if not_taken else 0


def _serve(args, say, home, choice, clock, environ, out, desk_pages: bool) -> int:
    DeskStore.open(desk_path(home), clock).close()  # fail now, not on the first request
    open_crm(home, choice, environ).close()
    drafter = make_backend(args.draft, environ)
    secret = secret_from(environ)
    decisions = pages.Decisions(secrets.token_urlsafe(24) if getattr(args, "allow_decisions", False) else None)
    server = IntakeServer(args.port, secret, lambda: open_desk(home, choice, environ, clock, drafter), log=out,
                          desk_pages=desk_pages, decisions=decisions)
    if secret:
        state = f"configured ({SECRET_ENV} is set)"
    else:
        state = f"NOT configured: {SECRET_ENV} is unset or shorter than 16 characters, so it answers 503"
    if desk_pages:
        say(f"serving http://127.0.0.1:{server.port}/desk  decisions on the page: "
            f"{'on' if decisions.active else 'off (the page shows the commands)'}")
        say(f"intake: POST http://127.0.0.1:{server.port}/intake  secret: {state}")
    else:
        say(f"listening on http://127.0.0.1:{server.port}/intake  secret: {state}")
    out.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        say("stopped")
    finally:
        server.server_close()
    return 0


def cmd_listen(args, say, home, choice, clock, environ, out) -> int:
    return _serve(args, say, home, choice, clock, environ, out, desk_pages=False)


def cmd_serve(args, say, home, choice, clock, environ, out) -> int:
    return _serve(args, say, home, choice, clock, environ, out, desk_pages=True)


def cmd_render(args, say, home, choice, clock, environ, out) -> int:
    store = DeskStore.open(desk_path(home), clock)
    try:
        folder = os.path.join(args.out, "enquiry")
        os.makedirs(folder, exist_ok=True)
        _write_text(os.path.join(args.out, "index.html"), pages.desk_page(store, pages.FileLinks(False), pages.Decisions()))
        enquiries = store.enquiries()
        for enquiry in enquiries:
            page = pages.enquiry_page(store, enquiry.key, pages.FileLinks(True), pages.Decisions())
            _write_text(os.path.join(folder, pages.slug(enquiry.key) + ".html"), page)
    finally:
        store.close()
    say(f"render: wrote index.html and {len(enquiries)} enquiry pages")
    return 0


def _write_text(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def cmd_plan(args, say, home, choice, clock, environ, out) -> int:
    with open_desk(home, choice, environ, clock, make_backend(args.draft, environ)) as desk:
        for key, change in desk.prepare():
            say(f"{key}: {change}")
        groups = desk.waiting()
        say("the dry run: nothing below has been written to the CRM")
        shown = 0
        for state, enquiries in groups.items():
            for enquiry in enquiries:
                note = ""
                if state == "approved":
                    note = _freshness(desk, enquiry)
                say()
                for line in reports.enquiry_lines(desk.store, enquiry, note):
                    say(line)
                shown += 1
        say()
        counts = ", ".join(f"{len(v)} {k}" for k, v in groups.items() if v) or "none"
        say(f"plan: {shown} waiting ({counts})")
    return 0


def _freshness(desk, enquiry) -> str:
    try:
        current = planner.assess(enquiry.key, enquiry.payload, enquiry.resolution, desk.crm)
    except CRMError as error:
        return f"the CRM could not be read to check this plan: {error}"
    if current.sha == enquiry.plan_sha:
        return "approved; the plan still fits the CRM as it is now"
    return "approved, but the CRM changed since: apply will refuse it and plan it again"


def cmd_approve(args, say, home, choice, clock, environ, out) -> int:
    with open_desk(home, choice, environ, clock) as desk:
        sha = desk.approve(args.key, args.by, args.plan)
    say(f"approved: {args.key}, plan {sha}, by {' '.join(args.by.split())}")
    return 0


def cmd_reject(args, say, home, choice, clock, environ, out) -> int:
    with open_desk(home, choice, environ, clock) as desk:
        desk.reject(args.key, args.by, args.reason)
    say(f"rejected: {args.key}; nothing will be written")
    return 0


def cmd_withdraw(args, say, home, choice, clock, environ, out) -> int:
    with open_desk(home, choice, environ, clock) as desk:
        desk.withdraw(args.key, args.by, args.reason)
    say(f"withdrawn: the approval of {args.key}; its plan waits again")
    return 0


def cmd_resolve(args, say, home, choice, clock, environ, out) -> int:
    with open_desk(home, choice, environ, clock) as desk:
        state = desk.resolve(args.key, args.as_, args.by)
    say(f"resolved: {args.key} as {args.as_}; now {state}")
    return 0


def cmd_apply(args, say, home, choice, clock, environ, out) -> int:
    with open_desk(home, choice, environ, clock) as desk:
        results = desk.apply(key=args.key, dry_run=args.dry_run)
    tally: dict = {}
    for result in results:
        tally[result.outcome] = tally.get(result.outcome, 0) + 1
        detail = f": {', '.join(result.steps)}" if result.steps else ""
        say(f"{result.key}: {result.message}{detail}")
    if not results:
        say("apply: nothing is approved")
        return 0
    order = ("applied", "apply_failed", "refused", "replanned", "dry_run")
    say("apply: " + ", ".join(f"{tally.get(name, 0)} {name}" for name in order if name in tally))
    return 1 if any(r.outcome in ("apply_failed", "refused", "replanned") for r in results) else 0


def cmd_trail(args, say, home, choice, clock, environ, out) -> int:
    store = DeskStore.open(desk_path(home), clock)
    try:
        if store.get_enquiry(args.key) is None:
            raise DeskError(f"no enquiry with the key {args.key!r}")
        for line in reports.trail_lines(store, args.key):
            say(line)
    finally:
        store.close()
    return 0


def cmd_report(args, say, home, choice, clock, environ, out) -> int:
    store = DeskStore.open(desk_path(home), clock)
    try:
        for line in reports.report_lines(store):
            say(line)
    finally:
        store.close()
    return 0


def cmd_doctor(args, say, home, choice, clock, environ, out) -> int:
    desk_file = desk_path(home)
    if not os.path.exists(desk_file):
        raise HomeError(f"no desk at {desk_file}: run `desk.py init` first")
    crm_file = crm_path(home) if choice == "practice" and os.path.exists(crm_path(home)) else None
    crm_named = os.path.basename(crm_file) if crm_file else f"not checked ({choice} adapter)"
    say(f"doctor: desk {os.path.basename(desk_file)}; CRM {crm_named}; both in --home")
    checks = doctor.examine(desk_file, crm_file)
    for line in reports.doctor_lines(checks):
        say(line)
    return 1 if any(c.problems for c in checks) else 0


def cmd_status(args, say, home, choice, clock, environ, out) -> int:
    secret = secret_from(environ)
    say("intake endpoint  POST /intake, 127.0.0.1 only: "
        + ("configured" if secret else f"not configured ({SECRET_ENV} unset or under 16 characters: every request gets 503)"))
    desk_file = desk_path(home)
    say(f"desk file        {os.path.basename(desk_file)} in --home: "
        + ("connected" if os.path.exists(desk_file) else "not configured (run desk.py init)"))
    states_seen = {}
    if os.path.exists(crm_path(home)):
        crm = PracticeCRM(crm_path(home))
        try:
            states_seen["practice"] = crm.check()
        finally:
            crm.close()
    else:
        states_seen["practice"] = ("not configured", "no practice CRM file; run desk.py init")
    url, token = environ.get(URL_ENV, ""), environ.get(TOKEN_ENV, "")
    if not (url and token):
        missing = " and ".join(n for n, v in ((URL_ENV, url), (TOKEN_ENV, token)) if not v)
        states_seen["http"] = ("not configured", f"set {missing}")
    else:
        try:
            crm = HttpCRM(url, token)
        except CRMError as error:
            states_seen["http"] = ("error", str(error))
        else:
            states_seen["http"] = crm.check()
    labels = {"practice": PracticeCRM.label, "http": HttpCRM.label}
    for name in ("practice", "http"):
        state, detail = states_seen[name]
        say(f"crm {name:<12} {labels[name]}: {state} ({detail})")
        say(f"  capabilities: {'; '.join(CAPABILITIES)}: {state}")
    state, detail = RecordedBackend().check()
    say(f"draft recorded   answers found by the request's sha256: {state} ({detail})")
    template = TemplateBackend()
    state, detail = template.check()
    say(f"draft template   {template.label}: {state} ({detail})")
    if environ.get(KEY_ENV):
        try:
            live = AnthropicBackend(environ[KEY_ENV], environ.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE,
                                    environ.get("INTAKE_MODEL") or DEFAULT_MODEL)
            state, detail = live.check()
            say(f"draft anthropic  {live.label}: {state} ({detail})")
        except DraftConfigError as error:
            say(f"draft anthropic  Live path: error ({error})")
    else:
        say(f"draft anthropic  Live path: not configured (set {KEY_ENV})")
    say(f"active CRM adapter: {choice}; active draft backend: {args.draft or environ.get(DRAFT_ENV) or 'recorded'}")
    return 1 if states_seen[choice][0] == "error" else 0


def _read_file(path: str) -> bytes:
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError as error:
        raise HomeError(f"cannot read {path}: {error.strerror}") from None


COMMANDS = {
    "init": cmd_init,
    "ingest": cmd_ingest,
    "listen": cmd_listen,
    "serve": cmd_serve,
    "render": cmd_render,
    "plan": cmd_plan,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "withdraw": cmd_withdraw,
    "resolve": cmd_resolve,
    "apply": cmd_apply,
    "trail": cmd_trail,
    "report": cmd_report,
    "doctor": cmd_doctor,
    "status": cmd_status,
}
