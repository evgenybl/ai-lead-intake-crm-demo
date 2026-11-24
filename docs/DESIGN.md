# Design decisions

This page records each decision behind the desk, and why. Where this page and the code disagree, the code and its
tests are right and this page is wrong; please report it.

## Where things are

| Module | What it does |
|---|---|
| `intake/arrive.py` | Reads one JSON object from an HTTP body or a line of a `.jsonl` file |
| `intake/validate.py` | The refusals, each with its reason |
| `intake/normalise.py` | What "normalised" means for an e-mail, a phone, a name and a message |
| `intake/identity.py` | The idempotency key, and matching a contact by exact keys |
| `intake/planner.py` | The plan: the exact operations an enquiry would cause, and its sha256 |
| `intake/ops.py` | The allow-list of four operations |
| `intake/gate.py` | The one write gate: every CRM write passes through it |
| `intake/flow.py` | Arrival, assessment, and a person's decisions (resolve, approve, reject, withdraw) |
| `intake/states.py` | The named states and the only moves between them |
| `intake/store.py` | The desk's SQLite file, and the apply lock |
| `intake/audit.py` | The audit trail: its triggers and its hash chain |
| `intake/doctor.py` | The invariants of the four promises |
| `intake/crm/` | The adapter interface, the practice CRM (SQLite) and the HTTP adapter |
| `intake/server.py` | `POST /intake`, and the desk pages under `serve` |
| `intake/pages.py` | The desk pages, rendered on the server with no script |
| `intake/clock.py` | The one clock |
| `intake/request.py` | The request to a model: the fixed prompt, the scrubbed message, and the guard |
| `intake/answer.py` | Reading a model's answer strictly, and its checks |
| `intake/draft.py` | The three backends of the model step: `recorded`, `template`, `anthropic` |
| `intake/reports.py` | The text of `report`, `trail`, `plan` and `doctor`, printed so that outside text cannot forge a line |
| `intake/cli.py`, `desk.py` | The command line |
| `demo.py`, `make_examples.py` | The demo of the whole story and its gallery of cases, and the examples made from its run |
| `tools/recordings.py` | Writes the requests and the constructed answers; keeps a real answer as it came |
| `tools/term_svg.py`, `tools/list_tests.py` | The demo's picture; `docs/TESTS.md` |

## The states

Every enquiry is in exactly one of these. A replay is not an enquiry: it is an arrival of a known key,
counted and recorded, and it changes no state.

| State | Meaning |
|---|---|
| `received` | Taken; identity not checked yet, because the CRM could not be read |
| `refused` | Failed validation; kept with its reasons; never reaches the model or the CRM, until a copy that passes arrives under the same key |
| `held_two_contacts` | The e-mail and the phone point to different contacts; waits for a person |
| `held_possible_duplicate` | A contact might be the same person: only the phone matches and the e-mails do not agree, or no key matches but the name and company do; waits for a person |
| `awaiting_approval` | A plan is ready and waits for a person's approval |
| `approved` | A person approved exactly this plan; not applied yet |
| `rejected` | A person rejected the plan, with a reason; nothing more is written for it. If an apply of it began and did not finish, what that apply wrote stays in the CRM, and the page and the command name it |
| `applied` | Every operation of the approved plan is in the CRM |
| `apply_failed` | A CRM call failed; the error is named; a retry completes what is missing |

The moves (`intake/states.py`, `TRANSITIONS`). Any other move raises an error, and every move that
happens is an audit row that records the state it comes from and the state it ends in.

| From | Event | To | Who |
|---|---|---|---|
| (not on the desk) | `arrived` | `refused` or `received` | system |
| `refused` | `resent` | `received` | system (a copy that passes, under the same key) |
| `received` | `assessed` | `awaiting_approval`, or a held state | system |
| a held state | `resolved` | `awaiting_approval`, or a held state if the resolution no longer applies | a person |
| `awaiting_approval` | `replanned` | `awaiting_approval`, or a held state | system (`desk.py plan`, when the CRM changed) |
| `awaiting_approval` | `resolved` | `awaiting_approval`, or a held state | a person (`resolve --as new`, for a match by the phone alone) |
| `awaiting_approval` | `approved` | `approved` | a person |
| `awaiting_approval` | `rejected` | `rejected` | a person, with a reason |
| `approved` | `withdrawn` | `awaiting_approval` | a person |
| `approved` | `replanned` | `awaiting_approval`, or a held state | system (the gate: the CRM changed since the approval) |
| `approved` | `applied` / `apply_failed` | `applied` / `apply_failed` | system (the gate) |
| `apply_failed` | `applied` / `apply_failed` | `applied` / `apply_failed` | system (a retry) |
| `apply_failed` | `replanned` | `awaiting_approval`, or a held state | system (a retry where no step had started, and the CRM changed) |
| `apply_failed` | `withdrawn` | `awaiting_approval` | a person |

