"""The whole story, and every case of the gallery, each on new files, one line per step.

    python -I demo.py --out demo-run                the whole story, then every case
    python -I demo.py --case <name> [--out <dir>]   one case
    python -I demo.py --list                        the cases

Each line says what was expected and what happened. The run ends with a closing table, one line per step
or case, and a total line. In that table the cases refused-inputs and ai-answer-refused are named
inputs-not-taken and ai-answer-not-used: the table is drawn as a picture by tools/term_svg.py, which colours
any line holding the word "refused" red, as a failure. The clock is fixed, the data is the invented file, the model step uses the
answers under recorded/ (a constructed answer is labelled so), and nothing leaves this computer: the CRM is
the practice CRM, and the two intake cases start a server on 127.0.0.1 and post to it.
"""
from __future__ import annotations

import argparse
import contextlib
import http.client
import io
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(1, os.path.join(HERE, "data"))

import generate  # noqa: E402
from intake.cli import main as desk_main  # noqa: E402
from intake.clock import Clock  # noqa: E402
from intake.crm.base import CRMError  # noqa: E402
from intake.draft import RECORDED_DIR, RecordedBackend  # noqa: E402
from intake.home import crm_path, open_desk  # noqa: E402
from intake.server import IntakeServer  # noqa: E402

DATA_FILE = os.path.join(HERE, "data", "enquiries.synthetic.jsonl")
PERSON = "Dana Reviewer (invented)"
ENV = {"INTAKE_DRAFT": "recorded"}  # nothing from the caller's environment reaches the demo
LINE_MAX = 100


def _lines_by_case() -> dict:
    with open(DATA_FILE, encoding="utf-8") as handle:
        lines = [line for line in handle.read().split("\n") if line.strip()]
    return {case: line for (case, _), line in zip(generate.CASES, lines)}


LINES = _lines_by_case()


class Home:
    """One new desk and practice CRM in a folder of its own."""

    def __init__(self, folder: str, at: str = "2026-01-05T08:55:00Z"):
        self.folder = folder
        self.files = 0
        os.makedirs(folder, exist_ok=False)
        self.desk("init", at=at)

    def desk(self, *argv, at: str = "2026-01-05T09:00:00Z") -> tuple:
        out = io.StringIO()
        code = desk_main([*argv, "--home", self.folder, "--at", at], out=out, environ=dict(ENV))
        return code, out.getvalue()

    def ingest(self, *cases, at: str = "2026-01-05T09:00:00Z") -> str:
        self.files += 1
        path = os.path.join(self.folder, f"enquiries-{self.files}.jsonl")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(LINES[case] + "\n" for case in cases))
        return self.desk("ingest", path, at=at)[1]

    def approve(self, key: str, at: str = "2026-01-05T09:00:00Z") -> tuple:
        """`desk.py approve`, naming the plan that waits by the 12 hex digits `desk.py plan` prints for it."""
        return self.desk("approve", key, "--by", PERSON, "--plan", self.enquiry(key).plan_sha[:12], at=at)

    def counts(self) -> dict:
        conn = sqlite3.connect(crm_path(self.folder))
        try:
            return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("contacts", "notes", "tasks")}
        finally:
            conn.close()

    def notes_with(self, key: str) -> int:
        conn = sqlite3.connect(crm_path(self.folder))
        try:
            return sum(1 for (body,) in conn.execute("SELECT body FROM notes") if body.split("\n", 1)[0] == key)
        finally:
            conn.close()

    @contextlib.contextmanager
    def open(self, drafter=None, at: str = "2026-01-05T09:00:00Z"):
        with open_desk(self.folder, "practice", {}, Clock(at), drafter) as desk:
            yield desk

    def enquiry(self, key: str):
        with self.open() as desk:
            return desk.store.get_enquiry(key)


def last_line(text: str, starts: str) -> str:
    found = [line for line in text.splitlines() if line.startswith(starts)]
    return found[-1][len(starts):].strip() if found else "(no such line)"


