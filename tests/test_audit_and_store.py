"""Integration tests on a real desk file: the append-only trail (section 3.6) and the store's rules."""
import sqlite3
import unittest

from intake import audit

from tests.helpers import PERSON, DeskCase, enquiry


class AuditTrailTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(), via="file")

    def test_the_database_refuses_to_change_an_audit_row(self):
        conn = self.desk_db()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
            conn.execute("UPDATE audit SET actor = 'someone' WHERE id = 1")

    def test_the_database_refuses_to_remove_an_audit_row(self):
        conn = self.desk_db()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
            conn.execute("DELETE FROM audit WHERE id = 1")

    def test_the_database_refuses_a_row_that_does_not_follow_the_last(self):
        conn = self.desk_db()
        with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
            conn.execute("INSERT INTO audit (at, key, event, detail, actor, prev_hash, row_hash) "
                         "VALUES ('t', 'k', 'e', '{}', 'a', 'not-the-last-hash', 'x')")

    def test_insert_or_replace_cannot_overwrite_a_row(self):
        conn = self.desk_db()
        last = conn.execute("SELECT row_hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()[0]
        with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
            conn.execute("INSERT OR REPLACE INTO audit (id, at, key, event, detail, actor, prev_hash, row_hash) "
                         "VALUES (1, 't', 'k', 'e', '{}', 'a', ?, 'x')", (last,))

    def test_the_chain_adds_up_and_the_triggers_are_there(self):
        conn = self.desk_db()
        self.assertEqual(audit.chain_problems(conn), [])
        self.assertEqual(audit.missing_triggers(conn), [])

    def test_with_the_triggers_gone_a_changed_row_breaks_the_chain(self):
        conn = self.desk_db()
        for name in audit.TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("UPDATE audit SET actor = 'someone else' WHERE id = 1")
        self.assertIn("row 1 was changed after it was written", audit.chain_problems(conn))

    def test_with_the_triggers_gone_a_removed_row_breaks_the_chain(self):
        conn = self.desk_db()
        for name in audit.TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute("DELETE FROM audit WHERE id = 1")
        self.assertTrue(any("does not follow" in p for p in audit.chain_problems(conn)))

    def test_each_row_names_time_key_event_detail_and_actor(self):
        with self.desk() as desk:
            desk.approve("webform:test-000001", PERSON)
            rows = desk.store.trail("webform:test-000001")
        events = [(r[3], r[5]) for r in rows]
        self.assertEqual(events, [("arrived", "system"), ("draft", "system"), ("assessed", "system"),
                                  ("approved", PERSON)])
        self.assertTrue(all(r[1] == "2026-01-05T09:00:00Z" for r in rows))


class StoreRuleTests(DeskCase):
    def setUp(self):
        super().setUp()
        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(), via="file")

    def test_one_live_approval_per_enquiry(self):
        conn = self.desk_db()
        sha = conn.execute("SELECT plan_sha FROM enquiries").fetchone()[0]
        conn.execute("INSERT INTO approvals (key, plan_sha, by, at, state) VALUES ('webform:test-000001', ?, 'a', 't', 'approved')", (sha,))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO approvals (key, plan_sha, by, at, state) VALUES ('webform:test-000001', ?, 'b', 't', 'approved')", (sha,))

    def test_a_rejection_needs_a_reason_in_the_database_too(self):
        conn = self.desk_db()
        sha = conn.execute("SELECT plan_sha FROM enquiries").fetchone()[0]
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO approvals (key, plan_sha, by, at, state, reason) VALUES ('webform:test-000001', ?, 'a', 't', 'rejected', '  ')", (sha,))

    def test_a_state_outside_the_named_states_is_refused(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.desk_db().execute("UPDATE enquiries SET state = 'lost'")

    def test_one_first_arrival_per_enquiry(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.desk_db().execute("INSERT INTO arrivals (key, at, via, kind) VALUES ('webform:test-000001', 't', 'file', 'first')")

    def test_a_desk_is_never_created_over_another(self):
        out = self.cli("init", expect=2)
        self.assertIn("already there; nothing was changed", out)
        self.assertEqual(self.desk_db().execute("SELECT count(*) FROM enquiries").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