`rejected` and `applied` are final. `refused` is final unless a corrected copy arrives (see "A corrected copy").

## What is refused

No name; no message; neither e-mail nor phone; an e-mail that is not an address; a phone that is not a phone
number (7 to 15 digits); consent that is not the JSON value `true`; the honeypot field filled; a message above
5,000 characters after trimming; a `received_at` that is not a date and time; a text field sent as another JSON
type; a name, company, e-mail or phone that starts like a spreadsheet formula (`=`, `+`, `-`, `@`, a tab or a
carriage return, as sent or after trimming; a phone may start with a `+` that a digit follows, as a country code
does); a name, company, e-mail or phone that holds a control character, or a character that reorders or hides
text (a bidi control, a zero-width space). These four are the fields the desk writes to the CRM. A phone may
otherwise hold any character: only its ASCII digits are compared, and it must have 7 to 15 of them, or none (a
phone with no digit, such as "call me", counts as no phone). Every reason that applies is kept, in a fixed
order.

## What "normalised" means

- **E-mail:** trimmed and lowercased, the whole address. It is an address when it has exactly one `@`, no
  whitespace, a dot after the `@`, and at most 254 characters.
- **Phone:** its ASCII digits only. `(000) 000-0106` and `000-000-0106` are the same phone. It is a phone
  number when it has 7 to 15 digits. Nothing is known about country codes: `+1 000...` and `000...` differ.
- **Name and company:** Unicode NFKC, case-folded, every run of characters that are not letters or digits
  made one space, trimmed. `Ada  O'Neil` equals `ada o neil`; `Zoe` does not equal `Zoë`.
- **Message** (for the hashed key only): every run of whitespace made one space, trimmed. Letter case is kept.

## The idempotency key

- `<source>:<source_id>` when the source (lowercased) is 1 to 40 characters of lowercase letters, digits,
  `_` or `-` starting with a letter, and is not `hash`; and the source_id is 6 to 80 characters of
  letters, digits, `_` or `-`. A source of `hash` is refused so that no source can imitate a hashed key.
- Otherwise `hash:` and the first 32 hex digits of the sha256 of the canonical JSON of
  `{"date", "email", "message", "phone"}`, normalised. The date is the UTC date of `received_at`; an
  enquiry with no `received_at` gets its arrival time, from the desk's clock. A message that differs only in
  letter case, by a full stop, or by another Unicode form of a letter makes another key.
- The source_id is case-sensitive: a form system's ids are its own.

## What a replay returns, and what it changes

A replay is answered with the original's key, its state now, and when and how the original first
arrived (HTTP: status 200 with `"replay": true` and `"original"`; a new enquiry is 201). The arrival is
counted (`arrivals` table, kind `replay`) and recorded in the trail, with whether its content equals the
original's. Nothing else changes: no new plan, no new approval, no CRM write.

**An approved plan that is replayed** stays approved, with the same approval. The replay does not
withdraw it, renew it or apply it. A replay with different content under the same key is still a replay;
the trail notes `same_content=false` and the original content is kept.

## A corrected copy

A refused enquiry never holds its key against a later copy that passes. When a copy that passes validation
arrives under the key of an enquiry in `refused`, it becomes the enquiry: its content replaces the refused
content, the enquiry moves `refused` → `received` (event `resent`, with the earlier reasons and the names of the
fields that changed), and it is assessed as any new enquiry. The arrival is counted as a `resend`; `report` and
`doctor` add the arrivals up as enquiries + replays + corrected resends. A copy that does not pass, under any
known key, is a replay; so is a bad copy of a good enquiry, which changes nothing. HTTP answers a corrected
copy 201 with `"earlier_copy_refused": true`.

## Identity

The contacts found by the e-mail and by the phone form one set.

- One contact in the set: **returning**, with one exception. When the contact was found by the phone alone, it
  is returning only if neither the enquiry nor the contact has an e-mail. If the enquiry gives an e-mail the
  contact does not have, or the contact has an e-mail the enquiry does not give, the enquiry is **held: phone
  only** (state `held_possible_duplicate`): a colleague on a shared office line must not be attached to someone
  else's contact, and the plan must not write a new person's e-mail into it. Found by the phone alone with no
  e-mail on either side, it is returning and waits for approval; as the phone may still be shared, a person may
  resolve it as new instead (see "How a hold is resolved").
- Two or more: **held: two contacts**. This includes a phone that alone belongs to two contacts (phones
  are not unique in a CRM).
- None: a contact with the same normalised name **and** the same normalised company is a **possible
  duplicate**. Both must be non-empty: two people with one name and no company are not evidence of
  anything. Otherwise **new**.

## How a hold is resolved

`desk.py resolve <key> --as new --by <name>` or `--as contact:<id> --by <name>`.