def crm_text(counts: dict) -> str:
    return ", ".join(_count(counts[name], name[:-1]) for name in ("contacts", "notes", "tasks"))


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")


class Run:
    def __init__(self, say):
        self.say = say
        self.rows = []

    def step(self, label: str, expected: str, happened: str, ok: bool, short: str, table_label: str = "") -> bool:
        verdict = "as expected" if ok else "NOT AS EXPECTED"
        self.say(f"[{label}] expected: {expected} | happened: {happened} | {verdict}")
        self.rows.append((table_label or label, ok, short))
        return ok

    def closing_table(self) -> list:
        lines = ["closing table: one line per step and case"]
        for label, ok, short in self.rows:
            lines.append(f"{'PASS' if ok else 'FAIL'}  {label:<29} {short}"[:LINE_MAX])
        good = sum(1 for _, ok, _ in self.rows if ok)
        lines.append(f"total: {good} of {len(self.rows)} steps and cases as expected")
        return lines


# -- the whole story ---------------------------------------------------------------------------------

APPROVED = ("webform:wf-000101", "webform:wf-000108", "webform:wf-000109", "webform:wf-000110", "webform:wf-000111")
# The story's clock starts after the last time an enquiry of the invented file was sent (14:39 that day), so that
# no page of the examples shows an enquiry taken in, approved or written before it was sent.
DAY = "2026-01-05"


def story(run: Run, folder: str, keep: dict | None = None) -> str:
    home = Home(folder, at=f"{DAY}T14:55:00Z")
    keep = {} if keep is None else keep
    before = home.counts()
    run.step("story 1 init", "a new desk; 10 starting contacts", crm_text(before), before["contacts"] == 10,
             "a new desk; 10 starting contacts in the practice CRM")

    out = home.ingest(*LINES, at=f"{DAY}T15:00:00Z")
    keep["ingest-1.txt"] = out
    got = last_line(out, "ingest: ")
    run.step("story 2 ingest the file", "27 new enquiries, 3 replays", got,
             got.startswith("27 new enquiries, 3 replays"), "27 new enquiries, 3 replays")

    _, out = home.desk("report", at=f"{DAY}T15:10:00Z")
    keep["report-1.txt"] = out
    got = last_line(out, "the states add up: ")
    run.step("story 3 report", "the states add up", got, got.startswith("yes"), "the states add up: " + got[5:])

    _, out = home.desk("plan", at=f"{DAY}T15:20:00Z")
    keep["plan.txt"] = out
    _, keep["plan-one.txt"] = home.desk("plan", "--key", "webform:wf-000109", "--full", at=f"{DAY}T15:21:00Z")
    after = home.counts()
    got = last_line(out, "plan: ")
    run.step("story 4 dry run (plan)", "every waiting plan printed; nothing written",
             f"{got}; CRM {crm_text(after)}", after == before, "plans printed; nothing written to the CRM")

    resolved, _ = home.desk("resolve", "webform:wf-000111", "--as", "contact:6", "--by", PERSON,
                            at=f"{DAY}T15:25:00Z")
    codes = [home.approve(key, at=f"{DAY}T15:30:00Z")[0] for key in APPROVED]
    run.step("story 5 resolve 1, approve 5", "a hold resolved; 5 approvals recorded",
             f"the phone-only hold resolved as contact 6; {codes.count(0)} approvals recorded",
             resolved == 0 and codes.count(0) == 5, "a person resolves a hold, then approves 5 plans by sha256")

    _, out = home.desk("apply", at=f"{DAY}T15:35:00Z")
    keep["apply-1.txt"] = out
    written = home.counts()
    got = last_line(out, "apply: ")
    run.step("story 6 apply", "5 applied: 1 new contact, 5 notes, 5 tasks", f"{got}; CRM {crm_text(written)}",
             got == "5 applied" and written == {"contacts": 11, "notes": 5, "tasks": 5},
             "5 written: 1 new contact; 4 returning contacts updated")

    out = home.ingest(*LINES, at=f"{DAY}T17:00:00Z")
    keep["ingest-2.txt"] = out
    got = last_line(out, "ingest: ")
    again = home.counts()
    run.step("story 7 ingest it again", "30 replays; nothing new written",
             f"{got}; CRM {crm_text(again)}", got.startswith("0 new enquiries, 30 replays") and again == written,
             "0 new, 30 replays; nothing new written")

    _, out = home.desk("apply", at=f"{DAY}T17:05:00Z")
    got = last_line(out, "apply: ")
    run.step("story 8 apply again", "nothing is approved; nothing written", f"{got}; CRM {crm_text(home.counts())}",
             got == "nothing is approved" and home.counts() == written, "nothing approved; nothing written")

    _, out = home.desk("report", at=f"{DAY}T17:10:00Z")
    keep["report-2.txt"] = out
    got = last_line(out, "the states add up: ")
    run.step("story 9 report again", "the states add up, with 33 replays", got,
             got.startswith("yes: 27 enquiries + 33 replays"), "the states add up: " + got[5:])

    _, keep["status.txt"] = home.desk("status", at=f"{DAY}T17:12:00Z")
    code, out = home.desk("doctor", at=f"{DAY}T17:15:00Z")
    keep["doctor.txt"] = out
    got = last_line(out, "doctor: ")
    run.step("story 10 doctor", "every check of the four promises passes", got, code == 0,
             "the doctor: " + got)
    _, keep["trail.txt"] = home.desk("trail", "webform:wf-000110", at=f"{DAY}T17:20:00Z")
    return home.folder


