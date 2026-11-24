"""Where the desk's two files live, and which CRM adapter a command uses.

--home (or INTAKE_HOME, or ./work) holds desk.sqlite3 and practice-crm.sqlite3.
--crm (or INTAKE_CRM, or "practice") picks the adapter; "http" reads INTAKE_CRM_URL and INTAKE_CRM_TOKEN.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

from .crm.base import CRMError
from .crm.http_crm import TOKEN_ENV, URL_ENV, HttpCRM
from .crm.practice import PracticeCRM
from .flow import Desk
from .store import DeskStore, HomeError

HOME_ENV = "INTAKE_HOME"
CRM_ENV = "INTAKE_CRM"
DEFAULT_HOME = "work"
DESK_FILE = "desk.sqlite3"
CRM_FILE = "practice-crm.sqlite3"
ADAPTERS = ("practice", "http")


def resolve_home(home: str | None, environ) -> str:
    return home or environ.get(HOME_ENV) or DEFAULT_HOME


def desk_path(home: str) -> str:
    return os.path.join(home, DESK_FILE)


def crm_path(home: str) -> str:
    return os.path.join(home, CRM_FILE)


def adapter_choice(choice: str | None, environ) -> str:
    chosen = choice or environ.get(CRM_ENV) or "practice"
    if chosen not in ADAPTERS:
        raise HomeError(f"{CRM_ENV} must be one of {', '.join(ADAPTERS)}, not {chosen!r}")
    return chosen


def open_crm(home: str, choice: str, environ):
    if choice == "practice":
        try:
            return PracticeCRM(crm_path(home))
        except CRMError as error:
            raise HomeError(str(error)) from None
    url, token = environ.get(URL_ENV, ""), environ.get(TOKEN_ENV, "")
    missing = [name for name, value in ((URL_ENV, url), (TOKEN_ENV, token)) if not value]
    if missing:
        raise HomeError(f"the HTTP CRM is not configured: set {' and '.join(missing)}")
    try:
        return HttpCRM(url, token)
    except CRMError as error:
        raise HomeError(str(error)) from None


@contextmanager
def open_desk(home: str, choice: str, environ, clock):
    """The desk and its CRM adapter for one command or one HTTP request; both closed afterwards."""
    store = DeskStore.open(desk_path(home), clock)
    try:
        crm = open_crm(home, choice, environ)
    except BaseException:
        store.close()
        raise
    try:
        yield Desk(store, crm, clock)
    finally:
        crm.close()
        store.close()
