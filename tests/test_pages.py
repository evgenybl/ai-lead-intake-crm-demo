"""The desk pages, `render` and `serve`. No script on any page; everything from an enquiry or a
model is escaped; Approve and Reject take two locks and work only with --allow-decisions."""
import http.client
import io
import json
import os
import re
import secrets
import shutil
import threading
import unittest
import urllib.parse

from intake import pages
from intake.clock import Clock
from intake.draft import RECORDED_DIR, RecordedBackend
from intake.home import open_desk
from intake.server import IntakeServer

from tests.helpers import DATA_FILE, FIXED, PERSON, DeskCase, enquiry, made_up

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
        page = self.read("enquiry", pages.slug("webform:wf-000110") + ".html")
        self.assertIn('href="../index.html"', page)
        self.assertIn("<h2>Its trail</h2>", page)

    def test_no_page_holds_a_script_or_a_form(self):
        for name in ["index.html"] + [os.path.join("enquiry", n) for n in os.listdir(os.path.join(self.out, "enquiry"))]:
            page = self.read(name).lower()
            self.assertNotIn("<script", page, name)
            self.assertNotIn("<form", page, name)  # a file cannot take a decision

    def test_every_page_is_self_contained_and_loads_nothing_from_a_network(self):
        loaders = re.compile(r"<script|<link|<img|<iframe|<object|<embed|\bsrc=|@import|url\(|https?://", re.I)
        names = ["index.html"] + [os.path.join("enquiry", n) for n in os.listdir(os.path.join(self.out, "enquiry"))]
        for name in names:
            page = self.read(name)
            self.assertIsNone(loaders.search(page), name)
            for href in re.findall(r'href="([^"]+)"', page):
                self.assertTrue(href.endswith(".html") and ":" not in href, (name, href))

    def test_markup_in_a_message_is_shown_as_text(self):
        page = self.read("enquiry", pages.slug("webform:wf-000125") + ".html")
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
        """Every invented enquiry that reaches the model has a recorded answer; four of them were refused whole."""
        index = self.read("index.html")
        self.assertEqual(index.count('<p><b>Model step:</b> <span class="label">Recorded</span></p>'), 20)
        self.assertEqual(index.count("<p><b>Model step:</b> skipped. A refused enquiry never reaches"), 7)
        self.assertEqual(index.count("AI draft refused: the answer has keys beyond the four"), 4)
        self.assertNotIn("Constructed answer, not from a model", index)

    def test_where_no_recording_exists_a_constructed_answer_is_labelled_so(self):
        only_constructed = os.path.join(self._tmp.name, "recorded")
        for folder in ("requests", "constructed"):
            shutil.copytree(os.path.join(RECORDED_DIR, folder), os.path.join(only_constructed, folder))
        self.home = os.path.join(self._tmp.name, "second")
        self.init()
        with self.desk() as desk:
            desk.drafter = RecordedBackend(root=only_constructed)
            with open(DATA_FILE, encoding="utf-8") as handle:
                for line in handle:
                    desk.receive(json.loads(line), via="file")
            page = pages.desk_page(desk.store, pages.FileLinks(False), pages.Decisions())
        self.assertEqual(page.count('<span class="label">Constructed answer, not from a model</span>'), 20)
        self.assertNotIn('<span class="label">Recorded</span>', page)

    def test_a_replay_with_different_content_is_shown_on_the_desk(self):
        page = self.read("enquiry", pages.slug("webform:wf-000101") + ".html")
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

    def test_an_approver_s_name_a_refusal_reason_and_a_replay_difference_are_escaped(self):
        self.init()
        refused = {"backend": "test", "label": "Recorded", "outcome": "refused", "request": "",
                   "reason": "the answer has keys beyond the four: " + MARKUP, "summary": None, "intent": None,
                   "urgency": None, "reply_draft": None}

        class Drafter:
            def draft(self, fields):
                return refused

        approver = '<b onmouseover="x()">Rev</b> & co (invented)'
        with self.desk() as desk:
            desk.drafter = Drafter()
            desk.receive(enquiry(), via="file")
            desk.receive(enquiry(message="Edited: " + MARKUP), via="http")  # a replay with different content
            desk.approve("webform:test-000001", approver, desk.store.get_enquiry("webform:test-000001").plan_sha)
            page = pages.enquiry_page(desk.store, "webform:test-000001", pages.ServerLinks(), pages.Decisions("t"))
            index = pages.desk_page(desk.store, pages.ServerLinks(), pages.Decisions("t"))
        for html_page in (page, index):
            self.assertNotIn("<script", html_page.lower())
            self.assertNotIn("<img", html_page.lower())
            self.assertNotIn("<b onmouseover", html_page)
        self.assertIn("&lt;b onmouseover=&quot;x()&quot;&gt;Rev&lt;/b&gt; &amp; co (invented)", page)
        self.assertIn("keys beyond the four: &lt;script&gt;", page)
        self.assertIn("Edited: &lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;", page)


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


class FileNameTests(DeskCase):
    def test_two_keys_that_differ_only_in_letter_case_get_two_pages_on_any_file_system(self):
        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(source_id="Form-AAAA01"), via="file")
            desk.receive(enquiry(source_id="form-aaaa01", email="other.person@example.com", phone="",
                                 name="Other Person (invented)", company=""), via="file")
        out = os.path.join(self._tmp.name, "pages")
        self.cli("render", "--out", out)
        names = sorted(os.listdir(os.path.join(out, "enquiry")))
        self.assertEqual(len(names), 2)
        self.assertEqual(len({name.casefold() for name in names}), 2)  # distinct even where case is not
        for key in ("webform:Form-AAAA01", "webform:form-aaaa01"):
            name = pages.slug(key) + ".html"
            self.assertIn(name, names)
            self.assertEqual(pages.unslug(pages.slug(key)), key)
            with open(os.path.join(out, "enquiry", name), encoding="utf-8") as handle:
                self.assertIn(f"<h1>Enquiry {key}</h1>", handle.read())


class ServeWithoutDecisionsTests(ServeCase):
    def test_the_desk_and_an_enquiry_page_are_served_with_no_script_allowed(self):
        status, headers, body = self.request("GET", "/desk")
        self.assertEqual(status, 200)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("<h1>Enquiries desk</h1>", body)
        status, _, body = self.request("GET", "/desk/enquiry/" + pages.slug("webform:wf-000110"))
        self.assertEqual(status, 200)
        self.assertIn("<h2>Its trail</h2>", body)
        self.assertEqual(self.request("GET", "/desk/enquiry/webform~nothing-here")[0], 404)
        self.assertEqual(self.request("GET", "/desk/enquiry/webform~wf-000110-00000000")[0], 404)  # a wrong hash
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
    secret = secrets.token_urlsafe(24)

    def plan_of(self, key):
        with self.desk() as desk:
            return desk.store.get_enquiry(key).plan_sha

    def test_the_forms_appear_with_two_locks_and_a_token(self):
        body = self.request("GET", "/desk/enquiry/" + pages.slug("webform:wf-000101"))[2]
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
        self.assertEqual(self.decide(token=made_up(), **fields)[0], 403)
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
        plan = self.plan_of(key)
        for named in ({}, {"plan": ""}, {"plan": plan[:7]}):  # no plan, an empty one, too few digits
            with self.subTest(named=named):
                status, _, body = self.decide(token=self.token, key=key, decision="approve", confirm="yes", by=PERSON,
                                              **named)
                self.assertEqual(status, 400)
                self.assertIn("an approval names the plan it approves", body)
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
