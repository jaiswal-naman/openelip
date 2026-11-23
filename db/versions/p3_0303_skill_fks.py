"""The two foreign keys that need both phases: profile_facts.skill_id and preferences.skill_id point at skills.

DATA_MODEL 3.14. skills comes from the job index (p2_0201_job_index) and the two record tables from the Career Record
(p3_0302_record), so neither phase could add these keys alone; p3_0301_trust says why the two histories are one line.
Both columns are empty when this runs and skills starts empty, so a fact or a preference can carry a skill_id only once
a taxonomy has been loaded. The keys have no ON DELETE action, as in DATA_MODEL: a skill that a fact uses cannot be
deleted from the taxonomy.
"""

from alembic import op

revision = "p3_0303_skill_fks"
down_revision = "p3_0302_record"
branch_labels = None
depends_on = None

# (table, constraint name); the name is the one PostgreSQL would give an unnamed key, so DATA_MODEL's own statement and
# this one produce the same schema.
KEYS = (("profile_facts", "profile_facts_skill_id_fkey"), ("preferences", "preferences_skill_id_fkey"))


def upgrade() -> None:
    op.execute("SET ROLE app_owner")  # the tables belong to app_owner
    for table, name in KEYS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} FOREIGN KEY (skill_id) REFERENCES skills(id)")
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    for table, name in KEYS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute("RESET ROLE")
