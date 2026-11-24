"""A local mock of the Messages API, for the tests of the `anthropic` backend only. It binds 127.0.0.1.

It checks what a request carries (the key header, the API version, a model, max_tokens, messages, and no
sampling parameters), records every request body, and answers from a queue the test fills: a text answer
(after a thinking block, as a model with thinking on returns it), a refusal, or an HTTP error.
This is the only "live" model the repository has ever been run against.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

API_VERSION = "2026-10-21"


class FakeAnthropic:
    def __init__(self, key: str):
        self.key = key
        self.requests: list = []
        self.violations: list = []
        self._answers: list = []
        self._server = None
        self._thread = None

    def start(self) -> str:
        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self._thread.start()
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)

    def answer(self, text: str, stop_reason: str = "end_turn") -> None:
        content = [{"type": "thinking", "thinking": "", "signature": "made-up-by-the-mock"},
                   {"type": "text", "text": text}]
        self._answers.append((200, {"id": "msg_mock", "type": "message", "role": "assistant", "model": "mock-model",
                                    "content": content, "stop_reason": stop_reason}))

    def refuse(self) -> None:
        self._answers.append((200, {"id": "msg_mock", "type": "message", "role": "assistant", "model": "mock-model",
                                    "content": [], "stop_reason": "refusal"}))

    def fail(self, status: int) -> None:
        self._answers.append((status, {"type": "error", "error": {"type": "api_error", "message": "mock failure"}}))


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        fake = self.server.fake
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path != "/v1/messages":
            return self._send(404, {"error": "not found"})
        if self.headers.get("x-api-key") != fake.key:
            fake.violations.append("a wrong or missing x-api-key")
            return self._send(401, {"type": "error"})
        if self.headers.get("anthropic-version") != API_VERSION:
            fake.violations.append("a wrong anthropic-version")
        body = json.loads(raw)
        fake.requests.append(body)
        for name in ("temperature", "top_p", "top_k"):
            if name in body:
                fake.violations.append(f"{name} was sent")
        if not body.get("model") or not body.get("max_tokens") or not body.get("messages"):
            fake.violations.append("model, max_tokens or messages missing")
        if not fake._answers:
            return self._send(500, {"type": "error", "error": {"message": "no answer queued"}})
        status, answer = fake._answers.pop(0)
        return self._send(status, answer)

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
