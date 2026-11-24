"""Integration tests: the one write gate (section 3.5) on real desk and practice CRM files."""
import json
import unittest

from intake.canonical import canonical_json, sha256_hex
from intake.crm.practice import PracticeCRM
from intake.gate import GateRefusal, WriteGate
from intake.home import crm_path

from tests.helpers import PERSON, DeskCase, FlakyCRM, enquiry

KEY = "webform:test-000001"


class GateTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(), via="file")

    def approve(self, key=KEY):
        with self.desk() as desk:
            return desk.approve(key, PERSON)

    def apply(self, crm=None, dry_run=False, key=KEY):
        with self.desk(crm=crm) as desk:
            (result,) = desk.apply(key=key, dry_run=dry_run)
        return result

    def state(self, key=KEY):
        with self.desk() as desk:
            return desk.store.get_enquiry(key)

    def test_an_approved_plan_is_applied_and_the_approval_is_used(self):
        self.approve()
        result = self.apply()
        self.assertEqual(result.outcome, "applied")
        self.assertEqual(self.crm_counts(), {"contacts": 11, "notes": 1, "tasks": 1})
        with self.desk() as desk:
            self.assertEqual([a.state for a in desk.store.approvals(KEY)], ["used"])
        self.assertEqual(self.problems(), [])

    def test_refused_with_no_approval_nothing_written(self):
        before = self.crm_counts()
        result = self.apply()
        self.assertEqual(result.outcome, "refused")
        self.assertIn("no approval in the state approved", result.message)
        self.assertEqual(self.crm_counts(), before)
        with self.desk() as desk:
            self.assertEqual(desk.store.trail(KEY)[-1][3], "apply_refused")

    def test_refused_twice_after_a_successful_apply(self):
        self.approve()
        self.apply()
        before = self.crm_counts()
        second = self.apply()
        self.assertEqual(second.outcome, "refused")
        self.assertEqual(self.crm_counts(), before)

    def test_an_operation_outside_the_allow_list_is_refused_before_anything_leaves(self):
        with self.desk() as desk:
            enquiry_now = desk.store.get_enquiry(KEY)
            plan = json.loads(desk.store.plan_body(enquiry_now.plan_sha))
            plan["operations"].insert(0, {"op": "delete_contact", "contact": "1"})
            sha = sha256_hex(plan)
            desk.store.save_plan(sha, KEY, canonical_json(plan))
            desk.store.set_fields(KEY, plan_sha=sha)
            desk.approve(KEY, PERSON)
        before = self.crm_counts()
        result = self.apply()
        self.assertEqual(result.outcome, "refused")
        self.assertIn("outside the allow-list: delete_contact", result.message)
        self.assertEqual(self.crm_counts(), before)

    def test_an_approval_for_another_plan_is_refused_and_the_enquiry_waits_again(self):
        old_sha = self.approve()
        crm = PracticeCRM(crm_path(self.home))  # the CRM changes after the approval: the contact now exists
        crm.create_contact({"name": "Yara Test (invented)", "email": "yara.test@example.com"}, "")
        crm.close()
        before = self.crm_counts()
        result = self.apply()
        self.assertEqual(result.outcome, "replanned")
        self.assertIn("the approval is for another plan", result.message)
        self.assertEqual(self.crm_counts(), before)
        now = self.state()
        self.assertEqual(now.state, "awaiting_approval")
        self.assertNotEqual(now.plan_sha, old_sha)
        with self.desk() as desk:
            (approval,) = desk.store.approvals(KEY)
        self.assertEqual((approval.state, approval.closed_by), ("withdrawn", "system"))
        self.assertEqual(self.problems(), [])

    def test_an_approval_whose_sha_differs_from_the_stored_plan_is_refused(self):
        self.approve()
        other = {"enquiry": KEY, "operations": []}
        with self.desk() as desk:
            desk.store.save_plan(sha256_hex(other), KEY, canonical_json(other))
        self.desk_db().execute("UPDATE approvals SET plan_sha = ?", (sha256_hex(other),))
        result = self.apply()
        self.assertEqual(result.outcome, "refused")
        self.assertIn("the approval is for another plan", result.message)
        self.assertEqual(self.crm_counts()["notes"], 0)

    def test_a_stored_plan_changed_after_approval_is_refused(self):
        self.approve()
        conn = self.desk_db()
        body = json.loads(conn.execute("SELECT body FROM plans").fetchone()[0])
        body["operations"][0]["fields"]["name"] = "Someone Else (invented)"
        conn.execute("UPDATE plans SET body = ?", (canonical_json(body),))
        result = self.apply()
        self.assertEqual(result.outcome, "refused")
        self.assertIn("does not match its sha256", result.message)
        self.assertEqual(self.crm_counts()["contacts"], 10)

    def test_a_dry_run_writes_nothing_even_when_approved(self):
        self.approve()
        before = self.crm_counts()
        result = self.apply(dry_run=True)
        self.assertEqual(result.outcome, "dry_run")
        self.assertEqual(self.crm_counts(), before)
        self.assertEqual(self.state().state, "approved")

    def test_the_gate_refuses_any_write_in_a_dry_run_even_if_called_directly(self):
        with self.desk() as desk:
            gate = WriteGate(desk, dry_run=True)
            with self.assertRaises(GateRefusal):
                gate._write(desk.crm.add_note, "1", "x")
        self.assertEqual(self.crm_counts()["notes"], 0)

    def test_crm_down_then_retry_completes_and_doubles_nothing(self):
        self.approve()
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, "add_note", "before")
            (first,) = desk.apply(key=KEY)
        self.assertEqual(first.outcome, "apply_failed")
        self.assertIn("add_note", first.message)
        self.assertEqual(self.state().state, "apply_failed")
        self.assertIn("add_note", self.state().error)
        report = self.cli("report")
        self.assertIn(f"{KEY}: step 2 add_note", report)
        second = self.apply()
        self.assertEqual(second.outcome, "applied")
        self.assertEqual(self.crm_counts(), {"contacts": 11, "notes": 1, "tasks": 1})
        self.assertEqual(self.problems(), [])

    def test_a_lost_answer_is_found_again_not_written_again(self):
        self.approve()
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, "add_note", "after")
            (first,) = desk.apply(key=KEY)
        self.assertEqual(first.outcome, "apply_failed")
        self.assertEqual(self.notes_with_key(KEY), 1)  # the write happened; only its answer was lost
        second = self.apply()
        self.assertEqual(second.outcome, "applied")
        self.assertIn("add_note found", " ".join(second.steps))
        self.assertEqual(self.notes_with_key(KEY), 1)
        self.assertEqual(self.problems(), [])

    def test_a_lost_ledger_writes_nothing_twice(self):
        self.approve()
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, "add_task", "after")
            desk.apply(key=KEY)  # contact, note and task are all in the CRM; the task's answer was lost
        after_failure = self.crm_counts()
        self.assertEqual(after_failure, {"contacts": 11, "notes": 1, "tasks": 1})
        self.desk_db().execute("DELETE FROM ledger")
        # With no ledger, nothing on the desk shows a started step: the gate checks the plan against the
        # CRM, finds the contact this plan created, and plans again. It writes nothing and asks again.
        second = self.apply()
        self.assertEqual(second.outcome, "replanned")
        self.assertEqual(self.crm_counts(), after_failure)
        self.approve()
        third = self.apply()
        self.assertEqual(third.outcome, "applied")
        self.assertEqual(self.crm_counts(), after_failure)
        joined = " ".join(third.steps)
        self.assertIn("add_note found", joined)
        self.assertIn("add_task found", joined)
        self.assertEqual(self.problems(), [])

    def test_a_lost_ledger_mid_plan_on_a_returning_contact_completes_once(self):
        with self.desk() as desk:
            desk.receive(enquiry(source_id="test-000002", email="ada.fenwick@example.com"), via="file")
            desk.approve("webform:test-000002", PERSON)
            desk.crm = FlakyCRM(desk.crm, "add_task", "before")
            desk.apply(key="webform:test-000002")  # the note is written, the task is not
        self.desk_db().execute("DELETE FROM ledger")
        result = self.apply(key="webform:test-000002")  # the plan is unchanged by its own note: no new approval
        self.assertEqual(result.outcome, "applied")
        self.assertIn("add_note found", " ".join(result.steps))
        self.assertEqual(self.notes_with_key("webform:test-000002"), 1)
        self.assertEqual(self.problems(), [])

    def test_a_retry_does_not_plan_again_once_a_step_has_started(self):
        self.approve()
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, "add_note", "before")
            desk.apply(key=KEY)  # the contact is created, the note fails
        second = self.apply()  # the CRM now holds this plan's own contact; that is not a change by anyone else
        self.assertEqual(second.outcome, "applied")
        self.assertEqual(self.crm_counts()["contacts"], 11)

    def test_withdrawn_after_a_failure_then_approved_again_completes_once(self):
        self.approve()
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, "add_note", "before")
            desk.apply(key=KEY)
            desk.withdraw(KEY, PERSON, "check the CRM first")
            self.assertTrue(desk.refresh(KEY))  # the contact this plan created is now found: a returning plan
            desk.approve(KEY, PERSON)
        result = self.apply()
        self.assertEqual(result.outcome, "applied")
        self.assertEqual(self.crm_counts(), {"contacts": 11, "notes": 1, "tasks": 1})
        self.assertEqual(self.problems(), [])

    def test_a_retry_after_a_failure_that_left_nothing_started_plans_again(self):
        self.approve()
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, "find_by_email", "before")
            (first,) = desk.apply(key=KEY)
        self.assertEqual(first.outcome, "apply_failed")
        self.assertIn("checking the plan against the CRM", first.message)
        crm = PracticeCRM(crm_path(self.home))
        crm.create_contact({"name": "Yara Test (invented)", "email": "yara.test@example.com"}, "")
        crm.close()
        second = self.apply()
        self.assertEqual(second.outcome, "replanned")
        self.assertEqual(self.crm_counts()["notes"], 0)


if __name__ == "__main__":
    unittest.main()
