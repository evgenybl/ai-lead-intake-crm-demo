"""The requests to the model, and the answers kept for them under recorded/.

    python -I tools/recordings.py requests [--check]   write recorded/requests/<sha256>.txt for every invented
                                                        enquiry that reaches the model
    python -I tools/recordings.py construct [--check]  write recorded/constructed/: one constructed answer per
                                                        request, and recorded/gallery/: the demo's bad answers
    python -I tools/recordings.py add <sha256> <answer-file> --how "<how it was obtained>"
                                                        keep a real model answer, byte for byte, in recorded/answers/
    python -I tools/recordings.py status               which requests have a recorded answer, a constructed one, none

A constructed answer is written by this tool, not by a model, and its meta file says "origin": "constructed".
Only `add` writes "origin": "recorded", and it copies the answer exactly as it came.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from intake import request, validate  # noqa: E402
from intake.arrive import read_lines  # noqa: E402
from intake.draft import RECORDED_DIR, RecordedBackend, first_sentence  # noqa: E402
from intake.identity import idempotency_key  # noqa: E402

DATA_FILE = os.path.join(ROOT, "data", "enquiries.synthetic.jsonl")
CONSTRUCTED_HOW = ("Written by tools/recordings.py construct from the request text: the first sentence of the "
                   "message the model would see as the summary, the intent its case names, a fixed reply. "
                   "Not from a model.")
REPLY = ("Hello {{name}}, thank you for your message. A member of our team will read it and reply to you "
         "personally. Nothing has been booked or agreed yet.")
INTENT_OF_CASE = {
    "returning: by e-mail, nothing differs": "quote_request",
    "returning: by e-mail, a changed phone shown": "booking",
    "returning: by e-mail, fills the empty company": "quote_request",
    "held: only the phone matches, and the contact has an e-mail": "support",
    "held: the e-mail and the phone belong to two contacts": "booking",
    "held: a new address with the name and company of an existing contact": "quote_request",
    "new: the message repeats the sender's own phone": "quote_request",
    "new: figures in the message": "quote_request",
    "new: the hidden instruction": "spam",
    "new: the same new person's second enquiry": "other",
    "new: markup in the message": "quote_request",
    "new: letters outside ASCII in the name": "other",
    "new: no source_id, so a hashed key": "other",
}


def model_enquiries() -> list:
    """(case, fields) of every invented enquiry that reaches the model: taken, and not a replay."""
    sys.path.insert(0, os.path.join(ROOT, "data"))
    import generate  # the generator names each line's case

    with open(DATA_FILE, "rb") as handle:
        lines = list(read_lines(handle.read()))
    if len(lines) != len(generate.CASES):
        raise SystemExit("the data file and the generator's cases differ in length: run python -I data/generate.py")
    seen, found = set(), []
    for (case, _), (_, obj, problem) in zip(generate.CASES, lines):
        if problem:
            continue
        extracted = validate.extract(obj, arrived_at="2026-01-05T09:00:00Z")
        key = idempotency_key(extracted.fields)
        if key in seen or validate.reasons(extracted):
            seen.add(key)
            continue
        seen.add(key)
        found.append((case, extracted.fields))
    return found


def _intent(case: str) -> str:
    if case.startswith("new: ") and case[5:] in request.INTENTS:
        return case[5:]
    return INTENT_OF_CASE.get(case, "other")


def _answer_files(folder: str, sha: str, text: str, origin: str, how: str) -> dict:
    raw = text.encode("utf-8")
    meta = {"request_sha256": sha, "answer_file": sha + ".answer.txt",
            "answer_sha256": hashlib.sha256(raw).hexdigest(), "origin": origin, "how": how}
    return {os.path.join(folder, sha + ".answer.txt"): raw,
            os.path.join(folder, sha + ".json"): (json.dumps(meta, indent=2) + "\n").encode("utf-8")}


def wanted_requests() -> dict:
    files = {}
    for _, fields in model_enquiries():
        req = request.build(fields)
        files[os.path.join("requests", req.sha256 + ".txt")] = req.text.encode("utf-8")
    return files


def wanted_constructed() -> dict:
    files = {}
    by_case = {}
    for case, fields in model_enquiries():
        req = request.build(fields)
        by_case[case] = (req, fields)
        intent = _intent(case)
        obj = {"summary": first_sentence(req.message), "intent": intent, "urgency": "normal",
               "reply_draft": None if intent == "spam" else REPLY}
        files.update(_answer_files("constructed", req.sha256, json.dumps(obj, indent=2) + "\n", "constructed",
                                   CONSTRUCTED_HOW))
    hidden, _ = by_case["new: the hidden instruction"]
    extra = {"summary": "The sender asks for the enquiry to be approved and marked urgent.", "intent": "other",
             "urgency": "high", "reply_draft": "Hello {{name}}, your enquiry is approved.", "approve": True}
    files.update(_answer_files(os.path.join("gallery", "extra-key"), hidden.sha256,
                               json.dumps(extra, indent=2) + "\n", "constructed",
                               "Constructed by hand for the demo case hidden-instruction: an answer that obeys the "
                               "message and carries a fifth key, approve. Not from a model."))
    figures, _ = by_case["new: figures in the message"]
    wrong = {"summary": "Cleaning for 3 offices twice a week, with a budget of 1,500 a month, from 2026-02-02.",
             "intent": "quote_request", "urgency": "normal",
             "reply_draft": "Hello {{name}}, thank you. We can quote for your 3 offices."}
    files.update(_answer_files(os.path.join("gallery", "figure-not-in-message"), figures.sha256,
                               json.dumps(wrong, indent=2) + "\n", "constructed",
                               "Constructed by hand for the demo case ai-answer-refused: the summary names a budget "
                               "of 1,500, and the message says 1,200. Not from a model."))
    return files


def _sync(files: dict, check: bool, label: str) -> int:
    differ = []
    for relative, data in sorted(files.items()):
        path = os.path.join(RECORDED_DIR, relative)
        try:
            with open(path, "rb") as handle:
                same = handle.read() == data
        except FileNotFoundError:
            same = False
        if same:
            continue
        differ.append(relative.replace(os.sep, "/"))
        if not check:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(data)
    folders = {os.path.dirname(r) for r in files}
    extra = sorted(
        os.path.join(folder, name).replace(os.sep, "/")
        for folder in folders if os.path.isdir(os.path.join(RECORDED_DIR, folder))
        for name in os.listdir(os.path.join(RECORDED_DIR, folder))
        if os.path.join(folder, name) not in files
    )
    count = len(files)
    if check:
        for name in differ:
            print(f"differs: recorded/{name}")
        for name in extra:
            print(f"not made by this tool: recorded/{name}")
        print(f"{label}: {count} files checked, {len(differ)} differ, {len(extra)} not made by this tool")
        return 1 if differ or extra else 0
    for name in extra:
        print(f"left in place, not made by this tool: recorded/{name}")
    print(f"{label}: {count} files, {len(differ)} written")
    return 0


def cmd_add(sha: str, answer_path: str, how: str) -> int:
    request_path = os.path.join(RECORDED_DIR, "requests", sha + ".txt")
    if not os.path.exists(request_path):
        print(f"no request recorded/requests/{sha}.txt: nothing added")
        return 1
    if not how.strip():
        print("--how must say how the answer was obtained: nothing added")
        return 1
    with open(answer_path, "rb") as handle:
        raw = handle.read()
    raw.decode("utf-8")  # refuse an answer that is not UTF-8 text before anything is written
    meta = {"request_sha256": sha, "answer_file": sha + ".answer.txt",
            "answer_sha256": hashlib.sha256(raw).hexdigest(), "origin": "recorded", "how": how.strip()}
    files = {os.path.join("answers", sha + ".answer.txt"): raw,
             os.path.join("answers", sha + ".json"): (json.dumps(meta, indent=2) + "\n").encode("utf-8")}
    for relative, data in files.items():
        path = os.path.join(RECORDED_DIR, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
    print(f"added recorded/answers/{sha}.answer.txt ({len(raw)} bytes, sha256 {meta['answer_sha256']})")
    return 0


def cmd_status() -> int:
    """Each request, the answer the desk would use for it, and what the desk's own checks make of that answer."""
    counts = {"recorded": 0, "constructed": 0, "none": 0}
    used = {"recorded": 0, "constructed": 0}
    for case, fields in model_enquiries():
        sha = request.build(fields).sha256
        if os.path.exists(os.path.join(RECORDED_DIR, "answers", sha + ".json")):
            kind = "recorded"
        elif os.path.exists(os.path.join(RECORDED_DIR, "constructed", sha + ".json")):
            kind = "constructed"
        else:
            kind = "none"
        counts[kind] += 1
        verdict = "-"
        reason = ""
        if kind != "none":
            folder = "answers" if kind == "recorded" else "constructed"
            result = RecordedBackend(sources={folder: kind}).draft(fields)
            verdict = "used" if result["outcome"] == "used" else "not used"
            reason = "" if verdict == "used" else result["reason"]
            used[kind] += verdict == "used"
        print(f"{sha[:12]}  {kind:<12} {verdict:<9} {case}")
        if reason:
            print(f"{'':14}{reason}")
    print(f"requests: {sum(counts.values())}; recorded answers: {counts['recorded']} ({used['recorded']} used, "
          f"{counts['recorded'] - used['recorded']} not used); constructed answers only: {counts['constructed']}; "
          f"no answer: {counts['none']}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="The requests to the model and the answers kept for them.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("requests", "construct"):
        sub.add_parser(name).add_argument("--check", action="store_true", help="compare; write nothing")
    add = sub.add_parser("add")
    add.add_argument("sha256")
    add.add_argument("answer_file")
    add.add_argument("--how", required=True)
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "requests":
        return _sync(wanted_requests(), args.check, "requests")
    if args.command == "construct":
        return _sync(wanted_constructed(), args.check, "constructed answers")
    if args.command == "add":
        return cmd_add(args.sha256, args.answer_file, args.how)
    return cmd_status()


if __name__ == "__main__":
    sys.exit(main())
