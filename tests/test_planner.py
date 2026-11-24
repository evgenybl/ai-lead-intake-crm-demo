"""Unit and integration tests: the plan, its sha256, and when it changes."""
import os
import tempfile
import unittest

from intake import planner, validate
from intake.canonical import canonical_json, sha256_hex
from intake.crm.practice import PracticeCRM
from intake.ops import ALLOWED_OPS

from tests.helpers import enquiry

KEY = "webform:test-000001"


class PlannerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.crm = PracticeCRM(os.path.join(tmp.name, "crm.sqlite3"), create=True)
        self.addCleanup(self.crm.close)
        self.crm.load_starting_contacts([
            {"name": "Yara Test (invented)", "email": "yara.test@example.com", "phone": "000-000-0399", "company": ""},
            {"name": "Zed Other (invented)", "email": "zed@example.com", "phone": "", "company": ""},
        ])

    def assess(self, resolution=None, **overrides):
        fields = validate.extract(enquiry(**overrides), "2026-01-05T09:00:00Z").fields
        return planner.assess(KEY, fields, resolution, self.crm)

    def test_a_new_contact_plan(self):
        result = self.assess(email="new.person@example.com", phone="000-000-0301")
        self.assertEqual(result.state, "awaiting_approval")
        self.assertEqual([op["op"] for op in result.plan["operations"]], ["create_contact", "add_note", "add_task"])
        self.assertEqual(result.plan["operations"][0]["fields"]["email"], "new.person@example.com")
        self.assertEqual(result.plan["operations"][1]["contact"], planner.CREATED)

    def test_the_note_and_the_task_begin_with_the_key(self):
        result = self.assess(email="new.person@example.com", phone="")
        for op in result.plan["operations"][1:]:
            self.assertEqual(op["body"].split("\n", 1)[0], KEY)

    def test_the_sha_is_the_sha_of_the_canonical_plan(self):
        result = self.assess(email="new.person@example.com", phone="")
        self.assertEqual(result.sha, sha256_hex(result.plan))
        self.assertEqual(canonical_json(result.plan), canonical_json(dict(reversed(list(result.plan.items())))))

    def test_returning_fills_empty_fields_and_shows_a_difference_without_writing_it(self):
        result = self.assess()  # the e-mail is contact 1's; phone 0301 differs from 0399; company is empty
        ops = result.plan["operations"]
        self.assertEqual(ops[0], {"op": "fill_empty_fields", "contact": "1", "fields": {"company": "Test Bakery (invented)"}})
        self.assertEqual(result.plan["shown"], [{"field": "phone", "crm": "000-000-0399", "enquiry": "000-000-0301"}])
        self.assertNotIn("phone", ops[0]["fields"])

    def test_a_difference_of_case_or_spacing_is_not_shown(self):
        result = self.assess(name="  yara   TEST (invented)", phone="(000) 000-0399")
        self.assertEqual(result.plan["shown"], [])

    def test_an_email_another_contact_holds_is_shown_not_filled(self):
        contact = self.crm.create_contact({"name": "Phone Only (invented)", "phone": "000-000-0777"}, "")
        result = self.assess(email="zed@example.com", phone="000-000-0777", resolution=f"contact:{contact.id}")
        fill = [op for op in result.plan["operations"] if op["op"] == "fill_empty_fields"]
        self.assertTrue(all("email" not in op["fields"] for op in fill))
        (email,) = [d for d in result.plan["shown"] if d["field"] == "email"]
        self.assertEqual(email["not_filled"], "the e-mail belongs to contact 2")

    def test_the_sha_changes_when_the_contact_now_exists(self):
        before = self.assess(email="new.person@example.com", phone="")
        self.crm.create_contact({"name": "New Person (invented)", "email": "new.person@example.com"}, "")
        after = self.assess(email="new.person@example.com", phone="")
        self.assertNotEqual(before.sha, after.sha)
        self.assertEqual(after.identity["kind"], "returning")

    def test_the_sha_changes_when_an_empty_field_was_filled_meanwhile(self):
        before = self.assess()
        self.crm.fill_empty_fields("1", {"company": "Somebody Else's Bakery (invented)"})
        after = self.assess()
        self.assertNotEqual(before.sha, after.sha)
        self.assertEqual(after.plan["shown"][-1]["field"], "company")

    def test_the_sha_does_not_change_for_what_does_not_matter(self):
        before = self.assess()
        self.crm.add_note("1", "an unrelated note")
        self.crm.add_task("2", "an unrelated task")
        self.assertEqual(before.sha, self.assess().sha)

    def test_only_allow_listed_operations_each_at_most_once(self):
        for overrides in ({}, {"email": "new.person@example.com"}):
            plan = self.assess(**overrides).plan
            names = [op["op"] for op in plan["operations"]]
            self.assertTrue(set(names) <= set(ALLOWED_OPS))
            self.assertEqual(len(names), len(set(names)))

    def test_a_hold_has_no_plan(self):
        self.crm.create_contact({"name": "Phone (invented)", "phone": "000-000-0000"}, "")
        result = self.assess(email="yara.test@example.com", phone="000-000-0000")
        self.assertEqual((result.state, result.plan, result.sha), ("held_two_contacts", None, None))

    def test_a_resolution_as_new_stops_applying_once_the_email_is_taken(self):
        self.assertEqual(self.assess(resolution="new", email="later@example.com").identity["kind"], "new")
        self.crm.create_contact({"name": "Later (invented)", "email": "later@example.com"}, "")
        result = self.assess(resolution="new", email="later@example.com")
        self.assertEqual(result.identity["kind"], "returning")
        self.assertIn("no longer applies", result.identity["notes"][0])

    def test_a_resolution_to_a_missing_contact_stops_applying(self):
        result = self.assess(resolution="contact:99", email="new.person@example.com")
        self.assertEqual(result.identity["kind"], "new")
        self.assertIn("contact 99 is not in the CRM", result.identity["notes"][0])

    def test_the_plan_holds_no_draft_text(self):
        plan = self.assess().plan
        text = canonical_json(plan)
        for word in ("summary", "intent", "urgency", "reply_draft", "stand-in"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
