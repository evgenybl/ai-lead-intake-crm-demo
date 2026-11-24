# AI Lead Intake to CRM (demo)

**Work in progress: this is phase 1 of 2, the core.** The full README, the desk pages, the model's draft
step and the demo come in phase 2.

A lead-intake desk: it takes enquiries from a web form (over HTTP) or a file, recognises the same enquiry
sent again, plans the CRM writes each enquiry would cause, and writes nothing to the CRM without a
recorded approval by a person.

- All people, companies and data here are invented; e-mail addresses are at example.com.
- No model is called in phase 1: the draft step is a stand-in labelled "stand-in, no model".
- The CRM is either a practice CRM in a local SQLite file, or an HTTP adapter for a REST shape defined in
  `docs/CRM-API.md`, which has been tested against a fake only. Nothing here is connected to a real CRM.

## Run the tests

Python 3.11 or later, standard library only, nothing to install:

```
python -I run_tests.py
```

## Try it

```
python -I desk.py init
python -I desk.py ingest data/enquiries.synthetic.jsonl
python -I desk.py report
python -I desk.py plan
python -I desk.py approve webform:wf-000101 --by "Your Name"
python -I desk.py apply
python -I desk.py trail webform:wf-000101
python -I desk.py doctor
```

`--home <folder>` chooses where the two files live (default `./work`); `--at 2026-01-05T09:00:00Z` fixes
the clock. `python -I desk.py --help` lists every command.

## Where to read

`docs/DESIGN.md` has the states, the rules and the decisions behind them. The write gate is
`intake/gate.py`; the identity rules are `intake/identity.py`; the audit triggers are `intake/audit.py`.
