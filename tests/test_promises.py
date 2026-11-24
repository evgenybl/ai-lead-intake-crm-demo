"""The four promises of section 2, one test each, named by the promise; and the doctor catching a break of each.

The five controls of section 6 (tests/test_controls.py) break the properties these rest on."""
import unittest

from tests.helpers import DATA_FILE, PERSON, DeskCase, FlakyCRM, enquiry


class PromiseTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.init()

    def test_promise_1_no_lead_is_lost_every_arrival_ends_in_one_named_state_and_they_add_up(self):
        self.cli("ingest", DATA_FILE)
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, "find_by_email", "before")
            answer = desk.receive(enquiry(source_id="crm-down-01"), via="http")  # the CRM is down at arrival
        self.assertEqual(answer.state, "received")
        report = self.cli("report")
        self.assertIn("the states add up: yes: 28 enquiries + 3 replays = 31 arrived", report)
        with self.desk() as desk:
            counts = desk.store.state_counts()
        self.assertEqual(counts["received"], 1)
        self.assertEqual(sum(counts.values()), 28)
        self.assertEqual(self.problems(), [])

    def test_promise_2_no_duplicate_is_created_a_replay_writes_nothing_and_a_returning_contact_is_not_doubled(self):
        self.cli("ingest", DATA_FILE)
        for key in ("webform:wf-000101", "webform:wf-000108", "webform:wf-000109", "webform:wf-000110"):
            self.cli("approve", key, "--by", PERSON)
        self.cli("apply")
        after_first = self.crm_counts()
        self.assertEqual(after_first["contacts"], 11)  # one new contact; three returning ones updated, not doubled
        for _ in range(3):
            self.cli("ingest", DATA_FILE)
            self.cli("plan")
            self.cli("apply")
        self.assertEqual(self.crm_counts(), after_first)
        self.assertEqual(self.notes_with_key("webform:wf-000101"), 1)
        self.assertEqual(self.problems(), [])

    def test_promise_3_nothing_is_written_before_a_person_approves(self):
        self.cli("ingest", DATA_FILE)
        self.cli("plan")
        self.cli("apply")
        self.cli("apply", "--key", "webform:wf-000101", expect=1)
        self.cli("apply", "--dry-run")
        self.assertEqual(self.crm_counts(), {"contacts": 10, "notes": 0, "tasks": 0})
        self.cli("approve", "webform:wf-000101", "--by", PERSON)
        self.cli("apply", "--dry-run")
        self.assertEqual(self.crm_counts(), {"contacts": 10, "notes": 0, "tasks": 0})
        self.cli("apply")
        self.assertEqual(self.crm_counts(), {"contacts": 11, "notes": 1, "tasks": 1})
        with self.desk() as desk:
            (approval,) = desk.store.approvals("webform:wf-000101")
            self.assertEqual((approval.state, approval.by), ("used", PERSON))
            self.assertEqual(approval.plan_sha, desk.store.get_enquiry("webform:wf-000101").plan_sha)
        self.assertEqual(self.problems(), [])

    def test_promise_4_every_step_is_on_the_record_in_a_trail_the_database_refuses_to_change(self):
        self.cli("ingest", DATA_FILE)
        self.cli("approve", "webform:wf-000110", "--by", PERSON)
        self.cli("apply")
        trail = self.cli("trail", "webform:wf-000110")
        for event in ("arrived", "assessed", "approved", "operation", "applied"):
            self.assertIn(f" {event} ", trail)
        self.assertIn(PERSON, trail)
        conn = self.desk_db()
        for statement in ("UPDATE audit SET actor = 'x'", "DELETE FROM audit"):
            with self.assertRaisesRegex(Exception, "append-only"):
                conn.execute(statement)
        self.assertEqual(self.problems(), [])


class DoctorFindsEachBrokenPromiseTests(DeskCase):
    """Beyond the five controls: the doctor names a break of each promise, made directly in the files."""

    def setUp(self):
        super().setUp()
        self.init()
        self.cli("ingest", DATA_FILE)
        self.cli("approve", "webform:wf-000101", "--by", PERSON)
        self.cli("apply")
        self.assertEqual(self.problems(), [])

    def test_promise_1_a_lost_lead_is_found(self):
        conn = self.desk_db()
        conn.execute("DELETE FROM arrivals WHERE key = 'webform:wf-000102'")  # its counts go with it...
        conn.execute("DELETE FROM enquiries WHERE key = 'webform:wf-000102'")
        found = self.problems()  # ...but the append-only trail still records that it arrived
        self.assertIn("every key in the trail is an enquiry on the desk: the trail records webform:wf-000102, "
                      "which is not on the desk: a lost lead", found)

    def test_promise_1_an_enquiry_with_no_arrival_is_found(self):
        self.desk_db().execute("DELETE FROM arrivals WHERE key = 'webform:wf-000102'")
        self.assertIn("the arrivals add up: webform:wf-000102 has 0 first arrivals, not 1", self.problems())

    def test_promise_1_a_failure_with_no_error_named_is_found(self):
        conn = self.desk_db()
        conn.execute("UPDATE enquiries SET state = 'apply_failed', error = NULL WHERE key = 'webform:wf-000101'")
        self.assertTrue(any("apply_failed with no error named" in p for p in self.problems()))

    def test_promise_2_a_second_note_with_one_key_is_found(self):
        conn = self.crm_db()
        conn.execute("INSERT INTO notes (contact_id, body) VALUES (11, 'webform:wf-000101\nagain')")
        conn.commit()
        self.assertIn("no two notes with the same enquiry key: 2 records carry webform:wf-000101", self.problems())

    def test_promise_3_a_write_that_bypassed_the_gate_is_found(self):
        conn = self.crm_db()
        conn.execute("INSERT INTO notes (contact_id, body) VALUES (1, 'webform:wf-000102\nwritten around the gate')")
        conn.commit()
        self.assertTrue(any("carries webform:wf-000102, which has no approval" in p for p in self.problems()))

    def test_promise_4_a_state_changed_off_the_record_is_found(self):
        self.desk_db().execute("UPDATE enquiries SET state = 'rejected' WHERE key = 'webform:wf-000103'")
        self.assertTrue(any("webform:wf-000103 is rejected, but its trail last records 'awaiting_approval'" in p
                            for p in self.problems()))

    def test_the_doctor_command_fails_when_a_check_fails(self):
        self.desk_db().execute("UPDATE enquiries SET state = 'rejected' WHERE key = 'webform:wf-000103'")
        out = self.cli("doctor", expect=1)
        self.assertIn("FAIL  each enquiry's state is the last state its trail records", out)
        self.assertIn("checks FAILED", out)


if __name__ == "__main__":
    unittest.main()