# -- the cases ----------------------------------------------------------------------------------------

def case_replay_three_times(run: Run, folder: str) -> None:
    home = Home(folder)
    first = "new: quote_request"
    copies = (first, "replay: the first enquiry sent again", "replay: the first enquiry sent a third time, "
              "its message edited")
    out = home.ingest(*copies)
    home.approve("webform:wf-000101")
    home.desk("apply")
    home.ingest(*copies, at="2026-01-05T10:00:00Z")
    home.desk("apply", at="2026-01-05T10:05:00Z")
    counts = home.counts()
    edited = "replayed with different content: message" in out
    happened = f"{counts['contacts'] - 10} new contact, {home.notes_with('webform:wf-000101')} note; edited copy named"
    run.step("replay-three-times", "one contact, one note", happened if edited else happened + " NOT",
             counts["contacts"] == 11 and home.notes_with("webform:wf-000101") == 1 and edited,
             "one contact, one note; the edited copy shown")


def case_returning_contact(run: Run, folder: str) -> None:
    home = Home(folder)
    home.ingest("returning: by e-mail, a changed phone shown", "returning: by e-mail, fills the empty company")
    _, plan = home.desk("plan")
    for key in ("webform:wf-000109", "webform:wf-000110"):
        home.approve(key)
    home.desk("apply")
    conn = sqlite3.connect(crm_path(home.folder))
    try:
        bruno_phone, celia_company = (conn.execute("SELECT phone FROM contacts WHERE id = 2").fetchone()[0],
                                      conn.execute("SELECT company FROM contacts WHERE id = 3").fetchone()[0])
    finally:
        conn.close()
    shown = "shown for a person: phone" in plan
    ok = home.counts()["contacts"] == 10 and bruno_phone == "000-000-0102" and celia_company and shown
    run.step("returning-contact", "updated, no second contact, a differing field shown not overwritten",
             f"{home.counts()['contacts']} contacts; empty company filled; phone kept as {bruno_phone}", bool(ok),
             "no second contact; empty field filled; phone shown, kept")


