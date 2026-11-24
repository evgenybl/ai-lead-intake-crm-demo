"""`POST /intake` (section 3.1). It binds 127.0.0.1 only, and it fails closed.

    no secret configured ............ 503 for every request (a secret shorter than 16 characters counts as none)
    a path other than /intake ....... 404
    a method other than POST ........ 405
    a wrong or missing X-Intake-Secret 401
    no Content-Length ............... 411;  a body above 64 KiB: 413
    a body that is not one JSON object 400
    a new enquiry ................... 201 {"key", "state", "replay": false}
    a replay ........................ 200 {"key", "state", "replay": true, "original": {"arrived_at", "via"}}

The secret is read from INTAKE_SECRET and compared in constant time. It is never printed or logged; the
log line of a request is its method, its path and its status, nothing else.
"""
from __future__ import annotations

import hmac
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from .arrive import parse_object

SECRET_ENV = "INTAKE_SECRET"
SECRET_HEADER = "X-Intake-Secret"
MIN_SECRET_CHARS = 16
MAX_BODY_BYTES = 64 * 1024
MAX_DRAIN_BYTES = 1024 * 1024
HOST = "127.0.0.1"


def secret_from(environ) -> str | None:
    """The configured secret, or None when there is none usable: the endpoint then refuses everything."""
    value = environ.get(SECRET_ENV, "")
    return value if len(value) >= MIN_SECRET_CHARS else None


class IntakeServer(HTTPServer):
    """One request at a time, so the desk file has one writer."""

    def __init__(self, port: int, secret: str | None, open_desk, log=None):
        self.secret = secret.encode("utf-8") if secret else None
        self.open_desk = open_desk  # a function that returns a context manager yielding a Desk
        self.log = log
        super().__init__((HOST, port), IntakeHandler)

    @property
    def port(self) -> int:
        return self.server_address[1]


class IntakeHandler(BaseHTTPRequestHandler):
    server_version = "lead-intake-desk"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 10

    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_PATCH(self):
        self._handle("PATCH")

    def _handle(self, method: str) -> None:
        self._body_read = False
        server = self.server
        if server.secret is None:
            return self._send(503, {"error": "the intake endpoint has no secret configured, so it refuses every request"})
        if urllib.parse.urlsplit(self.path).path != "/intake":
            return self._send(404, {"error": "not found"})
        if method != "POST":
            return self._send(405, {"error": "only POST"}, {"Allow": "POST"})
        given = self.headers.get(SECRET_HEADER, "").encode("utf-8", "surrogateescape")
        if not hmac.compare_digest(given, server.secret):
            return self._send(401, {"error": f"a wrong or missing {SECRET_HEADER} header"})
        length = self.headers.get("Content-Length")
        if length is None:
            return self._send(411, {"error": "a Content-Length header is needed"})
        if not length.isdigit():
            return self._send(400, {"error": "the Content-Length header is not a number"})
        if int(length) > MAX_BODY_BYTES:
            return self._send(413, {"error": f"the body is above {MAX_BODY_BYTES} bytes"})
        raw = self.rfile.read(int(length))
        self._body_read = True
        obj, problem = parse_object(raw)
        if problem:
            return self._send(400, {"error": f"the body is {problem}"})
        try:
            with server.open_desk() as desk:
                answer = desk.receive(obj, via="http")
        except Exception as error:  # the sender cannot know what was kept: sending again is always safe
            if server.log is not None:
                server.log.write(f"POST /intake failed: {type(error).__name__}\n")
            return self._send(500, {"error": "the enquiry may not have been recorded; send it again "
                                             "(a copy that was recorded is answered as a replay)"})
        return self._send(200 if answer.replay else 201, answer.as_dict())

    def _drain(self) -> None:
        """Read and drop an unread body (up to 1 MiB) before an error answer, so the client gets the
        answer instead of a reset connection. A larger body is not read: the connection is closed."""
        length = self.headers.get("Content-Length", "")
        if getattr(self, "_body_read", True) or not length.isdigit() or int(length) > MAX_DRAIN_BYTES:
            return
        left = int(length)
        while left > 0:
            chunk = self.rfile.read(min(left, MAX_BODY_BYTES))
            if not chunk:
                break
            left -= len(chunk)
        self._body_read = True

    def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
        if status >= 400:
            self._drain()
        data = json.dumps(body, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Connection", "close")  # one request per connection: the next client never waits
        self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def log_request(self, code="-", size="-"):
        if self.server.log is not None:
            path = urllib.parse.urlsplit(self.path).path
            self.server.log.write(f"{self.command} {path} {code}\n")
            self.server.log.flush()

    def log_message(self, format, *args):  # errors of the HTTP layer itself: no headers, no body
        if self.server.log is not None:
            self.server.log.write("http: " + (format % args).replace("\n", " ") + "\n")
            self.server.log.flush()
