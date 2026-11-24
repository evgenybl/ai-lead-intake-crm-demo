"""The local server. It binds 127.0.0.1 only.

`POST /intake` fails closed:

    no secret configured ............ 503 (a secret shorter than 16 characters counts as none)
    a wrong or missing X-Intake-Secret 401
    no Content-Length ............... 411;  a body above 64 KiB: 413
    a body that is not one JSON object 400
    a new enquiry ................... 201 {"key", "state", "replay": false}
    a replay ........................ 200 {"key", "state", "replay": true, "original": {"arrived_at", "via"}}

`desk.py listen` offers only that: with no secret, every request it gets is answered 503.
`desk.py serve` adds the desk pages: GET /desk, GET /desk/enquiry/<key>, and, only when it was
started with --allow-decisions, POST /desk/decide. A decision needs the ticked confirmation box and a name
(two locks), and the form's own token. The desk answers only requests addressed to 127.0.0.1 or localhost
on its own port, and a decision only from a page of its own (the Origin header, when sent, must be one of
those), so a web page elsewhere cannot read the desk or post to it through the browser.

The intake secret is read from INTAKE_SECRET and compared in constant time. It must be printable ASCII (a
header carries nothing else); a secret with any other character stops the server at start, with the reason.
It is never printed or logged; the log line of a request is its method, its path and its status, each with
every control character written as its escape and cut to 200 characters, so a client cannot write to the
terminal. A request line that cannot be read is answered 400 and logged with "-" for what it lacks.

One request at a time, so each has a deadline: a client that has not sent its whole request within 10
seconds of connecting is cut off, however slowly it drips. A desk file that another program holds locked
is answered 503, with a plain line, never a traceback.
"""
from __future__ import annotations

import hmac
import json
import socket
import sqlite3
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import pages, reports
from .arrive import parse_object
from .flow import DeskError
from .states import TransitionError

SECRET_ENV = "INTAKE_SECRET"
SECRET_HEADER = "X-Intake-Secret"
MIN_SECRET_CHARS = 16
MAX_BODY_BYTES = 64 * 1024
MAX_DRAIN_BYTES = 1024 * 1024
REQUEST_DEADLINE_SECONDS = 10.0  # the whole request (line, headers and body) must arrive within this
LOGGED_MAX_CHARS = 200  # how much of a request's method or path the log line shows
HOST = "127.0.0.1"
PAGE_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def loggable(text) -> str:
    """Text a client sent, as the log shows it: one line, every control character as its escape, cut."""
    shown = reports.printable(text)
    return shown if len(shown) <= LOGGED_MAX_CHARS else shown[:LOGGED_MAX_CHARS] + "..."


class SecretError(Exception):
    """A secret that no request could ever match; the server does not start with it."""


def secret_from(environ) -> str | None:
    """The configured secret, or None when there is none usable: the intake endpoint then refuses everything.

    Raises SecretError for a secret with a character that is not printable ASCII (a space, a letter such
    as é): a header carries ASCII only, so no request could ever give it, and every enquiry would be 401."""
    value = environ.get(SECRET_ENV, "")
    if any(not 33 <= ord(c) <= 126 for c in value):
        raise SecretError(f"{SECRET_ENV} holds a character that is not printable ASCII (a space, an accented "
                          "letter): an HTTP header carries ASCII only, so no request could match it. Use letters, "
                          "digits and punctuation")
    return value if len(value) >= MIN_SECRET_CHARS else None


class IntakeServer(HTTPServer):
    """One request at a time, so the desk file has one writer."""

    def __init__(self, port: int, secret: str | None, open_desk, log=None, desk_pages: bool = False,
                 decisions: pages.Decisions | None = None):
        self.secret = secret.encode("utf-8") if secret else None
        self.open_desk = open_desk  # a function that returns a context manager yielding a Desk
        self.log = log
        self.desk_pages = desk_pages
        self.decisions = decisions or pages.Decisions()
        super().__init__((HOST, port), IntakeHandler)

    @property
    def port(self) -> int:
        return self.server_address[1]

    def own_origins(self) -> set:
        return {f"http://{host}:{self.port}" for host in (HOST, "localhost")}

    def handle_error(self, request, client_address):
        """One plain line for a request that failed in a way no answer covered; never a traceback."""
        if self.log is not None:
            self.log.write("a request ended without an answer (the connection closed or failed)\n")
            self.log.flush()


