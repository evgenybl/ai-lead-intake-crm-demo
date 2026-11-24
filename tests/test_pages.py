"""Section 3.8: the desk pages, `render` and `serve`. No script on any page; everything from an enquiry or a
model is escaped; Approve and Reject take two locks and work only with --allow-decisions."""
import http.client
import io
import os
import re
import secrets
import threading
import unittest
import urllib.parse

from intake import pages
from intake.clock import Clock
from intake.home import open_desk
from intake.server import IntakeServer

from tests.helpers import DATA_FILE, FIXED, PERSON, DeskCase, enquiry

MARKUP = '<script>alert("x")</script><img src=x onerror=alert(1)>'


class RenderTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.init()
        self.cli("ingest", DATA_FILE)
        self.out = os.path.join(self._tmp.name, "pages")
        self.cli("render", "--out", self.out)

    def read(self, *parts):
        with open(os.path.join(self.out, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_render_writes_the_desk_and_a_page_per_enquiry_with_links_that_exist(self):
        names = sorted(os.listdir(os.path.join(self.out, "enquiry")))
        self.assertEqual(len(names), 27)
        index = self.read("index.html")
        for href in re.findall(r'href="([^"]+)"', index):
            self.assertTrue(os.path.exists(os.path.join(self.out, *href.split("/"))), href)
        page = self.read("enquiry", "webform~wf-000110.html")
        self.assertIn('href="../index.html"', page)
        self.assertIn("<h2>Its trail</h2>", page)

    def test_no_page_holds_a_script_or_a_form(self):
        for name in ["index.html"] + [os.path.join("enquiry", n) for n in os.listdir(os.path.join(self.out, "enquiry"))]:
            page = self.read(name).lower()
            self.assertNotIn("<script", page, name)
            self.assertNotIn("<form", page, name)  # a file cannot take a decision

    def test_markup_in_a_message_is_shown_as_text(self):
        page = self.read("enquiry", "webform~wf-000125.html")
        self.assertIn("Hello &lt;script&gt;alert(1)&lt;/script&gt; please quote", page)

    def test_the_counts_come_first_in_plain_words(self):
        index = self.read("index.html")
        self.assertLess(index.index("<h2>The counts</h2>"), index.index("<h2>Enquiries</h2>"))
        self.assertIn("The counts add up: 27 enquiries + 3 replays = 30 arrived.", index)
        for words in ("Waiting for your approval", "Held: might be the same person as an existing contact",
                      "Held: the e-mail and the phone belong to two different contacts",
                      "Not taken: it did not pass the checks at the door"):
            self.assertIn(words, index)

    def test_each_enquiry_names_the_label_of_its_model_step(self):
        index = self.read("index.html")
        self.assertEqual(index.count('<p><b>Model step:</b> <span class="label">'), 20)
        self.assertEqual(index.count("<p><b>Model step:</b> skipped. A refused enquiry never reaches"), 7)
        self.assertIn("Constructed answer, not from a model", index)

    def test_a_replay_with_different_content_is_shown_on_the_desk(self):
        page = self.read("enquiry", "webform~wf-000101.html")
        self.assertIn("<b>Replayed with different content</b>", page)
        self.assertIn("with a small kitchen.", page)


class EscapingTests(DeskCase):
    def test_everything_from_an_enquiry_or_a_model_is_escaped(self):
        self.init()
        drafted = {"backend": "test", "label": MARKUP, "outcome": "used", "reason": "", "request": "",
                   "summary": MARKUP, "intent": "other", "urgency": "low", "reply_draft": MARKUP}

        class Drafter:
            def draft(self, fields):
                return drafted

        with self.desk() as desk:
            desk.drafter = Drafter()
            desk.receive(enquiry(name=MARKUP, company=MARKUP, message=MARKUP), via="file")
            page = pages.enquiry_page(desk.store, "webform:test-000001", pages.ServerLinks(), pages.Decisions("t"))
            index = pages.desk_page(desk.store, pages.ServerLinks(), pages.Decisions("t"))
        for html_page in (page, index):
            self.assertNotIn("<script", html_page.lower())
            self.assertNotIn("<img", html_page.lower())
            self.assertIn("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", html_page)


class ServeCase(DeskCase):
    allow_decisions = False
    secret = None

    def setUp(self):
        super().setUp()
        self.init()
        self.cli("ingest", DATA_FILE)
        token = secrets.token_urlsafe(16) if self.allow_decisions else None
        self.server = IntakeServer(0, self.secret, lambda: open_desk(self.home, "practice", {}, Clock(FIXED)),
                                   log=io.StringIO(), desk_pages=True, decisions=pages.Decisions(token))
        self.token = token
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(5)

        self.addCleanup(stop)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read().decode("utf-8")
        finally:
            connection.close()

    def decide(self, **fields):
        body = urllib.parse.urlencode(fields).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded",
                   "Origin": f"http://127.0.0.1:{self.server.port}"}
        return self.request("POST", "/desk/decide", body, headers)

    def state(self, key):
        with self.desk() as desk:
            return desk.store.get_enquiry(key).state


class ServeWithoutDecisionsTests(ServeCase):
    def test_the_desk_and_an_enquiry_page_are_served_with_no_script_allowed(self):
        status, headers, body = self.request("GET", "/desk")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("<h1>Enquiries desk</h1>", body)
        status, _, body = self.request("GET", "/desk/enquiry/webform~wf-000110")
        self.assertEqual(status, 200)
        self.assertIn("<h2>Its trail</h2>", body)
        self.assertEqual(self.request("GET", "/desk/enquiry/webform~nothing-here")[0], 404)
        self.assertEqual(self.request("GET", "/")[1]["Location"], "/desk")

    def test_without_the_switch_the_page_shows_the_commands_and_takes_no_decision(self):
        body = self.request("GET", "/desk")[2]
        self.assertNotIn("<form", body)
        self.assertIn("python -I desk.py approve webform:wf-000101", body)
        status, _, _ = self.request("POST", "/desk/decide", b"key=webform%3Awf-000101",
                                    {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 403)
        self.assertEqual(self.state("webform:wf-000101"), "awaiting_approval")

    def test_a_request_addressed_to_another_name_is_not_answered(self):
        status, _, _ = self.request("GET", "/desk", headers={"Host": "attacker.example.com"})
        self.assertEqual(status, 400)

    def test_with_no_secret_the_intake_answers_503_and_the_desk_still_shows(self):
        self.assertEqual(self.request("POST", "/intake", b"{}", {"X-Intake-Secret": "x" * 20})[0], 503)
        self.assertEqual(self.request("GET", "/desk")[0], 200)


class ServeWithDecisionsTests(ServeCase):
    allow_decisions = True
    secret = "s" * 8 + "-" + secrets.token_hex(8)

    def plan_of(self, key):
        with self.desk() as desk:
            return desk.store.get_enquiry(key).plan_sha

    def test_the_forms_appear_with_two_locks_and_a_token(self):
        body = self.request("GET", "/desk/enquiry/webform~wf-000101")[2]
        self.assertEqual(body.count("<form"), 2)
        self.assertEqual(body.count('type="checkbox" name="confirm"'), 2)
        self.assertEqual(body.count('name="by"'), 2)

    def test_approve_needs_the_ticked_box_and_a_name(self):
        key, plan = "webform:wf-000101", self.plan_of("webform:wf-000101")
        base = {"token": self.token, "key": key, "plan": plan, "decision": "approve"}
        self.assertEqual(self.decide(**base, by=PERSON)[0], 400)  # no ticked box
        self.assertEqual(self.decide(**base, confirm="yes", by="")[0], 400)  # no name
        self.assertEqual(self.decide(**base, confirm="yes", by="=SUM(A1)")[0], 400)  # a formula, not a name
        self.assertEqual(self.state(key), "awaiting_approval")
        status, _, body = self.decide(**base, confirm="yes", by=PERSON)
        self.assertEqual(status, 200)
        self.assertIn("Approved: webform:wf-000101", body)
        self.assertEqual(self.state(key), "approved")
        self.assertEqual(self.crm_counts()["notes"], 0)  # approving writes nothing; apply does

    def test_a_form_without_this_desks_token_or_from_elsewhere_is_refused(self):
        key, plan = "webform:wf-000101", self.plan_of("webform:wf-000101")
        fields = {"key": key, "plan": plan, "decision": "approve", "confirm": "yes", "by": PERSON}
        self.assertEqual(self.decide(token="not-the-token", **fields)[0], 403)
        body = urllib.parse.urlencode({"token": self.token, **fields}).encode("utf-8")
        status, _, _ = self.request("POST", "/desk/decide", body, {"Content-Type": "application/x-www-form-urlencoded",
                                                                    "Origin": "http://attacker.example.com"})
        self.assertEqual(status, 403)
        self.assertEqual(self.state(key), "awaiting_approval")

    def test_an_approval_names_the_plan_the_person_saw(self):
        key = "webform:wf-000101"
        status, _, body = self.decide(token=self.token, key=key, plan="0" * 64, decision="approve", confirm="yes",
                                      by=PERSON)
        self.assertEqual(status, 400)
        self.assertIn("nothing approved", body)
        self.assertEqual(self.state(key), "awaiting_approval")

    def test_reject_needs_a_reason(self):
        key, plan = "webform:wf-000106", self.plan_of("webform:wf-000106")
        base = {"token": self.token, "key": key, "plan": plan, "decision": "reject", "confirm": "yes", "by": PERSON}
        self.assertEqual(self.decide(**base, reason=" ")[0], 400)
        self.assertEqual(self.decide(**base, reason="advertising, not an enquiry")[0], 200)
        self.assertEqual(self.state(key), "rejected")

    def test_the_intake_endpoint_still_works_beside_the_desk(self):
        status, _, _ = self.request("POST", "/intake", b'{"name": "x"}', {"X-Intake-Secret": self.secret,
                                                                         "Content-Type": "application/json"})
        self.assertEqual(status, 201)


if __name__ == "__main__":
    unittest.main()
