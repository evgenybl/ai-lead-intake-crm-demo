"""Rules of the repository itself (section 1), checked on every file, and the one-gate rule on the code."""
import ast
import os
import re
import unittest

from intake.server import HOST

from tests.helpers import ROOT

SKIP_DIRS = {".git", "__pycache__", "work"}
TEXT_SUFFIXES = {".py", ".md", ".jsonl", ".json", ".txt", ".toml", ".yml", ".yaml", ".cfg", ".ini"}
TEXT_NAMES = {".gitignore", ".gitattributes", "LICENSE"}
CRM_WRITES = {"create_contact", "fill_empty_fields", "add_note", "add_task"}

# Built from pieces, so that this file does not hold the shapes it looks for.
KEY_SHAPES = [
    re.compile("s" + "k-[A-Za-z0-9_-]{16,}"),
    re.compile("gh" + "p_[A-Za-z0-9]{20,}"),
    re.compile("github" + "_pat_[A-Za-z0-9_]{20,}"),
    re.compile("AK" + "IA[0-9A-Z]{16}"),
    re.compile("xo" + "x[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile("-----BEGIN" + " [A-Z ]*PRIVATE KEY"),
    re.compile("[\"'][A-Za-z0-9+/_=-]{32,}[\"']"),
]
ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"  # a drive letter
    + "|/" + "home/|/" + "Users/"  # a home folder
    + r"|\\\\[A-Za-z0-9._-]+\\"  # a network share
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def repository_files():
    for folder, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1] in TEXT_SUFFIXES or name in TEXT_NAMES:
                yield os.path.join(folder, name)


def relative(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


class RepositoryRuleTests(unittest.TestCase):
    def test_there_are_files_to_check(self):
        names = {relative(p) for p in repository_files()}
        self.assertIn("intake/gate.py", names)
        self.assertIn("data/enquiries.synthetic.jsonl", names)

    def test_no_absolute_path_in_any_file(self):
        found = [f"{relative(p)}: {m.group(0)!r}" for p in repository_files()
                 for m in ABSOLUTE_PATH.finditer(read(p).decode("utf-8"))]
        self.assertEqual(found, [])

    def test_no_key_shaped_or_token_shaped_literal_in_any_file(self):
        found = [f"{relative(p)}: {shape.pattern}" for p in repository_files()
                 for shape in KEY_SHAPES if shape.search(read(p).decode("utf-8"))]
        self.assertEqual(found, [])

    def test_every_email_address_is_at_example_com(self):
        found = [f"{relative(p)}: {m.group(0)}" for p in repository_files()
                 for m in EMAIL.finditer(read(p).decode("utf-8"))
                 if not m.group(0).lower().endswith("@example.com")]
        self.assertEqual(found, [])

    def test_every_text_file_has_lf_line_ends(self):
        self.assertEqual([relative(p) for p in repository_files() if b"\r" in read(p)], [])

    def test_every_python_file_is_valid_python_3_11(self):
        for path in repository_files():
            if path.endswith(".py"):
                with self.subTest(file=relative(path)):
                    ast.parse(read(path).decode("utf-8"), filename=relative(path), feature_version=(3, 11))

    def test_servers_bind_127_0_0_1_only(self):
        self.assertEqual(HOST, "127.0.0.1")
        for path in repository_files():
            if path.endswith(".py"):
                self.assertNotIn("0.0.0" + ".0", read(path).decode("utf-8"), relative(path))


class OneGateTests(unittest.TestCase):
    def test_only_the_gate_calls_a_crm_write(self):
        """Inside intake/, a call of create_contact, fill_empty_fields, add_note or add_task appears in
        intake/gate.py and nowhere else (the adapters in intake/crm/ define them)."""
        callers = []
        package = os.path.join(ROOT, "intake")
        for folder, _, files in os.walk(package):
            for name in files:
                path = os.path.join(folder, name)
                rel = relative(path)
                if not name.endswith(".py") or rel == "intake/gate.py" or rel.startswith("intake/crm/"):
                    continue
                for node in ast.walk(ast.parse(read(path).decode("utf-8"))):
                    if isinstance(node, ast.Attribute) and node.attr in CRM_WRITES:
                        callers.append(f"{rel}:{node.lineno} {node.attr}")
        self.assertEqual(callers, [])

    def test_the_gate_reaches_each_write_through_its_one_write_method(self):
        tree = ast.parse(read(os.path.join(ROOT, "intake", "gate.py")).decode("utf-8"))
        direct = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in CRM_WRITES:
                direct.append(f"line {node.lineno}: {node.func.attr}(...) called directly")
        self.assertEqual(direct, [], "every CRM write in the gate must pass through WriteGate._write")


if __name__ == "__main__":
    unittest.main()
