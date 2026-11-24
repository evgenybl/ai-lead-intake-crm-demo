"""End-to-end tests of POST /intake (section 3.1): it fails closed, and it binds 127.0.0.1 only."""
import http.client
import io
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import unittest

from intake.clock import Clock
from intake.home import open_desk
from intake.server import IntakeServer, secret_from

from tests.helpers import FIXED, ROOT, DeskCase, enquiry, made_up

THE_RIGHT_ONE = object()  # post() sends the server's own secret unless told otherwise


class IntakeEndpointCase(DeskCase):
    secret_configured = True

    def setUp(self):
        super().setUp()
        self.init()
        self.secret = secrets.token_urlsafe(24) if self.secret_configured else None
        self.log = io.StringIO()
        clock = Clock(FIXED)
        self.server = IntakeServer(0, self.secret, lambda: open_desk(self.home, "practice", {}, clock), log=self.log)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(5)

        self.addCleanup(stop)

    def post(self, body, secret=THE_RIGHT_ONE, path="/intake", method="POST", raw=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            sent = {"Content-Type": "application/json"}
            if secret is THE_RIGHT_ONE:
                secret = self.secret
            if secret is not None:
                sent["X-Intake-Secret"] = secret
            sent.update(headers or {})
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            connection.request(method, path, body=data, headers=sent)
            response = connection.getresponse()
            return response.status, json.loads(response.read() or b"{}")
        finally:
            connection.close()


class NoSecretTests(IntakeEndpointCase):
    secret_configured = False

    def test_with_no_secret_configured_every_request_is_refused_with_503(self):
        any_value = made_up()
        for method, path in (("POST", "/intake"), ("GET", "/intake"), ("GET", "/anything")):
            with self.subTest(method=method, path=path):
                status, _ = self.post(enquiry(), secret=any_value, path=path, method=method)
                self.assertEqual(status, 503)
        self.assertEqual(self.crm_counts()["notes"], 0)
        with self.desk() as desk:
            self.assertEqual(desk.store.enquiries(), [])

    def test_a_short_secret_counts_as_none(self):
        self.assertIsNone(secret_from({"INTAKE_SECRET": "x" * 15}))
        self.assertEqual(secret_from({"INTAKE_SECRET": "x" * 16}), "x" * 16)
        self.assertIsNone(secret_from({}))


class IntakeTests(IntakeEndpointCase):
    def test_it_binds_127_0_0_1_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_a_wrong_or_missing_secret_is_401_and_nothing_is_kept(self):
        self.assertEqual(self.post(enquiry(), secret=None)[0], 401)
        self.assertEqual(self.post(enquiry(), secret=made_up())[0], 401)
        self.assertEqual(self.post(enquiry(), secret=self.secret + "x")[0], 401)
        with self.desk() as desk:
            self.assertEqual(desk.store.enquiries(), [])

    def test_a_body_that_is_not_one_json_object_is_400(self):
        bodies = [b"not json", b"[1, 2]", b'"text"', b'{"name": "a", "name": "b"}', b"\xff\xfe",
                  b'{"x": NaN}', b'{"x": "\\ud800"}']
        for raw in bodies:
            with self.subTest(raw=raw):
                self.assertEqual(self.post(None, raw=raw)[0], 400)
        with self.desk() as desk:
            self.assertEqual(desk.store.enquiries(), [])

    def test_a_new_enquiry_is_201_with_its_key_and_state(self):
        status, answer = self.post(enquiry())
        self.assertEqual(status, 201)
        self.assertEqual(answer, {"key": "webform:test-000001", "state": "awaiting_approval", "replay": False})

    def test_a_replay_is_200_and_points_to_the_original(self):
        self.post(enquiry())
        status, answer = self.post(enquiry())
        self.assertEqual(status, 200)
        self.assertEqual(answer["original"], {"arrived_at": FIXED, "via": "http"})
        self.assertTrue(answer["replay"])
        with self.desk() as desk:
            self.assertEqual(desk.store.arrival_counts(), {"first": 1, "replay": 1})

    def test_a_refused_enquiry_is_kept_and_answered_by_its_state_only(self):
        status, answer = self.post(enquiry(consent=False, website="bot"))
        self.assertEqual((status, answer["state"]), (201, "refused"))
        self.assertNotIn("reasons", answer)  # a bot is not told which rule caught it
        with self.desk() as desk:
            self.assertEqual(desk.store.get_enquiry(answer["key"]).reasons, ["no_consent", "honeypot_filled"])

    def test_other_paths_methods_and_sizes(self):
        self.assertEqual(self.post(enquiry(), path="/desk")[0], 404)
        self.assertEqual(self.post(None, method="GET")[0], 405)
        self.assertEqual(self.post(None, raw=b"{" + b" " * (64 * 1024) + b"}")[0], 413)

    def test_no_content_length_is_411(self):
        with socket.create_connection(("127.0.0.1", self.server.port), timeout=10) as sock:
            sock.sendall(b"POST /intake HTTP/1.1\r\nHost: x\r\nX-Intake-Secret: " + self.secret.encode()
                         + b"\r\nConnection: close\r\n\r\n")
            reply = sock.recv(200)
        self.assertTrue(reply.startswith(b"HTTP/1.1 411"), reply)

    def test_the_secret_is_never_written_to_the_log(self):
        self.post(enquiry())
        self.post(enquiry(), secret=made_up())
        log = self.log.getvalue()
        self.assertIn("POST /intake 201", log)
        self.assertIn("POST /intake 401", log)
        self.assertNotIn(self.secret, log)
        self.assertNotIn("X-Intake-Secret", log)

    def test_markup_in_a_message_is_kept_as_text(self):
        status, answer = self.post(enquiry(message="Hello <script>alert(1)</script>"))
        self.assertEqual(status, 201)
        with self.desk() as desk:
            self.assertEqual(desk.store.get_enquiry(answer["key"]).payload["message"], "Hello <script>alert(1)</script>")


class ListenCommandTests(DeskCase):
    """`python -I desk.py listen` as a real process, on a port the system assigns."""

    def test_the_listen_command_takes_an_enquiry(self):
        self.init()
        secret = secrets.token_urlsafe(24)
        env = {k: v for k, v in os.environ.items() if not k.startswith("INTAKE_")}
        env.update({"INTAKE_SECRET": secret, "INTAKE_NOW": FIXED, "PYTHONIOENCODING": "utf-8"})
        process = subprocess.Popen(
            [sys.executable, "-I", os.path.join(ROOT, "desk.py"), "listen", "--port", "0", "--home", self.home],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, text=True, encoding="utf-8",
        )
        try:
            first = process.stdout.readline()
            self.assertIn("listening on http://127.0.0.1:", first)
            self.assertIn("secret: configured", first)
            self.assertNotIn(secret, first)
            port = int(first.split("127.0.0.1:")[1].split("/")[0])
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("POST", "/intake", body=json.dumps(enquiry()).encode(),
                               headers={"X-Intake-Secret": secret, "Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 201)
            self.assertEqual(json.loads(response.read())["key"], "webform:test-000001")
            connection.close()
        finally:
            process.terminate()
            process.communicate(timeout=10)
        with self.desk() as desk:
            self.assertEqual(desk.store.state_counts()["awaiting_approval"], 1)
            self.assertEqual(desk.store.get_enquiry("webform:test-000001").via, "http")


if __name__ == "__main__":
    unittest.main()
