"""The HTTP CRM adapter: the small REST shape of docs/CRM-API.md, with a bearer token from the environment.

Mock: it is tested against a strict fake in tests/fake_crm.py and has never been connected to any real
CRM. It is not any vendor's API; a real CRM needs its own adapter of about this size.

The token is sent in the Authorization header only, and never appears in an error message. Proxies
from the environment are not used: the adapter talks to the address it is given.
"""
from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from .. import normalise
from ..canonical import canonical_bytes
from .base import CONTACT_FIELDS, CRM, Contact, CRMConflict, CRMError, Note, Task

URL_ENV = "INTAKE_CRM_URL"
TOKEN_ENV = "INTAKE_CRM_TOKEN"


class HttpCRM(CRM):
    name = "http"
    label = "Mock: tested against a strict fake in the tests; not any vendor's API"

    def __init__(self, base_url: str, token: str, timeout: float = 5.0):
        parts = urllib.parse.urlsplit(base_url or "")
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise CRMError(f"{URL_ENV} must be an http or https address")
        if not token:
            raise CRMError(f"{TOKEN_ENV} is empty")
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # -- one call --
    def _call(self, method: str, path: str, query: dict | None = None, body: dict | None = None):
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json"}
        data = None
        if body is not None:
            data = canonical_bytes(body)
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read()
            error.close()
        except (OSError, http.client.HTTPException) as error:
            raise CRMError(f"{method} {path}: no answer ({type(error).__name__})") from None
        try:
            answer = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, ValueError):
            raise CRMError(f"{method} {path}: HTTP {status}, and the answer is not JSON") from None
        if not isinstance(answer, dict):
            raise CRMError(f"{method} {path}: HTTP {status}, and the answer is not a JSON object")
        return status, answer

    def _expect(self, method: str, path: str, ok: tuple, query=None, body=None) -> dict:
        status, answer = self._call(method, path, query, body)
        if status == 409:
            raise CRMConflict(f"{method} {path}: HTTP 409, the CRM already has a contact with this e-mail")
        if status not in ok:
            raise CRMError(f"{method} {path}: HTTP {status}")
        return answer

    @staticmethod
    def _id(contact_id: str) -> str:
        return urllib.parse.quote(str(contact_id), safe="")

    # -- reads --
    def _contacts(self, query: dict) -> list:
        answer = self._expect("GET", "/contacts", (200,), query=query)
        return [_contact(c) for c in _list(answer, "contacts")]

    def find_by_email(self, email: str) -> list:
        return self._contacts({"email": normalise.email(email)})

    def find_by_phone(self, digits: str) -> list:
        return self._contacts({"phone": digits})

    def find_by_name(self, name_norm: str, company_norm: str) -> list:
        """The server's name search gives candidates; the adapter keeps only exact normalised matches."""
        first_word = name_norm.split(" ", 1)[0]
        if not first_word or not company_norm:
            return []
        return [
            c for c in self._contacts({"name": first_word})
            if normalise.name(c.name) == name_norm and normalise.name(c.company) == company_norm
        ]

    def find_by_origin_key(self, key: str) -> list:
        return self._contacts({"origin_key": key})

    def get_contact(self, contact_id: str):
        status, answer = self._call("GET", f"/contacts/{self._id(contact_id)}")
        if status == 404:
            return None
        if status != 200:
            raise CRMError(f"GET /contacts/{contact_id}: HTTP {status}")
        return _contact(_object(answer, "contact"))

    def list_notes(self, contact_id: str) -> list:
        answer = self._expect("GET", f"/contacts/{self._id(contact_id)}/notes", (200,))
        return [_note(n) for n in _list(answer, "notes")]

    def search_notes(self, text: str) -> list:
        answer = self._expect("GET", "/notes", (200,), query={"search": text})
        return [_note(n) for n in _list(answer, "notes")]

    def list_tasks(self, contact_id: str) -> list:
        answer = self._expect("GET", f"/contacts/{self._id(contact_id)}/tasks", (200,))
        return [_task(t) for t in _list(answer, "tasks")]

    # -- writes: called by intake/gate.py only --
    def create_contact(self, fields: dict, origin_key: str) -> Contact:
        body = {name: str(fields.get(name, "")).strip() for name in CONTACT_FIELDS}
        body["email"] = normalise.email(body["email"])
        body["origin_key"] = origin_key
        answer = self._expect("POST", "/contacts", (201,), body=body)
        return _contact(_object(answer, "contact"))

    def fill_empty_fields(self, contact_id: str, fields: dict) -> dict:
        answer = self._expect("POST", f"/contacts/{self._id(contact_id)}/fill", (200,), body={"fields": dict(fields)})
        return {"filled": list(_list(answer, "filled")), "kept": list(_list(answer, "kept"))}

    def add_note(self, contact_id: str, body: str) -> Note:
        answer = self._expect("POST", f"/contacts/{self._id(contact_id)}/notes", (201,), body={"body": body})
        return _note(_object(answer, "note"))

    def add_task(self, contact_id: str, body: str) -> Task:
        answer = self._expect("POST", f"/contacts/{self._id(contact_id)}/tasks", (201,), body={"body": body})
        return _task(_object(answer, "task"))

    def check(self) -> tuple:
        try:
            self._expect("GET", "/health", (200,))
        except CRMError as error:
            return "error", str(error)
        return "connected", "GET /health answered"


# -- reading answers strictly --

def _list(answer: dict, name: str) -> list:
    value = answer.get(name)
    if not isinstance(value, list):
        raise CRMError(f"the CRM's answer has no list {name!r}")
    return value


def _object(answer: dict, name: str) -> dict:
    value = answer.get(name)
    if not isinstance(value, dict):
        raise CRMError(f"the CRM's answer has no object {name!r}")
    return value


def _text(obj: dict, name: str) -> str:
    value = obj.get(name, "")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        raise CRMError(f"the CRM's answer has a {name!r} that is not text")
    return value


def _contact(obj) -> Contact:
    if not isinstance(obj, dict):
        raise CRMError("the CRM's answer has a contact that is not an object")
    return Contact(
        _text(obj, "id"), _text(obj, "name"), _text(obj, "email"), _text(obj, "phone"),
        _text(obj, "company"), _text(obj, "origin_key"),
    )


def _note(obj) -> Note:
    if not isinstance(obj, dict):
        raise CRMError("the CRM's answer has a note that is not an object")
    return Note(_text(obj, "id"), _text(obj, "contact_id"), _text(obj, "body"))


def _task(obj) -> Task:
    if not isinstance(obj, dict):
        raise CRMError("the CRM's answer has a task that is not an object")
    return Task(_text(obj, "id"), _text(obj, "contact_id"), _text(obj, "body"))
