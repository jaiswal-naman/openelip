"""Matching: constraint-safe matches with their evidence, the decision ledger and feedback (MAT-01 to MAT-08).

Other modules import only app.match.api (ARCHITECTURE 3.3); everything else here belongs to this module. This is
layer 3: it reads the job index and the Career Record, and connects to the database as its own role, app_match.
"""

from . import audit_source  # noqa: E402, F401  registers the matching source of the audit log (TRU-03)
