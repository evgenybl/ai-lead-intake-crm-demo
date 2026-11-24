"""Write docs/TESTS.md: every test, by file and class, with the first line of its docstring, read from tests/.

    python -I tools/list_tests.py          write it
    python -I tools/list_tests.py --check  exit 1 if docs/TESTS.md is not what tests/ holds now
"""
from __future__ import annotations

import argparse
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
TARGET = os.path.join(ROOT, "docs", "TESTS.md")


def _first_line(node) -> str:
    text = ast.get_docstring(node) or ""
    return " ".join(text.strip().split("\n\n")[0].split()) if text else ""


def collect() -> list:
    """(file, module line, [(class, class line, [(test, test line)])]) for every test file, in name order."""
    found = []
    for name in sorted(os.listdir(TESTS)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        with open(os.path.join(TESTS, name), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        classes = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                tests = [(item.name, _first_line(item)) for item in node.body
                         if isinstance(item, ast.FunctionDef) and item.name.startswith("test_")]
                if tests:
                    classes.append((node.name, _first_line(node), tests))
        found.append((name, _first_line(tree), classes))
    return found


def render() -> str:
    found = collect()
    total = sum(len(tests) for _, _, classes in found for _, _, tests in classes)
    lines = [
        "# The tests",
        "",
        "Generated from `tests/` by `python -I tools/list_tests.py`; CI fails if it is out of date. Run every test with",
        "`python -I run_tests.py`. A test's name says what it proves; the line under a name is its docstring.",
        "",
        f"{total} test methods in {len(found)} files. Where a test uses subTest, one method checks several cases.",
        "",
    ]
    for name, doc, classes in found:
        lines += [f"## tests/{name}", ""]
        if doc:
            lines += [doc, ""]
        for class_name, class_doc, tests in classes:
            lines.append(f"### {class_name}")
            lines.append("")
            if class_doc:
                lines += [class_doc, ""]
            for test, test_doc in tests:
                lines.append(f"- `{test}`" + (f": {test_doc}" if test_doc else ""))
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Write docs/TESTS.md from tests/.")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = render().encode("utf-8")
    try:
        with open(TARGET, "rb") as handle:
            same = handle.read() == text
    except FileNotFoundError:
        same = False
    if args.check:
        print("docs/TESTS.md is up to date" if same else "docs/TESTS.md differs: run python -I tools/list_tests.py")
        return 0 if same else 1
    if not same:
        with open(TARGET, "wb") as handle:
            handle.write(text)
    print("wrote docs/TESTS.md" if not same else "docs/TESTS.md was already up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
