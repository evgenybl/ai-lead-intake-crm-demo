"""Unit tests: validation (section 3.2), the idempotency key and contact matching (section 3.3)."""
import os
import tempfile
import unittest

from intake import identity, validate
from intake.crm.practice import PracticeCRM

from tests.helpers import enquiry

ARRIVED = "2026-01-05T09:00:00Z"


def reasons_of(obj):
    return validate.reasons(validate.extract(obj, ARRIVED))


class ValidateTests(unittest.TestCase):
    def test_a_good_enquiry_is_taken(self):
        self.assertEqual(reasons_of(enquiry()), [])
        self.assertEqual(reasons_of(enquiry(email="")), [])  # a phone alone is a way to answer
        self.assertEqual(reasons_of(enquiry(phone="")), [])  # an e-mail alone too

    def test_each_refusal_of_section_3_2_with_its_reason(self):
        cases = {
            "no_name": enquiry(name="  "),
            "no_message": enquiry(message="\n"),
            "no_way_to_answer": enquiry(email="", phone=""),
            "email_invalid": enquiry(email="yara.test-at-example.com"),
            "no_consent": enquiry(consent=False),
            "honeypot_filled": enquiry(website="anything"),
            "message_too_long": enquiry(message="x" * 5001),
        }
        for code, obj in cases.items():
            with self.subTest(code=code):
                self.assertEqual(reasons_of(obj), [code])

    def test_the_boundaries(self):
        self.assertEqual(reasons_of(enquiry(message="x" * 5000)), [])
        self.assertEqual(reasons_of(enquiry(message="  " + "x" * 5000 + "  ")), [])  # counted after trimming

    def test_consent_must_be_the_json_value_true(self):
        for value in ("true", 1, None, "yes"):
            with self.subTest(value=value):
                self.assertIn("no_consent", reasons_of(enquiry(consent=value)))
        without = enquiry()
        del without["consent"]
        self.assertIn("no_consent", reasons_of(without))

    def test_every_reason_is_kept_not_only_the_first(self):
        self.assertEqual(reasons_of(enquiry(name="", consent=False, website="x")),
                         ["no_name", "no_consent", "honeypot_filled"])

    def test_a_phone_with_too_few_digits_is_not_a_phone(self):
        self.assertEqual(reasons_of(enquiry(email="", phone="12345")), ["phone_invalid"])
        self.assertEqual(reasons_of(enquiry(email="", phone="call me")), ["no_way_to_answer"])

    def test_a_field_of_the_wrong_type_is_a_reason_not_a_crash(self):
        found = reasons_of(enquiry(name=42, message=["a"]))
        self.assertIn("not_text:name", found)
        self.assertIn("not_text:message", found)
        self.assertIn("honeypot_filled", reasons_of(enquiry(website=True)))

    def test_received_at(self):
        extracted = validate.extract({k: v for k, v in enquiry().items() if k != "received_at"}, ARRIVED)
        self.assertTrue(extracted.received_at_filled)
        self.assertEqual(extracted.fields["received_at"], ARRIVED)
        self.assertIn("received_at_invalid", reasons_of(enquiry(received_at="last Tuesday")))
        self.assertIn("received_at_invalid", reasons_of(enquiry(received_at="2026-02-30T08:00:00Z")))

    def test_times_are_read_the_same_way_on_every_python(self):
        self.assertEqual(validate.parse_time("2026-01-05T23:30:00-02:00").isoformat(), "2026-01-06T01:30:00+00:00")
        self.assertEqual(validate.parse_time("2026-01-05T08:00:00.5Z").microsecond, 500000)
        self.assertEqual(validate.parse_time("2026-01-05T08:00").isoformat(), "2026-01-05T08:00:00+00:00")
        self.assertIsNone(validate.parse_time("2026-01-05T08:00:00+24:00"))

    def test_unknown_fields_are_ignored_and_named(self):
        extracted = validate.extract(enquiry(utm_campaign="x"), ARRIVED)
        self.assertEqual(extracted.ignored, ("utm_campaign",))
        self.assertNotIn("utm_campaign", extracted.fields)


