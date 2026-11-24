# Design decisions

This page records the points the specification left open, how each was settled, and why. Where this page and the code disagree, the code and its tests are right and this page
is wrong; please report it.

## Where things are

| Module | What it does |
|---|---|
| `intake/arrive.py` | Reads one JSON object from an HTTP body or a line of a `.jsonl` file |
| `intake/validate.py` | The refusals of section 3.2, each with its reason |
| `intake/normalise.py` | What "normalised" means for an e-mail, a phone, a name and a message |
| `intake/identity.py` | The idempotency key, and matching a contact by exact keys |
| `intake/planner.py` | The plan: the exact operations an enquiry would cause, and its sha256 |
| `intake/ops.py` | The allow-list of four operations |
| `intake/gate.py` | The one write gate: every CRM write passes through it |
| `intake/flow.py` | Arrival, assessment, and a person's decisions (resolve, approve, reject, withdraw) |
| `intake/states.py` | The named states and the only moves between them |
| `intake/store.py` | The desk's SQLite file |
| `intake/audit.py` | The audit trail: its triggers and its hash chain |
| `intake/doctor.py` | The invariants of the four promises |
| `intake/crm/` | The adapter interface, the practice CRM (SQLite) and the HTTP adapter |
| `intake/server.py` | `POST /intake`, and the desk pages under `serve` |
| `intake/pages.py` | The desk pages, rendered on the server with no script |
| `intake/clock.py` | The one clock |
| `intake/request.py` | The request to a model: the fixed prompt, the scrubbed message, and the guard |
| `intake/answer.py` | Reading a model's answer strictly, and its checks |
| `intake/draft.py` | The three backends of the model step: `recorded`, `template`, `anthropic` |
| `intake/reports.py` | The text of `report`, `trail`, `plan` and `doctor` |
| `intake/cli.py`, `desk.py` | The command line |
| `demo.py`, `make_examples.py` | The demo of section 5, and the examples made from its run |
| `tools/recordings.py` | Writes the requests and the constructed answers; keeps a real answer as it came |
| `tools/term_svg.py`, `tools/list_tests.py` | The demo's picture; `docs/TESTS.md` |

## The states

Every enquiry is in exactly one of these. A replay is not an enquiry: it is an arrival of a known key,
counted and recorded, and it changes no state.

| State | Meaning |
|---|---|
| `received` | Taken; identity not checked yet, because the CRM could not be read |
| `refused` | Failed validation; kept with its reasons; never reaches the model or the CRM |
| `held_two_contacts` | The e-mail and the phone point to different contacts; waits for a person |
| `held_possible_duplicate` | No exact key matches, but a contact has the same name and company; waits for a person |
| `awaiting_approval` | A plan is ready and waits for a person's approval |
| `approved` | A person approved exactly this plan; not applied yet |
| `rejected` | A person rejected the plan, with a reason; nothing is written |
| `applied` | Every operation of the approved plan is in the CRM |
| `apply_failed` | A CRM call failed; the error is named; a retry completes what is missing |

The moves (`intake/states.py`, `TRANSITIONS`). Any other move raises an error, and every move that
happens is an audit row that records the state it ends in.

| From | Event | To | Who |
|---|---|---|---|
| (not on the desk) | `arrived` | `refused` or `received` | system |
| `received` | `assessed` | `awaiting_approval`, or a held state | system |
| a held state | `resolved` | `awaiting_approval`, or a held state if the resolution no longer applies | a person |
| `awaiting_approval` | `replanned` | `awaiting_approval`, or a held state | system (`desk.py plan`, when the CRM changed) |
| `awaiting_approval` | `approved` | `approved` | a person |
| `awaiting_approval` | `rejected` | `rejected` | a person, with a reason |
| `approved` | `withdrawn` | `awaiting_approval` | a person |
| `approved` | `replanned` | `awaiting_approval`, or a held state | system (the gate: the CRM changed since the approval) |
| `approved` | `applied` / `apply_failed` | `applied` / `apply_failed` | system (the gate) |
| `apply_failed` | `applied` / `apply_failed` | `applied` / `apply_failed` | system (a retry) |
| `apply_failed` | `replanned` | `awaiting_approval`, or a held state | system (a retry where no step had started, and the CRM changed) |
| `apply_failed` | `withdrawn` | `awaiting_approval` | a person |

`refused`, `rejected` and `applied` are final.

## What "normalised" means

- **E-mail:** trimmed and lowercased (the whole address, as the specification says). It is an address when
  it has exactly one `@`, no whitespace, a dot after the `@`, and at most 254 characters.
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
  enquiry with no `received_at` gets its arrival time, from the desk's clock.