def case_two_contacts_held(run: Run, folder: str) -> None:
    key = "webform:wf-000112"
    homes = [Home(os.path.join(folder, "as-contact-7")), Home(os.path.join(folder, "as-contact-8"))]
    states = []
    for home, contact in zip(homes, ("7", "8")):
        home.ingest("held: the e-mail and the phone belong to two contacts")
        held = home.enquiry(key)
        code, _ = home.desk("apply", "--key", key)
        new_code, _ = home.desk("resolve", key, "--as", "new", "--by", PERSON)
        home.desk("resolve", key, "--as", f"contact:{contact}", "--by", PERSON)
        after = home.enquiry(key)
        states.append((held.state, held.plan_sha, code, new_code, after.state, after.identity.get("contact")))
    ok = all(s[0] == "held_two_contacts" and s[1] is None and s[2] == 1 and s[3] == 1 and s[4] == "awaiting_approval"
             for s in states) and [s[5] for s in states] == ["7", "8"]
    run.step("two-contacts-held", "held; nothing planned until resolved; then resolved each way",
             "held with no plan; as new: not allowed (the e-mail is taken); resolved as contact 7, as contact 8",
             ok, "held, no plan; resolved as contact 7, then as 8")


def case_possible_duplicate_held(run: Run, folder: str) -> None:
    key = "webform:wf-000113"
    results = []
    for way in ("new", "contact:5"):
        home = Home(os.path.join(folder, way.replace(":", "-")))
        home.ingest("held: a new address with the name and company of an existing contact")
        held = home.enquiry(key)
        home.desk("resolve", key, "--as", way, "--by", PERSON)
        after = home.enquiry(key)
        results.append((held.state, held.plan_sha, after.state, after.identity["kind"]))
    ok = results == [("held_possible_duplicate", None, "awaiting_approval", "new"),
                     ("held_possible_duplicate", None, "awaiting_approval", "returning")]
    run.step("possible-duplicate-held", "held; nothing planned until resolved; then resolved each way",
             "held with no plan; resolved as new (a contact to create), and as contact 5", ok,
             "held, no plan; resolved as new, then as contact 5")


def case_refused_inputs(run: Run, folder: str) -> None:
    home = Home(folder)
    cases = [case for case, expected in generate.CASES if expected == "refused"]
    out = home.ingest(*cases)
    lines = [line for line in out.splitlines() if " -> refused: " in line]
    with home.open() as desk:
        drafted = [e.key for e in desk.store.enquiries() if e.draft is not None]
    ok = len(lines) == len(cases) and not drafted and home.counts() == {"contacts": 10, "notes": 0, "tasks": 0}
    reasons = sorted({line.split(" -> refused: ")[1] for line in lines})
    run.step("refused-inputs", "each refused with its reason; nothing reaches the model or the CRM",
             f"{len(lines)} refused ({'; '.join(reasons)}); model step run for {len(drafted)}; CRM untouched", ok,
             f"{len(lines)} not taken, each with its reason; model and CRM untouched",
             table_label="inputs-not-taken")  # the picture tool draws a line red when it holds "refused"


def case_hidden_instruction(run: Run, folder: str) -> None:
    key = "webform:wf-000120"
    plain, extra = Home(os.path.join(folder, "its-answer")), Home(os.path.join(folder, "extra-key"))
    plain.ingest("new: the hidden instruction")
    gallery = RecordedBackend(sources={os.path.join("gallery", "extra-key"): "constructed"})
    with extra.open(drafter=gallery) as desk:
        desk.receive(json.loads(LINES["new: the hidden instruction"]), via="file")
    code, _ = extra.desk("apply", "--key", key)
    first, second = plain.enquiry(key), extra.enquiry(key)
    ok = (first.state == second.state == "awaiting_approval" and second.draft["outcome"] == "refused"
          and "beyond the four" in second.draft["reason"] and code == 1 and extra.counts()["notes"] == 0)
    run.step("hidden-instruction", "still waiting for a person; an answer with an extra key refused",
             f"waits for approval ({first.draft['label']}); with an extra key: {second.draft['reason']}; "
             "the gate wrote nothing", ok, "waits for a person; the answer with a 5th key not used")