class IntakeHandler(BaseHTTPRequestHandler):
    server_version = "lead-intake-desk"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # A request line that cannot be read leaves no version; without this the 400 would go out with no status line.
    default_request_version = "HTTP/1.0"
    timeout = 10

    def setup(self):
        super().setup()
        self._deadline = threading.Timer(REQUEST_DEADLINE_SECONDS, self._cut_off)
        self._deadline.daemon = True
        self._deadline.start()

    def _cut_off(self) -> None:
        """The request did not arrive in time: close the connection under the reader."""
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _arrived(self) -> None:
        """The whole request is in: from here on, the deadline does not apply."""
        self._deadline.cancel()

    def finish(self):
        self._deadline.cancel()
        super().finish()

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
        if method != "POST" or self.headers.get("Content-Length") in (None, "0"):
            self._arrived()
        server = self.server
        path = urllib.parse.urlsplit(self.path).path
        if server.desk_pages and (path == "/" or path == "/desk" or path.startswith("/desk/")):
            return self._desk(method, path)
        if server.secret is None:
            return self._send(503, {"error": "the intake endpoint has no secret configured, so it refuses every request"})
        if path != "/intake":
            return self._send(404, {"error": "not found"})
        if method != "POST":
            return self._send(405, {"error": "only POST"}, {"Allow": "POST"})
        given = self.headers.get(SECRET_HEADER, "").encode("utf-8", "surrogateescape")
        if not hmac.compare_digest(given, server.secret):
            return self._send(401, {"error": f"a wrong or missing {SECRET_HEADER} header"})
        raw = self._body()
        if raw is None:
            return
        obj, problem = parse_object(raw)
        if problem:
            return self._send(400, {"error": f"the body is {problem}"})
        try:
            with server.open_desk() as desk:
                answer = desk.receive(obj, via="http")
        except sqlite3.OperationalError:
            return self._send(503, {"error": "the desk is busy (its file is held by another program); send the "
                                             "enquiry again in a moment (a copy that was recorded is answered as "
                                             "a replay)"})
        except Exception as error:  # the sender cannot know what was kept: sending again is always safe
            if server.log is not None:
                server.log.write(f"POST /intake failed: {type(error).__name__}\n")
            return self._send(500, {"error": "the enquiry may not have been recorded; send it again "
                                             "(a copy that was recorded is answered as a replay)"})
        return self._send(200 if answer.replay else 201, answer.as_dict())

    def _body(self) -> bytes | None:
        length = self.headers.get("Content-Length")
        if length is None:
            self._send(411, {"error": "a Content-Length header is needed"})
            return None
        if not length.isdigit():
            self._send(400, {"error": "the Content-Length header is not a number"})
            return None
        if int(length) > MAX_BODY_BYTES:
            self._send(413, {"error": f"the body is above {MAX_BODY_BYTES} bytes"})
            return None
        raw = self.rfile.read(int(length))
        self._body_read = True
        if len(raw) < int(length):  # cut off at the deadline, or the client stopped sending
            self.close_connection = True
            return None
        self._arrived()
        return raw

    # -- the desk pages ------------------------------------------------------------------------------

    def _desk(self, method: str, path: str) -> None:
        server = self.server
        links = pages.ServerLinks()
        host = self.headers.get("Host", "")
        if f"http://{host}" not in server.own_origins():
            return self._html(400, pages.message_page("Not this desk", "The request was not addressed to this "
                                                      "desk's own address.", links))
        if path == "/":
            return self._redirect("/desk")
        if method == "POST" and path == "/desk/decide":
            return self._decide(links)
        if method != "GET":
            return self._html(405, pages.message_page("Not allowed", "This page only shows.", links), {"Allow": "GET"})
        try:
            with server.open_desk() as desk:
                if path == "/desk":
                    return self._html(200, pages.desk_page(desk.store, links, server.decisions))
                if path.startswith("/desk/enquiry/"):
                    key = pages.unslug(urllib.parse.unquote(path[len("/desk/enquiry/"):]))
                    page = pages.enquiry_page(desk.store, key, links, server.decisions) if key else None
                    if page is not None:
                        return self._html(200, page)
        except sqlite3.OperationalError:
            return self._busy(links)
        return self._html(404, pages.message_page("Not found", "There is no such page on this desk.", links))

    def _busy(self, links) -> None:
        return self._html(503, pages.message_page(
            "The desk is busy", "Another program holds the desk file just now. Nothing was changed; try again in "
                                "a moment.", links), {"Retry-After": "5"})

    def _decide(self, links) -> None:
        server = self.server
        if not server.decisions.active:
            return self._html(403, pages.message_page(
                "Decisions are off", "This desk was started without --allow-decisions; use the commands shown on "
                                     "each enquiry.", links))
        origin = self.headers.get("Origin")
        if origin is not None and origin not in server.own_origins():
            return self._html(403, pages.message_page("Not this desk", "The form did not come from this desk.", links))
        raw = self._body()
        if raw is None:
            return
        try:
            form = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True, max_num_fields=20)
        except (UnicodeDecodeError, ValueError):
            return self._html(400, pages.message_page("Not done", "The form could not be read.", links))
        field = {name: values[0] for name, values in form.items() if len(values) == 1}
        token = field.get("token", "").encode("utf-8")
        if not hmac.compare_digest(token, server.decisions.token.encode("utf-8")):
            return self._html(403, pages.message_page("Not this desk", "The form is not from this desk.", links))
        key = field.get("key", "")
        if field.get("confirm") != "yes":
            return self._html(400, pages.message_page(
                "Not done", "Tick the box to confirm, and give your name: both are needed.", links))
        decision = field.get("decision")
        try:
            with server.open_desk() as desk:
                if decision == "approve":
                    desk.approve(key, field.get("by", ""), field.get("plan", ""))
                    done = f"Approved: {key}, by {field.get('by', '').strip()}. It is written when someone runs apply."
                elif decision == "reject":
                    desk.reject(key, field.get("by", ""), field.get("reason", ""))
                    earlier = reports.written_before(desk.store, desk.store.get_enquiry(key))
                    done = (f"Rejected: {key}, by {field.get('by', '').strip()}, {earlier}. Nothing more will be "
                            "written." if earlier else
                            f"Rejected: {key}, by {field.get('by', '').strip()}. Nothing will be written.")
                else:
                    return self._html(400, pages.message_page("Not done", "No decision was given.", links))
        except (DeskError, TransitionError) as error:
            return self._html(400, pages.message_page("Not done", str(error), links))
        except sqlite3.OperationalError:
            return self._busy(links)
        return self._html(200, pages.message_page("Done", done, links))

    # -- answers --------------------------------------------------------------------------------------

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

    def _write(self, status: int, content_type: str, data: bytes, headers: dict | None = None) -> None:
        if status >= 400:
            self._drain()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Connection", "close")  # one request per connection: the next client never waits
        self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def _send(self, status: int, body: dict, headers: dict | None = None) -> None:
        self._write(status, "application/json", json.dumps(body, sort_keys=True).encode("utf-8"), headers)

    def _html(self, status: int, page: str, headers: dict | None = None) -> None:
        self._write(status, "text/html; charset=utf-8", page.encode("utf-8"), {**PAGE_HEADERS, **(headers or {})})

    def _redirect(self, location: str) -> None:
        self._write(303, "text/plain; charset=utf-8", b"", {"Location": location})

    def log_request(self, code="-", size="-"):
        """Method, path and status; "-" for what a request line that could not be read does not have."""
        if self.server.log is not None:
            path = getattr(self, "path", None)
            shown = loggable(urllib.parse.urlsplit(path).path) if path is not None else "-"
            self.server.log.write(f"{loggable(self.command or '-')} {shown} {code}\n")
            self.server.log.flush()

    def log_message(self, format, *args):  # errors of the HTTP layer itself: no headers, no body
        if self.server.log is not None:
            self.server.log.write("http: " + loggable(format % args) + "\n")
            self.server.log.flush()
