"""The Guardian (ARCHITECTURE layer 4): the policy engine, the six checks, the flight recorder, approvals and undo.

Plan 07-01 lays the package down so the composition roots import it once; 07-03 fills it (api, policy, checks, scam,
explain, recorder, approvals, sender, budget, autonomy) and 07-04 adds runtime.
"""

from . import audit_source  # noqa: E402, F401  registers the Guardian source of the audit log (TRU-03)
