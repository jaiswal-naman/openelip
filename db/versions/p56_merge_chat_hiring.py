"""Joins the Phase 5 and Phase 6 revisions, which both follow p4_0401_match_brief and were built in parallel.

It runs nothing. The two revisions create disjoint sets of tables, types and functions, and each alters only tables it
creates (checked when the phases were integrated), so they apply in either order and this revision only records that
both have run.
"""

revision = "p56_merge_chat_hiring"
down_revision = ("p5_0501_chat_tailor_apply", "p6_0601_hiring")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
