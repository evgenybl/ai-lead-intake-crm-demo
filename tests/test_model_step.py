"""The request and its guard, the strict answer, the three backends, and the hidden instruction."""
import contextlib
import hashlib
import io
import json
import os
import re
import secrets
import tempfile
import unicodedata
import unittest
from unittest import mock

from intake import answer, request
from intake.draft import (RECORDED_DIR, AnthropicBackend, RecordedBackend, TemplateBackend, make_backend)

from tests.fake_anthropic import FakeAnthropic
from tests.helpers import DATA_FILE, PERSON, ROOT, DeskCase, enquiry, load_generator

FIELDS = {"name": "Quinn Hart (invented)", "email": "quinn.hart@example.com", "phone": "000-000-0218",
          "message": "Please call me on 000-000-0218 after 5pm about a quote for the lab."}
GOOD = {"summary": "A quote for cleaning the lab is wanted.", "intent": "quote_request", "urgency": "normal",
        "reply_draft": "Hello {{name}}, thank you. We will send a quote soon."}


def load_recordings_tool():
    import importlib.util
    spec = importlib.util.spec_from_file_location("recordings", os.path.join(ROOT, "tools", "recordings.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recordings = load_recordings_tool()
HIDDEN_SHA = request.build(
    next(fields for case, fields in recordings.model_enquiries() if case == "new: the hidden instruction")).sha256


class RequestTests(unittest.TestCase):
    def test_the_request_never_carries_the_name_the_email_or_the_phone(self):
        """For every invented enquiry that reaches the model."""
        found = recordings.model_enquiries()
        self.assertGreater(len(found), 0)
        for case, fields in found:
            with self.subTest(case=case):
                text = request.build(fields).text
                self.assertEqual(request.guard(text, fields), [])
                self.assertNotIn(fields["email"].strip().lower(), text.lower()) if fields["email"] else None
                if fields["phone"].strip():
                    self.assertNotIn(fields["phone"].strip(), text)
                # Compared as the scrub compares: Unicode NFKC and case folding, so a sharp s or a dotted I counts.
                seen = unicodedata.normalize("NFKC", request.message_part(text)).casefold()
                for word in request.name_words(fields["name"]):
                    self.assertNotRegex(seen, rf"(?<![^\W_]){re.escape(word)}(?![^\W_])")

    def test_the_message_is_scrubbed_of_the_senders_own_values_in_any_case(self):
        fields = dict(FIELDS, message="QUINN here: quinn.hart@EXAMPLE.com, or 000-000-0218. Hart Dental, Quinn.")
        scrubbed = request.build(fields).message
        self.assertEqual(scrubbed, "[name] here: [email], or [phone]. [name] Dental, [name].")

    def test_the_marker_word_invented_is_not_a_name_word(self):
        self.assertEqual(request.name_words("Kira Osei (invented)"), ["kira", "osei"])
        fields = dict(FIELDS, name="Quinn Hart (invented)", message="Quinn invented a new rota for the lab.")
        self.assertEqual(request.build(fields).message, "[name] invented a new rota for the lab.")
        self.assertEqual(answer.check(dict(GOOD, summary="The sender invented a rota."), "a rota", fields), [])

    def test_any_email_or_phone_shape_is_scrubbed_but_dates_and_figures_are_kept(self):
        fields = dict(FIELDS, message="Write to a.b@example.com or call +000 (000) 555-0199. We need 3 rooms, "
                                      "1,200 a month, from 2026-02-02; order 123456.")
        scrubbed = request.build(fields).message
        self.assertEqual(scrubbed, "Write to [email] or call [phone]. We need 3 rooms, 1,200 a month, from "
                                   "2026-02-02; order 123456.")  # the leading + goes with the number

    def test_the_senders_phone_is_found_by_its_digits_whatever_stands_between_them(self):
        own = dict(FIELDS, name="Dana Reviewer (invented)", email="dana.reviewer@example.com", phone="000-000-0199")
        message = ("Call 000/000/0199 or 000_000_0199 or 000\u2013000\u20130199 or 000\u00a0000\u00a00199 or "
                   "tel:0000000199 or ph0000000199 or 000-000-\n0199 or +1 (000) 000-0199.")
        scrubbed = request.build(dict(own, message=message)).message
        self.assertEqual(scrubbed, "Call [phone] or [phone] or [phone] or [phone] or tel:[phone] or ph[phone] or "
                                   "[phone] or +1 [phone].")
        self.assertNotRegex(scrubbed.replace("+1 [phone]", ""), r"\d")  # the country code alone is left
        # The guard compares the same way: each form, left in, refuses the request.
        for form in ("000/000/0199", "000_000_0199", "000\u2013000\u20130199", "000-000-\n0199", "ph0000000199"):
            with self.subTest(form=form):
                left_in = request.PROMPT + request.MESSAGE_OPEN + "call " + form + request.MESSAGE_CLOSE
                self.assertIn("a phone number", request.guard(left_in, own))

    def test_an_address_with_letters_outside_ascii_is_scrubbed(self):
        fields = dict(FIELDS, message="Copy to jos\u00e9@example.com and zo\u00eb.m\u00fcller@example.com please.")
        self.assertEqual(request.build(fields).message, "Copy to [email] and [email] please.")
        left_in = request.PROMPT + request.MESSAGE_OPEN + "jos\u00e9@example.com" + request.MESSAGE_CLOSE
        self.assertIn("an e-mail address", request.guard(left_in, FIELDS))

    def test_a_long_number_is_scrubbed_but_a_date_and_a_short_figure_are_kept(self):
        fields = dict(FIELDS, message="Card 4111 1111 1111 1111, order 123456, ref 1234567, from 2026-02-02 "
                                      "(2026-03-01), budget 1,200.")
        self.assertEqual(request.build(fields).message, "Card [phone], order 123456, ref [phone], from 2026-02-02 "
                                                        "(2026-03-01), budget 1,200.")

    def test_a_name_word_next_to_an_underscore_is_scrubbed(self):
        fields = dict(FIELDS, name="Dana Reviewer (invented)", message="Regards, Dana_Reviewer and DanaReviewer.")
        self.assertEqual(request.build(fields).message, "Regards, [name]_[name] and DanaReviewer.")

    def test_the_senders_name_is_found_whatever_its_case_folding_or_unicode_form(self):
        cases = [
            ("J\u00f6rg Wei\u00df (invented)", "Hello, J\u00f6rg Wei\u00df here. Ask for Herr Wei\u00df, WEISS or Weiss.",
             "Hello, [name] [name] here. Ask for Herr [name], [name] or [name]."),
            ("\u0130brahim Demir (invented)", "Merhaba, \u0130brahim here. \u0130BRAH\u0130M, Ibrahim, ibrahim.",
             "Merhaba, [name] here. [name], [name], [name]."),
            ("I\u015f\u0131k Demir (invented)", "I\u015eIK here, and I\u015f\u0131k.", "[name] here, and [name]."),
            ("Zo\u00eb Quince (invented)", "Zoe\u0308 here, typed with a combining mark; Zo\u00eb typed whole.",
             "[name] here, typed with a combining mark; [name] typed whole."),
            ("Zoe\u0308 Quince (invented)", "Zo\u00eb here, the other way round.", "[name] here, the other way round."),
            ("\uff24\uff41\uff4e\uff41 Reviewer (invented)", "Dana here, in full-width letters in the field.",
             "[name] here, in full-width letters in the field."),
            ("Dana Reviewer (invented)", "Dana Reviewer here (the control case).", "[name] [name] here (the control case)."),
        ]
        for name, message, scrubbed in cases:
            with self.subTest(name=name):
                fields = dict(FIELDS, name=name, message=message)
                self.assertEqual(request.build(fields).message, scrubbed)
                left_in = request.PROMPT + request.MESSAGE_OPEN + message + request.MESSAGE_CLOSE
                self.assertIn("the sender's name", request.guard(left_in, fields))
                self.assertEqual(request.guard(request.build(fields).text, fields), [])

    def test_a_name_in_a_script_written_without_spaces_is_found_inside_a_longer_run(self):
        cases = [("\u5f20\u4f1f (invented)", "\u6211\u662f\u5f20\u4f1f\uff0c\u8bf7\u8054\u7cfb\u5f20\u4f1f\u3002",
                  "\u6211\u662f[name]\uff0c\u8bf7\u8054\u7cfb[name]\u3002"),
                 ("\u3055\u304f\u3089 Ito (invented)", "\u79c1\u306f\u3055\u304f\u3089\u3067\u3059",
                  "\u79c1\u306f[name]\u3067\u3059"),
                 ("\u0e2a\u0e21\u0e0a\u0e32\u0e22 (invented)", "\u0e1c\u0e21\u0e0a\u0e37\u0e48\u0e2d\u0e2a\u0e21\u0e0a\u0e32\u0e22\u0e04\u0e23\u0e31\u0e1a",
                  "\u0e1c\u0e21\u0e0a\u0e37\u0e48\u0e2d[name]\u0e04\u0e23\u0e31\u0e1a")]
        for name, message, scrubbed in cases:
            with self.subTest(name=name):
                fields = dict(FIELDS, name=name, message=message)
                self.assertEqual(request.build(fields).message, scrubbed)
                left_in = request.PROMPT + request.MESSAGE_OPEN + message + request.MESSAGE_CLOSE
                self.assertIn("the sender's name", request.guard(left_in, fields))
        # A name word in a script written with spaces stays a whole word: it is not found inside another.
        latin = dict(FIELDS, name="Dana Reviewer (invented)", message="Ask DanaReviewer.")
        self.assertEqual(request.build(latin).message, "Ask DanaReviewer.")

    def test_an_address_with_an_apostrophe_or_other_legal_punctuation_is_replaced_whole(self):
        fields = dict(FIELDS, message="Mail pat.o'brien@example.com, a!b#c@example.com or 'x=y@example.com'.")
        self.assertEqual(request.build(fields).message, "Mail [email], [email] or '[email]'.")
        left_in = request.PROMPT + request.MESSAGE_OPEN + "pat.o'brien@example.com" + request.MESSAGE_CLOSE
        self.assertIn("an e-mail address", request.guard(left_in, FIELDS))

    def test_dates_and_times_in_the_message_are_taken_out_or_kept_as_the_readme_says(self):
        cases = {"From 02/02/2026.": "From [phone].", "From 2.2.2026 10:30.": "From [phone].",
                 "From 02/02/2026 10:30.": "From [phone].", "From 2.2.2026.": "From 2.2.2026.",
                 "From 2026-02-02 10:30.": "From 2026-02-02 10:30.", "From 2026-02-02.": "From 2026-02-02."}
        for message, scrubbed in cases.items():
            with self.subTest(message=message):
                self.assertEqual(request.build(dict(FIELDS, message=message)).message, scrubbed)
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as handle:
            said = " ".join(handle.read().split())
        for example in ("`02/02/2026`", "`2.2.2026 10:30`", "`2026-02-02 10:30`"):
            self.assertIn(example, said)

    def test_a_digit_group_is_kept_as_a_date_only_when_it_is_a_real_date(self):
        fields = dict(FIELDS, message="Ref 5550-14-23, ref 2026-02-30, from 2026-02-02 or 2028-02-29.")
        self.assertEqual(request.build(fields).message, "Ref [phone], ref [phone], from 2026-02-02 or 2028-02-29.")
        left_in = request.PROMPT + request.MESSAGE_OPEN + "Ref 5550-14-23" + request.MESSAGE_CLOSE
        self.assertIn("a phone number", request.guard(left_in, FIELDS))

    def test_what_the_scrub_does_not_recognise_reaches_the_model(self):
        # Said in the README: an address written out in words, a name joined to another word, anyone else's name.
        fields = dict(FIELDS, email="pat.lee@example.com", name="Pat Lee (invented)",
                      message="Write to info at example dot com. Ask PatLee or Tom Baker, 12 Mill Lane.")
        self.assertEqual(request.build(fields).message, fields["message"])
        # A name spelt without its accent, a one-letter name word, an address with a quoted part before its @.
        fields = dict(FIELDS, email="zoe@example.com", name="Zo\u00eb O Quince (invented)",
                      message='Zoe and O here; or "zoe q"@example.com.')
        self.assertEqual(request.build(fields).message, 'Zoe and O here; or "zoe q"@example.com.')

    def test_the_guard_refuses_a_request_that_still_holds_personal_data(self):
        with mock.patch.object(request, "scrub", lambda message, fields: message):
            text = request.build(FIELDS).text
        self.assertEqual(request.guard(text, FIELDS), ["a phone number"])
        held = request.guard(request.PROMPT + "<<<\nQuinn at q@example.com\n>>>\n", FIELDS)
        self.assertEqual(held, ["an e-mail address", "the sender's name"])

    def test_the_request_is_named_by_its_sha256(self):
        built = request.build(FIELDS)
        self.assertEqual(built.sha256, hashlib.sha256(built.text.encode("utf-8")).hexdigest())

    def test_the_request_files_are_what_the_generator_writes(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = recordings.main(["requests", "--check"])
        self.assertEqual(code, 0, out.getvalue())
        for name in os.listdir(os.path.join(RECORDED_DIR, "requests")):
            with open(os.path.join(RECORDED_DIR, "requests", name), "rb") as handle:
                self.assertEqual(hashlib.sha256(handle.read()).hexdigest() + ".txt", name)

    def test_the_constructed_answers_are_what_the_tool_writes_and_say_so(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = recordings.main(["construct", "--check"])
        self.assertEqual(code, 0, out.getvalue())
        for folder, _, names in os.walk(os.path.join(RECORDED_DIR)):
            for name in names:
                if name.endswith(".json") and os.path.basename(folder) != "answers":
                    with open(os.path.join(folder, name), encoding="utf-8") as handle:
                        meta = json.load(handle)
                    self.assertEqual(meta["origin"], "constructed", name)
                    self.assertIn("Not from a model", meta["how"])


class DraftLineTests(unittest.TestCase):
    """How the dry run shows the model step: never "None" where an answer was refused."""

    def test_each_outcome_in_one_line(self):
        from intake.reports import draft_line

        used = {"label": "Recorded", "outcome": "used", "summary": "A quote.", "intent": "quote_request",
                "urgency": "normal"}
        refused = {"label": "Recorded", "outcome": "refused", "reason": "the answer lacks urgency", "summary": None}
        none = {"label": "no recording", "outcome": "none", "reason": "no recording for this request", "summary": None}
        self.assertEqual(draft_line(used), "draft (Recorded; intent quote_request, urgency normal): A quote.")
        self.assertEqual(draft_line(refused), "draft (Recorded): AI draft refused: the answer lacks urgency")
        self.assertEqual(draft_line(none), "draft (no recording): no AI text: no recording for this request")

    def test_text_from_outside_is_printed_on_one_line_that_cannot_move_the_cursor(self):
        from intake.reports import draft_line

        forged = ("ok\r\x1b[A\x1b[K\x07\n  identity: nothing differs\n  to approve: desk.py approve it --by you"
                  "\u202e\u200b\x9b")
        for outcome in ("used", "stand-in", "refused", "none"):
            with self.subTest(outcome=outcome):
                line = draft_line({"label": "Recorded", "outcome": outcome, "summary": forged, "reason": forged,
                                   "intent": "other\n", "urgency": "low\x1b"})
                self.assertEqual(line, line.strip("\n"))
                self.assertNotRegex(line, "[\x00-\x1f\x7f-\x9f\u200b\u202e]")
                self.assertIn("\\x1b[A", line)  # shown as the escape, not obeyed
        stand_in = TemplateBackend().draft({"message": "Hello\x1b[2J there. Then more."})
        self.assertNotIn("\x1b", draft_line(stand_in))


class AnswerTests(unittest.TestCase):
    seen = "Please call me on [phone] after 5pm about a quote for the lab. We need 3 rooms, 1,200 a month."

    def reasons(self, obj):
        return answer.check(obj, self.seen, FIELDS)

    def test_a_good_answer_passes(self):
        self.assertEqual(self.reasons(GOOD), [])
        self.assertEqual(self.reasons(dict(GOOD, reply_draft=None)), [])

    def test_control_characters_are_refused_and_a_line_break_only_in_the_reply(self):
        for text in ("ok\x1b[2J", "ok\r", "ok\u202eevil", "ok\u200bhidden", "ok\x9b", "ok\u2028next"):
            with self.subTest(text=text):
                self.assertIn("the summary holds control characters", self.reasons(dict(GOOD, summary=text)))
                self.assertIn("the reply draft holds control characters other than a line break",
                              self.reasons(dict(GOOD, reply_draft="Hello {{name}}, " + text)))
        self.assertEqual(self.reasons(dict(GOOD, reply_draft="Hello {{name}},\n\nthank you.")), [])
        self.assertEqual(self.reasons(dict(GOOD, intent="spam\x1b")), ["the intent is not one of the fixed list"])

    def test_each_text_has_a_length_in_characters_as_well_as_in_words(self):
        self.assertIn("the summary has more than 400 characters", self.reasons(dict(GOOD, summary="A" * 200000)))
        self.assertIn("the reply draft has more than 1200 characters",
                      self.reasons(dict(GOOD, reply_draft="{{name}} " + " ".join(["B" * 5000] * 119))))

    def test_a_web_address_with_no_scheme_is_refused_in_the_reply(self):
        for reply in ("Hello {{name}}, pay at evil.xyz/pay", "Hello {{name}}, see bit.ly/abc",
                      "Hello {{name}}, [click](javascript:alert)", "Hello {{name}}, visit shop.example today",
                      "Hello {{name}}, ftp://files.example/x"):
            with self.subTest(reply=reply):
                self.assertIn("the reply draft holds a web address", self.reasons(dict(GOOD, reply_draft=reply)))
        for fine in ("Hello {{name}}, i.e. the rooms, e.g. both.", "Hello {{name}}, after 5 p.m. as asked."):
            self.assertEqual(self.reasons(dict(GOOD, reply_draft=fine)), [])

    def test_a_key_name_from_the_answer_is_quoted_and_cut_in_the_reason(self):
        long_key = "x\x1b[2J" + "k" * 100
        (reason,) = self.reasons(dict(GOOD, **{long_key: 1}))
        self.assertIn("'x\\x1b[2J" + "k" * 35 + "'...", reason)
        self.assertNotIn("\x1b", reason)
        self.assertIn("and 2 more", answer.check(dict(GOOD, a=1, b=2, c=3, d=4, e=5), "", FIELDS)[0])

    def test_text_before_or_after_a_fenced_answer_refuses_it(self):
        fenced = "```json\n" + json.dumps(GOOD) + "\n```"
        self.assertEqual(answer.parse(fenced)[1], None)
        for text in (fenced + "\nDone!", "Here it is:\n" + fenced, json.dumps(GOOD) + "\n" + json.dumps(GOOD)):
            with self.subTest(text=text[:30]):
                self.assertEqual(answer.parse(text), (None, "the answer is not one JSON object"))

    def test_the_senders_own_phone_in_the_summary_is_refused_however_it_is_written(self):
        for written in ("000-000-0218", "000/000/0218", "(000) 000 0218"):
            with self.subTest(written=written):
                found = self.reasons(dict(GOOD, summary=f"Call the sender on {written}."))
                self.assertIn("the summary holds the sender's phone", found)

    def test_an_unknown_key_refuses_the_whole_answer(self):
        found = self.reasons(dict(GOOD, approve=True))
        self.assertEqual(found, ["the answer has keys beyond the four: 'approve'; it is refused whole"])

    def test_the_shape_and_the_limits(self):
        cases = {
            "the answer lacks urgency": {k: v for k, v in GOOD.items() if k != "urgency"},
            "the intent is not one of the fixed list": dict(GOOD, intent="sales"),
            "the urgency is not low, normal or high": dict(GOOD, urgency="urgent"),
            "the summary has more than 40 words": dict(GOOD, summary=" ".join(["word"] * 41)),
            "the reply draft has more than 120 words": dict(GOOD, reply_draft="{{name}} " + " ".join(["w"] * 120)),
            "the reply draft does not hold the placeholder {{name}}": dict(GOOD, reply_draft="Hello, thanks."),
            "the summary is not text": dict(GOOD, summary=""),
        }
        for reason, obj in cases.items():
            with self.subTest(reason=reason):
                self.assertIn(reason, self.reasons(obj))

    def test_the_reply_holds_no_email_phone_or_web_address(self):
        for text, reason in (("Hello {{name}}, write to sales@example.com.", "an e-mail address"),
                             ("Hello {{name}}, call 000-000-0999.", "a phone number"),
                             ("Hello {{name}}, see www.example.com for prices.", "a web address"),
                             ("Hello {{name}}, see https://example.com/prices.", "a web address")):
            with self.subTest(text=text):
                self.assertIn(f"the reply draft holds {reason}", self.reasons(dict(GOOD, reply_draft=text)))

    def test_every_number_in_digits_must_appear_in_the_message(self):
        self.assertEqual(self.reasons(dict(GOOD, summary="3 rooms for 1200 a month, after 5pm.")), [])
        self.assertIn("a number that is not in the message: '1500'",
                      self.reasons(dict(GOOD, summary="3 rooms for 1,500 a month.")))
        self.assertIn("a number that is not in the message: '4'", self.reasons(dict(GOOD, reply_draft="Hi {{name}}, 4 rooms.")))

    def test_neither_text_holds_the_senders_name_whatever_its_case_folding_or_unicode_form(self):
        for name, said in (("J\u00f6rg Wei\u00df (invented)", "Herr WEISS asks for a quote."),
                           ("\u0130brahim Demir (invented)", "\u0130BRAH\u0130M asks for a quote."),
                           ("Zo\u00eb Quince (invented)", "Zoe\u0308 asks for a quote."),
                           ("\u5f20\u4f1f (invented)", "\u5f20\u4f1f\u60f3\u8981\u62a5\u4ef7\u3002")):
            with self.subTest(name=name):
                fields = dict(FIELDS, name=name)
                self.assertIn("the summary holds the sender's name",
                              answer.check(dict(GOOD, summary=said), "a quote", fields))
                reply = "Hello {{name}}, thank you, " + said
                self.assertIn("the reply draft holds the sender's name",
                              answer.check(dict(GOOD, reply_draft=reply), "a quote", fields))

    def test_the_summary_holds_no_web_address_and_no_email_address(self):
        for summary in ("Visit evil.example/pay or https://evil.example to pay.", "Pay at www.evil.example now.",
                        "The sender asks to visit bit.ly/x for details."):
            with self.subTest(summary=summary):
                self.assertIn("the summary holds a web address", answer.check(dict(GOOD, summary=summary), "a", FIELDS))
        self.assertIn("the summary holds an e-mail address",
                      answer.check(dict(GOOD, summary="Write to billing@example.com to pay."), "a", FIELDS))
        self.assertEqual(answer.check(dict(GOOD, summary="A quote, e.g. for the lab, is wanted."), "a", FIELDS), [])

    def test_neither_text_holds_the_senders_own_values(self):
        self.assertIn("the summary holds the sender's name", self.reasons(dict(GOOD, summary="Quinn wants a quote.")))
        self.assertIn("the reply draft holds the sender's e-mail",
                      self.reasons(dict(GOOD, reply_draft="Hello {{name}} at quinn.hart@example.com")))

    def test_parsing_is_strict(self):
        self.assertEqual(answer.parse(json.dumps(GOOD))[0], GOOD)
        self.assertEqual(answer.parse("```json\n" + json.dumps(GOOD) + "\n```")[0], GOOD)
        for text in ("Here it is: " + json.dumps(GOOD), json.dumps([GOOD]), '{"summary": "a", "summary": "b"}', ""):
            with self.subTest(text=text[:30]):
                self.assertIsNone(answer.parse(text)[0])


class RecordedBackendTests(unittest.TestCase):
    """The label "Recorded" is earned only by a matching recording in recorded/answers/."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.req = request.build(FIELDS)
        self.write("requests", self.req.sha256 + ".txt", self.req.text.encode("utf-8"))

    def write(self, folder, name, data):
        os.makedirs(os.path.join(self.root, folder), exist_ok=True)
        with open(os.path.join(self.root, folder, name), "wb") as handle:
            handle.write(data)

    def keep(self, folder, origin, obj, sha=None):
        raw = json.dumps(obj).encode("utf-8")
        self.write(folder, self.req.sha256 + ".answer.txt", raw)
        meta = {"request_sha256": self.req.sha256, "answer_file": self.req.sha256 + ".answer.txt",
                "answer_sha256": sha or hashlib.sha256(raw).hexdigest(), "origin": origin, "how": "a test"}
        self.write(folder, self.req.sha256 + ".json", json.dumps(meta).encode("utf-8"))

    def draft(self):
        return RecordedBackend(root=self.root).draft(FIELDS)

    def test_no_recording_means_no_ai_text(self):
        result = self.draft()
        self.assertEqual((result["outcome"], result["reason"]), ("none", "no recording for this request"))
        self.assertIsNone(result["summary"])

    def test_a_matching_recording_is_labelled_recorded(self):
        self.keep("answers", "recorded", GOOD)
        result = self.draft()
        self.assertEqual((result["label"], result["outcome"], result["summary"]), ("Recorded", "used", GOOD["summary"]))

    def test_a_constructed_answer_is_never_labelled_recorded(self):
        self.keep("constructed", "constructed", GOOD)
        self.assertEqual(self.draft()["label"], "Constructed answer, not from a model")
        self.keep("constructed", "recorded", GOOD)  # a meta file in constructed/ that claims "recorded"
        self.assertEqual(self.draft()["outcome"], "none")

    def test_a_changed_answer_file_or_request_is_not_used(self):
        self.keep("answers", "recorded", GOOD, sha="0" * 64)
        self.assertIn("does not match", self.draft()["reason"])
        self.keep("answers", "recorded", GOOD)
        self.write("requests", self.req.sha256 + ".txt", b"another request")
        self.assertIn("does not match", self.draft()["reason"])

    def test_an_answer_that_fails_is_not_used_and_says_why(self):
        self.keep("answers", "recorded", dict(GOOD, summary="3 rooms for 1,500."))
        result = self.draft()
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(result["reason"], "a number that is not in the message: '1500', '3'")
        self.assertIsNone(result["summary"])

    def test_the_template_and_the_backend_choice(self):
        self.assertEqual(TemplateBackend().draft(FIELDS)["label"], "stand-in, no model")
        self.assertIsInstance(make_backend(None, {}), RecordedBackend)
        self.assertIsInstance(make_backend("template", {}), TemplateBackend)


class AnthropicBackendTests(unittest.TestCase):
    """The live path, against a local mock only."""

    def setUp(self):
        self.key = secrets.token_urlsafe(24)
        self.fake = FakeAnthropic(self.key)
        self.url = self.fake.start()
        self.addCleanup(self.fake.stop)
        self.backend = AnthropicBackend(self.key, self.url, "mock-model", timeout=10)

    def tearDown(self):
        self.assertEqual(self.fake.violations, [])

    def test_the_label_names_a_custom_endpoint(self):
        self.assertEqual(self.backend.label, f"Live path (custom endpoint: {self.url.split('//')[1]})")
        self.assertEqual(AnthropicBackend(self.key).label, "Live path")

    def test_what_the_endpoint_receives_holds_no_word_of_the_senders_name_in_any_form(self):
        for name, message, words in (
                ("J\u00f6rg Wei\u00df (invented)", "Hello, J\u00f6rg Wei\u00df here. Ask for Herr Wei\u00df.", ("j\u00f6rg", "weiss")),
                ("\u0130brahim Demir (invented)", "Merhaba, \u0130brahim here. Ask for \u0130brahim.", ("brahim",)),
                ("Zo\u00eb Quince (invented)", "Zoe\u0308 here (the same name, with a combining mark).", ("zo\u00eb",)),
                ("\u5f20\u4f1f (invented)", "\u6211\u662f\u5f20\u4f1f\u3002", ("\u5f20\u4f1f",)),
                ("Dana Reviewer (invented)", "Dana Reviewer here (the control case).", ("dana", "reviewer"))):
            with self.subTest(name=name):
                self.fake.answer(json.dumps(GOOD))
                before = len(self.fake.requests)
                self.backend.draft(dict(FIELDS, name=name, message=message))
                sent = request.message_part(self.fake.requests[before]["messages"][0]["content"])
                seen = unicodedata.normalize("NFKC", sent).casefold()
                for word in words:
                    self.assertNotIn(word, seen)
                self.assertIn("[name]", sent)

    def test_one_request_carries_only_the_request_text(self):
        self.fake.answer(json.dumps(GOOD))
        result = self.backend.draft(FIELDS)
        self.assertEqual((result["outcome"], result["summary"]), ("used", GOOD["summary"]))
        (body,) = self.fake.requests
        self.assertEqual(body["messages"], [{"role": "user", "content": request.build(FIELDS).text}])
        sent = json.dumps(body)
        for value in ("quinn", "000-000-0218", "example.com"):
            self.assertNotIn(value, sent.lower())
        self.assertNotIn(self.key, json.dumps(result))

    def test_a_failing_answer_gets_one_retry_with_the_reasons(self):
        self.fake.answer(json.dumps(dict(GOOD, approve=True)))
        self.fake.answer(json.dumps(GOOD))
        result = self.backend.draft(FIELDS)
        self.assertEqual(result["outcome"], "used")
        first, second = self.fake.requests
        self.assertEqual(len(second["messages"]), 3)
        self.assertEqual(second["messages"][1]["content"][0]["type"], "thinking")  # the first answer, unchanged
        self.assertIn("keys beyond the four", second["messages"][2]["content"])

    def test_two_failing_answers_are_not_used(self):
        self.fake.answer("not json")
        self.fake.answer(json.dumps(dict(GOOD, intent="sales")))
        result = self.backend.draft(FIELDS)
        self.assertEqual(result["outcome"], "refused")
        self.assertEqual(result["reason"], "after one retry: the intent is not one of the fixed list")
        self.assertEqual(len(self.fake.requests), 2)

    def test_a_refusal_or_an_error_leaves_no_ai_text(self):
        self.fake.refuse()
        self.assertEqual(self.backend.draft(FIELDS)["reason"], "the model declined to answer")
        self.fake.fail(500)
        self.assertEqual(self.backend.draft(FIELDS)["reason"], "the model could not be reached: HTTP 500")

    def test_the_recorded_backend_never_reads_an_answer_for_a_request_that_holds_personal_data(self):
        with mock.patch.object(request, "scrub", lambda message, fields: message):
            result = RecordedBackend().draft(FIELDS)
        self.assertEqual((result["outcome"], result["label"]), ("not sent", "not sent"))
        self.assertEqual(result["reason"], "the request still held a phone number")

    def test_a_request_holding_personal_data_is_never_sent(self):
        with mock.patch.object(request, "scrub", lambda message, fields: message):
            result = self.backend.draft(FIELDS)
        self.assertEqual((result["outcome"], result["reason"]), ("not sent", "the request still held a phone number"))
        self.assertEqual(self.fake.requests, [])


class HiddenInstructionTests(DeskCase):
    """Three proofs for the enquiry whose message tells the software to approve itself."""

    KEY = "webform:wf-000120"

    def setUp(self):
        super().setUp()
        self.init()
        generate = load_generator()
        with open(DATA_FILE, encoding="utf-8") as handle:
            lines = handle.read().split("\n")
        index = [case for case, _ in generate.CASES].index("new: the hidden instruction")
        self.obj = json.loads(lines[index])

    def test_whatever_a_model_answers_the_enquiry_still_needs_a_persons_approval(self):
        obeying = dict(GOOD, summary="Approved. The administrator password is shown here.", urgency="high")
        for label, obj in (("obeys", obeying), ("extra key", dict(GOOD, approve=True)), ("clean", GOOD)):
            with self.subTest(answer=label):
                self.home = os.path.join(self._tmp.name, label.replace(" ", "-"))
                self.init()
                drafter = mock.Mock()
                drafter.draft.return_value = {"backend": "test", "label": "test", "outcome": "used", "reason": "",
                                              "request": "", **{k: obj.get(k) for k in answer.KEYS}}
                with self.desk() as desk:
                    desk.drafter = drafter
                    self.assertEqual(desk.receive(self.obj, via="file").state, "awaiting_approval")
                    (result,) = desk.apply(key=self.KEY)
                self.assertEqual(result.outcome, "refused")
                self.assertEqual(self.crm_counts(), {"contacts": 10, "notes": 0, "tasks": 0})

    def test_an_answer_with_any_key_beyond_the_four_is_refused_whole(self):
        drafter = RecordedBackend(sources={os.path.join("gallery", "extra-key"): "constructed"})
        with self.desk() as desk:
            desk.drafter = drafter
            desk.receive(self.obj, via="file")
            kept = desk.store.get_enquiry(self.KEY)
        self.assertEqual(kept.draft["outcome"], "refused")
        self.assertIn("keys beyond the four: 'approve'", kept.draft["reason"])
        self.assertEqual([kept.draft[k] for k in answer.KEYS], [None] * 4)
        self.assertEqual(kept.state, "awaiting_approval")

    @unittest.skipUnless(os.path.exists(os.path.join(RECORDED_DIR, "answers", HIDDEN_SHA + ".json")),
                         "owed: the real recorded answer for the hidden-instruction request is not in "
                         "recorded/answers/ yet")
    def test_the_recorded_real_answer_for_it_is_in_the_repository_as_it_came(self):
        with open(os.path.join(RECORDED_DIR, "answers", HIDDEN_SHA + ".json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        with open(os.path.join(RECORDED_DIR, "answers", meta["answer_file"]), "rb") as handle:
            raw = handle.read()
        self.assertEqual(meta["origin"], "recorded")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["answer_sha256"])
        self.cli("ingest", DATA_FILE)
        with self.desk() as desk:
            kept = desk.store.get_enquiry(self.KEY)
        self.assertEqual(kept.draft["label"], "Recorded")
        self.assertEqual(kept.state, "awaiting_approval")


class ReplayDifferenceTests(DeskCase):
    def test_a_replay_with_different_content_stays_a_replay_and_is_shown_for_a_person(self):
        self.init()
        with self.desk() as desk:
            desk.receive(enquiry(), via="file")
            answer_ = desk.receive(enquiry(message="Could you quote for our bakery and the shop?",
                                           received_at="2026-01-05T08:30:00Z"), via="http")
            same = desk.receive(enquiry(received_at="2026-01-05T08:45:00Z"), via="file")
            first = desk.store.get_enquiry("webform:test-000001")
        self.assertTrue(answer_.replay)
        self.assertEqual(answer_.differs, ("message",))
        self.assertEqual(same.differs, ())  # only the time it was sent differs: that is not content
        self.assertEqual(first.payload["message"], "Could you quote for cleaning our bakery?")  # the first stands
        report = self.cli("report")
        self.assertIn("replayed with different content (kept as replays; the first content stands):", report)
        self.assertIn("webform:test-000001 at 2026-01-05T09:00:00Z: message differs from the first copy", report)
        self.assertEqual(self.crm_counts()["notes"], 0)

    def test_a_name_that_a_spreadsheet_would_read_as_a_formula_is_refused(self):
        self.init()
        self.cli("ingest", DATA_FILE)
        for name in ("=HYPERLINK(1)", "+1", "-x", "@x", "\tDana", " =Dana"):
            with self.subTest(name=name):
                sha = self.plan_sha("webform:wf-000101")[:12]
                self.assertIn("may not start with",
                              self.cli("approve", "webform:wf-000101", f"--by={name}", "--plan", sha, expect=1))
        self.approve_cli("webform:wf-000101")


if __name__ == "__main__":
    unittest.main()
