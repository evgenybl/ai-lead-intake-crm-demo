"""Integration tests: the one write gate on real desk and practice CRM files."""
import json
import unittest
from unittest import mock

from intake.canonical import canonical_json, sha256_hex
from intake import pages
from intake.crm.practice import PracticeCRM
from intake.flow import DeskError
from intake.gate import GateRefusal, WriteGate
from intake.home import crm_path
from intake.store import DeskStore

from tests.helpers import DATA_FILE, DeskCase, FlakyCRM, PERSON, approve_in, enquiry

KEY = "webform:test-000001"


class GateTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(), via="file")

    def approve(self, key=KEY):
        with self.desk() as desk:
            return approve_in(desk, key)

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
            approve_in(desk, KEY)
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

    def test_an_operation_that_appears_twice_is_refused_before_anything_leaves(self):
        with self.desk() as desk:
            plan = json.loads(desk.store.plan_body(desk.store.get_enquiry(KEY).plan_sha))
            plan["operations"].append(dict(plan["operations"][1]))  # a second add_note
            sha = sha256_hex(plan)
            desk.store.save_plan(sha, KEY, canonical_json(plan))
            desk.store.set_fields(KEY, plan_sha=sha)
            approve_in(desk, KEY)
        before = self.crm_counts()
        result = self.apply()
        self.assertEqual(result.outcome, "refused")
        self.assertIn("an operation that appears more than once: add_note", result.message)
        self.assertEqual(self.crm_counts(), before)

    def test_a_search_hit_counts_as_the_note_only_when_its_first_line_is_the_key(self):
        self.approve()
        conn = self.crm_db()
        conn.execute("INSERT INTO notes (contact_id, body) VALUES (1, ?)", (f"about something else\nsee {KEY}",))
        conn.commit()
        with self.desk() as desk:
            gate = WriteGate(desk)
            self.assertEqual([n.first_line for n in desk.crm.search_notes(KEY)], ["about something else"])
            self.assertIsNone(gate._find_note("1", KEY))
        result = self.apply()
        self.assertIn("add_note written", " ".join(result.steps))
        self.assertEqual(self.notes_with_key(KEY), 1)

    def test_a_contact_created_whose_answer_was_lost_is_found_by_its_origin_key(self):
        self.approve()
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, "create_contact", "after")
            self.assertEqual(desk.apply(key=KEY)[0].outcome, "apply_failed")
        self.assertEqual(self.crm_counts()["contacts"], 11)  # it was written; its answer was lost
        second = self.apply()
        self.assertEqual(second.outcome, "applied")
        self.assertIn("create_contact found by its origin key", " ".join(second.steps))
        self.assertEqual(self.crm_counts()["contacts"], 11)
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
        # The loss itself stays visible: the trail records a create_contact the ledger no longer holds.
        self.assertEqual(self.problems(), [
            "every operation in the trail has its ledger row; no step is left half recorded: webform:test-000001: "
            "row 6 records create_contact, but the ledger has no row for it"])

    def test_a_lost_ledger_mid_plan_on_a_returning_contact_completes_once(self):
        with self.desk() as desk:
            desk.receive(enquiry(source_id="test-000002", email="ada.fenwick@example.com"), via="file")
            approve_in(desk, "webform:test-000002")
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
            approve_in(desk, KEY)
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


class Killed(BaseException):
    """The process stopped: not an error the gate can catch (as KeyboardInterrupt, or a kill, is not)."""


class KilledAfter:
    """Wraps a CRM: the chosen write happens, then the process 'stops' before the gate records it."""

    def __init__(self, inner, method: str):
        self._inner, self._method, self._left = inner, method, 1

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if name != self._method:
            return attribute

        def write_then_stop(*args):
            result = attribute(*args)
            if self._left:
                self._left -= 1
                raise Killed(f"stopped right after {name}")
            return result

        return write_then_stop


