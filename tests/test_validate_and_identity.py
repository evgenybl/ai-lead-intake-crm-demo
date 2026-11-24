"""Unit tests: validation, the idempotency key and contact matching."""
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

    def test_each_refusal_with_its_reason(self):
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

    def test_a_name_or_company_a_spreadsheet_would_read_as_a_formula_is_refused(self):
        for start in ("=HYPERLINK(1)", "+SUM(A1)", "-x", "@cmd", "\tTab", "\rReturn", " =spaced"):
            for field in ("name", "company"):
                with self.subTest(field=field, start=start):
                    found = reasons_of(enquiry(**{field: start + " (invented)"}))
                    self.assertEqual(found[0], f"formula_start:{field}")  # a tab or a return is a control character too
                    self.assertTrue(set(found) <= {f"formula_start:{field}", f"control_characters:{field}"}, found)
        self.assertEqual(reasons_of(enquiry(name="Ada-Marie Fenwick (invented)", company="A+B Cleaning (invented)")), [])
        self.assertIn("starts like a spreadsheet formula", validate.describe("formula_start:name"))

    def test_a_name_or_company_with_control_characters_is_refused(self):
        for value in ("A\x00B", "Esc\x1b[2J", "Bidi \u202eevil", "Zero\u200bwidth", "C1 \x9b"):
            with self.subTest(value=value):
                self.assertEqual(reasons_of(enquiry(name=value)), ["control_characters:name"])
                self.assertEqual(reasons_of(enquiry(company=value)), ["control_characters:company"])
        self.assertEqual(reasons_of(enquiry(name="Zoë Ångström (invented)")), [])

    def test_an_email_or_a_phone_a_spreadsheet_would_read_as_a_formula_is_refused(self):
        cases = [("phone", "=1+1+cmd|' /C calc'!A0 5550142"), ("email", "=1+1@example.com"), ("phone", "+x 000-000-0101"),
                 ("phone", "+ 44 000 000 0101"), ("phone", "-000-000-0101"), ("phone", "@000 000 0101"),
                 ("phone", " =000 000 0101"), ("email", "+tag@example.com"), ("email", "-dash@example.com"),
                 ("email", " =spaced@example.com"), ("phone", "+1+1+cmd|' /C calc'!A0 5550142"),
                 ("phone", '+0000000101=HYPERLINK("x")'), ("phone", "+1 000 000 0101 ext 12"),
                 ("phone", " +1 000 000 0101;x")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.assertIn(f"formula_start:{field}", reasons_of(enquiry(**{field: value})))
        for phone in ("+1 000-000-0101", "+44 (0)20 0000 0101", "(000) 000-0106", "000.000.0106", " +1 000 000 0101",
                      "+1/000/000/0101", "000 000 0101 ext 12"):
            with self.subTest(phone=phone):
                self.assertEqual(reasons_of(enquiry(phone=phone)), [])
        self.assertIn("a phone may start with + and a digit, and then holds only digits, spaces and ( ) - . /",
                      validate.describe("formula_start:phone"))

    def test_an_email_or_a_phone_with_control_characters_is_refused(self):
        self.assertEqual(reasons_of(enquiry(email="esc\x1b[31m@example.com")), ["control_characters:email"])
        self.assertEqual(reasons_of(enquiry(phone="000-000\x1b[2J-0101")), ["control_characters:phone"])
        self.assertEqual(reasons_of(enquiry(phone="000\u200b000\u200b0101")), ["control_characters:phone"])
        self.assertEqual(reasons_of(enquiry(email="", phone="call me")), ["no_way_to_answer"])  # free text, no digits

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
    """The matching table of intake/identity.py, against a real practice CRM file."""

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
        contact = self.crm.create_contact({"name": "Ed Roe (invented)", "phone": "000-000-0109"}, "")
        found = self.match(email="", phone="(000) 000 0109")
        self.assertEqual((found.kind, found.contact.id), ("returning", contact.id))

    def test_returning_when_email_and_phone_point_to_the_same_contact(self):
        found = self.match(email="ada@example.com", phone="000-000-0101")
        self.assertEqual((found.kind, found.by_email, found.by_phone), ("returning", ("1",), ("1",)))

    def test_held_when_the_email_and_the_phone_belong_to_two_contacts(self):
        found = self.match(email="ada@example.com", phone="000-000-0102")
        self.assertEqual(found.kind, "two_contacts")
        self.assertEqual((found.by_email, found.by_phone), (("1",), ("2", "3")))

    def test_held_when_only_the_phone_matches_and_the_emails_do_not_agree(self):
        # A colleague on a shared office line: a new name and e-mail, the contact's phone.
        found = self.match(email="tom.newman@example.com", phone="000-000-0101", name="Tom Newman (invented)")
        self.assertEqual((found.kind, found.contact, found.by_phone), ("phone_only", None, ("1",)))
        # No e-mail given, and the contact has one: the phone alone cannot say it is the same person.
        self.assertEqual(self.match(email="", phone="000-000-0101").kind, "phone_only")
        # An e-mail given, and the contact has none: the plan would fill another person's e-mail in.
        self.assertEqual(self.match(email="cy.new@example.com", phone="000-000-0109").kind, "new")
        self.crm.create_contact({"name": "Ed Roe (invented)", "phone": "000-000-0109"}, "")
        self.assertEqual(self.match(email="cy.new@example.com", phone="000-000-0109").kind, "phone_only")
        # Neither has an e-mail: the phone is all there is, and it matches.
        self.assertEqual(self.match(email="", phone="000-000-0109").kind, "returning")

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
