"""A lead-intake desk: enquiries in, plans out, nothing written to a CRM without a recorded approval.

Where to look first:

- the write gate ............................ intake/gate.py
- the identity rules (key and matching) ..... intake/identity.py
- the audit trail, its triggers and chain ... intake/audit.py
- the state names and transitions ........... intake/states.py
- the invariants that `desk.py doctor` runs . intake/doctor.py
- the allow-list of CRM operations .......... intake/ops.py

Everything here is the Python standard library. All people and companies in data/ are invented.
"""
