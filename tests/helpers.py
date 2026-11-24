"""What the tests share: a desk in a new temporary folder, the command line driven through main(argv),
a valid invented enquiry, and a CRM wrapper that fails a chosen call."""
from __future__ import annotations

import importlib.util
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager

from intake import doctor
from intake.cli import main
from intake.clock import Clock
from intake.crm.base import CRMError
from intake.crm.practice import PracticeCRM
from intake.flow import Desk
from intake.home import crm_path, desk_path
from intake.store import DeskStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_FILE = os.path.join(ROOT, "data", "enquiries.synthetic.jsonl")
FIXED = "2026-01-05T09:00:00Z"
PERSON = "Dana Reviewer (invented)"
WRONG = "wrong" + "-"  # the start of a made-up value that must be refused; joined so no line reads as a credential


def made_up(length: int = 8) -> str:
    """A value made at run time, never one this desk or its fakes accept."""
    import secrets

    return WRONG + secrets.token_hex(length)


def load_generator():
    spec = importlib.util.spec_from_file_location("data_generate", os.path.join(ROOT, "data", "generate.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def enquiry(**overrides) -> dict:
    """A valid, new enquiry from an invented person; override any field."""
    value = {
        "source": "webform",
        "source_id": "test-000001",
        "received_at": "2026-01-05T08:00:00Z",
        "name": "Yara Test (invented)",
        "email": "yara.test@example.com",
        "phone": "000-000-0301",
        "company": "Test Bakery (invented)",
        "message": "Could you quote for cleaning our bakery?",
        "consent": True,
        "website": "",
    }
    value.update(overrides)
    return value


def approve_in(desk, key: str, by: str = PERSON) -> str:
    """Approve the plan that waits now, named by its sha256, as a person does after reading `desk.py plan`."""
    return desk.approve(key, by, desk.store.get_enquiry(key).plan_sha)


class FlakyCRM:
    """Wraps a CRM and fails one chosen write, once.

    mode "before": raise before the write (a CRM that is down: nothing happens).
    mode "after":  do the write, then raise (the answer was lost: the write happened)."""

    def __init__(self, inner, method: str, mode: str, times: int = 1):
        self._inner, self._method, self._mode, self._left = inner, method, mode, times

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if name != self._method:
            return attribute

        def flaky(*args):
            if self._left > 0:
                self._left -= 1
                if self._mode == "before":
                    raise CRMError(f"{name}: the CRM did not answer (injected)")
                attribute(*args)
                raise CRMError(f"{name}: the answer was lost (injected)")
            return attribute(*args)

        return flaky


class DeskCase(unittest.TestCase):
    """A new home in a temporary folder for every test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.join(self._tmp.name, "home")

    # -- the command line --
    def cli(self, *argv, env=None, expect=0, at=FIXED) -> str:
        out = io.StringIO()
        environ = {"INTAKE_NOW": at}
        environ.update(env or {})
        code = main([*argv, "--home", self.home], out=out, environ=environ)
        self.assertEqual(code, expect, f"desk.py {' '.join(argv)} gave {code}:\n{out.getvalue()}")
        return out.getvalue()

    def init(self, empty: bool = False) -> None:
        self.cli("init", *(["--empty-crm"] if empty else []))

    def plan_sha(self, key: str) -> str:
        with self.desk() as desk:
            return desk.store.get_enquiry(key).plan_sha

    def approve_cli(self, key: str, by: str = PERSON, expect: int = 0, at: str = FIXED) -> str:
        """`desk.py approve`, naming the plan that waits now by the 12 hex digits `desk.py plan` prints."""
        return self.cli("approve", key, "--by", by, "--plan", self.plan_sha(key)[:12], expect=expect, at=at)

    # -- the same objects the commands use --
    @contextmanager
    def desk(self, crm=None, at: str = FIXED):
        clock = Clock(at)
        store = DeskStore.open(desk_path(self.home), clock)
        own = crm is None
        crm = crm if crm is not None else PracticeCRM(crm_path(self.home))
        try:
            yield Desk(store, crm, clock)
        finally:
            if own:
                crm.close()
            store.close()

    def crm_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(crm_path(self.home))
        self.addCleanup(conn.close)
        return conn

    def desk_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(desk_path(self.home), isolation_level=None)
        self.addCleanup(conn.close)
        return conn

    def crm_counts(self) -> dict:
        conn = sqlite3.connect(crm_path(self.home))
        try:
            return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("contacts", "notes", "tasks")}
        finally:
            conn.close()

    def problems(self, with_crm: bool = True) -> list:
        """Every problem `desk.py doctor` finds, as text."""
        checks = doctor.examine(desk_path(self.home), crm_path(self.home) if with_crm else None)
        return [f"{c.name}: {p}" for c in checks for p in c.problems]

    def notes_with_key(self, key: str) -> int:
        rows = self.crm_db().execute("SELECT body FROM notes").fetchall()
        return sum(1 for (body,) in rows if body.split("\n", 1)[0] == key)
