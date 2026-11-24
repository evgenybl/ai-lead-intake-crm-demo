"""A strict fake of the HTTP CRM of docs/CRM-API.md, for the tests only (section 6).

- It refuses an unknown path, method, query parameter or body field, and records each refusal in
  `violations`, so a test can assert that the adapter never sent one.
- `fail(method, path, mode)` answers 500, or hangs up without an answer, on a chosen call; "hangup_after"
  does the work first and then hangs up, which is a write whose answer was lost.
- `search_lag = n` keeps every new note out of GET /notes?search for the next n searches.

Contact ids are text ("c-1"), not numbers, so the adapter is tested with ids it cannot assume.
It binds 127.0.0.1 on a port the operating system assigns.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

CONTACT_BODY = {"name", "email", "phone", "company", "origin_key"}
FILLABLE = {"name", "email", "phone", "company"}

ROUTES = [
    ("GET", r"/health", "health"),
    ("GET", r"/contacts", "find"),
    ("POST", r"/contacts", "create"),
    ("GET", r"/contacts/(?P<id>[^/]+)", "get"),
    ("POST", r"/contacts/(?P<id>[^/]+)/fill", "fill"),
    ("GET", r"/contacts/(?P<id>[^/]+)/notes", "list_notes"),
    ("POST", r"/contacts/(?P<id>[^/]+)/notes", "add_note"),
    ("GET", r"/contacts/(?P<id>[^/]+)/tasks", "list_tasks"),
    ("POST", r"/contacts/(?P<id>[^/]+)/tasks", "add_task"),
    ("GET", r"/notes", "search"),
]
FIND_BY = {"email", "phone", "origin_key", "name"}
BODIES = {"create": CONTACT_BODY, "fill": {"fields"}, "add_note": {"body"}, "add_task": {"body"}}


def _digits(text: str) -> str:
    return "".join(ch for ch in text if ch in "0123456789")


class FakeCRM:
    def __init__(self, token: str):
        self.token = token
        self.contacts: dict = {}
        self.notes: dict = {}
        self.tasks: dict = {}
        self.calls: list = []
        self.violations: list = []
        self.search_lag = 0
        self._hidden: dict = {}
        self._failures: list = []
        self._counter = 0
        self.lock = threading.Lock()
        self._server = None
        self._thread = None

    # -- control from a test --
    def start(self) -> str:
        self._server = _Server(self)
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)

    def fail(self, method: str, path_pattern: str, mode: str, times: int = 1) -> None:
        if mode not in ("500", "hangup", "hangup_after"):
            raise ValueError(mode)
        self._failures.append([method, re.compile(path_pattern + "$"), mode, times])

    def seed(self, name: str, email: str = "", phone: str = "", company: str = "") -> str:
        contact_id = self._new_id("c")
        self.contacts[contact_id] = {"id": contact_id, "name": name, "email": email.lower(), "phone": phone,
                                     "company": company, "origin_key": ""}
        return contact_id

    def notes_carrying(self, key: str) -> list:
        return [n for n in self.notes.values() if n["body"].split("\n", 1)[0] == key]

    # -- inside --
    def _new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _failure_for(self, method: str, path: str):
        for failure in self._failures:
            if failure[3] > 0 and failure[0] == method and failure[1].match(path):
                failure[3] -= 1
                return failure[2]
        return None

    def _email_taken(self, email: str, but: str | None = None) -> bool:
        return bool(email) and any(c["email"] == email and c["id"] != but for c in self.contacts.values())

    def health(self, ids, query, body):
        return 200, {"ok": True}

    def find(self, ids, query, body):
        (name, value), = query.items()
        if name == "email":
            found = [c for c in self.contacts.values() if c["email"] and c["email"] == value.lower()]
        elif name == "phone":
            found = [c for c in self.contacts.values() if _digits(c["phone"]) and _digits(c["phone"]) == value]
        elif name == "origin_key":
            found = [c for c in self.contacts.values() if c["origin_key"] and c["origin_key"] == value]
        else:
            found = [c for c in self.contacts.values() if value.lower() in c["name"].lower()]
        return 200, {"contacts": [dict(c) for c in found]}

    def create(self, ids, query, body):
        if not all(isinstance(v, str) for v in body.values()):
            return None
        if self._email_taken(body["email"].lower()):
            return 409, {"error": "a contact with this e-mail exists"}
        contact_id = self._new_id("c")
        self.contacts[contact_id] = {"id": contact_id, **body, "email": body["email"].lower()}
        return 201, {"contact": dict(self.contacts[contact_id])}

    def get(self, ids, query, body):
        contact = self.contacts.get(ids["id"])
        return (200, {"contact": dict(contact)}) if contact else (404, {"error": "no such contact"})

    def fill(self, ids, query, body):
        fields = body["fields"]
        if not isinstance(fields, dict) or set(fields) - FILLABLE or not all(isinstance(v, str) for v in fields.values()):
            return None
        contact = self.contacts.get(ids["id"])
        if contact is None:
            return 404, {"error": "no such contact"}
        filled, kept = [], []
        for name in sorted(fields):
            if contact[name].strip():
                kept.append(name)
            elif fields[name]:
                if name == "email" and self._email_taken(fields[name].lower(), but=contact["id"]):
                    return 409, {"error": "a contact with this e-mail exists"}
                contact[name] = fields[name].lower() if name == "email" else fields[name]
                filled.append(name)
        return 200, {"filled": filled, "kept": kept}

    def _records(self, table: dict, ids) -> tuple:
        if ids["id"] not in self.contacts:
            return 404, {"error": "no such contact"}
        return 200, [dict(r) for r in table.values() if r["contact_id"] == ids["id"]]

    def list_notes(self, ids, query, body):
        status, found = self._records(self.notes, ids)
        return (status, {"notes": found}) if status == 200 else (status, found)

    def list_tasks(self, ids, query, body):
        status, found = self._records(self.tasks, ids)
        return (status, {"tasks": found}) if status == 200 else (status, found)

    def _add(self, table: dict, prefix: str, ids, body):
        if not isinstance(body["body"], str):
            return None
        if ids["id"] not in self.contacts:
            return 404, {"error": "no such contact"}
        record_id = self._new_id(prefix)
        table[record_id] = {"id": record_id, "contact_id": ids["id"], "body": body["body"]}
        return 201, dict(table[record_id])

    def add_note(self, ids, query, body):
        answer = self._add(self.notes, "n", ids, body)
        if answer and answer[0] == 201:
            self._hidden[answer[1]["id"]] = self.search_lag
            return 201, {"note": answer[1]}
        return answer

    def add_task(self, ids, query, body):
        answer = self._add(self.tasks, "t", ids, body)
        return (201, {"task": answer[1]}) if answer and answer[0] == 201 else answer

    def search(self, ids, query, body):
        text = query["search"]
        found = []
        for note in self.notes.values():
            if self._hidden.get(note["id"], 0) > 0:
                continue
            if text in note["body"]:
                found.append(dict(note))
        for note_id, left in list(self._hidden.items()):
            if left > 0:
                self._hidden[note_id] = left - 1
        return 200, {"notes": found}


class _Server(HTTPServer):
    def __init__(self, fake: FakeCRM):
        self.fake = fake
        super().__init__(("127.0.0.1", 0), _Handler)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def _handle(self, method: str) -> None:
        fake = self.server.fake
        parts = urllib.parse.urlsplit(self.path)
        length = self.headers.get("Content-Length")
        raw = self.rfile.read(int(length)) if length and length.isdigit() else b""
        if self.headers.get("Authorization") != "Bearer " + fake.token:
            return self._send(401, {"error": "a wrong or missing bearer token"})
        route = None
        for route_method, pattern, name in ROUTES:
            found = re.fullmatch(pattern, parts.path)
            if found and route_method == method:
                route = (name, found.groupdict())
                break
        if route is None:
            known = any(re.fullmatch(pattern, parts.path) for _, pattern, _ in ROUTES)
            return self._refuse(405 if known else 404, f"{method} {parts.path}: unknown {'method' if known else 'path'}")
        name, ids = route
        query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
        if any(len(v) != 1 for v in query.values()):
            return self._refuse(400, f"{method} {parts.path}: a query parameter given twice")
        query = {k: v[0] for k, v in query.items()}
        if name == "find":
            if len(query) != 1 or set(query) - FIND_BY:
                return self._refuse(400, f"GET /contacts: unknown or missing query {sorted(query)}")
        elif name == "search":
            if set(query) != {"search"}:
                return self._refuse(400, f"GET /notes: unknown or missing query {sorted(query)}")
        elif query:
            return self._refuse(400, f"{method} {parts.path}: unknown query {sorted(query)}")
        body = None
        if name in BODIES:
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return self._refuse(400, f"{method} {parts.path}: the body is not JSON")
            if not isinstance(body, dict) or set(body) != BODIES[name]:
                got = sorted(body) if isinstance(body, dict) else type(body).__name__
                return self._refuse(400, f"{method} {parts.path}: unknown or missing fields {got}")
        elif raw:
            return self._refuse(400, f"{method} {parts.path}: a body on a call that takes none")
        with fake.lock:
            fake.calls.append(f"{method} {parts.path}")
            mode = fake._failure_for(method, parts.path)
            if mode == "500":
                return self._send(500, {"error": "injected failure"})
            if mode == "hangup":
                return self._hang_up()
            answer = getattr(fake, name)(ids, query, body)
            if answer is None:
                return self._refuse(400, f"{method} {parts.path}: a field of the wrong type")
            if mode == "hangup_after":
                return self._hang_up()
        return self._send(*answer)

    def _refuse(self, status: int, violation: str) -> None:
        self.server.fake.violations.append(violation)
        self._send(status, {"error": violation})

    def _hang_up(self) -> None:
        self.close_connection = True

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(data)
