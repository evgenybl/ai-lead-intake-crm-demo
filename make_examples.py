"""Make examples/ and docs/img/demo.svg from a real run of the whole demo.

    python -I make_examples.py          write them
    python -I make_examples.py --check  compare with what is committed; exit 1 on any difference

The run uses the committed data, its fixed seed, fixed times and the answers under recorded/, so the output is
the same on every computer. CI runs --check on Linux and Windows. Nothing here is typed by hand: every file is
what a command printed or wrote.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import demo  # noqa: E402
from intake.cli import main as desk_main  # noqa: E402

EXAMPLES = "examples"
PICTURE = "docs/img/demo.svg"
PICTURE_TITLE = "python -I demo.py --out demo-run   (its closing table)"
KEPT = {  # file in examples/ -> what the story kept under that name
    "ingest-first.txt": "ingest-1.txt",
    "report-first.txt": "report-1.txt",
    "plan.txt": "plan.txt",
    "plan-one-in-full.txt": "plan-one.txt",
    "apply.txt": "apply-1.txt",
    "ingest-again.txt": "ingest-2.txt",
    "report.txt": "report-2.txt",
    "status.txt": "status.txt",
    "doctor.txt": "doctor.txt",
    "trail.txt": "trail.txt",
}


def _term_svg():
    spec = importlib.util.spec_from_file_location("term_svg", os.path.join(HERE, "tools", "term_svg.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(scratch: str) -> dict:
    """{path relative to the repository: its exact bytes}, from one run of the whole demo."""
    printed, kept = [], {}
    run = demo.run_all(os.path.join(scratch, "demo-run"), printed.append, kept)
    if not all(ok for _, ok, _ in run.rows):
        raise SystemExit("make_examples.py: the demo did not go as expected; nothing written:\n" + "\n".join(printed))
    files = {f"{EXAMPLES}/demo.txt": "\n".join(printed) + "\n"}
    closing = run.closing_table()
    files[f"{EXAMPLES}/closing-table.txt"] = "\n".join(closing) + "\n"
    for name, source in KEPT.items():
        files[f"{EXAMPLES}/{name}"] = kept[source]
    pages = os.path.join(scratch, "desk")
    with open(os.devnull, "w") as sink:
        desk_main(["render", "--out", pages, "--home", os.path.join(scratch, "demo-run", "story"),
                   "--at", "2026-01-05T11:30:00Z"], out=sink, environ=dict(demo.ENV))
    for folder, _, names in os.walk(pages):
        for name in names:
            path = os.path.join(folder, name)
            relative = os.path.relpath(path, pages).replace(os.sep, "/")
            with open(path, encoding="utf-8") as handle:
                files[f"{EXAMPLES}/desk/{relative}"] = handle.read()
    svg, cut = _term_svg().draw(closing, PICTURE_TITLE, demo.LINE_MAX)
    if cut:
        raise SystemExit(f"make_examples.py: {cut} lines of the closing table are longer than {demo.LINE_MAX}")
    files[PICTURE] = svg
    return {path: text.encode("utf-8") for path, text in files.items()}


def committed() -> set:
    found = {PICTURE} if os.path.exists(os.path.join(HERE, PICTURE)) else set()
    for folder, _, names in os.walk(os.path.join(HERE, EXAMPLES)):
        for name in names:
            found.add(os.path.relpath(os.path.join(folder, name), HERE).replace(os.sep, "/"))
    return found


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Make examples/ and docs/img/demo.svg from a real run.")
    parser.add_argument("--check", action="store_true", help="compare with the committed files; write nothing")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as scratch:
        wanted = build(scratch)
    differ = []
    for relative, data in sorted(wanted.items()):
        path = os.path.join(HERE, *relative.split("/"))
        try:
            with open(path, "rb") as handle:
                same = handle.read() == data
        except FileNotFoundError:
            same = False
        if not same:
            differ.append(relative)
            if not args.check:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as handle:
                    handle.write(data)
    stale = sorted(committed() - set(wanted))
    if args.check:
        for relative in differ:
            print(f"differs: {relative}")
        for relative in stale:
            print(f"not made by this run: {relative}")
        print(f"make_examples: {len(wanted)} files compared, {len(differ)} differ, {len(stale)} not made by this run")
        return 1 if differ or stale else 0
    for relative in stale:
        os.remove(os.path.join(HERE, *relative.split("/")))
        print(f"removed, no longer made: {relative}")
    print(f"make_examples: {len(wanted)} files, {len(differ)} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
