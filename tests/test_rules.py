"""Rules of the repository itself, checked on every file, and the one-gate rule on the code."""
import ast
import json
import os
import re
import unicodedata
import unittest

from intake.server import HOST

from tests.helpers import ROOT

SKIP_DIRS = {".git", "__pycache__", "work", "demo-run"}
TEXT_SUFFIXES = {".py", ".md", ".jsonl", ".json", ".txt", ".toml", ".yml", ".yaml", ".cfg", ".ini", ".html", ".svg"}
TEXT_NAMES = {".gitignore", ".gitattributes", "LICENSE"}
CRM_WRITES = {"create_contact", "fill_empty_fields", "add_note", "add_task"}

# Built from pieces, so that this file does not hold the shapes it looks for.
KEY_SHAPES = [
    re.compile("s" + "k-[A-Za-z0-9_-]{16,}"),
    re.compile("gh" + "p_[A-Za-z0-9]{20,}"),
    re.compile("github" + "_pat_[A-Za-z0-9_]{20,}"),
    re.compile("AK" + "IA[0-9A-Z]{16}"),
    re.compile("xo" + "x[abposr]-[A-Za-z0-9-]{10,}"),
    re.compile("-" * 5 + "BEG" + "IN [A-Z ]*PRIVATE KEY"),
    # a quoted run of 32 or more token characters that mixes letters and digits
    re.compile("[\"'](?=[A-Za-z0-9+/_=-]*[0-9])(?=[A-Za-z0-9+/_=-]*[A-Za-z])[A-Za-z0-9+/_=-]{32,}[\"']"),
]
SHA256_LITERAL = re.compile("[\"'][0-9a-f]{64}[\"']")
ABSOLUTE_PATH = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"  # a drive letter
    + "|/" + "home/|/" + "Users/"  # a home folder
    + r"|\\\\[A-Za-z0-9._-]+\\"  # a network share
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
# A pointer to something the reader does not have. Built from pieces, so that this file does not hold what it finds.
_REVIEW = "rev" + "iew"
OUTSIDE = re.compile(
    r"\b(?:section|sections)\s+\d"  # the numbered parts of another document
    + r"|\bthe\s+" + "specif" + r"ication\b"
    + r"|\b(?:the|a|this|that|our)\s+" + _REVIEW + r"(?:er|ers)?\b(?!\s+a\b)"  # one, or the person who made it
    + r"|\b" + _REVIEW + r"(?:er)?'s\b"
    + r"|(?-i:" + _REVIEW.capitalize() + r"ed)(?=[A-Z])"  # inside a name written in capitalised words
    + r"|\b" + "pha" + r"se[\s-]*(?:\d|one|two|three)\b",
    re.IGNORECASE)


def letter_runs(text):
    """Each run of letters and combining marks in the text, in Unicode NFC."""
    runs, current = [], []
    for c in text + " ":
        if unicodedata.category(c)[0] in "LM":
            current.append(c)
        elif current:
            runs.append(unicodedata.normalize("NFC", "".join(current)))
            current = []
    return runs


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

    def test_no_file_points_to_a_document_the_reader_does_not_have(self):
        """Each rule is said where it applies, never as a pointer to something the reader does not have: an outside
        document or its numbered parts, an outside assessment of the project, who made it, its scripts or its
        phases."""
        found = [f"{relative(p)}: {m.group(0)!r}" for p in repository_files()
                 for m in OUTSIDE.finditer(read(p).decode("utf-8"))]
        self.assertEqual(found, [])

    def test_the_pointer_rule_catches_each_kind_it_names(self):
        for text in ("see sec" + "tion 4", "as the " + "specif" + "ication says", "or was run with the " + "rev" + "iew's own "
                     "script", "The " + "rev" + "iew of this project changed", "the " + "rev" + "iewer found",
                     "class DoctorFinds" + "Rev" + "iewedThings", "the " + "pha" + "se-1 notes", "in " + "pha" + "se 2"):
            with self.subTest(text=text):
                self.assertIsNotNone(OUTSIDE.search(text))
        for text in ("Dana Reviewer (invented)", "the checks, agreements and review a real deployment needs",
                     "They have not been reviewed as a defence"):
            with self.subTest(text=text):
                self.assertIsNone(OUTSIDE.search(text))

    def test_no_key_shaped_or_token_shaped_literal_in_any_file(self):
        """A sha256 digest (exactly 64 lowercase hex digits) names a file's content; it is not a secret."""
        found = []
        for path in repository_files():
            text = SHA256_LITERAL.sub('""', read(path).decode("utf-8"))
            found += [f"{relative(path)}: {shape.pattern}" for shape in KEY_SHAPES if shape.search(text)]
        self.assertEqual(found, [])

    def test_every_email_address_is_at_example_com(self):
        found = [f"{relative(p)}: {m.group(0)}" for p in repository_files()
                 for m in EMAIL.finditer(read(p).decode("utf-8"))
                 if not m.group(0).lower().endswith("@example.com")]
        self.assertEqual(found, [])

    def test_every_letter_outside_ascii_is_part_of_an_invented_name(self):
        """English text, in ASCII letters: the only letters outside ASCII are words of the invented names and
        companies in data/ (Zo\u00eb, \u00c5ngstr\u00f6m). Any other is written as its code point (U+00DF) in
        prose, or as an escape (\\u00df) in code."""
        invented = set()
        for name in ("enquiries.synthetic.jsonl", "crm-start.synthetic.jsonl"):
            for line in read(os.path.join(ROOT, "data", name)).decode("utf-8").splitlines():
                if line.strip():
                    obj = json.loads(line)
                    invented.update(letter_runs(f"{obj.get('name') or ''} {obj.get('company') or ''}"))
        found = [f"{relative(p)}: {word!r}" for p in repository_files() for word in letter_runs(read(p).decode("utf-8"))
                 if any(ord(c) > 127 for c in word) and word not in invented]
        self.assertEqual(found, [])

    def test_no_file_holds_an_invisible_character_written_as_itself(self):
        """A character that is not shown (a byte-order mark, a zero-width space, a bidi control) is written as an
        escape, so that a reader of the file sees it."""
        found = [f"{relative(p)}: U+{ord(c):04X}" for p in repository_files() for c in set(read(p).decode("utf-8"))
                 if unicodedata.category(c) in ("Cf", "Co", "Cs")]
        self.assertEqual(sorted(found), [])

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
