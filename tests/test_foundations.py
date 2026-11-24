"""Unit tests: the clock, canonical JSON, normalisation, the draft stand-in, the allow-list, the state table."""
import unittest

from intake import normalise, ops, states
from intake.canonical import canonical_bytes, canonical_json, sha256_hex
from intake.clock import Clock, ClockError, make_clock
from intake.draft import TemplateBackend, first_sentence


class ClockTests(unittest.TestCase):
    def test_a_fixed_clock_returns_its_time(self):
        self.assertEqual(Clock("2026-01-05T09:00:00Z").now(), "2026-01-05T09:00:00Z")

    def test_at_comes_before_the_environment(self):
        clock = make_clock("2026-01-05T09:00:00Z", {"INTAKE_NOW": "2027-01-01T00:00:00Z"})
        self.assertEqual(clock.now(), "2026-01-05T09:00:00Z")
        self.assertEqual(make_clock(None, {"INTAKE_NOW": "2027-01-01T00:00:00Z"}).now(), "2027-01-01T00:00:00Z")

    def test_without_either_the_system_clock_is_used(self):
        clock = make_clock(None, {})
        self.assertFalse(clock.is_fixed)
        self.assertRegex(clock.now(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_a_malformed_or_impossible_time_is_refused(self):
        for bad in ("2026-01-05 09:00:00", "2026-01-05T09:00:00+00:00", "2026-02-30T09:00:00Z", "yesterday"):
            with self.subTest(bad=bad), self.assertRaises(ClockError):
                Clock(bad)


class CanonicalTests(unittest.TestCase):
    def test_sorted_keys_fixed_separators_utf8(self):
        self.assertEqual(canonical_json({"b": 1, "a": "Zoë"}), '{"a":"Zoë","b":1}')
        self.assertEqual(canonical_bytes({"a": "\u00e5"}), b'{"a":"\xc3\xa5"}')

    def test_the_same_value_gives_the_same_sha_whatever_the_key_order(self):
        self.assertEqual(sha256_hex({"a": 1, "b": [1, 2]}), sha256_hex({"b": [1, 2], "a": 1}))
        self.assertNotEqual(sha256_hex({"a": 1}), sha256_hex({"a": 2}))

    def test_nan_and_lone_surrogates_are_refused(self):
        with self.assertRaises(ValueError):
            canonical_json({"a": float("nan")})
        with self.assertRaises(UnicodeEncodeError):
            canonical_bytes({"a": "\ud800"})


class NormaliseTests(unittest.TestCase):
    def test_email_is_trimmed_and_lowercased(self):
        self.assertEqual(normalise.email("  Ada.Fenwick@Example.COM "), "ada.fenwick@example.com")
        self.assertTrue(normalise.is_email("a.b@example.com"))
        for bad in ("a.b-at-example.com", "a b@example.com", "a@b", "@example.com", "a@@example.com"):
            with self.subTest(bad=bad):
                self.assertFalse(normalise.is_email(normalise.email(bad)))

    def test_phone_is_its_ascii_digits(self):
        self.assertEqual(normalise.phone_digits("(000) 000-0106"), "0000000106")
        self.assertEqual(normalise.phone_digits("+000 000 0106 ext"), "0000000106")
        self.assertEqual(normalise.phone_digits("\u0661\u0662\u0663"), "")  # digits of another script are not read
        self.assertTrue(normalise.is_phone("0000000"))
        self.assertFalse(normalise.is_phone("000000"))
        self.assertFalse(normalise.is_phone("0" * 16))

    def test_name_ignores_case_spacing_and_punctuation_not_letters(self):
        self.assertEqual(normalise.name("  Ada   O'Neil-Fenwick "), "ada o neil fenwick")
        self.assertEqual(normalise.name("ADA O NEIL FENWICK"), normalise.name("Ada O'Neil-Fenwick"))
        self.assertEqual(normalise.name("Zoe\u0308"), normalise.name("Zoë"))  # one letter, two encodings
        self.assertNotEqual(normalise.name("Zoe"), normalise.name("Zoë"))

    def test_message_whitespace_is_collapsed(self):
        self.assertEqual(normalise.message("  a \n\t b  "), "a b")


class DraftTests(unittest.TestCase):
    def test_the_stand_in_says_it_is_not_a_model(self):
        draft = TemplateBackend().draft({"message": "Hello there. A second sentence."})
        self.assertEqual(draft["label"], "stand-in, no model")
        self.assertEqual(draft["outcome"], "stand-in")
        self.assertEqual(draft["summary"], "Hello there.")
        self.assertEqual(draft["intent"], "other")
        self.assertIsNone(draft["reply_draft"])

    def test_the_summary_is_at_most_forty_words(self):
        summary = first_sentence(" ".join(["word"] * 60))
        self.assertEqual(len(summary.split()), 41)  # forty words and the "..." that marks the cut
        self.assertTrue(summary.endswith(" ..."))

    def test_a_decimal_point_does_not_end_the_sentence(self):
        self.assertEqual(first_sentence("It costs 1.5 a day. More."), "It costs 1.5 a day.")


class AllowListTests(unittest.TestCase):
    def test_exactly_four_operations(self):
        self.assertEqual(ops.ALLOWED_OPS, ("create_contact", "fill_empty_fields", "add_note", "add_task"))

    def test_outside_and_repeated(self):
        plan = {"operations": [{"op": "add_note"}, {"op": "merge_contacts"}, {"op": "add_note"}]}
        self.assertEqual(ops.operations_outside(plan), ["merge_contacts"])
        self.assertEqual(ops.repeated_operations(plan), ["add_note"])


class StateTableTests(unittest.TestCase):
    def test_every_target_is_a_named_state(self):
        for (current, event), targets in states.TRANSITIONS.items():
            self.assertTrue(current is None or current in states.STATES, current)
            self.assertTrue(targets <= set(states.STATES), (event, targets))

    def test_no_move_out_of_a_final_state(self):
        for current, _ in states.TRANSITIONS:
            self.assertNotIn(current, states.FINAL)

    def test_an_unlisted_move_is_refused(self):
        states.check("approved", "applied", "applied")
        with self.assertRaises(states.TransitionError):
            states.check("awaiting_approval", "applied", "applied")  # never applied without an approval
        with self.assertRaises(states.TransitionError):
            states.check("refused", "assessed", "awaiting_approval")
        with self.assertRaises(states.TransitionError):
            states.check("received", "assessed", "applied")

    def test_every_state_can_be_reached(self):
        reached = {t for targets in states.TRANSITIONS.values() for t in targets}
        self.assertEqual(reached, set(states.STATES))


if __name__ == "__main__":
    unittest.main()