class KilledBetweenWriteAndRecordTests(DeskCase):
    """The intent is on the record before each CRM write; a retry continues the same plan, once."""

    CASES = {
        "new contact": ({}, ("create_contact", "add_note", "add_task")),
        "returning contact": ({"email": "celia.marsh@example.com", "phone": "", "name": "Celia Marsh (invented)",
                               "company": "Marsh Studio (invented)"}, ("fill_empty_fields", "add_note", "add_task")),
    }

    def test_killed_right_after_each_write_the_doctor_names_the_step_and_a_retry_completes_once(self):
        for label, (fields, methods) in self.CASES.items():
            for method in methods:
                with self.subTest(case=label, killed_after=method):
                    self.home = self.new_home(f"{label}-{method}")
                    self.init()
                    with self.desk() as desk:
                        desk.receive(enquiry(**fields), via="file")
                        approve_in(desk, KEY)
                    with self.desk() as desk:
                        desk.crm = KilledAfter(desk.crm, method)
                        with self.assertRaises(Killed):
                            desk.apply(key=KEY)
                    with self.desk() as desk:
                        self.assertEqual(desk.store.get_enquiry(KEY).state, "approved")
                        started = [e for _, _, _, e, *_ in desk.store.trail(KEY)][-1]
                    self.assertEqual(started, "operation_started")
                    said = [p for p in self.problems() if f"the step {method} began and its end is not recorded" in p]
                    self.assertEqual(len(said), 1, self.problems())
                    # It says what the desk knows, and a command that works in this state; never a cause it guesses.
                    self.assertIn(f"{KEY} (approved): the step {method} began and its end is not recorded, so the desk "
                                  f"does not know whether it reached the CRM; `desk.py apply --key {KEY}` continues "
                                  "the plan under the same approval", said[0])
                    self.assertNotIn("process stopped", said[0])
                    out = self.cli("apply")  # the same approval: no new decision is asked for
                    self.assertIn(f"{KEY}: applied", out)
                    self.assertIn("apply: 1 applied", out)
                    counts = self.crm_counts()
                    self.assertEqual(counts["contacts"], 11 if label == "new contact" else 10)
                    self.assertEqual((counts["notes"], counts["tasks"]), (1, 1))
                    with self.desk() as desk:
                        self.assertEqual([a.state for a in desk.store.approvals(KEY)], ["used"])
                    self.assertEqual(self.problems(), [])

    def new_home(self, name):
        import os
        return os.path.join(self._tmp.name, name.replace(" ", "-"))


