"""The documents say only what a run printed; the demo and the examples are what their commands make."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest

from tests.helpers import ROOT

QUOTE = re.compile(r"<!-- from (examples/[^ ]+) -->\n([ \t]*)```[a-z]*\n(.*?)\n[ \t]*```", re.DOTALL)
ANSWER_QUOTE = re.compile(r"<!-- quote (recorded/answers/[0-9a-f]{64}\.answer\.txt) -->\n[ \t]*> (.+)\n")
IMAGE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)|<img[^>]+src=\"([^\"]+)\"")
GALLERY_CASES = ["replay-three-times", "returning-contact", "two-contacts-held", "possible-duplicate-held",
                   "refused-inputs", "hidden-instruction", "ai-answer-refused", "apply-without-approval",
                   "approval-for-another-plan", "apply-twice", "crm-down-then-retry", "wrong-secret",
                   "no-secret-configured"]


def load(name, *path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, *path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def markdown_files():
    for folder, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", "work", "demo-run")]
        for name in names:
            if name.endswith(".md"):
                yield os.path.join(folder, name)


def read(*path):
    with open(os.path.join(ROOT, *path), encoding="utf-8") as handle:
        return handle.read()


class DocumentTests(unittest.TestCase):
    def test_the_flow_comes_first_after_the_opening_paragraph_and_before_any_terminal_block(self):
        readme = read("README.md")
        invented = readme.index("**A demonstration on invented data:**")
        flow = readme.index("```mermaid")
        self.assertEqual(readme.count("```mermaid"), 1)
        self.assertLess(invented, flow)
        self.assertLess(flow, readme.index("```text"))
        self.assertEqual(readme[invented:flow].count("\n\n"), 1)  # directly after that paragraph

    def test_the_enquiry_page_picture_is_captioned_with_how_it_was_made(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        self.assertIn("](docs/img/enquiry-page.png)", readme)
        self.assertIn("*A capture of `examples/desk/enquiry/webform~wf-000109-ad3b9127.html`", readme)
        self.assertTrue(os.path.exists(os.path.join(ROOT, "examples", "desk", "enquiry", "webform~wf-000109-ad3b9127.html")))

    def test_every_image_a_markdown_file_embeds_exists(self):
        seen = 0
        for path in markdown_files():
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            for match in IMAGE.finditer(text):
                target = match.group(1) or match.group(2)
                seen += 1
                with self.subTest(file=os.path.relpath(path, ROOT), image=target):
                    self.assertFalse(re.match(r"[a-z]+:", target), "an image must be a file in the repository")
                    self.assertTrue(os.path.exists(os.path.join(os.path.dirname(path), *target.split("/"))))
        self.assertGreater(seen, 0)

    def test_every_block_quoted_from_the_examples_is_a_real_slice_of_its_file(self):
        readme = read("README.md")
        quotes = QUOTE.findall(readme)
        self.assertEqual({source for source, _, _ in quotes}, {"examples/plan-one-in-full.txt", "examples/report.txt",
                                                              "examples/doctor.txt", "examples/ingest-again.txt"})
        for source, indent, block in quotes:
            with self.subTest(source=source):
                lines = [line[len(indent):] if line.startswith(indent) else line for line in block.split("\n")]
                original = read(*source.split("/")).split("\n")
                starts = [i for i, line in enumerate(original) if line == lines[0]]
                self.assertTrue(any(original[i:i + len(lines)] == lines for i in starts),
                                f"the block quoted from {source} is not in it as quoted")

    def test_every_quoted_model_sentence_is_a_real_slice_of_its_recorded_answer(self):
        quotes = ANSWER_QUOTE.findall(read("README.md"))
        self.assertGreaterEqual(len(quotes), 6)
        for source, sentence in quotes:
            with self.subTest(source=source):
                with open(os.path.join(ROOT, *source.split("/")), "rb") as handle:
                    raw = handle.read()
                meta_path = source.replace(".answer.txt", ".json")
                meta = json.loads(read(*meta_path.split("/")))
                self.assertEqual(meta["origin"], "recorded")
                self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["answer_sha256"])  # unchanged since added
                answer = json.loads(raw.decode("utf-8"))
                said = [answer.get("summary") or "", answer.get("reply_draft") or ""]
                self.assertTrue(any(sentence.strip() in text for text in said), sentence)

    def test_the_status_output_on_the_readme_is_what_the_command_prints(self):
        block = re.search(r"<!-- output of python -I tools/recordings.py status -->\n```text\n(.*?)```",
                          read("README.md"), re.DOTALL)
        self.assertIsNotNone(block)
        recordings = load("recordings_for_docs", "tools", "recordings.py")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            recordings.main(["status"])
        self.assertEqual(block.group(1), out.getvalue())

    def test_the_test_list_is_generated_from_the_tests(self):
        tool = load("list_tests", "tools", "list_tests.py")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(tool.main(["--check"]), 0, out.getvalue())
        loader = unittest.defaultTestLoader.discover(os.path.join(ROOT, "tests"), top_level_dir=ROOT)
        listed = sum(len(tests) for _, _, classes in tool.collect() for _, _, tests in classes)
        self.assertEqual(listed, loader.countTestCases())

    def test_each_row_of_the_design_s_table_of_hand_edits_names_a_test_that_exists_or_says_it_is_not_tested(self):
        design = read("docs", "DESIGN.md")
        table = design.split("| The manipulation | What `doctor` reports | Test |")[1].split("\n\n")[0]
        rows = [line for line in table.strip().splitlines()[1:] if line.startswith("|")]
        self.assertGreaterEqual(len(rows), 8)
        named = set()
        for row in rows:
            test_cell = row.rstrip(" |").rsplit(" | ", 1)[1]
            found = re.findall(r"`(test_\w+)`", test_cell)
            with self.subTest(row=row[:60]):
                self.assertTrue(found or "not tested" in test_cell)
            named.update(found)
        defined = set()
        for name in os.listdir(os.path.join(ROOT, "tests")):
            if name.endswith(".py"):
                defined.update(re.findall(r"def (test_\w+)", read("tests", name)))
        self.assertEqual(sorted(named - defined), [])
        self.assertEqual(sorted(set(re.findall(r"`(test_\w+)`", design)) - defined), [])  # anywhere in DESIGN

    def test_the_licence_is_mit_with_its_copyright_line(self):
        licence = read("LICENSE")
        self.assertTrue(licence.startswith("MIT License\n\nCopyright (c) 2026 Evgeny Blekhman\n"))

    def test_ci_pins_its_actions_by_commit_and_runs_both_systems_and_pythons(self):
        ci = read(".github", "workflows", "ci.yml")
        uses = re.findall(r"uses: (\S+)", ci)
        self.assertEqual(uses, ["actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
                                "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"])
        self.assertIn("os: [ubuntu-latest, windows-latest]", ci)
        self.assertIn('python: ["3.11", "3.13"]', ci)
        commands = ["python -I run_tests.py", "python -I demo.py --out demo-run", "python -I make_examples.py --check",
                    "python -I data/generate.py --check", "python -I tools/recordings.py requests --check",
                    "python -I tools/recordings.py construct --check", "python -I tools/list_tests.py --check",
                    "git diff --exit-code"]
        positions = [ci.find(command) for command in commands]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))  # in this order, the diff last

    def test_the_readme_never_says_live_for_a_path_that_was_run(self):
        table = read("README.md").split("## What is real here")[1].split("\n## ")[0]
        rows = [line for line in table.splitlines() if line.startswith("| ") and not line.startswith("| Path")]
        kinds = {line.split("|")[2].strip() for line in rows if not line.startswith("|---")}
        self.assertEqual(kinds - {"Local", "Recorded", "Mock"}, set())


class PictureToolTests(unittest.TestCase):
    def test_it_draws_exactly_the_lines_it_is_given(self):
        term_svg = load("term_svg", "tools", "term_svg.py")
        svg, cut = term_svg.draw(["PASS  a case", "a <b> & c", "x refused here"], "a title", 40)
        self.assertEqual(cut, 0)
        texts = re.findall(r'<text x="16" y="\d+"([^>]*)>([^<]*)</text>', svg)
        self.assertEqual([t for _, t in texts], ["PASS  a case", "a &lt;b&gt; &amp; c", "x refused here"])
        self.assertIn("#7ee787", texts[0][0])  # green
        self.assertIn("#ffa198", texts[2][0])  # red

    def test_the_closing_table_fits_and_its_passing_lines_hold_no_red_word(self):
        lines = read("examples", "closing-table.txt").splitlines()
        for line in lines:
            self.assertLessEqual(len(line), 100, line)
            if line.startswith("PASS"):
                self.assertIsNone(re.search(r"\b(REFUSED|FAIL|failed|rejected|refused)\b", line), line)


class DemoTests(unittest.TestCase):
    def test_the_demo_has_every_case_of_the_gallery(self):
        demo = load("demo_for_tests", "demo.py")
        self.assertEqual(list(demo.CASES), GALLERY_CASES)

    def test_one_case_runs_on_its_own_new_files(self):
        demo = load("demo_for_tests", "demo.py")
        with tempfile.TemporaryDirectory() as folder:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = demo.main(["--case", "crm-down-then-retry", "--out", os.path.join(folder, "run")])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("total: 1 of 1 steps and cases as expected", out.getvalue())

    def test_no_example_page_shows_an_enquiry_taken_in_approved_or_written_before_it_was_sent(self):
        folder = os.path.join(ROOT, "examples", "desk", "enquiry")
        pages = sorted(os.listdir(folder))
        self.assertEqual(len(pages), 27)
        for name in pages:
            page = read("examples", "desk", "enquiry", name)
            with self.subTest(page=name):
                received, arrived = re.search(r"Received (\S+) \(arrived (\S+), by", page).groups()
                times = re.findall(r"<tr><td>\d+</td><td>(\S+?)</td>", page)  # each row of its trail
                self.assertTrue(times)
                self.assertLessEqual(received, arrived)
                self.assertLessEqual(received, min(times))

    def test_the_examples_and_the_picture_are_what_a_new_run_makes(self):
        make_examples = load("make_examples_for_tests", "make_examples.py")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = make_examples.main(["--check"])
        self.assertEqual(code, 0, out.getvalue())


if __name__ == "__main__":
    unittest.main()
