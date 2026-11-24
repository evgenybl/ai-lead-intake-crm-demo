"""The HTTP CRM adapter against the strict fake (section 3.7, section 6). Mock: no real CRM is reached."""
import json
import secrets
import unittest
import urllib.error
import urllib.request

from intake.crm.base import CRMConflict, CRMError
from intake.crm.http_crm import HttpCRM

from tests.fake_crm import FakeCRM
from tests.helpers import PERSON, DeskCase, enquiry

KEY = "webform:test-000001"


class FakeCRMCase(DeskCase):
    def setUp(self):
        super().setUp()
        self.token = secrets.token_urlsafe(24)
        self.fake = FakeCRM(self.token)
        self.url = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.crm = HttpCRM(self.url, self.token, timeout=5)

    def raw(self, method, path, body=None, token=None):
        """A request that bypasses the adapter, to test the fake's own strictness."""
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method,
                                         headers={"Authorization": "Bearer " + (token or self.token)})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            error.close()
            return error.code


class StrictFakeTests(FakeCRMCase):
    def test_the_fake_refuses_an_unknown_field_path_or_query(self):
        self.assertEqual(self.raw("POST", "/contacts", {"name": "x", "email": "", "phone": "", "company": "",
                                                        "origin_key": "", "vip": True}), 400)
        self.assertEqual(self.raw("GET", "/deals"), 404)
        self.assertEqual(self.raw("DELETE", "/contacts/c-1"), 405)
        self.assertEqual(self.raw("GET", "/contacts?colour=blue"), 400)
        self.assertEqual(len(self.fake.violations), 4)

    def test_the_fake_refuses_a_wrong_token(self):
        self.assertEqual(self.raw("GET", "/health", token="wrong-" + secrets.token_hex(4)), 401)


class AdapterTests(FakeCRMCase):
    def tearDown(self):
        self.assertEqual(self.fake.violations, [], "the adapter sent something the CRM's API does not have")

    def test_every_call_of_the_api_round_trips(self):
        seeded = self.fake.seed("Ada Fenwick (invented)", "ada@example.com", "000-000-0101", "Fenwick Dental (invented)")
        self.assertEqual([c.id for c in self.crm.find_by_email(" ADA@example.com")], [seeded])
        self.assertEqual([c.id for c in self.crm.find_by_phone("0000000101")], [seeded])
        self.assertEqual([c.id for c in self.crm.find_by_name("ada fenwick invented", "fenwick dental invented")], [seeded])
        self.assertEqual(self.crm.find_by_name("ada fenwick invented", "another invented"), [])
        created = self.crm.create_contact({"name": "New (invented)", "email": "New@example.com"}, KEY)
        self.assertEqual(created.email, "new@example.com")
        self.assertEqual([c.id for c in self.crm.find_by_origin_key(KEY)], [created.id])
        self.assertEqual(self.crm.fill_empty_fields(created.id, {"phone": "000-000-0102", "name": "x"}),
                         {"filled": ["phone"], "kept": ["name"]})
        note = self.crm.add_note(created.id, KEY + "\nbody")
        self.assertEqual([n.id for n in self.crm.list_notes(created.id)], [note.id])
        self.assertEqual([n.first_line for n in self.crm.search_notes(KEY)], [KEY])
        task = self.crm.add_task(created.id, KEY + "\nanswer")
        self.assertEqual([t.id for t in self.crm.list_tasks(created.id)], [task.id])
        self.assertIsNone(self.crm.get_contact("c-999"))
        self.assertEqual(self.crm.check()[0], "connected")

    def test_a_second_contact_with_one_email_is_a_conflict(self):
        self.crm.create_contact({"name": "A (invented)", "email": "a@example.com"}, "")
        with self.assertRaises(CRMConflict):
            self.crm.create_contact({"name": "B (invented)", "email": "a@example.com"}, "")

    def test_an_error_names_the_call_and_never_the_token(self):
        self.fake.fail("GET", "/health", "500")
        state, detail = self.crm.check()
        self.assertEqual(state, "error")
        self.assertIn("GET /health: HTTP 500", detail)
        wrong = HttpCRM(self.url, "wrong-" + self.token)
        with self.assertRaises(CRMError) as caught:
            wrong.find_by_email("a@example.com")
        self.assertNotIn(self.token, str(caught.exception))
        self.assertIn("HTTP 401", str(caught.exception))

    def test_a_hang_up_is_an_error_not_an_answer(self):
        self.fake.fail("GET", "/contacts", "hangup")
        with self.assertRaises(CRMError) as caught:
            self.crm.find_by_email("a@example.com")
        self.assertIn("no answer", str(caught.exception))

    def test_a_server_that_is_not_there_is_an_error(self):
        gone = HttpCRM("http://127.0.0.1:9", self.token, timeout=2)
        with self.assertRaises(CRMError):
            gone.find_by_phone("0000000101")


