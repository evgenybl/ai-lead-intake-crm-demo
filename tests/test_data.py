"""The invented data of section 4: the files equal the generator's output, and each case does what it is for."""
import json
import os
import re
import unittest

from intake.home import desk_path

from tests.helpers import ROOT, DeskCase, load_generator

generate = load_generator()


def lines(name):
    with open(os.path.join(ROOT, "data", name), "rb") as handle:
        return handle.read()


class GeneratorTests(unittest.TestCase):
    def test_the_files_equal_the_generators_output(self):
        for name, text in generate.render().items():
            with self.subTest(file=name):
                self.assertEqual(lines(name), text.encode("utf-8"))

    def test_the_generator_gives_the_same_output_every_time(self):
        self.assertEqual(generate.render(), generate.render())

    def test_the_data_holds_every_case_section_4_asks_for(self):
        cases = [case for case, _ in generate.CASES]

        def count(prefix):
            return sum(1 for case in cases if case.startswith(prefix))

        intents = ("quote_request", "support", "booking", "partnership", "job_application", "spam", "other")
        for intent in intents:
            self.assertEqual(count(f"new: {intent}"), 1, intent)
        self.assertEqual(count("replay:"), 3)
        self.assertEqual(count("returning:"), 3)
        self.assertEqual(count("returning: by e-mail, a changed phone"), 1)
        self.assertEqual(count("held: the e-mail and the phone"), 1)
        self.assertEqual(count("held: a new address with the name and company"), 1)
        self.assertEqual(count("held: only the phone matches"), 1)
        self.assertEqual(count("refused: consent"), 2)
        self.assertEqual(count("refused: the honeypot"), 1)
        self.assertEqual(count("refused: no way to answer"), 1)
        self.assertEqual(count("new: the message repeats the sender's own phone"), 1)
        self.assertEqual(count("new: figures in the message"), 1)
        self.assertEqual(count("new: the hidden instruction"), 1)
        self.assertEqual(len(generate.STARTING_CONTACTS), 10)
        self.assertTrue(25 <= len(cases) <= 35)

    def test_everyone_is_invented_and_reachable_only_at_placeholders(self):
        records = [json.loads(line) for name in (generate.ENQUIRIES_FILE, generate.CONTACTS_FILE)
                   for line in lines(name).decode("utf-8").splitlines()]
        for record in records:
            for field in ("name", "company"):
                value = record.get(field, "")
                self.assertTrue(value == "" or value.endswith("(invented)"), value)
            email = record.get("email", "").strip()
            if "@" in email:
                self.assertTrue(email.lower().endswith("@example.com"), email)
            phone = record.get("phone", "")
            self.assertTrue(phone == "" or re.fullmatch(r"(\(000\) |000-)000-0\d{3}", phone), phone)


class CasesReachTheirStatesTests(DeskCase):
    def test_each_case_reaches_the_state_it_is_for(self):
        self.init()
        with self.desk() as desk:
            for (case, expected), enquiry in zip(generate.CASES, [e for _, _, e in generate.build()]):
                with self.subTest(case=case):
                    answer = desk.receive(enquiry, via="file")
                    self.assertEqual("replay" if answer.replay else answer.state, expected)
        self.assertTrue(os.path.exists(desk_path(self.home)))

    def test_the_hidden_instruction_still_waits_for_a_person(self):
        self.init()
        self.cli("ingest", os.path.join(ROOT, "data", generate.ENQUIRIES_FILE))
        self.cli("apply", "--key", "webform:wf-000120", expect=1)
        with self.desk() as desk:
            self.assertEqual(desk.store.get_enquiry("webform:wf-000120").state, "awaiting_approval")
        self.assertEqual(self.crm_counts()["notes"], 0)


if __name__ == "__main__":
    unittest.main()