class AfterAFailedApplyTests(DeskCase):
    """Withdrawing or rejecting after an apply that did not finish leaves the ledger true: each step it began is
    looked for in the CRM and recorded as written (with where) or not written. Nothing then says "nothing written"
    of an enquiry part of whose plan is in the CRM, and the gate never applies a plan that would not do what it
    says."""

    def setUp(self):
        super().setUp()
        self.init()
        self.cli("ingest", DATA_FILE)

    def failed_at(self, key, method, mode="before"):
        self.approve_cli(key)
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, method, mode)
            (result,) = desk.apply(key=key)
        self.assertEqual(result.outcome, "apply_failed")
        return result

    def page(self, key):
        with self.desk() as desk:
            return pages.enquiry_page(desk.store, key, pages.FileLinks(True), pages.Decisions())

    def test_withdraw_then_reject_after_a_failure_at_the_first_step_leaves_the_doctor_green(self):
        key = "webform:wf-000102"
        self.failed_at(key, "create_contact")  # the CRM refused the first write: nothing reached it
        self.assertEqual(self.problems(), [])
        out = self.cli("withdraw", key, "--by", PERSON)
        self.assertIn("the step create_contact, begun by an apply that did not finish, is not in the CRM: not "
                      "written; the desk records it", out)
        self.assertEqual(self.problems(), [])
        self.assertIn(f"rejected: {key}; nothing will be written",
                      self.cli("reject", key, "--by", PERSON, "--reason", "not for us"))
        self.assertEqual(self.cli("apply"), "apply: nothing is approved\n")
        self.assertEqual(self.problems(), [])
        self.assertRegex(self.cli("doctor"), r"doctor: (\d+) of \1 checks passed")
        self.assertIn(" operation_not_written ", self.cli("trail", key))
        self.assertEqual(self.crm_counts(), {"contacts": 10, "notes": 0, "tasks": 0})

    def test_a_write_the_apply_made_and_did_not_record_is_found_and_recorded_when_withdrawn(self):
        key = "webform:wf-000102"
        self.failed_at(key, "create_contact", "after")  # the contact was written; its answer was lost
        out = self.cli("withdraw", key, "--by", PERSON)
        self.assertIn("the step create_contact, begun by an apply that did not finish, is found in the CRM: "
                      "contact 11 created; the desk records it", out)
        with self.desk() as desk:
            (row,) = desk.store.ledger_rows(key)
        self.assertEqual((row.op, row.status, row.crm_ref, row.contact), ("create_contact", "done", "11", "11"))
        self.assertEqual(self.problems(), [])

    def test_rejected_after_part_of_the_plan_was_written_says_what_was_written_and_where(self):
        key = "webform:wf-000103"
        failed = self.failed_at(key, "add_note")  # the contact is created, the note is not
        self.assertIn("step 2 add_note", failed.message)
        self.cli("withdraw", key, "--by", PERSON)
        out = self.cli("reject", key, "--by", PERSON, "--reason", "not for us")
        self.assertIn(f"rejected: {key} after 1 of 3 steps had been written: contact 11 created; nothing more will "
                      "be written", out)
        self.assertNotIn("nothing will be written", out)
        page = self.page(key)
        self.assertIn("Rejected by a person after 1 of 3 steps had been written: contact 11 created; nothing more "
                      "is written", page)
        self.assertNotIn("nothing written", page)
        refused = self.cli("apply", "--key", key, expect=1)
        self.assertIn("nothing written by this apply (written before: contact 11 created)", refused)
        self.assertEqual(self.crm_db().execute("SELECT origin_key FROM contacts WHERE id = 11").fetchone()[0], key)
        self.assertEqual(self.problems(), [])

    def contact_2_s_note_then_its_e_mail_changed(self, key):
        self.failed_at(key, "add_task")  # the note is on contact 2; the task is not written
        conn = self.crm_db()
        conn.execute("UPDATE contacts SET email = 'b.calloway@example.com', email_norm = 'b.calloway@example.com' "
                     "WHERE id = 2")
        conn.commit()
        self.cli("withdraw", key, "--by", PERSON)
        self.assertIn(f"{key}: planned again: the CRM changed", self.cli("plan"))
        with self.desk() as desk:
            self.assertEqual(desk.store.get_enquiry(key).state, "held_possible_duplicate")

    def test_a_note_already_on_another_contact_is_named_in_the_new_plan_and_not_planned_again(self):
        key = "webform:wf-000109"
        self.contact_2_s_note_then_its_e_mail_changed(key)
        self.cli("resolve", key, "--as", "new", "--by", PERSON)
        plan = self.cli("plan", "--key", key)
        self.assertIn("already in the CRM, written by an apply that did not complete, not written again: note 1 on "
                      "contact 2", plan)
        self.assertNotIn("add_note on contact created", plan)
        self.assertIn("add_task on contact created", plan)
        self.assertIn("Already in the CRM, written by an apply that did not complete, and not written again:",
                      self.page(key))
        self.approve_cli(key)
        out = self.cli("apply")
        self.assertIn(f"{key}: applied: create_contact written (11), add_task written (1)", out)
        conn = self.crm_db()
        self.assertEqual(conn.execute("SELECT contact_id FROM notes").fetchall(), [(2,)])
        self.assertEqual(conn.execute("SELECT contact_id FROM tasks").fetchall(), [(11,)])
        self.assertEqual(self.problems(), [])

    def test_the_gate_refuses_a_plan_that_states_a_step_the_ledger_records_on_another_contact(self):
        key = "webform:wf-000109"
        self.contact_2_s_note_then_its_e_mail_changed(key)
        # A plan made without the ledger's knowledge: it states the note on the contact it would create.
        with mock.patch.object(DeskStore, "written", lambda store, k: {}):
            self.cli("resolve", key, "--as", "new", "--by", PERSON)
            self.assertIn("add_note on contact created", self.cli("plan", "--key", key))
            self.approve_cli(key)
        before = self.crm_counts()
        result = self.apply_one(key)
        self.assertEqual(result.outcome, "replanned")
        self.assertIn("the plan states add_note on the new contact, but the ledger records it written on contact 2",
                      result.message)
        self.assertEqual(self.crm_counts(), before)
        with self.desk() as desk:
            now = desk.store.get_enquiry(key)
            self.assertEqual(now.state, "awaiting_approval")
            self.assertEqual([a.state for a in desk.store.approvals(key)][-1], "withdrawn")
            plan = json.loads(desk.store.plan_body(now.plan_sha))
        self.assertEqual([op["op"] for op in plan["operations"]], ["create_contact", "add_task"])
        self.assertEqual(plan["already_written"], [{"op": "add_note", "contact": "2", "ref": "1"}])
        self.approve_cli(key)
        self.assertEqual(self.apply_one(key).outcome, "applied")
        self.assertEqual(self.notes_with_key(key), 1)
        self.assertEqual(self.problems(), [])

    def test_the_doctor_offers_no_command_for_a_started_step_in_a_state_where_none_would_work(self):
        key = "webform:wf-000103"
        self.failed_at(key, "add_note")
        self.cli("withdraw", key, "--by", PERSON)
        self.cli("reject", key, "--by", PERSON, "--reason", "not for us")
        self.assertEqual(self.problems(), [])
        with self.desk() as desk:  # a step left started on a rejected enquiry, as no command now leaves one
            sha = desk.store.get_enquiry(key).plan_sha
            desk.store.ledger_start(key, "add_task", sha)
        (said,) = self.problems()
        self.assertTrue(said.endswith(f"{key} (rejected): the step add_task began and its end is not recorded, so the "
                                      "desk does not know whether it reached the CRM"), said)

    def test_a_crm_id_in_what_was_written_is_printed_on_one_line_that_cannot_move_the_cursor(self):
        key = "webform:wf-000103"
        self.failed_at(key, "add_note")
        self.desk_db().execute("UPDATE ledger SET crm_ref = '11\x1b[2J', contact = '11\x1b[2J' WHERE key = ?", (key,))
        self.cli("withdraw", key, "--by", PERSON)
        for out in (self.cli("plan", "--key", key), self.cli("reject", key, "--by", PERSON, "--reason", "not for us")):
            self.assertNotIn("\x1b", out)
            self.assertIn("contact 11\\x1b[2J created", out)

    def test_a_crm_id_found_when_withdrawn_is_printed_on_one_line_that_cannot_move_the_cursor(self):
        from intake.cli import _say_settled
        from intake.store import LedgerRow

        said = []
        row = LedgerRow("webform:wf-000103", "add_note", "0" * 64, "started", None, None, "now", None)
        _say_settled(said.append, [(row, ("n-1\x1b[2J", "found", "c-9\x1b[2J"))])
        self.assertEqual(said, ["the step add_note, begun by an apply that did not finish, is found in the CRM: note "
                                "n-1\\x1b[2J on contact c-9\\x1b[2J; the desk records it"])

    def apply_one(self, key):
        with self.desk() as desk:
            (result,) = desk.apply(key=key)
        return result

    def test_withdraw_waits_for_a_crm_that_cannot_be_read_and_changes_nothing(self):
        key = "webform:wf-000102"
        self.failed_at(key, "create_contact")
        with self.desk() as desk:
            desk.crm = FlakyCRM(desk.crm, "find_by_origin_key", "before")
            with self.assertRaises(DeskError) as refused:
                desk.withdraw(key, PERSON)
        self.assertIn("the CRM could not be read", str(refused.exception))
        with self.desk() as desk:
            self.assertEqual(desk.store.get_enquiry(key).state, "apply_failed")
            self.assertEqual([r.status for r in desk.store.ledger_rows(key)], ["started"])