- The source_id is case-sensitive: a form system's ids are its own.

## What a replay returns, and what it changes

A replay is answered with the original's key, its state now, and when and how the original first
arrived (HTTP: status 200 with `"replay": true` and `"original"`; a new enquiry is 201). The arrival is
counted (`arrivals` table, kind `replay`) and recorded in the trail, with whether its content equals the
original's. Nothing else changes: no new plan, no new approval, no CRM write.

**An approved plan that is replayed** stays approved, with the same approval. The replay does not
withdraw it, renew it or apply it. A replay with different content under the same key is still a replay;
the trail notes `same_content=false` and the original content is kept.

## Identity

Section 3.3's table, made precise: the contacts found by the e-mail and by the phone form one set.

- One contact in the set: **returning**. This includes the case where the e-mail and the phone both
  point to the same contact.
- Two or more: **held: two contacts**. This includes a phone that alone belongs to two contacts (phones
  are not unique in a CRM), which the table did not name.
- None: a contact with the same normalised name **and** the same normalised company is a **possible
  duplicate**. Both must be non-empty: two people with one name and no company are not evidence of
  anything. Otherwise **new**.

## How a hold is resolved

`desk.py resolve <key> --as new --by <name>` or `--as contact:<id> --by <name>`.

- `contact:<id>` must be one of the contacts identity found for that enquiry (they are listed by `plan`).
  This stops a mistyped id from attaching an enquiry to an unrelated contact.
- `new` is refused while the enquiry's e-mail belongs to a contact: a second contact with that e-mail
  would break promise 2. For a two-contacts hold that is found by its e-mail, a person therefore chooses
  between the contacts found. A possible duplicate can be resolved either way.
- The resolution is an audit row with the person's name, and the enquiry gets its plan at once.
- A resolution applies only while it still makes sense: `new` stops applying if the e-mail later
  belongs to a contact; `contact:<id>` stops applying if the contact is gone. The plan then follows the
  ordinary rules and says why in its identity notes. The system never merges contacts.

## The plan and its sha256

A plan is `{"enquiry", "identity", "operations", "shown"}`: what identity found, each operation with its
values, and the differences shown to a person and not written. Its sha256 is over canonical JSON (sorted
keys, `,` and `:` separators, UTF-8, no NaN).

The plan is computed from the CRM as it is now, so its sha256 changes when the CRM changes in a way that
matters: the contact now exists, a field was filled meanwhile, a contact's differing value changed. A
note or task on the contact does not change it. The note and the task begin with the enquiry's key.

**The draft is never in the plan.** The stand-in's summary is shown on the desk only; the CRM path does
not depend on it.

`desk.py plan` is the dry run: it writes nothing to the CRM. It does write to the desk: it assesses any
enquiry still `received` and re-plans any `awaiting_approval` enquiry whose plan the CRM has changed, each
with its audit row, so that what it prints is the plan as it stands now. It never changes an approval; an
approved plan the CRM has changed is printed with a warning, and the gate deals with it.

## Approvals

An approval is a row: key, plan sha256, who, when, state (`approved`, `used`, `withdrawn`, `rejected`)
and a reason (required for `rejected`, by the database too). At most one approval per enquiry is
`approved` at a time (a unique index). `approve --plan <first 8+ hex digits>` refuses unless the waiting
plan is the one the person saw.

When the plan changes under a live approval, the system withdraws that approval (`closed_by` is
`system`, with the reason) rather than leave an approval for a plan that no longer exists. A person's
withdrawal and the system's are told apart by `closed_by`.

## The write gate

`intake/gate.py`, `WriteGate.apply`. Before anything leaves, in this order:

1. the stored plan matches its sha256;
2. every operation is on the allow-list, and none appears twice;
3. there is an approval in the state `approved`, for exactly the stored plan;
4. on a first attempt at this plan, the plan is computed again from the CRM as it is now; if its sha256
   is not the approved one, the approval is for another plan: refused, re-planned, it waits again;
5. a dry run stops here and writes nothing; the gate's one write method also refuses in a dry run.

**A first attempt** is an enquiry in `approved`, or one in `apply_failed` none of whose plan's steps has
started. A retry after a step has started does not re-plan: the CRM now holds this plan's own first
writes, which are not a change by anyone else. It completes the approved plan step by step.

**Idempotent writes.** The ledger has one row per enquiry and operation kind, `started` before the call
and `done` after it, with the CRM's id. Before each write the gate looks:

- `create_contact`: the ledger, then a contact whose origin key is this enquiry's key. The contact
  records the key of the enquiry that created it (`origin_key`), so a contact the gate created is found
  again even with no ledger.
- `add_note`: the ledger, then the contact's own notes by their first line, then a search for the key,
  each hit confirmed by its first line. The contact's notes come first because a search may lag behind
  a write. A note with the key on any contact counts as done.
- `add_task`: the ledger, then the contact's own tasks by their first line.
- `fill_empty_fields`: the CRM itself writes only fields that are still empty, so calling it again writes
  nothing new.

**A lost ledger writes nothing twice**, and asks a person again when it matters: with no ledger, nothing
on the desk shows that a step started, so the gate treats the apply as a first attempt and re-plans. If
the plan created a contact, the CRM now holds that contact, the plan changes, and the enquiry waits for
a new approval; applying the new plan finds the note and the task and writes nothing twice. If the plan
is unchanged (a returning contact), the gate completes it at once and finds what is already there.

## The record

- `arrivals` has one row per arrival, `first` or `replay`; a unique index allows one `first` per key.
  `report` says the states add up when the enquiries in their states equal the first arrivals; with the
  replays, that is everything that arrived.
- A body or a line that is not one JSON object never becomes an enquiry: HTTP answers 400, `ingest` names
  the line. It is not counted as an arrival, because there is nothing to key or to keep.
- The audit trail is append-only by three triggers: no update, no delete, and no insert that does not
  name the last row's hash or that reuses a row number (`INSERT OR REPLACE` skips delete triggers, so
  this one is needed). Each row also carries the sha256 of its content and of the row before it; the
  doctor recomputes the chain. Without the triggers, a changed or removed row is found; removing only
  the very last row leaves no gap in the chain, and is not found by it.

## The intake endpoint

- Fails closed: no `INTAKE_SECRET`, or one shorter than 16 characters, means every request gets 503. The
  header is compared in constant time. The log line of a request is its method, its path and its status.
- One request at a time (a plain `HTTPServer`), and every answer closes its connection, so the desk file
  has one writer and no client holds the server open.
- A body above 64 KiB is 413; no `Content-Length` is 411. A body that is a JSON object but has a key
  twice, `NaN`, or text UTF-8 cannot carry is 400.
- A refused enquiry is answered 201 with its state `refused` and no reasons: a script that fills the
  honeypot is not told which rule caught it. `ingest` shows the reasons to the operator.
- If the CRM cannot be read when an enquiry arrives, the enquiry is kept as `received` and the answer is
  201; `desk.py plan` assesses it later. A lead is not lost because the CRM was down.

## A replay with different content

A copy that arrives under a known key is a replay, whatever it holds: the first content stands and nothing
new is planned. When its content differs (any field but the time it was sent), the trail's replay row
records each differing field with both values, and `report`, `ingest` and the desk page say "replayed with
different content" and name the fields, so that a person sees an edited resend. The invented data holds
one: the third copy of the first enquiry, whose message was edited.

## The model step

- **The request** is one text: a fixed prompt, the fixed list of intents, and the message. It never holds
  the name, e-mail or phone fields. The message is scrubbed of the sender's own e-mail and phone as written,
  of anything shaped like an e-mail address or a phone number (7 to 15 digits; an ISO date such as
  2026-02-02 is kept), and of each word of the sender's name, as whole words in any letter case (the data's
  marker word "invented", from "(invented)", is not a name word and stays). All
  replacements are found on the original text in one pass, so one replacement never makes or hides another.
  A guard reads the finished request again and refuses to send it if any of these is still there. Any
  number of 7 or more digits counts as a possible phone, so a large figure in a message is scrubbed too.
- **The request's sha256** is over its UTF-8 bytes and names it; `recorded/requests/<sha256>.txt` holds it.
  `python -I tools/recordings.py requests` writes one for every invented enquiry that reaches the model:
  the ones not refused, and not replays (a held enquiry reaches it: its draft helps the person resolve it).