def case_ai_answer_refused(run: Run, folder: str) -> None:
    key = "webform:wf-000119"
    with_bad, without = Home(os.path.join(folder, "constructed-answer")), Home(os.path.join(folder, "no-ai-text"))
    gallery = RecordedBackend(sources={os.path.join("gallery", "figure-not-in-message"): "constructed"})
    obj = json.loads(LINES["new: figures in the message"])
    with with_bad.open(drafter=gallery) as desk:
        desk.receive(obj, via="file")
    with without.open(drafter=RecordedBackend(root=os.path.join(folder, "no-recordings"))) as desk:
        desk.receive(obj, via="file")
    bad, plain = with_bad.enquiry(key), without.enquiry(key)
    ok = bad.draft["outcome"] == "refused" and plain.draft["outcome"] == "none" and bad.plan_sha == plain.plan_sha
    run.step("ai-answer-refused", "a figure not in the message: not used; the plan unchanged",
             f"AI draft refused: {bad.draft['reason']}; plan {bad.plan_sha[:12]} with it, "
             f"{plain.plan_sha[:12]} without", ok, "a figure not in the message: not used; plan the same",
             table_label="ai-answer-not-used")


def case_apply_without_approval(run: Run, folder: str) -> None:
    home = Home(folder)
    home.ingest("new: quote_request")
    code, out = home.desk("apply", "--key", "webform:wf-000101")
    ok = code == 1 and "no approval in the state approved" in out and home.counts()["notes"] == 0
    run.step("apply-without-approval", "stopped; nothing written",
             f"the gate: no approval in the state approved; CRM {crm_text(home.counts())}", ok,
             "the gate stops it: no approval; nothing written")


def case_approval_for_another_plan(run: Run, folder: str) -> None:
    home = Home(folder)
    home.ingest("new: quote_request", "new: the same new person's second enquiry")
    for key in ("webform:wf-000101", "webform:wf-000124"):
        home.approve(key)
    home.desk("apply", "--key", "webform:wf-000101", at="2026-01-05T09:35:00Z")
    code, out = home.desk("apply", "--key", "webform:wf-000124", at="2026-01-05T09:40:00Z")
    after = home.enquiry("webform:wf-000124")
    ok = (code == 1 and "the approval is for another plan" in out and after.state == "awaiting_approval"
          and home.notes_with("webform:wf-000124") == 0)
    run.step("approval-for-another-plan", "stopped; nothing written; planned again",
             "the contact now exists, so the plan changed; the gate stopped it; it waits again", ok,
             "the CRM changed since approval: planned again, waits")


def case_apply_twice(run: Run, folder: str) -> None:
    home = Home(folder)
    home.ingest("new: quote_request")
    home.approve("webform:wf-000101")
    home.desk("apply")
    once = home.counts()
    code, out = home.desk("apply", "--key", "webform:wf-000101", at="2026-01-05T09:45:00Z")
    ok = code == 1 and home.counts() == once and home.notes_with("webform:wf-000101") == 1
    run.step("apply-twice", "stopped; nothing written", f"the second apply wrote nothing; CRM {crm_text(home.counts())}",
             ok, "the approval is used once; the second apply writes nothing")


class _DownOnce:
    """The practice CRM, made to fail one call of add_note: the demo's stand-in for a CRM that is down."""

    def __init__(self, inner):
        self._inner, self._failed = inner, False

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if name != "add_note":
            return attribute

        def down(*args):
            if not self._failed:
                self._failed = True
                raise CRMError("add_note: the CRM did not answer (simulated)")
            return attribute(*args)

        return down


def case_crm_down_then_retry(run: Run, folder: str) -> None:
    home = Home(folder)
    key = "webform:wf-000101"
    home.ingest("new: quote_request")
    home.approve(key)
    with home.open() as desk:
        desk.crm = _DownOnce(desk.crm)
        (first,) = desk.apply(key=key)
    _, report = home.desk("report", at="2026-01-05T09:40:00Z")
    named = f"{key}: step 2 add_note" in report
    code, _ = home.desk("apply", at="2026-01-05T09:45:00Z")
    ok = (first.outcome == "apply_failed" and named and code == 0 and home.notes_with(key) == 1
          and home.counts()["contacts"] == 11)
    run.step("crm-down-then-retry", "apply_failed, named; the retry completes; nothing doubled",
             f"{first.outcome}, named in the report; the retry completed; CRM {crm_text(home.counts())}", ok,
             "apply_failed, named; the retry completed; nothing doubled")


