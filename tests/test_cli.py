"""End-to-end tests that drive `desk.py` through main(argv), on new files in a temporary folder."""
import os
import re
import secrets
import unittest

from tests.helpers import DATA_FILE, FIXED, PERSON, DeskCase


class StoryTests(DeskCase):
    """The whole story of the invented data, command by command."""

    def test_the_whole_story(self):
        self.init()
        out = self.cli("ingest", DATA_FILE)
        self.assertIn("ingest: 27 new enquiries, 3 replays, 0 lines not taken", out)
        report = self.cli("report")
        self.assertIn("the states add up: yes: 27 enquiries + 3 replays = 30 arrived", report)

        plan = self.cli("plan")
        self.assertIn("the dry run: nothing below has been written to the CRM", plan)
        self.assertEqual(self.crm_counts(), {"contacts": 10, "notes": 0, "tasks": 0})

        for key in ("webform:wf-000101", "webform:wf-000109", "webform:wf-000110", "webform:wf-000111"):
            self.cli("approve", key, "--by", PERSON)
        applied = self.cli("apply")
        self.assertIn("apply: 4 applied", applied)
        written = self.crm_counts()
        self.assertEqual(written, {"contacts": 11, "notes": 4, "tasks": 4})

        again = self.cli("ingest", DATA_FILE, at="2026-01-05T11:00:00Z")
        self.assertIn("ingest: 0 new enquiries, 30 replays, 0 lines not taken", again)
        self.assertIn("apply: nothing is approved", self.cli("apply"))
        self.assertEqual(self.crm_counts(), written)

        self.assertIn("the states add up: yes: 27 enquiries + 33 replays = 60 arrived", self.cli("report"))
        doctor = self.cli("doctor")
        self.assertIn("doctor: 16 of 16 checks passed", doctor)

    def test_outputs_are_the_same_for_the_same_fixed_clock(self):
        outputs = []
        for name in ("first", "second"):
            self.home = os.path.join(self._tmp.name, name)
            self.init()
            steps = [("ingest", DATA_FILE), ("approve", "webform:wf-000101", "--by", PERSON), ("apply",),
                     ("trail", "webform:wf-000101"), ("report",)]
            outputs.append([self.cli(*step) for step in steps])
        self.assertEqual(outputs[0], outputs[1])


class CommandTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.init()

    def write(self, name, text):
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        return path

    def test_a_line_that_is_not_json_is_named_and_the_others_are_taken(self):
        path = self.write("mixed.jsonl", "\n".join([
            '{"source": "webform", "source_id": "line-000001", "name": "A (invented)", "email": "a@example.com", '
            '"message": "Hello.", "consent": true, "website": ""}',
            "this is not json",
            "",
            "[1, 2]",
            '{"source": "webform", "source_id": "line-000005", "name": "B (invented)", "email": "b@example.com", '
            '"message": "Hello.", "consent": true, "website": ""}',
        ]) + "\n")
        out = self.cli("ingest", path, expect=1)
        self.assertIn("line 2: not taken: not JSON", out)
        self.assertIn("line 4: not taken: not one JSON object", out)
        self.assertIn("ingest: 2 new enquiries, 0 replays, 2 lines not taken", out)

    def test_a_missing_desk_or_file_is_a_usage_error(self):
        self.home = os.path.join(self._tmp.name, "nowhere")
        self.assertIn("run `desk.py init` first", self.cli("report", expect=2))
        self.assertIn("run `desk.py init` first", self.cli("doctor", expect=2))

    def test_a_bad_fixed_time_is_a_usage_error(self):
        self.assertIn("must look like", self.cli("report", at="tomorrow", expect=2))

    def test_approve_checks_the_plan_the_person_saw(self):
        self.cli("ingest", DATA_FILE)
        refused = self.cli("approve", "webform:wf-000101", "--by", PERSON, "--plan", "0123456789ab", expect=1)
        self.assertIn("nothing approved", refused)
        self.assertIn("refused", self.cli("approve", "webform:wf-000101", "--by", PERSON, "--plan", "0123", expect=1))
        plan = self.cli("plan")
        sha = plan.split("--plan ")[1].split()[0]
        self.assertIn("approved: webform:wf-000101", self.cli("approve", "webform:wf-000101", "--by", PERSON, "--plan", sha))

    def test_a_decision_needs_a_person_not_the_system(self):
        self.cli("ingest", DATA_FILE)
        self.assertIn("a person's name", self.cli("approve", "webform:wf-000101", "--by", "  ", expect=1))
        self.assertIn("the desk's own name", self.cli("approve", "webform:wf-000101", "--by", "System", expect=1))

    def test_reject_needs_a_reason_and_then_nothing_is_written(self):
        self.cli("ingest", DATA_FILE)
        self.assertIn("needs a reason", self.cli("reject", "webform:wf-000106", "--by", PERSON, "--reason", " ", expect=1))
        self.cli("reject", "webform:wf-000106", "--by", PERSON, "--reason", "advertising, not an enquiry")
        self.assertIn("no approval in the state approved", self.cli("apply", "--key", "webform:wf-000106", expect=1))
        self.assertIn("advertising, not an enquiry", self.cli("trail", "webform:wf-000106"))
        self.assertEqual(self.crm_counts()["notes"], 0)

    def test_withdraw_puts_the_plan_back_to_wait(self):
        self.cli("ingest", DATA_FILE)
        self.cli("approve", "webform:wf-000101", "--by", PERSON)
        self.cli("withdraw", "webform:wf-000101", "--by", PERSON, "--reason", "wrong person")
        self.assertIn("no approval in the state approved", self.cli("apply", "--key", "webform:wf-000101", expect=1))
        self.assertIn("refused", self.cli("withdraw", "webform:wf-000101", "--by", PERSON, expect=1))

    def test_a_two_contacts_hold_is_resolved_as_either_contact_never_as_a_second_holder_of_the_email(self):
        self.cli("ingest", DATA_FILE)
        key = "webform:wf-000112"
        self.assertIn("nothing", self.cli("apply", "--key", key, expect=1))
        self.assertIn("a second contact with it would be a duplicate",
                      self.cli("resolve", key, "--as", "new", "--by", PERSON, expect=1))
        self.assertIn("not one that identity found", self.cli("resolve", key, "--as", "contact:1", "--by", PERSON, expect=1))
        self.assertIn("now awaiting_approval", self.cli("resolve", key, "--as", "contact:8", "--by", PERSON))
        self.cli("approve", key, "--by", PERSON)
        self.cli("apply")
        self.assertEqual(self.notes_with_key(key), 1)
        note_contact = self.crm_db().execute("SELECT contact_id FROM notes").fetchone()[0]
        self.assertEqual(note_contact, 8)
        self.assertIn("doctor: 16 of 16 checks passed", self.cli("doctor"))

    def test_a_possible_duplicate_resolved_as_new_creates_a_contact(self):
        self.cli("ingest", DATA_FILE)
        key = "webform:wf-000113"
        self.cli("resolve", key, "--as", "new", "--by", PERSON)
        self.cli("approve", key, "--by", PERSON)
        self.cli("apply")
        self.assertEqual(self.crm_counts()["contacts"], 11)
        self.assertRegex(self.cli("trail", key), re.escape(PERSON) + r" +resolution ")

    def test_a_possible_duplicate_resolved_as_the_contact_adds_to_it(self):
        self.cli("ingest", DATA_FILE)
        key = "webform:wf-000113"
        self.cli("resolve", key, "--as", "contact:5", "--by", PERSON)
        self.assertIn("shown for a person: email", self.cli("plan"))
        self.cli("approve", key, "--by", PERSON)
        self.cli("apply")
        self.assertEqual(self.crm_counts()["contacts"], 10)  # the system never merges; the person chose contact 5
        self.assertEqual(self.crm_db().execute("SELECT email FROM contacts WHERE id = 5").fetchone()[0],
                         "elena.ruiz@example.com")  # the differing e-mail is shown, never overwritten

    def test_status_names_each_adapter(self):
        out = self.cli("status")
        self.assertIn("intake endpoint  POST /intake, 127.0.0.1 only: not configured", out)
        self.assertIn("crm practice     Local: it really runs: connected (10 contacts, 0 notes, 0 tasks)", out)
        self.assertIn("crm http", out)
        self.assertIn("not configured (set INTAKE_CRM_URL and INTAKE_CRM_TOKEN)", out)
        self.assertIn("draft template   stand-in, no model: connected", out)
        configured = self.cli("status", env={"INTAKE_SECRET": secrets.token_urlsafe(24)})
        self.assertIn("intake endpoint  POST /intake, 127.0.0.1 only: configured", configured)

    def test_the_trail_shows_one_enquirys_life_in_order(self):
        self.cli("ingest", DATA_FILE)
        self.cli("approve", "webform:wf-000109", "--by", PERSON)
        self.cli("apply")
        lines = self.cli("trail", "webform:wf-000109").splitlines()
        events = [line.split()[3] if line.split()[2] == "system" else None for line in lines[1:]]
        self.assertTrue(lines[0].startswith("trail of webform:wf-000109 (state now: applied)"))
        self.assertEqual(events[:3], ["arrived", "draft", "assessed"])
        self.assertTrue(all(FIXED in line for line in lines[1:]))
        self.assertIn("no enquiry with the key", self.cli("trail", "webform:nothing-here", expect=1))


if __name__ == "__main__":
    unittest.main()
