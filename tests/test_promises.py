"""The four promises, one test each, named by the promise; and the doctor catching a break of each.

The five controls (tests/test_controls.py) break the properties these rest on."""
import json
import unittest

from intake import audit
from intake.canonical import canonical_json, sha256_hex

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
            self.approve_cli(key)
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
        self.approve_cli("webform:wf-000101")
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
        self.approve_cli("webform:wf-000110")
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


class CorrectedResendTests(DeskCase):
    """A refused enquiry never holds its key against a later copy that passes."""

    def setUp(self):
        super().setUp()
        self.init()

    def events(self, key):
        with self.desk() as desk:
            return [(row[3], audit.detail_of(row)) for row in desk.store.trail(key)]

    def test_refused_then_a_copy_that_passes_becomes_the_enquiry_and_the_trail_says_why_the_first_was_refused(self):
        for label, key_fields in (("keyed", {}), ("hashed", {"source_id": ""})):
            with self.subTest(label=label):
                with self.desk() as desk:
                    first = desk.receive(enquiry(consent=False, **key_fields), via="http")
                    second = desk.receive(enquiry(**key_fields), via="http")
                    stored = desk.store.get_enquiry(second.key)
                self.assertEqual((first.state, first.key), ("refused", second.key))
                self.assertEqual((second.state, second.replay, second.resent), ("awaiting_approval", False, True))
                self.assertTrue(second.as_dict()["earlier_copy_refused"])
                self.assertIs(stored.payload["consent"], True)
                self.assertEqual(stored.reasons, [])
                events = self.events(second.key)
                self.assertEqual([e for e, _ in events][:3], ["arrived", "resent", "draft"])
                resent = events[1][1]
                self.assertEqual((resent["from"], resent["state"], resent["earlier_copy_refused"],
                                  resent["fields_changed"]), ("refused", "received", ["no_consent"], ["consent"]))
                self.assertEqual(self.problems(), [])
        report = self.cli("report")
        self.assertIn("corrected resends            2", report)
        self.assertIn("the states add up: yes: 2 enquiries + 0 replays + 2 corrected resends = 4 arrived", report)

    def test_a_good_enquiry_then_a_bad_copy_stays_a_replay_and_changes_nothing(self):
        with self.desk() as desk:
            good = desk.receive(enquiry(), via="http")
            bad = desk.receive(enquiry(consent=False), via="http")
            self.assertEqual(desk.store.get_enquiry(good.key).payload["consent"], True)
        self.assertEqual((good.state, bad.replay, bad.state, bad.resent), ("awaiting_approval", True, "awaiting_approval", False))
        self.assertEqual(self.problems(), [])

    def test_refused_twice_stays_refused_and_the_second_is_a_replay(self):
        with self.desk() as desk:
            desk.receive(enquiry(consent=False), via="file")
            again = desk.receive(enquiry(consent=False, website="filled"), via="file")
        self.assertEqual((again.replay, again.state), (True, "refused"))
        self.assertEqual(self.problems(), [])


class FormulaLikeContactFieldsTests(DeskCase):
    """A phone or an e-mail that would start a formula in a spreadsheet, or holds a control character, is refused at
    the door, as a name or a company is: none reaches the CRM, whoever approves what."""

    def test_none_of_the_four_contact_fields_reaches_the_crm_starting_like_a_formula(self):
        self.init()
        cases = {"formula-phone": {"phone": "=1+1+cmd|' /C calc'!A0 5550142", "email": ""},
                 "plus-formula-phone": {"phone": "+1+1+cmd|' /C calc'!A0 5550142", "email": ""},
                 "formula-email": {"email": "=1+1@example.com", "phone": ""},
                 "escape-email": {"email": "esc\x1b[31m@example.com", "phone": ""}}
        with self.desk() as desk:
            for source_id, fields in cases.items():
                with self.subTest(case=source_id):
                    answer = desk.receive(enquiry(source_id=source_id, **fields), via="file")
                    self.assertEqual(answer.state, "refused")
        self.assertEqual(self.cli("apply"), "apply: nothing is approved\n")
        self.assertEqual(self.crm_counts(), {"contacts": 10, "notes": 0, "tasks": 0})
        self.assertEqual(self.problems(), [])


class DoctorFindsEachBrokenPromiseTests(DeskCase):
    """Beyond the five controls: the doctor names a break of each promise, made directly in the files."""

    def setUp(self):
        super().setUp()
        self.init()
        self.cli("ingest", DATA_FILE)
        self.approve_cli("webform:wf-000101")
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
        self.assertIn("the arrivals add up, and agree with the trail: webform:wf-000102 has 0 first arrivals, not 1", self.problems())

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
        self.assertIn("FAIL  each enquiry's state, content and plan are the ones its trail last records", out)
        self.assertIn("checks FAILED", out)


