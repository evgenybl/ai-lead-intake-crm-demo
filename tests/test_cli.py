"""End-to-end tests that drive `desk.py` through main(argv), on new files in a temporary folder."""
import contextlib
import io
import os
import re
import secrets
import unittest

from intake.flow import DeskError

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

        self.cli("resolve", "webform:wf-000111", "--as", "contact:6", "--by", PERSON)  # held: only the phone matched
        for key in ("webform:wf-000101", "webform:wf-000109", "webform:wf-000110", "webform:wf-000111"):
            self.approve_cli(key)
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
        self.assertRegex(doctor, r"doctor: (\d+) of \1 checks passed")

    def test_outputs_are_the_same_for_the_same_fixed_clock(self):
        outputs = []
        for name in ("first", "second"):
            self.home = os.path.join(self._tmp.name, name)
            self.init()
            outputs.append([self.cli("ingest", DATA_FILE), self.approve_cli("webform:wf-000101"), self.cli("apply"),
                            self.cli("trail", "webform:wf-000101"), self.cli("report")])
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

    def test_an_approval_without_the_plan_is_refused_so_a_plan_made_again_is_never_approved_unseen(self):
        # The sequence the review reproduced: the plan is shown, the CRM changes, the desk plans again, and an
        # approval that did not name the plan approved the new one, which apply then wrote.
        self.cli("ingest", DATA_FILE)
        key = "webform:wf-000109"
        seen = self.cli("plan").split(f"to approve: desk.py approve {key} --by <name> --plan ")[1].split()[0]
        conn = self.crm_db()
        conn.execute("UPDATE contacts SET phone = '000-000-0188', phone_digits = '0000000188' WHERE id = 2")
        conn.commit()
        self.assertIn(f"{key}: planned again: the CRM changed", self.cli("plan"))
        self.assertNotEqual(self.plan_sha(key)[:12], seen)
        before = self.crm_counts()
        usage = io.StringIO()
        with contextlib.redirect_stderr(usage):
            self.cli("approve", key, "--by", PERSON, expect=2)
        self.assertIn("the following arguments are required: --plan", usage.getvalue())
        refused = self.cli("approve", key, "--by", PERSON, "--plan", seen, expect=1)
        self.assertIn("the desk planned again since", refused)
        self.assertIn("nothing is approved", self.cli("apply"))
        self.assertEqual(self.crm_counts(), before)
        with self.desk() as desk:
            self.assertEqual(desk.store.get_enquiry(key).state, "awaiting_approval")
            self.assertEqual(desk.store.approvals(key), [])
            with self.assertRaises(DeskError):
                desk.approve(key, PERSON, None)
            with self.assertRaises(DeskError):
                desk.approve(key, PERSON, "")

    def test_an_apply_failed_that_a_retry_cannot_finish_says_how_to_clear_it_and_that_way_works(self):
        # The review's loop: the first write failed before it happened, then someone else took the e-mail.
        from intake.crm.practice import PracticeCRM
        from intake.home import crm_path
        from intake import pages
        from tests.helpers import FlakyCRM, approve_in, enquiry

        key = "webform:test-000001"
        with self.desk() as desk:
            desk.receive(enquiry(), via="file")
            approve_in(desk, key)
            desk.crm = FlakyCRM(desk.crm, "create_contact", "before")
            self.assertEqual(desk.apply(key=key)[0].outcome, "apply_failed")
        other = PracticeCRM(crm_path(self.home))
        other.create_contact({"name": "Yara Test (invented)", "email": "yara.test@example.com"}, "")
        other.close()
        for _ in range(2):
            self.assertIn("apply_failed", self.cli("apply", expect=1))
        plan = self.cli("plan")
        hint = (f"if a retry keeps failing because the CRM changed (the e-mail now belongs to another contact): "
                f"desk.py withdraw {key} --by <name>, then desk.py plan: it is planned again and waits for a new "
                "approval")
        self.assertIn(hint, plan)
        with self.desk() as desk:
            page = pages.enquiry_page(desk.store, key, pages.FileLinks(True), pages.Decisions())
        self.assertIn(f'python -I desk.py withdraw {key} --by "Your name" --reason "the CRM changed"', page)
        self.cli("withdraw", key, "--by", PERSON, "--reason", "the CRM changed")
        self.assertIn(f"{key}: planned again: the CRM changed", self.cli("plan"))
        self.approve_cli(key)
        self.assertIn(f"{key}: applied", self.cli("apply"))
        self.assertEqual(self.crm_counts()["contacts"], 11)  # the contact someone else made; no second one
        self.assertRegex(self.cli("doctor"), r"doctor: (\d+) of \1 checks passed")

    def test_a_decision_needs_a_person_not_the_system(self):
        self.cli("ingest", DATA_FILE)
        self.assertIn("a person's name", self.approve_cli("webform:wf-000101", by="  ", expect=1))
        self.assertIn("the desk's own name", self.approve_cli("webform:wf-000101", by="System", expect=1))

    def test_reject_needs_a_reason_and_then_nothing_is_written(self):
        self.cli("ingest", DATA_FILE)
        self.assertIn("needs a reason", self.cli("reject", "webform:wf-000106", "--by", PERSON, "--reason", " ", expect=1))
        self.cli("reject", "webform:wf-000106", "--by", PERSON, "--reason", "advertising, not an enquiry")
        self.assertIn("no approval in the state approved", self.cli("apply", "--key", "webform:wf-000106", expect=1))
        self.assertIn("advertising, not an enquiry", self.cli("trail", "webform:wf-000106"))
        self.assertEqual(self.crm_counts()["notes"], 0)

    def test_withdraw_puts_the_plan_back_to_wait(self):
        self.cli("ingest", DATA_FILE)
        self.approve_cli("webform:wf-000101")
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
        self.approve_cli(key)
        self.cli("apply")
        self.assertEqual(self.notes_with_key(key), 1)
        note_contact = self.crm_db().execute("SELECT contact_id FROM notes").fetchone()[0]
        self.assertEqual(note_contact, 8)
        self.assertRegex(self.cli("doctor"), r"doctor: (\d+) of \1 checks passed")

    def test_a_possible_duplicate_resolved_as_new_creates_a_contact(self):
        self.cli("ingest", DATA_FILE)
        key = "webform:wf-000113"
        self.cli("resolve", key, "--as", "new", "--by", PERSON)
        self.approve_cli(key)
        self.cli("apply")
        self.assertEqual(self.crm_counts()["contacts"], 11)
        self.assertRegex(self.cli("trail", key), re.escape(PERSON) + r" +resolution ")

    def test_a_possible_duplicate_resolved_as_the_contact_adds_to_it(self):
        self.cli("ingest", DATA_FILE)
        key = "webform:wf-000113"
        self.cli("resolve", key, "--as", "contact:5", "--by", PERSON)
        self.assertIn("shown for a person: email", self.cli("plan"))
        self.approve_cli(key)
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
        self.approve_cli("webform:wf-000109")
        self.cli("apply")
        lines = self.cli("trail", "webform:wf-000109").splitlines()
        events = [line.split()[3] if line.split()[2] == "system" else None for line in lines[1:]]
        self.assertTrue(lines[0].startswith("trail of webform:wf-000109 (state now: applied)"))
        self.assertEqual(events[:3], ["arrived", "draft", "assessed"])
        self.assertTrue(all(FIXED in line for line in lines[1:]))
        self.assertIn("no enquiry with the key", self.cli("trail", "webform:nothing-here", expect=1))


if __name__ == "__main__":
    unittest.main()
