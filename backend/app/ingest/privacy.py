"""The ingest tables in the deletion registry (TRU-02; ARCHITECTURE 8.4; DATA_MODEL 7.2).

The job index is public postings, public company boards and the skills taxonomy. None of it is about a person, so
erasing a person touches none of it and an export has nothing of it to add. The tables are registered all the same, so
every table in the database has an owner in the registry.

Registered on import. Both composition roots import this module, as they do profile.privacy.
"""

from app.trust.privacy import register

TABLES = ("job_sources", "jobs", "fetch_runs", "skills", "job_requirements")

register("ingest", dict.fromkeys(TABLES, "no_personal_data"))