class DoctorComparesTheLedgerWithTheCRMTests(DeskCase):
    """Each write the ledger records is looked for in the practice CRM, where the ledger says it is; and what the
    doctor does not notice, because it touches nothing the doctor compares."""

    def setUp(self):
        super().setUp()
        self.init()
        self.cli("ingest", DATA_FILE)
        for key in ("webform:wf-000101", "webform:wf-000109"):  # contact 11 created; contact 2 returning
            self.approve_cli(key)
        self.cli("apply")
        self.assertEqual(self.problems(), [])
        self.crm = self.crm_db()

    def edit(self, sql, *args):
        self.crm.execute(sql, args)
        self.crm.commit()
        return self.problems()

    def id_of(self, table, key):
        return self.crm.execute(f"SELECT id FROM {table} WHERE body LIKE ?", (key + "%",)).fetchone()[0]

    def test_a_note_or_a_task_the_desk_wrote_deleted_is_found(self):
        note, task = self.id_of("notes", "webform:wf-000109"), self.id_of("tasks", "webform:wf-000109")
        found = self.edit("DELETE FROM notes WHERE id = ?", note)
        self.assertIn(f"every write the ledger records is in the CRM, where the ledger says: webform:wf-000109: the "
                      f"ledger records note {note} on contact 2, but the CRM has no note {note}", found)
        found = self.edit("DELETE FROM tasks WHERE id = ?", task)
        self.assertTrue(any(f"the ledger records task {task} on contact 2, but the CRM has no task {task}" in p
                            for p in found), found)

    def test_a_note_moved_to_another_contact_is_found(self):
        note = self.id_of("notes", "webform:wf-000109")
        self.assertIn(f"every write the ledger records is in the CRM, where the ledger says: webform:wf-000109: the "
                      f"ledger records note {note} on contact 2, but it is on contact 3",
                      self.edit("UPDATE notes SET contact_id = 3 WHERE id = ?", note))

    def test_a_ledger_row_pointed_at_another_record_is_found(self):
        other = self.id_of("notes", "webform:wf-000101")
        self.desk_db().execute("UPDATE ledger SET crm_ref = ? WHERE key = 'webform:wf-000109' AND op = 'add_note'",
                               (str(other),))
        self.assertTrue(any(f"webform:wf-000109: the ledger records note {other} on contact 2, but its first line is "
                            "no longer the enquiry's key" in p for p in self.problems()), self.problems())

    def test_a_created_contact_deleted_or_stripped_of_its_origin_key_is_found(self):
        self.assertIn("every write the ledger records is in the CRM, where the ledger says: webform:wf-000101: the "
                      "ledger records contact 11 created by this enquiry, but the contact no longer carries its key",
                      self.edit("UPDATE contacts SET origin_key = '' WHERE id = 11"))
        self.assertTrue(any("webform:wf-000101: the ledger records contact 11 created, but the CRM has no contact 11"
                            in p for p in self.edit("DELETE FROM contacts WHERE id = 11")))

    def test_what_it_does_not_notice_a_note_rewritten_below_its_first_line_and_a_contact_s_fields_changed(self):
        note = self.id_of("notes", "webform:wf-000109")
        self.assertEqual(self.edit("UPDATE notes SET body = 'webform:wf-000109' || char(10) || 'rewritten' "
                                   "WHERE id = ?", note), [])
        self.assertEqual(self.edit("UPDATE contacts SET name = 'Someone Else (invented)', phone = '000-000-0999', "
                                   "phone_digits = '0000000999' WHERE id = 2"), [])
        self.assertEqual(self.edit("UPDATE contacts SET company = 'Changed (invented)' WHERE id = 11"), [])

    def test_what_it_does_not_notice_a_desk_column_no_trail_row_records_edited_by_hand(self):
        desk = self.desk_db()
        for sql in ("UPDATE enquiries SET draft = replace(draft, 'quote', 'EDITED') WHERE key = 'webform:wf-000101'",
                    "UPDATE enquiries SET reasons = '[]' WHERE key = 'webform:wf-000114'",
                    "UPDATE enquiries SET resolution = 'contact:2' WHERE key = 'webform:wf-000112'",
                    "UPDATE enquiries SET identity = '{\"kind\": \"new\"}' WHERE key = 'webform:wf-000113'"):
            with self.subTest(sql=sql):
                self.assertEqual(desk.execute(sql).rowcount, 1)
                self.assertEqual(self.problems(), [])


