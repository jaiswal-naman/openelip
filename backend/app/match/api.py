"""What other modules may call from app.match (the shared contracts in the Phase 4 index). Nothing else in this
package is a public interface.

  brief_matches(conn, user_id, local_date)   what a day's brief lists
  activity_since(conn, user_id, since)        the runs that finished after a time, for "done since the last brief"
  nightly_status(conn, user_id, local_date)   whether the day's nightly run is still to come, running, done or failed
  run(user_id, trigger=..., local_date=...)   one matching run for a person; returns a RunResult
  retrieve(conn, user_id, mode=...)           the candidates for a person's own record (keyword, vector or hybrid)
  violations(job, constraints)                the independent checker: the hard constraints a job breaks
  run_role(role_id, job_id, candidate_ids)    one employer role against pooled candidates: introduced or withheld
  role_selections(conn, run_id)               the candidates a role run introduced, with their evidence
  latest_role_run(conn, role_id)              the newest run for a role
  RANKER_VERSION                              the name of the ranking rules, stored on every run

BriefMatch, RunActivity, NightlyStatus and RunResult are what they return.
"""

from .checker import violations
from .queries import BriefMatch, NightlyStatus, RunActivity, activity_since, brief_matches, nightly_status
from .ranking import RANKER_VERSION
from .retrieval import retrieve
from .role_run import (
    RoleEvidence,
    RoleRunInfo,
    RoleRunResult,
    RoleSelection,
    latest_role_run,
    role_selections,
    run_role,
)
from .service import RunResult, run

__all__ = [
    "RANKER_VERSION",
    "BriefMatch",
    "RoleEvidence",
    "RoleRunInfo",
    "RoleRunResult",
    "RoleSelection",
    "NightlyStatus",
    "RunActivity",
    "RunResult",
    "activity_since",
    "latest_role_run",
    "brief_matches",
    "nightly_status",
    "retrieve",
    "role_selections",
    "run",
    "run_role",
    "violations",
]