class KeyTests(unittest.TestCase):
    def fields(self, **overrides):
        return validate.extract(enquiry(**overrides), ARRIVED).fields

    def test_a_valid_source_and_source_id_make_the_key(self):
        self.assertEqual(identity.idempotency_key(self.fields(source=" WebForm ", source_id="abc_123")),
                         "webform:abc_123")

    def test_an_invalid_source_id_falls_back_to_the_hash(self):
        for source_id in ("", "short", "x" * 81, "has space", "semi;colon"):
            with self.subTest(source_id=source_id):
                key = identity.idempotency_key(self.fields(source_id=source_id))
                self.assertRegex(key, r"^hash:[0-9a-f]{32}$")

    def test_the_source_hash_cannot_imitate_a_hashed_key(self):
        key = identity.idempotency_key(self.fields(source="hash", source_id="0" * 32))
        self.assertNotEqual(key, "hash:" + "0" * 32)
        self.assertRegex(key, r"^hash:[0-9a-f]{32}$")

    def test_the_hash_reads_normalised_values_and_the_date_only(self):
        base = identity.idempotency_key(self.fields(source_id=""))
        same = identity.idempotency_key(self.fields(
            source_id="", email=" YARA.TEST@example.com", phone="(000) 000-0301",
            message="Could you  quote for cleaning\nour bakery?", received_at="2026-01-05T23:59:00Z",
            name="Someone Else (invented)", company="Other (invented)"))
        self.assertEqual(base, same)
        next_day = identity.idempotency_key(self.fields(source_id="", received_at="2026-01-06T00:00:01Z"))
        other_message = identity.idempotency_key(self.fields(source_id="", message="Another question."))
        self.assertNotEqual(base, next_day)
        self.assertNotEqual(base, other_message)

    def test_the_date_is_the_utc_date(self):
        late_evening_west = identity.idempotency_key(self.fields(source_id="", received_at="2026-01-05T22:00:00-05:00"))
        next_morning_utc = identity.idempotency_key(self.fields(source_id="", received_at="2026-01-06T03:00:00Z"))
        self.assertEqual(late_evening_west, next_morning_utc)


class MatchTests(unittest.TestCase):
    """The table of section 3.3, against a real practice CRM file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.crm = PracticeCRM(os.path.join(tmp.name, "crm.sqlite3"), create=True)
        self.addCleanup(self.crm.close)
        self.crm.load_starting_contacts([
            {"name": "Ada Fenwick (invented)", "email": "ada@example.com", "phone": "000-000-0101", "company": "Fenwick Dental (invented)"},
            {"name": "Bo Lind (invented)", "email": "bo@example.com", "phone": "000-000-0102", "company": "Lind Yoga (invented)"},
            {"name": "Cy Ash (invented)", "email": "", "phone": "000-000-0102", "company": ""},
            {"name": "Di Moss (invented)", "email": "di@example.com", "phone": "", "company": ""},
        ])

    def match(self, **overrides):
        return identity.match_contact(validate.extract(enquiry(**overrides), ARRIVED).fields, self.crm)

    def test_new(self):
        self.assertEqual(self.match().kind, "new")

    def test_returning_by_email_whatever_its_case(self):
        found = self.match(email=" ADA@Example.com", phone="")
        self.assertEqual((found.kind, found.contact.id), ("returning", "1"))

    def test_returning_by_phone_as_digits(self):
        found = self.match(email="", phone="(000) 000 0101")
        self.assertEqual((found.kind, found.contact.id), ("returning", "1"))

    def test_returning_when_email_and_phone_point_to_the_same_contact(self):
        found = self.match(email="ada@example.com", phone="000-000-0101")
        self.assertEqual((found.kind, found.by_email, found.by_phone), ("returning", ("1",), ("1",)))

    def test_held_when_the_email_and_the_phone_belong_to_two_contacts(self):
        found = self.match(email="ada@example.com", phone="000-000-0102")
        self.assertEqual(found.kind, "two_contacts")
        self.assertEqual((found.by_email, found.by_phone), (("1",), ("2", "3")))

    def test_held_when_the_phone_alone_belongs_to_two_contacts(self):
        self.assertEqual(self.match(email="", phone="000-000-0102").kind, "two_contacts")

    def test_possible_duplicate_needs_the_same_name_and_company(self):
        found = self.match(name="ada   fenwick (INVENTED)", company="Fenwick Dental (invented)")
        self.assertEqual((found.kind, found.by_name), ("possible_duplicate", ("1",)))
        self.assertEqual(self.match(name="Ada Fenwick (invented)", company="Another (invented)").kind, "new")

    def test_never_a_similar_name(self):
        self.assertEqual(self.match(name="Ada Fenwik (invented)", company="Fenwick Dental (invented)").kind, "new")

    def test_an_empty_company_never_makes_a_possible_duplicate(self):
        self.assertEqual(self.match(name="Di Moss (invented)", company="").kind, "new")

    def test_an_exact_key_wins_over_a_same_name(self):
        found = self.match(email="bo@example.com", phone="", name="Ada Fenwick (invented)", company="Fenwick Dental (invented)")
        self.assertEqual((found.kind, found.contact.id), ("returning", "2"))


if __name__ == "__main__":
    unittest.main()