class DoctorFindsHandEditsOfTheDeskFileTests(DeskCase):
    """Edits of the desk file made by hand, as a person who can open it might make them: what the doctor finds, and,
    in the tests that say so, what it cannot. docs/DESIGN.md names each of these tests in its table."""

    KEY = "webform:wf-000101"

    def setUp(self):
        super().setUp()
        self.init()
        self.cli("ingest", DATA_FILE)
        self.approve_cli(self.KEY)
        self.cli("apply")
        self.assertEqual(self.problems(), [])

    def with_triggers_off(self, *statements):
        conn = self.desk_db()
        for name in audit.TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        for sql, args in statements:
            conn.execute(sql, args)
        conn.executescript(audit.SCHEMA)

    def last_row(self):
        return self.desk_db().execute("SELECT id, key, event FROM audit ORDER BY id DESC LIMIT 1").fetchone()

    def test_the_last_row_removed_is_found_when_it_is_a_state_move(self):
        # A state move: the enquiry's state is no longer the last its trail records.
        row_id, key, event = self.last_row()
        self.assertEqual((key, event), (self.KEY, "applied"))
        self.with_triggers_off(("DELETE FROM audit WHERE id = ?", (row_id,)))
        self.assertIn(f"each enquiry's state, content and plan are the ones its trail last records: {self.KEY} is "
                      "applied, but its trail last records 'approved'", self.problems())

    def test_the_last_row_removed_is_found_when_it_is_a_replay(self):
        self.cli("ingest", DATA_FILE, at="2026-01-05T10:00:00Z")
        row_id, _, event = self.last_row()
        self.assertEqual(event, "replay")
        self.with_triggers_off(("DELETE FROM audit WHERE id = ?", (row_id,)))
        self.assertIn("the arrivals add up, and agree with the trail: the trail records 32 'replay' events, but there "
                      "are 33 replays", self.problems())

    def test_what_it_cannot_find_the_last_row_removed_when_it_records_no_state_and_no_arrival(self):
        self.cli("apply", "--key", "webform:wf-000102", expect=1)  # refused for want of an approval
        row_id, key, event = self.last_row()
        self.assertEqual((key, event), ("webform:wf-000102", "apply_refused"))
        self.with_triggers_off(("DELETE FROM audit WHERE id = ?", (row_id,)))
        self.assertEqual(self.problems(), [])

    def test_what_it_cannot_find_a_whole_enquiry_that_arrived_last_removed_with_its_rows(self):
        with self.desk() as desk:
            last = desk.receive(enquiry(source_id="arrived-last"), via="file").key
        self.with_triggers_off(("DELETE FROM audit WHERE key = ?", (last,)), ("DELETE FROM arrivals WHERE key = ?", (last,)),
                               ("DELETE FROM enquiries WHERE key = ?", (last,)))
        self.assertEqual(self.problems(), [])

    def test_a_trigger_replaced_by_one_of_the_same_name_that_does_nothing_is_found(self):
        conn = self.desk_db()
        conn.execute("DROP TRIGGER audit_refuses_update")
        conn.execute("CREATE TRIGGER audit_refuses_update BEFORE UPDATE ON audit WHEN 0 BEGIN SELECT 1; END")
        self.assertIn("the audit triggers are in place, as written: the trigger audit_refuses_update is not as "
                      "written: the trail may be changed", self.problems())

    def test_an_approval_inserted_by_sql_with_no_decision_in_the_trail_is_found(self):
        key = "webform:wf-000102"
        conn = self.desk_db()
        sha = self.plan_sha(key)
        conn.execute("INSERT INTO approvals (key, plan_sha, by, at, state) VALUES (?, ?, 'nobody', 'now', 'approved')",
                     (key, sha))
        conn.execute("UPDATE enquiries SET state = 'approved' WHERE key = ?", (key,))
        self.cli("apply")  # the gate sees an approval row; the doctor sees it was never decided on the desk
        found = self.problems()
        self.assertTrue(any(p.startswith("every approval matches the decision its trail records: approval 2 for "
                                         f"{key} has no 'approved' event in the trail") for p in found), found)
        self.assertTrue(any(f"{key}: row" in p and "moves from approved, but the rows before it leave it in "
                            "awaiting_approval" in p for p in found), found)

    def test_the_name_on_an_approval_edited_is_found(self):
        self.desk_db().execute("UPDATE approvals SET by = 'Forged Name (invented)'")
        self.assertTrue(any("does not match the 'approved' event" in p for p in self.problems()))

    def test_an_enquiry_s_stored_content_edited_is_found(self):
        self.desk_db().execute("UPDATE enquiries SET payload = replace(payload, 'office', 'EDITED office') "
                               "WHERE key = ?", (self.KEY,))
        self.assertIn(f"each enquiry's state, content and plan are the ones its trail last records: {self.KEY}: the "
                      "stored content does not match the sha256 its trail records", self.problems())

    def test_a_plan_rewritten_with_its_sha256_changed_everywhere_on_the_desk_is_found(self):
        sha = self.plan_sha("webform:wf-000102")
        conn = self.desk_db()
        body = json.loads(conn.execute("SELECT body FROM plans WHERE sha = ?", (sha,)).fetchone()[0])
        body["operations"][1]["body"] += "\nINJECTED LINE"
        new = sha256_hex(body)
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE plans SET sha = ?, body = ? WHERE sha = ?", (new, canonical_json(body), sha))
        conn.execute("UPDATE enquiries SET plan_sha = ? WHERE plan_sha = ?", (new, sha))
        found = self.problems()
        self.assertIn(f"each enquiry's state, content and plan are the ones its trail last records: webform:wf-000102: "
                      f"its plan is {new[:12]}, but its trail last records {sha[:12]}", found)

    def test_the_ledger_emptied_is_found(self):
        self.desk_db().execute("DELETE FROM ledger")
        found = self.problems()
        self.assertTrue(any("records create_contact, but the ledger has no row for it" in p for p in found), found)

    def test_a_replay_row_removed_from_the_middle_of_the_trail_is_found(self):
        self.cli("ingest", DATA_FILE, at="2026-01-05T10:00:00Z")
        self.with_triggers_off(("DELETE FROM audit WHERE id = (SELECT min(id) FROM audit WHERE event = 'replay')", ()))
        found = self.problems()
        self.assertTrue(any(p.startswith("the audit chain is unbroken: row") for p in found), found)
        self.assertIn("the arrivals add up, and agree with the trail: the trail records 32 'replay' events, but there "
                      "are 33 replays", found)

    def test_an_applied_enquiry_whose_approval_is_not_used_is_found(self):
        self.desk_db().execute("UPDATE approvals SET state = 'withdrawn' WHERE key = ?", (self.KEY,))
        self.assertIn(f"every applied enquiry has a used approval for its plan: {self.KEY} is applied, but no "
                      "approval of its plan is used", self.problems())

    def test_a_stored_plan_that_does_not_match_its_sha256_is_found(self):
        sha = self.plan_sha("webform:wf-000102")
        self.desk_db().execute("UPDATE plans SET body = replace(body, 'Answer the enquiry', 'Ignore the enquiry') "
                               "WHERE sha = ?", (sha,))
        self.assertIn(f"every stored plan matches its sha256: the plan stored as {sha[:12]} does not match its "
                      "sha256", self.problems())

    def test_a_second_contact_created_by_one_enquiry_is_found(self):
        conn = self.crm_db()
        conn.execute("INSERT INTO contacts (name, origin_key) VALUES ('Copy (invented)', ?)", (self.KEY,))
        conn.commit()
        self.assertIn(f"no two contacts created by the same enquiry: 2 contacts were created by {self.KEY}",
                      self.problems())

    def test_what_it_cannot_find_a_consistent_rewrite_by_a_person_who_can_open_the_file(self):
        # The triggers dropped, the approver's name changed in the trail and on the approval, every later hash
        # recomputed, the triggers put back: nothing on the desk disagrees with anything else.
        conn = self.desk_db()
        for name in audit.TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        prev = audit.GENESIS
        for row_id, at, key, event, detail, actor in conn.execute(
                "SELECT id, at, key, event, detail, actor FROM audit ORDER BY id").fetchall():
            if event == "approved":
                actor = "Someone Else (invented)"
            digest = audit.row_hash(prev, at, key, event, detail, actor)
            conn.execute("UPDATE audit SET actor = ?, prev_hash = ?, row_hash = ? WHERE id = ?",
                         (actor, prev, digest + "~", row_id))
            prev = digest
        conn.execute("UPDATE audit SET row_hash = substr(row_hash, 1, 64)")
        conn.execute("UPDATE approvals SET by = 'Someone Else (invented)'")
        conn.executescript(audit.SCHEMA)
        self.assertEqual(self.problems(), [])
        self.assertRegex(self.cli("trail", self.KEY), r"Someone Else \(invented\) +approved")


if __name__ == "__main__":
    unittest.main()