class OneApplyAtATimeTests(DeskCase):
    """Two appliers forced to interleave between the look-up and the write: the second is refused."""

    def test_a_second_apply_while_one_is_between_its_look_up_and_its_write_is_refused(self):
        import threading

        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(email="ada.fenwick@example.com", phone="000-000-0101", name="Ada Fenwick (invented)",
                                 company="Fenwick Dental (invented)"), via="file")
            approve_in(desk, KEY)
        looked, release = threading.Event(), threading.Event()

        class PausedAfterLookUp:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                attribute = getattr(self._inner, name)
                if name != "list_notes":
                    return attribute

                def look_then_wait(*args):
                    found = attribute(*args)  # the look-up has answered: no note yet
                    looked.set()
                    release.wait(10)
                    return found

                return look_then_wait

        first = []

        def first_apply():
            with self.desk() as desk:
                desk.crm = PausedAfterLookUp(desk.crm)
                first.extend(desk.apply(key=KEY))

        thread = threading.Thread(target=first_apply)
        thread.start()
        try:
            self.assertTrue(looked.wait(10))
            with self.desk() as desk:
                with self.assertRaises(DeskError) as refused:
                    desk.apply(key=KEY)
            self.assertIn("another `desk.py apply` is running on this desk", str(refused.exception))
        finally:
            release.set()
            thread.join(10)
        self.assertEqual([r.outcome for r in first], ["applied"])
        self.assertEqual(self.notes_with_key(KEY), 1)
        self.assertEqual(self.problems(), [])
        self.assertEqual(self.cli("apply"), "apply: nothing is approved\n")  # the lock was released

    def test_a_withdrawal_while_an_apply_runs_is_refused_and_the_apply_writes_on_a_live_approval(self):
        import threading

        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(email="ada.fenwick@example.com", phone="000-000-0101", name="Ada Fenwick (invented)",
                                 company="Fenwick Dental (invented)"), via="file")
            approve_in(desk, KEY)
        looked, release = threading.Event(), threading.Event()

        class PausedAtItsFirstRead:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                attribute = getattr(self._inner, name)
                if name != "find_by_email":
                    return attribute

                def read_then_wait(*args):
                    found = attribute(*args)  # the gate checks the plan against the CRM before its first step
                    looked.set()
                    release.wait(10)
                    return found

                return read_then_wait

        first = []

        def first_apply():
            with self.desk() as desk:
                desk.crm = PausedAtItsFirstRead(desk.crm)
                first.extend(desk.apply(key=KEY))

        thread = threading.Thread(target=first_apply)
        thread.start()
        try:
            self.assertTrue(looked.wait(10))
            with self.desk() as desk:  # the apply has read the approval and begun no step yet
                self.assertEqual([row.status for row in desk.store.ledger_rows(KEY)], [])
                with self.assertRaises(DeskError) as refused:
                    desk.withdraw(KEY, PERSON)
                self.assertEqual(desk.store.get_enquiry(KEY).state, "approved")
                self.assertIsNotNone(desk.store.live_approval(KEY))
            self.assertIn("another `desk.py apply` is running on this desk; nothing was changed", str(refused.exception))
        finally:
            release.set()
            thread.join(10)
        self.assertEqual([r.outcome for r in first], ["applied"])
        self.assertNotIn(" withdrawn ", self.cli("trail", KEY))
        self.assertEqual(self.problems(), [])
        refused = self.cli("withdraw", KEY, "--by", PERSON, expect=1)  # afterwards: the approval was used
        self.assertIn(f"{KEY} has no approval to withdraw; it is applied", refused)

    def test_a_withdrawal_that_finds_its_approval_used_by_an_apply_that_just_ended_says_so(self):
        from contextlib import contextmanager

        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(email="ada.fenwick@example.com", phone="000-000-0101", name="Ada Fenwick (invented)",
                                 company="Fenwick Dental (invented)"), via="file")
            approve_in(desk, KEY)
        with self.desk() as desk:
            take_the_lock = desk.store.apply_lock

            @contextmanager
            def after_an_apply_that_ran_to_its_end():  # between the withdrawal's first look and its lock
                with self.desk() as other:
                    self.assertEqual([r.outcome for r in other.apply(key=KEY)], ["applied"])
                with take_the_lock():
                    yield

            desk.store.apply_lock = after_an_apply_that_ran_to_its_end
            with self.assertRaises(DeskError) as refused:
                desk.withdraw(KEY, PERSON)
        self.assertEqual(str(refused.exception), f"{KEY} has no approval to withdraw; it is applied")
        self.assertNotIn(" withdrawn ", self.cli("trail", KEY))
        self.assertEqual(self.problems(), [])


class LockedCRMFileTests(DeskCase):
    def test_a_practice_crm_file_locked_by_another_program_is_apply_failed_not_a_crash(self):
        import sqlite3

        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(), via="file")
            approve_in(desk, KEY)
        holder = sqlite3.connect(crm_path(self.home), isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        crm = PracticeCRM(crm_path(self.home))
        self.addCleanup(crm.close)
        crm.conn.execute("PRAGMA busy_timeout = 100")
        with self.desk(crm=crm) as desk:
            (result,) = desk.apply(key=KEY)
        self.assertEqual(result.outcome, "apply_failed")
        self.assertIn("the practice CRM file could not be used (database is locked)", result.message)
        holder.execute("ROLLBACK")
        self.assertIn("apply: 1 applied", self.cli("apply"))
        self.assertEqual(self.problems(), [])


if __name__ == "__main__":
    unittest.main()
