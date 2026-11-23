"""What other modules may call from app.brief (the shared contracts in the Phase 4 index). Nothing else in this
package is a public interface.

  compose(conn, user_id, local_date, *, now)   compose one person's brief for one local date; ComposeResult is what
                                                it returns (the brief.compose job and the demo seed call it)
"""

from .compose import ComposeResult, compose

__all__ = ["ComposeResult", "compose"]