class DeskOverHttpTests(FakeCRMCase):
    """The whole flow with the HTTP adapter in place of the practice CRM."""

    def setUp(self):
        super().setUp()
        self.init(empty=True)
        self.fake.seed("Ada Fenwick (invented)", "ada.fenwick@example.com", "000-000-0101", "")

    def tearDown(self):
        self.assertEqual(self.fake.violations, [])

    def receive_and_approve(self, **overrides):
        with self.desk(crm=self.crm) as desk:
            answer = desk.receive(enquiry(**overrides), via="file")
            desk.approve(answer.key, PERSON)
        return answer.key

    def apply(self, key):
        with self.desk(crm=self.crm) as desk:
            (result,) = desk.apply(key=key)
        return result

    def test_a_new_contact_through_the_adapter(self):
        key = self.receive_and_approve()
        self.assertEqual(self.apply(key).outcome, "applied")
        self.assertEqual(len(self.fake.contacts), 2)
        self.assertEqual(len(self.fake.notes_carrying(key)), 1)

    def test_a_returning_contact_is_filled_never_doubled(self):
        key = self.receive_and_approve(email="ada.fenwick@example.com", company="Fenwick Dental (invented)")
        self.assertEqual(self.apply(key).outcome, "applied")
        self.assertEqual(len(self.fake.contacts), 1)
        self.assertEqual(self.fake.contacts["c-1"]["company"], "Fenwick Dental (invented)")

    def test_a_500_then_a_retry_completes_and_doubles_nothing(self):
        key = self.receive_and_approve()
        self.fake.fail("POST", "/contacts/[^/]+/notes", "500")
        first = self.apply(key)
        self.assertEqual(first.outcome, "apply_failed")
        self.assertIn("HTTP 500", first.message)
        self.assertEqual(self.apply(key).outcome, "applied")
        self.assertEqual(len(self.fake.notes_carrying(key)), 1)
        self.assertEqual(len(self.fake.contacts), 2)

    def test_a_hang_up_after_the_write_with_search_lagging_is_found_on_the_contact(self):
        key = self.receive_and_approve()
        self.fake.search_lag = 5  # a new note stays out of search: only the contact's own notes show it
        self.fake.fail("POST", "/contacts/[^/]+/notes", "hangup_after")
        self.assertEqual(self.apply(key).outcome, "apply_failed")
        self.assertEqual(len(self.fake.notes_carrying(key)), 1)
        second = self.apply(key)
        self.assertEqual(second.outcome, "applied")
        self.assertEqual(len(self.fake.notes_carrying(key)), 1)
        note_calls = [c for c in self.fake.calls if c.endswith("/notes") or c == "GET /notes"]
        self.assertEqual(note_calls[-1], f"GET /contacts/{self.fake.notes_carrying(key)[0]['contact_id']}/notes")

    def test_a_note_on_another_contact_is_found_by_search_confirmed_by_its_first_line(self):
        key = self.receive_and_approve(email="ada.fenwick@example.com")
        self.fake.notes["n-90"] = {"id": "n-90", "contact_id": "c-elsewhere", "body": key + "\nwritten earlier"}
        self.fake.notes["n-91"] = {"id": "n-91", "contact_id": "c-1", "body": "mentions " + key + " in passing"}
        result = self.apply(key)
        self.assertEqual(result.outcome, "applied")
        self.assertIn("add_note found (n-90)", " ".join(result.steps))
        self.assertEqual(len(self.fake.notes_carrying(key)), 1)

    def test_the_crm_down_at_arrival_leaves_the_enquiry_received_and_counted(self):
        self.fake.fail("GET", "/contacts", "500")
        with self.desk(crm=self.crm) as desk:
            answer = desk.receive(enquiry(), via="http")
            self.assertEqual(answer.state, "received")
            self.assertIn("HTTP 500", desk.store.get_enquiry(answer.key).error)
            self.assertEqual(desk.prepare(), [(answer.key, "assessed: awaiting_approval")])


if __name__ == "__main__":
    unittest.main()
