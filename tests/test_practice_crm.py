"""Integration tests: the practice CRM on a real SQLite file (section 3.7)."""
import os
import tempfile
import unittest

from intake.crm.base import CRMConflict, CRMError
from intake.crm.practice import PracticeCRM


class PracticeCRMTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "crm.sqlite3")
        self.crm = PracticeCRM(self.path, create=True)
        self.addCleanup(self.crm.close)

    def test_the_unique_index_refuses_a_second_contact_for_one_email(self):
        self.crm.create_contact({"name": "A (invented)", "email": "a@example.com"}, "k:000001")
        with self.assertRaises(CRMConflict):
            self.crm.create_contact({"name": "B (invented)", "email": " A@EXAMPLE.com"}, "k:000002")
        self.assertEqual(self.crm.counts()["contacts"], 1)

    def test_contacts_without_an_email_do_not_collide(self):
        self.crm.create_contact({"name": "A (invented)", "phone": "000-000-0001"}, "")
        self.crm.create_contact({"name": "B (invented)", "phone": "000-000-0001"}, "")
        self.assertEqual(len(self.crm.find_by_phone("0000000001")), 2)

    def test_fill_writes_only_empty_fields_and_says_which(self):
        contact = self.crm.create_contact({"name": "A (invented)", "email": "a@example.com", "phone": ""}, "")
        result = self.crm.fill_empty_fields(contact.id, {"phone": "000-000-0009", "name": "Other (invented)"})
        self.assertEqual(result, {"filled": ["phone"], "kept": ["name"]})
        again = self.crm.get_contact(contact.id)
        self.assertEqual((again.name, again.phone), ("A (invented)", "000-000-0009"))
        self.assertEqual(self.crm.find_by_phone("0000000009")[0].id, contact.id)

    def test_fill_refuses_an_email_another_contact_holds(self):
        self.crm.create_contact({"name": "A (invented)", "email": "a@example.com"}, "")
        b = self.crm.create_contact({"name": "B (invented)"}, "")
        with self.assertRaises(CRMConflict):
            self.crm.fill_empty_fields(b.id, {"email": "a@example.com"})
        self.assertEqual(self.crm.get_contact(b.id).email, "")

    def test_fill_refuses_a_field_that_is_not_a_contact_field(self):
        contact = self.crm.create_contact({"name": "A (invented)"}, "")
        with self.assertRaises(CRMError):
            self.crm.fill_empty_fields(contact.id, {"notes": "x"})

    def test_search_reads_underscores_as_text_not_as_wildcards(self):
        contact = self.crm.create_contact({"name": "A (invented)"}, "")
        self.crm.add_note(contact.id, "webform:ab_cd12\nbody")
        self.crm.add_note(contact.id, "webform:abXcd12\nbody")
        self.assertEqual([n.first_line for n in self.crm.search_notes("webform:ab_cd12")], ["webform:ab_cd12"])

    def test_origin_key_finds_the_contact_an_enquiry_created(self):
        contact = self.crm.create_contact({"name": "A (invented)"}, "webform:abc123")
        self.assertEqual([c.id for c in self.crm.find_by_origin_key("webform:abc123")], [contact.id])
        self.assertEqual(self.crm.find_by_origin_key(""), [])

    def test_notes_and_tasks_need_an_existing_contact(self):
        with self.assertRaises(CRMError):
            self.crm.add_note("99", "x")
        with self.assertRaises(CRMError):
            self.crm.add_task("99", "x")
        self.assertIsNone(self.crm.get_contact("not-a-number"))

    def test_a_missing_file_is_an_error_not_a_new_crm(self):
        with self.assertRaises(CRMError):
            PracticeCRM(self.path + ".missing")
        self.assertFalse(os.path.exists(self.path + ".missing"))


if __name__ == "__main__":
    unittest.main()
