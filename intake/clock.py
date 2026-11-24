"""The one clock. Every time the desk records comes from Clock.now().

A fixed time makes every output reproducible: pass `--at 2026-01-05T09:00:00Z` to a command, or set
INTAKE_NOW. A fixed clock does not tick; the trail keeps its order by row number.
"""
from __future__ import annotations

import datetime as _dt
import os
import re

ENV_VAR = "INTAKE_NOW"
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ClockError(ValueError):
    """A fixed time that is not a UTC time of the form 2026-01-05T09:00:00Z."""


class Clock:
    def __init__(self, fixed: str | None = None):
        if fixed is not None:
            if not _STAMP.match(fixed):
                raise ClockError(f"a fixed time must look like 2026-01-05T09:00:00Z, not {fixed!r}")
            try:
                _dt.datetime.strptime(fixed, _FORMAT)
            except ValueError:
                raise ClockError(f"{fixed!r} is not a real date and time") from None
        self._fixed = fixed

    @property
    def is_fixed(self) -> bool:
        return self._fixed is not None

    def now(self) -> str:
        if self._fixed is not None:
            return self._fixed
        return _dt.datetime.now(_dt.timezone.utc).strftime(_FORMAT)


def make_clock(at: str | None = None, environ=None) -> Clock:
    """The clock for one command: --at first, then INTAKE_NOW, then the system clock."""
    environ = os.environ if environ is None else environ
    fixed = at if at else (environ.get(ENV_VAR) or None)
    return Clock(fixed)
