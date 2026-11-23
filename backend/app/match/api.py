"""What other modules may call from app.match (the shared contracts in the Phase 4 index). Nothing else in this
package is a public interface.

  brief_matches(conn, user_id, local_date)   what a day's brief lists
  activity_since(conn, user_id, since)        the runs that finished after a time, for "done since the last brief"
  nightly_status(conn, user_id, local_date)   whether the day's nightly run is still to come, running, done or failed

BriefMatch, RunActivity and NightlyStatus are what they return. The pipeline entry points (run, retrieve, violations)
are added by the plan that builds them.
"""

from .queries import BriefMatch, NightlyStatus, RunActivity, activity_since, brief_matches, nightly_status

__all__ = ["BriefMatch", "NightlyStatus", "RunActivity", "activity_since", "brief_matches", "nightly_status"]