def _post(home: Home, secret, header_secret: str) -> int:
    server = IntakeServer(0, secret, lambda: open_desk(home.folder, "practice", {}, Clock("2026-01-05T09:00:00Z")))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        connection.request("POST", "/intake", body=LINES["new: quote_request"].encode("utf-8"),
                           headers={"X-Intake-Secret": header_secret, "Content-Type": "application/json"})
        status = connection.getresponse().status
        connection.close()
        return status
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def case_wrong_secret(run: Run, folder: str) -> None:
    home = Home(folder)
    made_up = "wrong" + "-" + secrets.token_hex(8)  # never the server's secret, made at run time
    status = _post(home, secrets.token_urlsafe(24), made_up)
    with home.open() as desk:
        kept = len(desk.store.enquiries())
    run.step("wrong-secret", "401", f"{status}; enquiries kept: {kept}", status == 401 and kept == 0,
             "401; nothing kept")


def case_no_secret_configured(run: Run, folder: str) -> None:
    home = Home(folder)
    status = _post(home, None, secrets.token_urlsafe(24))
    with home.open() as desk:
        kept = len(desk.store.enquiries())
    run.step("no-secret-configured", "503", f"{status}; enquiries kept: {kept}", status == 503 and kept == 0,
             "503: no secret configured, every request is turned away")


CASES = {
    "replay-three-times": case_replay_three_times,
    "returning-contact": case_returning_contact,
    "two-contacts-held": case_two_contacts_held,
    "possible-duplicate-held": case_possible_duplicate_held,
    "refused-inputs": case_refused_inputs,
    "hidden-instruction": case_hidden_instruction,
    "ai-answer-refused": case_ai_answer_refused,
    "apply-without-approval": case_apply_without_approval,
    "approval-for-another-plan": case_approval_for_another_plan,
    "apply-twice": case_apply_twice,
    "crm-down-then-retry": case_crm_down_then_retry,
    "wrong-secret": case_wrong_secret,
    "no-secret-configured": case_no_secret_configured,
}


def run_all(out: str, say, keep: dict | None = None) -> Run:
    """The story, then every case, each in a folder of its own under `out`; then the closing table."""
    run = Run(say)
    say("the whole story, on new files, with the invented data and a fixed clock")
    story(run, os.path.join(out, "story"), keep)
    say("")
    say("every case of the gallery, each on new files")
    for name, case in CASES.items():
        case(run, os.path.join(out, "case-" + name))
    say("")
    for line in run.closing_table():
        say(line)
    return run


def _check_out(out: str) -> None:
    if os.path.exists(out) and os.listdir(out):
        raise SystemExit(f"demo.py: {out} is not empty; the demo runs on new files only")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The whole story and the gallery of cases, on new files.")
    parser.add_argument("--out", help="a new or empty folder for the demo's files (default: a temporary folder)")
    parser.add_argument("--case", choices=sorted(CASES), help="run one case only")
    parser.add_argument("--list", action="store_true", help="list the cases")
    args = parser.parse_args(argv)
    if args.list:
        for name in CASES:
            print(name)
        return 0
    if not os.path.isdir(RECORDED_DIR):
        raise SystemExit("demo.py: recorded/ is missing")
    with tempfile.TemporaryDirectory() as scratch:
        out = args.out or os.path.join(scratch, "demo-run")
        _check_out(out)
        if args.case:
            run = Run(print)
            CASES[args.case](run, os.path.join(out, "case-" + args.case))
            for line in run.closing_table()[1:]:
                print(line)
        else:
            run = run_all(out, print)
    return 0 if all(ok for _, ok, _ in run.rows) else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    sys.exit(main())