- `contact:<id>` must be one of the contacts identity found for that enquiry (they are listed by `plan`).
  This stops a mistyped id from attaching an enquiry to an unrelated contact.
- `new` is refused while the enquiry's e-mail belongs to a contact: a second contact with that e-mail
  would break promise 2. For a two-contacts hold that is found by its e-mail, a person therefore chooses
  between the contacts found. A possible duplicate, and a phone-only hold, can be resolved either way.
- An enquiry waiting for approval whose contact was found by the phone alone (no e-mail on either side, no
  person's resolution yet) can be resolved as `new`, and only as new: it is planned again as a new contact, and
  waits for approval of that plan. `plan` and the page show the command.
- The resolution is an audit row with the person's name, and the enquiry gets its plan at once.
- A resolution applies only while it still makes sense: `new` stops applying if the e-mail later
  belongs to a contact; `contact:<id>` stops applying if the contact is gone. The plan then follows the
  ordinary rules and says why in its identity notes. The system never merges contacts.

## The plan and its sha256

A plan is `{"enquiry", "identity", "operations", "shown"}`: what identity found, each operation with its
values, and the differences shown to a person and not written. Its sha256 is over canonical JSON (sorted
keys, `,` and `:` separators, UTF-8, no NaN).

A plan is also computed from the desk's ledger: what an apply of an earlier plan of the same enquiry wrote
before it stopped. A note or a task the ledger records on another contact than the new plan would use is not
planned again (a second one would break promise 2); a contact created, or fields filled, that the new plan does
not stand for, are named too. Each goes under a fifth key, `"already_written"`, which `plan` and the page show as
"already in the CRM, not written again". A plan with nothing already written has no such key, so its sha256 is
what it always was.

The plan is computed from the CRM as it is now, so its sha256 changes when the CRM changes in a way that
matters: the contact now exists, a field was filled meanwhile, a contact's differing value changed. A
note or task on the contact does not change it. The note and the task begin with the enquiry's key.

**The draft is never in the plan.** The stand-in's summary is shown on the desk only; the CRM path does
not depend on it.

`desk.py plan` is the dry run: it writes nothing to the CRM. It does write to the desk: it assesses any
enquiry still `received` and re-plans any `awaiting_approval` enquiry whose plan the CRM has changed, each
with its audit row, so that what it prints is the plan as it stands now. It never changes an approval; an
approved plan the CRM has changed is printed with a warning, and the gate deals with it. `--key` prints one
enquiry; `--full` prints each note and task exactly as it would be written, instead of its first line.

## Approvals

An approval is a row: key, plan sha256, who, when, state (`approved`, `used`, `withdrawn`, `rejected`)
and a reason (required for `rejected`, by the database too). At most one approval per enquiry is
`approved` at a time (a unique index).

**An approval must name its plan**: `approve <key> --by <name> --plan <sha256, or its first 8 or more hex
digits>`, and the desk page's form carries the plan's sha256. There is no approval of "whatever plan waits
now": the desk may have planned again since the person read the plan (the CRM changed), and an approval that
names the old plan is refused, so a plan is never approved unseen.

When the plan changes under a live approval, the system withdraws that approval (`closed_by` is
`system`, with the reason) rather than leave an approval for a plan that no longer exists. A person's
withdrawal and the system's are told apart by `closed_by`.

## The write gate

`intake/gate.py`, `WriteGate.apply`. Before anything leaves, in this order:

1. the stored plan matches its sha256;
2. every operation is on the allow-list, and none appears twice;
3. there is an approval in the state `approved`, for exactly the stored plan;
4. no note or task of the plan is one the ledger records as written on another contact than the plan states:
   such a plan would not do what it says, so it is refused, planned again (the new plan names that write under
   "already written") and waits again;
5. on a first attempt at this plan, the plan is computed again from the CRM as it is now; if its sha256
   is not the approved one, the approval is for another plan: refused, re-planned, it waits again;
6. a dry run stops here and writes nothing; the gate's one write method also refuses in a dry run.

**A first attempt** is one where no step of this plan has started (no ledger row for its sha256), whatever the
enquiry's state. Once a step has started, the gate does not re-plan: the CRM may now hold this plan's own first
writes, which are not a change by anyone else. It completes the approved plan step by step.

**The intent is recorded before each write.** For each operation, the ledger row `started` and the audit row
`operation_started` are written in one transaction before the CRM is called; after the call, the ledger row
`done`, with the CRM's id and the contact the write is on (for a note found on another contact, that contact),
and the audit row `operation`. A process killed between a write and its record
leaves the enquiry `approved` with a step `started`: `doctor` names that step, and the next `apply` continues the
same plan under the same approval, and finds the write (below).

**Idempotent writes.** Before each write the gate looks:

- `create_contact`: the ledger, then a contact whose origin key is this enquiry's key. The contact
  records the key of the enquiry that created it (`origin_key`), so a contact the gate created is found
  again even with no ledger.
- `add_note`: the ledger, then the contact's own notes by their first line, then a search for the key,
  each hit confirmed by its first line. The contact's notes come first because a search may lag behind
  a write. A note with the key on any contact counts as done.
- `add_task`: the ledger, then the contact's own tasks by their first line.
- `fill_empty_fields`: the CRM itself writes only fields that are still empty, so calling it again writes
  nothing new.

**One apply at a time.** `Desk.apply` holds the desk's apply lock for the whole run: an exclusive SQLite
transaction on a file of its own beside the desk file (`desk.sqlite3-apply-lock`). A second `apply` started
meanwhile finds it taken and stops before it reads anything, with a message, and writes nothing. The operating
system releases the lock when the process ends, so a killed apply never leaves it held. A test forces two
appliers to interleave between a look-up and a write: the second is refused, and one note is written. The lock
does not stop another program, or a second desk with files of its own, from writing the same CRM.

**A lost ledger writes nothing twice**, and asks a person again when it matters: with no ledger, nothing
on the desk shows that a step started, so the gate treats the apply as a first attempt and re-plans. If
the plan created a contact, the CRM now holds that contact, the plan changes, and the enquiry waits for
a new approval; applying the new plan finds the note and the task and writes nothing twice. If the plan
is unchanged (a returning contact), the gate completes it at once and finds what is already there. `doctor`
names each operation of the trail whose ledger row is gone.

**An `apply_failed` that a retry cannot finish.** When the CRM changed so that the approved plan cannot be
written (the e-mail now belongs to a contact someone else made), each retry fails the same way, because a retry
continues the plan it started. `plan` and the enquiry page say what clears it: `withdraw`, then `plan`, which
plans the enquiry again against the CRM as it is now, for a new approval.

**Withdrawing or rejecting after an apply that did not finish.** A step the apply began and did not record is
left `started` in the ledger. `withdraw`, and `reject` if one is still left, first look in the CRM for each such
write by the enquiry's key, as a retry does (a contact by its origin key, a note or a task by its first line,
filled fields by their values), while holding the apply lock, and record what they found: the ledger row `done`
with where the write is and the audit row `operation`, or the ledger row removed and the audit row
`operation_not_written`. The command prints each finding. If the CRM cannot be read, the command changes nothing
and says so. So no step is left `started` once a person has taken the plan back, and `doctor` stays green. A
rejection of a plan part of which is in the CRM says what is there ("rejected after 1 of 3 steps had been
written: contact 11 created"); the desk never removes what it wrote.

A practice CRM file that another program holds locked is a CRM that did not answer: the SQLite error becomes a
CRM error, and the enquiry is `apply_failed` with it named (or stays `received`, when it arrived meanwhile).

## The record

- `arrivals` has one row per arrival, `first`, `replay` or `resend`; a unique index allows one `first` per key.
  `report` says the states add up when the enquiries in their states equal the first arrivals; with the
  replays and the corrected resends, that is everything that arrived.
- A body or a line that is not one JSON object never becomes an enquiry: HTTP answers 400, `ingest` names
  the line. It is not counted as an arrival, because there is nothing to key or to keep.
- Each arrival and each corrected resend records the sha256 of the content it stored.
- The audit trail is append-only by three triggers: no update, no delete, and no insert that does not
  name the last row's hash or that reuses a row number (`INSERT OR REPLACE` skips delete triggers, so
  this one is needed). Each row also carries the sha256 of its content and of the row before it.

