"""What other modules may call from app.hiring (the shared contracts in the Phase 6 index). Nothing else in this
package is a public interface.

  my_organizations(conn, user_id)   the organizations a person belongs to and the invitations waiting for them

Membership and Invitation are what it returns. Plan 06-05 adds the reads the morning brief needs.
"""

from .queries import Invitation, Membership, my_organizations

__all__ = ["Invitation", "Membership", "my_organizations"]