- **The answer** must be one JSON object with exactly the four keys; a fifth key refuses the whole answer.
  The object may stand alone or inside one ```` ```json ```` fence, and nothing else may surround it.
  `reply_draft` may be null (the prompt asks for null on spam); when it is text it must hold `{{name}}`.
- **Numbers** are compared as numbers written in digits: `1,200` and `1200` are one, `02` and `2` are one.
  Each must appear in the message the model saw.
- **An answer that fails** is not used: the desk keeps no part of it, shows "AI draft refused" with the
  reasons, and the plan is the plan with no AI text (the draft is never part of a plan). The reasons name a
  check, never a personal value, so the one retry of the live path can carry them back to the model.
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
- **The real answers.** All 20 requests have a recorded answer. Each meta file's `how` records how it was
  obtained: the request text, whole and unchanged, given as the whole prompt to a Claude Code subagent on
  2026-11-24, whose transcript names the model as claude-haiku-5-5; such a subagent also runs under its
  harness's own system prompt, so this is not the live path's API call. A sha256 shows only that a file has
  not changed since it was added and which request it answers; the origin is the author's record.
  `python -I tools/recordings.py status` runs the desk's own checks on each kept answer and prints the
  result.
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
  name by writing `~` for `:`; neither a source nor a source_id may hold `~`, so the names do not collide.
- **A person's name** is refused when it starts with `=`, `+`, `-`, `@`, a tab or a carriage return, on the
  command line and in the form. The project writes no CSV, but a name is kept in the trail and could reach a
  spreadsheet some day.

## The demo and the examples

- `demo.py` drives the real commands through `main(argv)` on new files, with a fixed clock and an
  environment of its own, so nothing from the caller's environment changes it. A CRM that is down is
  simulated by a wrapper that fails one call; the two intake cases start a server on 127.0.0.1.
- `make_examples.py` runs the whole demo and keeps what it printed and wrote: the outputs of the story's
  steps, the desk pages after the story, the closing table, and `docs/img/demo.svg` drawn from that table by
  `tools/term_svg.py`. No path of the computer that ran it is in any of them, so `--check` gives the same
  answer on every computer.
- In the closing table the cases `refused-inputs` and `ai-answer-refused` are named `inputs-not-taken` and
  `ai-answer-not-used`: the picture tool draws any line with the word "refused" red, as a failure.

## Changes against the specification, and additions

- **Added the state `received`**, for an enquiry taken while the CRM could not be read (see above).
- **Added a refusal:** a phone with digits, but not 7 to 15 of them. The specification refuses an e-mail
  that is not an address; a phone of two digits would otherwise become a matching key.
- **Added a refusal:** a field that must be text but is another JSON type, and a `received_at` that is not
  a date and time.
- **A secret shorter than 16 characters counts as no secret** (503).
- **`resolve --as new` is refused** while the e-mail belongs to a contact (see "How a hold is resolved").
- **The REST shape** of `docs/CRM-API.md` adds, to the calls section 3.7 lists, a look-up by name, a
  look-up by origin key, a search of notes, a list of a contact's tasks and a health check: identity, the
  idempotent writes and `status` need them.
- **Added commands:** `init` creates the two files and loads the practice CRM's starting contacts;
  `listen` offers `POST /intake` alone; `serve` adds the desk pages.
- Unknown fields in an enquiry are ignored, and their names are recorded in the trail.
- Blank lines in a `.jsonl` file are skipped and not reported.
- **The model step adds a fourth kind of draft source**, the constructed answer: the fallback for a request
  with no recorded answer, and the two gallery cases. It is never labelled "Recorded".
- **The answer may sit in one ```` ```json ```` fence**, and `reply_draft` may be null.
- **The live path asks for the server-side fallback** on a refusal, as the Messages API documentation
  advises for this model.
- **The third copy of the first enquiry has an edited message**, so the data holds a replay with different
  content. The data still holds three replays.
- **The closing table renames two cases** (see "The demo and the examples").

## Limits

- One writer at a time is assumed. SQLite's locking keeps both files consistent, and a move that lost a
  race is refused by the state table, but the command that lost it stops with an error.
- The doctor lists only the practice CRM; with the HTTP adapter its CRM checks say "not checked".
- The HTTP adapter's look-up by name sends the first word of the normalised name and keeps exact
  matches; a CRM whose search folds letters differently could miss a possible duplicate.
- A note written while search lags, on a contact other than the one the plan names, would not be seen;
  the gate looks on the named contact first, where it is written.
- **The checks cannot catch an answer that is well formed and wrong.** The real answers show four kinds, each
  passing every check: an assumed date (the figures enquiry: "2026-02-02 appears to be in the past", though
  the model was given no date and the enquiry arrives in January 2026); an invented service ("Yes, we do clean
  windows on the outside", and the same for the pottery studio and the sawdust workshop); a commitment nobody
  made ("We will call you after 5pm as you asked"); a mistaken intent (the refillable-products supplier classed
  as `quote_request` and answered as if the business were the supplier). The model was told nothing about the
  business or the date. No code check is added for these: a check for "invents a service" would be a
  pretence. This is why a person approves each plan and edits each reply; the reply is never sent by this
  project. The README quotes each sentence, and a test checks it is in its recorded answer file.