**What the trail guards against, and what it does not.** It guards against mistakes, bugs, and plain edits of
the file, by the program or by hand. It does not guard against a person who can open the database file: such a
person can drop the triggers (SQLite does not refuse a `DROP TRIGGER`), change rows, recompute every hash and put the
triggers back as written. Nothing inside one file can tell that rewrite from the truth; a deployment that needs
to would keep a copy of the chain's last hash somewhere that person cannot write.

`desk.py doctor` checks, beyond the counts: each trigger's SQL against the SQL the desk writes; the hash chain;
each state move against the one before it in the trail, and against the state table; each approval row against
the person's decision in the trail (who, which plan); each enquiry's state, stored content and plan against the
last ones its trail records; every operation in the trail against its ledger row; no step of a current plan left
`started` outside `apply_failed` (it names such a step as one the desk does not know reached the CRM, and only for
an `approved` enquiry, where it works, offers `desk.py apply --key`); the arrival counts against the trail's
arrival events; and, in the practice CRM, every write the ledger records as done: the contact a plan created
(still there, still carrying the enquiry's key as its origin), the contact whose fields it filled (still there),
and each note and task (still there, on the contact the ledger names, still beginning with the enquiry's key).

**What `doctor` does not notice.** It compares what the two files hold with each other, so an edit that leaves
them agreeing is not found. Each of these leaves it green, as the tests of `DoctorComparesTheLedgerWithTheCRMTests`
show: a note the desk wrote, rewritten below its first line; a contact's fields changed in the CRM by someone else,
including a contact the desk created; and, on the desk file, a column that no trail row records, edited by hand:
the model's draft kept for an enquiry, a refused enquiry's reasons, a held enquiry's resolution, what identity
found. With an HTTP CRM it does not look in the CRM at all.

The desk file changed by hand, as a person who can open it might change it, and what `doctor` reports. Each row
names its test in `tests/test_promises.py` (class `DoctorFindsHandEditsOfTheDeskFileTests` unless another is named),
or says it is not tested:

| The manipulation | What `doctor` reports | Test |
|---|---|---|
| 1. With the triggers in place: `UPDATE`, `DELETE`, `INSERT OR REPLACE` on the trail | Each is refused by the database itself. `DROP TRIGGER` is not; a missing trigger is reported | `tests/test_audit_and_store.py`: `test_the_database_refuses_to_change_an_audit_row`, `test_the_database_refuses_to_remove_an_audit_row`, `test_insert_or_replace_cannot_overwrite_a_row`; `tests/test_controls.py`: `test_control_5_the_audit_triggers_dropped_a_changed_row_is_found` |
| 2. Triggers dropped, the last row removed, triggers put back | Reported when that row records a state move (the enquiry's state is not the last its trail records) or an arrival, a replay or a corrected resend (the counts no longer agree with the trail). **Not reported** when it records anything else, such as an `apply_refused` row (an apply refused for want of an approval) or an `identity_failed` row | `test_the_last_row_removed_is_found_when_it_is_a_state_move`, `test_the_last_row_removed_is_found_when_it_is_a_replay`, `test_what_it_cannot_find_the_last_row_removed_when_it_records_no_state_and_no_arrival` (an `apply_refused` row); the other kinds are not tested |
| 2. The same, a row in the middle (a replay) | Reported: the chain is broken, and the replay count no longer agrees with the trail | `test_a_replay_row_removed_from_the_middle_of_the_trail_is_found` |
| 2. The same, a whole enquiry that arrived last, with its trail rows, arrivals and enquiry row | **Not reported**: nothing left on the desk names it | `test_what_it_cannot_find_a_whole_enquiry_that_arrived_last_removed_with_its_rows` |
| 3. Triggers dropped, a row changed (the approver's name), every later hash recomputed, the approval row changed to match, triggers put back | **Not reported**: every part of the file agrees with every other | `test_what_it_cannot_find_a_consistent_rewrite_by_a_person_who_can_open_the_file` |
| 4. A trigger replaced by one of the same name that does nothing | Reported: the trigger is not as written | `test_a_trigger_replaced_by_one_of_the_same_name_that_does_nothing_is_found` |
| 5. Outside the trail: the stored content of an enquiry edited; the name on an approval edited; the ledger emptied | Reported: the content does not match its recorded sha256; the approval does not match the decision in the trail; operations of the trail have no ledger row | `test_an_enquiry_s_stored_content_edited_is_found`, `test_the_name_on_an_approval_edited_is_found`, `test_the_ledger_emptied_is_found` |
| Also: an approval row inserted with SQL, and the enquiry set to `approved` | Reported: the approval has no decision in the trail, and the next move does not follow the one before it | `test_an_approval_inserted_by_sql_with_no_decision_in_the_trail_is_found` |
| Also: a plan rewritten, with its sha256 changed everywhere on the desk | Reported: the enquiry's plan is not the plan its trail records | `test_a_plan_rewritten_with_its_sha256_changed_everywhere_on_the_desk_is_found` |

## The intake endpoint

- Fails closed: no `INTAKE_SECRET`, or one shorter than 16 characters, means every request gets 503. The
  header is compared in constant time. A secret with a character that is not printable ASCII (a space, an
  accented letter) stops `listen` and `serve` at start with the reason: an HTTP header carries ASCII only, so
  no request could ever match it. The log line of a request is its method, its path and its status; the method
  and the path, which any local program can send without the secret, are written with every control character
  as its escape and cut to 200 characters, so they cannot move the cursor or clear the terminal.
- One request at a time (a plain `HTTPServer`), and every answer closes its connection, so the desk file
  has one writer and no client holds the server open. Each request has a deadline of 10 seconds from
  connecting to its last byte; a client that drips its request slowly is cut off, and the next one is served.
- A body above 64 KiB is 413; no `Content-Length` is 411. A body that is a JSON object but has a key
  twice, `NaN`, or text UTF-8 cannot carry is 400. A request line that cannot be read (one word, a carriage
  return inside it, a version that is not one) is answered 400 with a status line, and logged with `-` for the
  method and the path it lacks.
- A refused enquiry is answered 201 with its state `refused` and no reasons: a script that fills the
  honeypot is not told which rule caught it. `ingest` shows the reasons to the operator.
- If the CRM cannot be read when an enquiry arrives, the enquiry is kept as `received` and the answer is
  201; `desk.py plan` assesses it later. A lead is not lost because the CRM was down.
- A desk file that another program holds locked is answered 503 with a plain line ("send it again"), on
  `POST /intake` and on the desk pages; a request that fails in a way no answer covers is logged as one plain
  line, never a traceback.

## A replay with different content

A copy that arrives under a known key is a replay, whatever it holds: the first content stands and nothing
new is planned. When its content differs (any field but the time it was sent), the trail's replay row
records each differing field with both values, and `report`, `ingest` and the desk page say "replayed with
different content" and name the fields, so that a person sees an edited resend. The invented data holds
one: the third copy of the first enquiry, whose message was edited. Those values stay in the append-only trail
for good (see "Limits").

## The model step

- **The request** is one text: a fixed prompt, the fixed list of intents, and the message. It never holds
  the name, e-mail or phone fields. A **run of digits** is digits with nothing but non-letters between them
  (spaces of any kind, line breaks, dashes of any kind, dots, slashes, underscores, brackets). The message is
  scrubbed of exactly these, each replaced by `[email]`, `[phone]` or `[name]`:
  the sender's own e-mail as written, in any letter case; the digits of the sender's own phone, wherever a run
  of digits holds them, whatever stands between them; anything shaped like an e-mail address, with letters of
  any alphabet and, before the `@`, any of `. ! # $ % & ' * + / = ? ^ _ ` { | } ~ -` (an apostrophe not first,
  so a quote before an address stays); any run of 7 or more digits, except a real date written `2026-02-02` (a
  date is never part of a run, so two dates side by side stay; `5550-14-23` and `2026-02-30` are not dates, so
  they are part of one); each word of the sender's name (two letters or more), as a whole word. Names are
  compared folded: Unicode NFKC, case folding, NFKC again, and the dotted and dotless i of Turkish taken as
  `i`, so `Weiß` and `WEISS`, `İbrahim` and `IBRAHIM`, a composed and a decomposed `ë`, a full-width `Ｄａｎａ`
  and `Dana`, are one. A word is a run of letters, digits and combining marks: anything else, an underscore
  too, ends it. A name word in a script written without spaces (Han, Hiragana, Katakana, Bopomofo, Thai, Lao,
  Khmer, Myanmar, Tibetan, Yi, Javanese, Balinese) is found wherever it stands, inside a longer run of letters
  too. The data's marker word "invented", from "(invented)", is not a name word and stays. All replacements are
  found on the original text in one pass, so one replacement never makes or hides another.
- **What the scrub does not recognise:** an address written out in words (`name at example dot com`,
  `name(at)example.com`, spaces around the `@`); a name in a script written with spaces joined to another word
  with no separator (`DanaReviewer`), changed (`Danas`), spelt without its accent (`Zoe` for `Zoë`) or given as
  an initial; a name word of one letter; an address with a quoted part before its `@` (`"pat lee"@example.com`);
  anyone else's name; a street address; a number of 6 digits or fewer;
  anything else that identifies a person. A test sends such a message and shows it reaches the request as it
  was. A **guard** reads the finished request again and refuses to send it if any of the replaced kinds is
  still there, by the same comparisons (the digits of the sender's phone in any run; any run of 7 or more; a
  name word, folded). The answer checks compare the sender's name the same way.
- **The request's sha256** is over its UTF-8 bytes and names it; `recorded/requests/<sha256>.txt` holds it.
  `python -I tools/recordings.py requests` writes one for every invented enquiry that reaches the model:
  the ones not refused, and not replays (a held enquiry reaches it: its draft helps the person resolve it).
- **The answer** must be one JSON object with exactly the four keys; a fifth key refuses the whole answer.
  The object may stand alone or inside one ```` ```json ```` fence, and nothing else may surround it.
  `reply_draft` may be null (the prompt asks for null on spam); when it is text it must hold `{{name}}`.
- **The limits:** a summary of 1 to 40 words and at most 400 characters; a reply of at most 120 words and
  1,200 characters. No field may hold a control character, a bidi control or a zero-width character; the
  reply may hold line breaks (LF). A web address in the reply is refused with or without its scheme
  (`evil.example/pay`, `bit.ly/x`, `javascript:`), as are an e-mail address and a phone number. The summary, which
  a person reads first, is refused for a web address or an e-mail address too.
- **Numbers** are compared as numbers written in digits: `1,200` and `1200` are one, `02` and `2` are one.
  Each must appear in the message the model saw.
- **An answer that fails** is not used: the desk keeps no part of it, shows "AI draft refused" with the
  reasons, and the plan is the plan with no AI text (the draft is never part of a plan). The reasons name a
  check, never a personal value, so the one retry of the live path can carry them back to the model. Where a
  reason names something the answer holds (a key, a number), it is quoted and cut to 40 characters.
- **Printing.** Text from outside the desk (a sender's message, a model's answer, a CRM's error) is printed by
  `plan`, `trail`, `report` and `apply` with every control or format character written as its escape, so it
  cannot move the cursor, clear the screen or begin a line of its own.
- **The live path** sends the Messages API request with `anthropic-version: 2026-10-21` and the default
  model `claude-opus-5-5` (`INTAKE_MODEL` changes it), with no sampling parameters, and asks for the
  server-side fallback on a refusal (`fallbacks: "default"`, the `server-side-fallback-2026-07-01` beta).
  It reads only the answer's `text` blocks (a thinking block may come first), treats `stop_reason:
  "refusal"` as no AI text, and on the one retry sends the first answer back unchanged, thinking blocks
  included, followed by the reasons. It has been run against a local mock only.
- **Labels are earned.** "Recorded" needs an answer in `recorded/answers/` whose meta file says
  `"origin": "recorded"` and whose request file and answer file both match the sha256s it names. An
  answer in `recorded/constructed/` is "Constructed answer, not from a model"; its meta file says
  `"origin": "constructed"` and how it was made. `tools/recordings.py add` is the only writer of
  `"origin": "recorded"`, and it copies the answer byte for byte.
- **The real answers.** All 20 requests have a recorded answer, from a small model: each meta file's `how`
  records that the request text, whole and unchanged, was given as the whole prompt to a Claude Code subagent
  on 2026-11-24, whose transcript names the model as claude-haiku-5-5. Such a subagent also runs under its
  harness's own system prompt, so this is not the live path's API call, and the live path's default model is
  another one. A sha256 shows only that a file has not changed since it was added and which request it
  answers; the origin is the author's record. `python -I tools/recordings.py status` runs the desk's own
  checks on each kept answer and prints the result.
- **When a constructed answer is used:** the `recorded` backend reads `recorded/answers/` first and
  `recorded/constructed/` only for a request with no recorded answer, so with all 20 recorded the desk and
  the demo use none of them. They stay as the fallback, and they are what a test uses (in a folder that holds
  only constructed answers) to show the label.
- **The gallery** (`recorded/gallery/`) holds constructed bad answers for two demo cases: one with a fifth
  key, one with a figure that is not in the message. Each is named as constructed. Four of the twenty real
  answers carried a fifth key of their own and are refused whole in the same way.
- **The template** shows the message's own first sentence on the local desk, with intent "other". No model
  is involved and nothing leaves the computer, so it is neither scrubbed nor checked.
- **The dry run** (`plan`) prints the model step in one line: the label, the intent, the urgency and the
  summary of a used answer, or "AI draft refused" with the reasons, or why there is no AI text.

## The desk pages

- Rendered on the server as plain HTML; no page has a script, and `serve` sends a Content-Security-Policy
  that allows none. Every value from an enquiry, a person or a model passes through one escape function.
- The counts come first, in plain words for each state, then the enquiries grouped by state, those waiting
  for a person first. Each enquiry names the label of its model step.
- **Approve and Reject** appear as forms only when `serve` was started with `--allow-decisions`. Each needs
  the ticked confirmation box and a name (the two locks), and carries the plan's sha256, so the approval is
  for the plan on the page. Without the switch the page shows the commands.
- **The form token and the Host and Origin checks** are the author's design for a page served on one's own
  computer, not a security claim: each form carries a token made when the server started; the server
  answers only requests addressed to 127.0.0.1 or localhost on its own port; and it takes a decision only when
  the Origin, if the browser sends one, is its own. They are meant to stop a web page elsewhere from reading
  the desk or posting a decision to it through the browser. They have not been reviewed as a defence, and the
  desk has no login.
- `serve` with no secret still shows the desk; only `POST /intake` answers 503. `listen` stays as before:
  the intake alone, refusing every request when it has no secret.
- `render --out` writes the same pages as files, with relative links and never a form. A key becomes a file
  name by writing `~` for `:`, then `-` and 8 hex digits of the key's sha256. Neither a source nor a source_id
  may hold `~`; the hash keeps two keys that differ only in letter case (a source_id is case-sensitive) apart on
  a file system that ignores case, as Windows' and macOS' usually do. A served page whose hash does not fit its
  key is 404.
- **A person's name** is refused when it starts with `=`, `+`, `-`, `@`, a tab or a carriage return, or holds
  a control character, on the command line and in the form. The project writes no CSV, but a name is kept in
  the trail and could reach a spreadsheet some day.

## The demo and the examples

- `demo.py` drives the real commands through `main(argv)` on new files, with a fixed clock and an
  environment of its own, so nothing from the caller's environment changes it. A CRM that is down is
  simulated by a wrapper that fails one call; the two intake cases start a server on 127.0.0.1. The story
  resolves the phone-only hold as contact 6 before it approves. Its clock starts at 15:00, after the last time an
  enquiry of the invented file was sent (14:39 that day): it takes the file in at 15:00, approves at 15:30, writes
  at 15:35 and takes the file in again at 17:00, so no page of the examples shows an enquiry taken in, approved or
  written before it was sent.
- `make_examples.py` runs the whole demo and keeps what it printed and wrote: the outputs of the story's
  steps (`plan-one-in-full.txt` is the plan the README opens with), the desk pages after the story, the closing
  table, and `docs/img/demo.svg` drawn from that table by `tools/term_svg.py`. No path of the computer that ran
  it is in any of them, so `--check` gives the same answer on every computer.
- In the closing table the cases `refused-inputs` and `ai-answer-refused` are named `inputs-not-taken` and
  `ai-answer-not-used`: the picture tool draws any line with the word "refused" red, as a failure.
- `tests/test_readme.py` runs the README's commands in the README's order, with `INTAKE_NOW` fixed, and
  compares every line it shows.

## Decisions beyond the basic flow

- **The state `received`**, for an enquiry taken while the CRM could not be read (see above).
- **A phone with digits, but not 7 to 15 of them, is refused**, as an e-mail that is not an address is; a phone
  of two digits would otherwise become a matching key.
- **A field that must be text but is another JSON type, and a `received_at` that is not a date and time,** are
  refused.
- **A secret shorter than 16 characters counts as no secret** (503).
- **`resolve --as new` is refused** while the e-mail belongs to a contact (see "How a hold is resolved").
- **The REST shape** of `docs/CRM-API.md` adds, to the calls the desk's core needs, a look-up by name, a
  look-up by origin key, a search of notes, a list of a contact's tasks and a health check: identity, the
  idempotent writes and `status` need them.
- **Commands:** `init` creates the two files and loads the practice CRM's starting contacts; `listen` offers
  `POST /intake` alone; `serve` adds the desk pages; `render` writes them as files.
- Unknown fields in an enquiry are ignored, and their names are recorded in the trail.
- Blank lines in a `.jsonl` file are skipped and not reported.
- **The model step has a fourth kind of draft source**, the constructed answer: the fallback for a request
  with no recorded answer, and the two gallery cases. It is never labelled "Recorded".
- **The answer may sit in one ```` ```json ```` fence**, and `reply_draft` may be null.
- **The live path asks for the server-side fallback** on a refusal, as the Messages API documentation
  advises for this model.
- **The third copy of the first enquiry has an edited message**, so the data holds a replay with different
  content. The data still holds three replays.
- **The closing table renames two cases** (see "The demo and the examples").

## Limits

- The doctor lists only the practice CRM; with the HTTP adapter its CRM checks say "not checked".
- The HTTP adapter's look-up by name sends the first word of the normalised name and keeps exact
  matches; a CRM whose search folds letters differently could miss a possible duplicate.
- A note written while search lags, on a contact other than the one the plan names, would not be seen;
  the gate looks on the named contact first, where it is written.
- One apply runs at a time per desk; another program writing the same CRM, or a second desk with its own
  files, is not stopped by the apply lock, and only the look-ups before each write stand between them and a
  second note.
- A command other than `apply` that loses a race for the desk file (two people deciding at once) is refused by
  the state table, or waits up to 5 seconds for the file and then stops with a plain message; its transaction is
  rolled back, so nothing half done is kept.
- **Personal values stay in the trail for good.** The trail is append-only: a replay with different content
  records both versions of each differing field, and a refused enquiry is kept with its content. A real
  deployment needs a deletion story (on request, and after a retention period), which this desk does not have.
- **The hashed key** treats letter case, a dropped full stop or another Unicode form of the same letter in the
  message as a new enquiry, and the same message sent either side of midnight (UTC) as two.
- **A match by phone alone is held** whenever the e-mails do not agree, so a person confirms every such case,
  even a customer calling from their known number without giving an e-mail. With no e-mail on either side it is
  returning, and a person may resolve it as new.
- **The checks cannot catch an answer that is well formed and wrong.** The real answers show four kinds, each
  passing every check: an assumed date (the figures enquiry: "2026-02-02 appears to be in the past", though
  the model was given no date and the enquiry arrives in January 2026); an invented service ("Yes, we do clean
  windows on the outside", and the same for the pottery studio and the sawdust workshop); a commitment nobody
  made ("We will call you after 5pm as you asked"); a mistaken intent (the refillable-products supplier classed
  as `quote_request` and answered as if the business were the supplier). The model was told nothing about the
  business or the date. No code check is added for these: a check for "invents a service" would be a
  pretence. This is why a person approves each plan and edits each reply; the reply is never sent by this
  project. The README quotes each sentence, and a test checks it is in its recorded answer file.
