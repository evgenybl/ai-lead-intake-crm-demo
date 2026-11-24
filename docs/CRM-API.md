# The HTTP CRM: a REST shape defined here

This is a small REST shape defined by this project, for `intake/crm/http_crm.py`. **It is not any
vendor's API**, and the adapter has never been connected to a real CRM. It is tested against a strict
fake (`tests/fake_crm.py`) only. A real CRM needs its own adapter of about the size of
`intake/crm/http_crm.py`, written against that CRM's own API.

## Every call

- `Authorization: Bearer <token>`; the token comes from `INTAKE_CRM_TOKEN`, the address from
  `INTAKE_CRM_URL`. A wrong or missing token is 401.
- Bodies and answers are JSON objects. A field, a path, a method or a query parameter this page does not
  name is refused with 400 (404 for a path, 405 for a method). The fake records each such refusal, and the
  tests assert that the adapter never caused one.
- Any status other than the one a call names is an error. The adapter then assumes nothing about whether
  the call took effect; the write gate looks before it writes again.

## The records

| Record | Fields |
|---|---|
| Contact | `id` (text), `name`, `email`, `phone`, `company`, `origin_key` (the key of the enquiry that created it, or empty) |
| Note | `id`, `contact_id`, `body` (its first line is an enquiry's key when the desk wrote it) |
| Task | `id`, `contact_id`, `body` (the same) |

## The calls

| Call | Body | Answer |
|---|---|---|
| `GET /health` | none | 200 `{"ok": true}` |
| `GET /contacts?email=<e-mail>` | none | 200 `{"contacts": [...]}`: the contacts with this e-mail, compared lowercased |
| `GET /contacts?phone=<digits>` | none | 200 `{"contacts": [...]}`: the contacts whose phone has exactly these digits |
| `GET /contacts?origin_key=<key>` | none | 200 `{"contacts": [...]}`: the contacts created by this enquiry |
| `GET /contacts?name=<text>` | none | 200 `{"contacts": [...]}`: contacts whose name contains the text, ignoring letter case. A search: the adapter keeps only exact normalised matches of name and company |
| `GET /contacts/{id}` | none | 200 `{"contact": {...}}`, or 404 |
| `POST /contacts` | `{"name", "email", "phone", "company", "origin_key"}`, all five | 201 `{"contact": {...}}`; 409 when another contact has this e-mail |
| `POST /contacts/{id}/fill` | `{"fields": {...}}` with any of `name`, `email`, `phone`, `company` | 200 `{"filled": [...], "kept": [...]}`: each field is written only where the contact's field is empty; 409 for an e-mail another contact has |
| `GET /contacts/{id}/notes` | none | 200 `{"notes": [...]}`, always up to date |
| `POST /contacts/{id}/notes` | `{"body"}` | 201 `{"note": {...}}` |
| `GET /notes?search=<text>` | none | 200 `{"notes": [...]}`: notes whose body contains the text. **It may lag behind writes**; the gate checks the contact's own notes first |
| `GET /contacts/{id}/tasks` | none | 200 `{"tasks": [...]}` |
| `POST /contacts/{id}/tasks` | `{"body"}` | 201 `{"task": {...}}` |

`GET /contacts` takes exactly one of its four query parameters.

## Against section 3.7 of the specification

Section 3.7 lists: find a contact by e-mail or phone, create, fill empty fields, list and add notes, add
a task. This shape adds the look-up by name (for the possible-duplicate rule), the look-up by origin key
and the list of a contact's tasks (so that a write is found again without the desk's ledger), the search
of notes (the third place the gate looks), and a health check (for `desk.py status`).
