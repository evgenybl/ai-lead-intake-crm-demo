"""The five controls of section 6. Each breaks one property in memory or in its own temporary files, and
asserts that `desk.py doctor`, or the matching test's check, reports the break. A control that passed
while the property was broken would mean the doctor could not see that failure."""
import unittest
from unittest import mock

from intake import audit, planner
from intake.crm.base import CRMConflict
from intake.crm.practice import UNIQUE_EMAIL_INDEX, PracticeCRM
from intake.gate import WriteGate
from intake.home import crm_path
from intake.ops import ALLOWED_OPS
from intake.states import TransitionError

from tests.helpers import DeskCase, FlakyCRM, PERSON, approve_in, enquiry

KEY = "webform:test-000001"


class ReadsFromBefore:
    """A CRM whose look-ups were answered just before another writer's contact landed: they find nothing."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        if name in ("find_by_email", "find_by_phone", "find_by_name", "find_by_origin_key"):
            return lambda *args: []
        return getattr(self._inner, name)


class ControlTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(), via="file")
        self.assertEqual(self.problems(), [])

    def approve(self, key=KEY):
        with self.desk() as desk:
            approve_in(desk, key)

    def test_control_1_the_key_lookup_made_to_miss_a_duplicate_note_appears(self):
        self.approve()
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, "add_note", "after")  # the note is written; its answer is lost
            desk.apply(key=KEY)
        with self.desk() as desk:
            gate = WriteGate(desk)
            gate._find_note = lambda contact_id, key: None  # the control: the look-up always misses
            desk.apply(key=KEY, gate=gate)
        self.assertEqual(self.notes_with_key(KEY), 2)
        self.assertIn(f"no two notes with the same enquiry key: 2 records carry {KEY}", self.problems())

    def test_control_2_the_unique_index_dropped_a_second_contact_appears(self):
        with self.desk() as desk:
            desk.receive(enquiry(source_id="test-000002", message="A second question."), via="file")
        self.approve()
        self.approve("webform:test-000002")
        with self.desk() as desk:
            desk.apply(key=KEY)
            desk.crm = ReadsFromBefore(desk.crm)  # the second apply read the CRM before the first one's write
            (raced,) = desk.apply(key="webform:test-000002")
        # The matching check, with the index in place: the CRM refuses the second contact.
        self.assertEqual(raced.outcome, "apply_failed")
        self.assertIn("already has a contact with this e-mail", raced.message)
        self.assertEqual(self.crm_counts()["contacts"], 11)
        # The control: the index dropped, the same race leaves a second contact, and the doctor names it.
        self.crm_db().execute(f"DROP INDEX {UNIQUE_EMAIL_INDEX}")
        crm = PracticeCRM(crm_path(self.home))
        try:
            crm.create_contact({"name": "Yara Test (invented)", "email": "yara.test@example.com"}, "")
        except CRMConflict:
            self.fail("with the index dropped, the CRM should no longer refuse")
        finally:
            crm.close()
        self.assertEqual(self.crm_counts()["contacts"], 12)
        self.assertTrue(any(p.startswith("one contact per normalised e-mail: 2 contacts share one e-mail")
                            for p in self.problems()), self.problems())

    def test_control_3_the_gates_approval_check_removed_a_write_with_no_approval_is_found(self):
        with self.desk() as desk:
            gate = WriteGate(desk)
            gate._check_approval = lambda enquiry: None  # the control: no approval is asked for
            with self.assertRaises(TransitionError):  # the state table still will not call it applied...
                desk.apply(key=KEY, gate=gate)
        self.assertEqual(self.notes_with_key(KEY), 1)  # ...but the write has left the gate
        found = self.problems()
        self.assertTrue(any(f"carries {KEY}, which has no approval: a write with no approval" in p for p in found), found)
        self.assertTrue(any("every ledger row has an approval of its plan" in p for p in found), found)

    def test_control_4_the_allow_list_widened_a_foreign_operation_is_found(self):
        build = planner.build_plan

        def with_a_merge(*args):  # a planner fault that adds an operation outside the allow-list
            plan = build(*args)
            plan["operations"].append({"op": "merge_contacts", "contact": "1", "into": "2"})
            return plan

        with mock.patch.object(planner, "build_plan", with_a_merge):
            with self.desk() as desk:
                desk.receive(enquiry(source_id="test-000003"), via="file")
                approve_in(desk, "webform:test-000003")
                # The matching check, with the allow-list as it is: refused before anything leaves.
                (refused,) = desk.apply(key="webform:test-000003")
                self.assertEqual(refused.outcome, "refused")
                self.assertIn("outside the allow-list: merge_contacts", refused.message)
                # The control: the allow-list widened, and a way to perform the operation added.
                gate = WriteGate(desk, allowed_ops=ALLOWED_OPS + ("merge_contacts",))
                gate._do_merge_contacts = lambda key, op, refs: ("1", "written")
                (applied,) = desk.apply(key="webform:test-000003", gate=gate)
        self.assertEqual(applied.outcome, "applied")
        found = self.problems()
        self.assertTrue(any("holds 'merge_contacts', outside the allow-list" in p for p in found), found)
        self.assertIn("every operation is in the allow-list: the ledger records 'merge_contacts' for "
                      "webform:test-000003: a foreign operation reached the CRM", found)

    def test_control_5_the_audit_triggers_dropped_a_changed_row_is_found(self):
        self.approve()
        conn = self.desk_db()
        for name in audit.TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")  # the control
        conn.execute("UPDATE audit SET actor = 'someone else' WHERE event = 'approved'")  # no longer refused
        (row_id,) = conn.execute("SELECT id FROM audit WHERE event = 'approved'").fetchone()
        found = self.problems()
        self.assertIn(f"the audit chain is unbroken: row {row_id} was changed after it was written", found)
        self.assertIn("the audit triggers are in place: the trigger audit_refuses_update is missing: "
                      "the trail can be changed", found)


if __name__ == "__main__":
    unittest.main()
