"""The README's commands, run in the order it gives them, print what it shows.

Each block after a `<!-- run: ... -->` marker holds commands (lines starting with "$ ") and what each printed.
The test runs them in a new folder that holds a copy of the invented data, with the clock the README names,
and compares every line shown; a line "..." stands for lines the README leaves out. The server block is run
as a real process on a port the system assigns, and the curl lines are sent to it as curl would send them."""
import contextlib
import http.client
import io
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

from intake.cli import main as desk_main

from tests.helpers import ROOT

RUN = re.compile(r"<!-- run: ([^>]*) -->\n([ \t]*)```text\n(.*?)\n[ \t]*```", re.DOTALL)
NOW = "2026-01-05T15:00:00Z"  # the clock of the example story: after the last enquiry was sent
README_PORT = "8765"


def readme_blocks() -> list:
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
        text = handle.read()
    blocks = []
    for what, indent, body in RUN.findall(text):
        lines = [line[len(indent):] if line.startswith(indent) else line for line in body.split("\n")]
        blocks.append((what, lines))
    return blocks


def commands(lines: list) -> list:
    """[(the command, [the lines shown after it])], joining a command's continuation lines (ending in \\)."""
    found = []
    for line in lines:
        if line.startswith("$ "):
            found.append([line[2:], []])
        elif found and found[-1][0] and found[-1][0].endswith("\\"):
            found[-1][0] = found[-1][0][:-1] + " " + line.strip()
        elif found and found[-1][0] and not _closed(found[-1][0]):  # a quoted argument goes on to this line
            found[-1][0] += "\n" + line
        elif found:
            found[-1][1].append(line)
        else:
            found.append([None, [line]])  # output with no command of its own: the command before, sent again
    return found


def _closed(command: str) -> bool:
    try:
        shlex.split(command)
    except ValueError:
        return False
    return True


def shown_in(expected: list, actual: list) -> bool:
    """Every line shown is printed, in order; "..." stands for lines left out, anywhere but at no edge it lacks."""
    segments, current = [], []
    for line in expected:
        if line == "...":
            segments.append(current)
            current = []
        else:
            current.append(line)
    segments.append(current)
    position = 0
    for number, segment in enumerate(segments):
        if not segment:
            continue
        first, last = number == 0, number == len(segments) - 1
        starts = [i for i in range(position, len(actual) - len(segment) + 1) if actual[i:i + len(segment)] == segment]
        if first:
            starts = [i for i in starts if i == 0]
        if last:
            starts = [i for i in starts if i + len(segment) == len(actual)]
        if not starts:
            return False
        position = starts[0] + len(segment)
    return True


class ReadmeCommandTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = self._tmp.name
        os.makedirs(os.path.join(self.folder, "data"))
        shutil.copy(os.path.join(ROOT, "data", "enquiries.synthetic.jsonl"), os.path.join(self.folder, "data"))
        before = os.getcwd()
        os.chdir(self.folder)  # the README's commands name their files relative to the repository's folder
        self.addCleanup(os.chdir, before)

    def desk(self, command: str) -> list:
        argv = shlex.split(command)
        self.assertEqual(argv[:3], ["python", "-I", "desk.py"], command)
        out = io.StringIO()
        desk_main(argv[3:], out=out, environ={"INTAKE_NOW": NOW})
        return out.getvalue().rstrip("\n").split("\n")

    def test_the_walk_through_s_clock_comes_after_every_enquiry_of_the_invented_file_was_sent(self):
        with open(os.path.join(ROOT, "data", "enquiries.synthetic.jsonl"), encoding="utf-8") as handle:
            sent = [json.loads(line)["received_at"] for line in handle if line.strip()]
        self.assertGreater(NOW, max(sent))
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        for named in (f"`export INTAKE_NOW={NOW}`", f'`$env:INTAKE_NOW = "{NOW}"`', f"with INTAKE_NOW={NOW} -->"):
            self.assertIn(named, readme)
        self.assertEqual(sorted(set(re.findall(r"INTAKE_NOW\W+(2026-[0-9T:Z-]+)", readme))), [NOW])

    def test_the_blocks_are_there(self):
        whats = [what for what, _ in readme_blocks()]
        self.assertTrue(whats and whats[0].startswith("in order, from the repository's folder, with INTAKE_NOW=" + NOW))
        self.assertIn("python -I demo.py --case apply-twice", whats)
        self.assertTrue(any(what.startswith("the server") for what in whats))

    def test_the_opening_command_prints_the_opening_block_after_the_first_two_commands_of_try_it(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        opening = re.search(r"\(`(python -I desk\.py plan [^`]+)`, once the first two commands of \"Try it\"", readme)
        self.assertIsNotNone(opening)
        block = re.search(r"<!-- from examples/plan-one-in-full.txt -->\n```text\n(.*?)\n```", readme, re.DOTALL)
        first, second = [command for command, _ in commands(readme_blocks()[0][1])][:2]
        self.assertEqual((first.split()[3], second.split()[3]), ("init", "ingest"))
        self.desk(first)
        self.desk(second)
        printed = self.desk(" ".join(opening.group(1).split()))
        self.assertTrue(shown_in(["...", *block.group(1).split("\n"), "..."], printed), "\n".join(printed))

    def test_the_commands_may_be_python3_with_3_11_or_later(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            try_it = " ".join(handle.read().split("## Try it", 1)[1].split("<!-- run:", 1)[0].split())
        self.assertIn("on macOS and Linux the command may be `python3` instead, and it must be Python 3.11 or later",
                      try_it)

    def test_every_command_prints_what_the_readme_shows_in_the_readme_s_order(self):
        server = None
        try:
            for what, lines in readme_blocks():
                for command, expected in commands(lines):
                    with self.subTest(command=command or "(sent again)"):
                        if what.startswith("the server"):
                            server = self.server_step(server, command, expected)
                        elif command and command.startswith("python -I demo.py"):
                            self.assertEqual(self.demo(command), expected)
                        elif command and command.startswith("python -I desk.py"):
                            actual = self.desk(command)
                            self.assertTrue(shown_in(expected, actual),
                                            f"$ {command}\nshown:\n" + "\n".join(expected) + "\nprinted:\n" + "\n".join(actual))
                        else:
                            self.fail(f"a command the test does not know: {command!r}")
        finally:
            if server is not None:
                server["process"].terminate()
                server["process"].communicate(timeout=10)
        self.assertIsNotNone(server, "the server block was not run")

    def demo(self, command: str) -> list:
        sys.path.insert(0, ROOT)
        try:
            import demo
        finally:
            sys.path.remove(ROOT)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            demo.main(shlex.split(command)[3:])
        return out.getvalue().rstrip("\n").split("\n")

    def server_step(self, server, command, expected):
        if command is not None and command.startswith("export INTAKE_SECRET="):
            return {"secret": secrets.token_urlsafe(24)}
        if command is not None and command.startswith("python -I desk.py serve"):
            argv = shlex.split(command)[3:]
            argv[argv.index("--port") + 1] = "0"
            env = {k: v for k, v in os.environ.items() if not k.startswith(("INTAKE_", "ANTHROPIC_"))}
            env.update({"INTAKE_SECRET": server["secret"], "INTAKE_NOW": NOW, "PYTHONIOENCODING": "utf-8"})
            process = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "desk.py"), *argv], cwd=self.folder,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True,
                                       encoding="utf-8")
            server["process"] = process
            first = [process.stdout.readline().rstrip("\n") for _ in expected]
            server["port"] = int(re.search(r"127\.0\.0\.1:(\d+)/", first[0]).group(1))
            self.assertEqual([line.replace(f":{server['port']}/", f":{README_PORT}/") for line in first], expected)
            return server
        if command is not None and command.startswith("curl "):
            server["curl"] = shlex.split(command)
        argv = server["curl"]
        body = argv[argv.index("-d") + 1].encode("utf-8")
        headers = {}
        for i, word in enumerate(argv):
            if word == "-H":
                name, _, value = argv[i + 1].partition(": ")
                headers[name] = server["secret"] if value == "$INTAKE_SECRET" else value
        connection = http.client.HTTPConnection("127.0.0.1", server["port"], timeout=10)
        try:
            connection.request("POST", "/intake", body=body, headers=headers)
            printed = connection.getresponse().read().decode("utf-8")  # curl -s prints the body as it came
        finally:
            connection.close()
        self.assertEqual([printed], expected)
        json.loads(printed)
        return server


if __name__ == "__main__":
    unittest.main()
