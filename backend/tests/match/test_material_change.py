"""MAT-07 and owner decision G5 (plan 04-03, tasks 2 and 3): a job shown at one content version is not shown again at
that version; once ingest stores a new version it may be shown again, with the reason material_change and the note
ingest wrote, and with several versions passed the note says how many and when the person last saw it.

The posting goes through ingest's own store(), so the note is the one ingest writes. Everything is invented. A run opens
its own connections, so the data is committed and the tables are emptied around each test.
"""

import logging
from datetime import date

import pytest
from sqlalchemy import text

from app.ingest.fetch import store
from app.match import service
from tests.match.fixtures.run import world

COMPANY = "Initech Demo"
DAYS = [date(2026, 11, day) for day in (23, 24, 25, 26)]
REQUIREMENTS = (("Python", "required"), ("SQL", "required"))
DESCRIPTION = "Invented posting for the material change test. You need Python. You need SQL."
REWORDED = "Invented posting, reworded for the test. You need Python. You need SQL. Pay is agreed at interview."


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    world.empty_tables(engine)
    world.no_embedder(monkeypatch)
    yield
    world.empty_tables(engine)


def ingest(engine, source_id, *, title="Fixture engineer", location="Pune, India", description=DESCRIPTION):
    """One fetch of the posting, committed; returns what store() counted."""
    posting = {
        "external_id": "fixture-1",
        "title": title,
        "location_raw": location,
        "country_hint": None,
        "remote_hint": None,
        "employment_type": None,
        "department": None,
        "description": description,
        "apply_url": "https://example.test/jobs/1",
        "posted_at": None,
    }
    with engine.begin() as c:
        return store(c, source_id, COMPANY, [posting])


def decisions(engine, user):
    return world.fetch(
        engine,
        """SELECT d.id, d.local_date, d.outcome::text AS outcome, d.reason_codes::text[] AS reasons,
                  d.job_content_version AS version, d.change_note, d.rank_position
             FROM match_decisions d WHERE d.user_id = :u ORDER BY d.local_date, d.created_at""",
        u=user,
    )


def test_a_shown_job_is_not_offered_again_until_ingest_stores_a_new_version_and_the_note_comes_with_it(
    engine, monkeypatch, caplog
):
    world.use_provider(monkeypatch, "all_met.json")
    user = world.add_person(engine)
    with engine.begin() as c:
        source_id = c.execute(
            text(
                "INSERT INTO job_sources (ats, token, company_name) VALUES ('greenhouse', 'fixture', :c) RETURNING id"
            ),
            {"c": COMPANY},
        ).scalar_one()
    assert ingest(engine, source_id)["new"] == 1
    (job_id,) = world.fetch(engine, "SELECT id FROM jobs")[0]
    world.add_requirements(engine, job_id, 1, REQUIREMENTS)

    first = service.run(user, trigger="nightly", local_date=DAYS[0])

    assert first.shown == 1
    (shown,) = decisions(engine, user)
    assert (shown.outcome, shown.reasons, shown.version, shown.change_note) == ("shown", ["top_ranked"], 1, None)

    # the next night nothing has changed: the job is not retrieved, so it is neither shown nor withheld
    second = service.run(user, trigger="nightly", local_date=DAYS[1])
    assert (second.shown, second.withheld) == (0, 0)
    assert len(decisions(engine, user)) == 1
    (run,) = world.fetch(engine, "SELECT retrieved_count FROM match_runs WHERE id = :r", r=second.run_id)
    assert run.retrieved_count == 0

    # ingest stores a new version with a new location: the job may be shown again, and says what changed
    assert ingest(engine, source_id, location="Mumbai, India")["changed"] == 1
    version_two = world.add_requirements(engine, job_id, 2, REQUIREMENTS)
    with caplog.at_level(logging.INFO, logger="app.event"):
        third = service.run(user, trigger="nightly", local_date=DAYS[2])

    assert third.shown == 1
    again = [d for d in decisions(engine, user) if d.outcome == "shown" and d.version == 2]
    assert [(d.reasons, d.change_note) for d in again] == [(["top_ranked", "material_change"], "Location changed.")]
    cited = world.fetch(
        engine, "SELECT job_requirement_id FROM match_evidence WHERE match_decision_id = :d", d=again[0].id
    )
    assert sorted(row.job_requirement_id for row in cited) == sorted(version_two)  # verdicts about the new version
    reshown = [r.getMessage() for r in caplog.records if "match.reshown" in r.getMessage()]
    assert len(reshown) == 1 and f'"job_id":{job_id}' in reshown[0] and '"content_version":2' in reshown[0]

    # two more versions pass unseen: the note says so, and when the person last saw the job
    assert ingest(engine, source_id, location="Mumbai, India", title="Fixture engineer, senior")["changed"] == 1
    assert (
        ingest(engine, source_id, location="Mumbai, India", title="Fixture engineer, senior", description=REWORDED)[
            "changed"
        ]
        == 1
    )
    world.add_requirements(engine, job_id, 4, REQUIREMENTS)
    fourth = service.run(user, trigger="nightly", local_date=DAYS[3])

    assert fourth.shown == 1
    last = [d for d in decisions(engine, user) if d.version == 4]
    assert [(d.reasons, d.change_note) for d in last] == [
        (["top_ranked", "material_change"], "Changed 2 times since you saw it on 2026-11-25: The posting text changed.")
    ]


def test_a_new_version_that_ingest_wrote_no_note_for_still_says_the_posting_changed(engine, monkeypatch):
    world.use_provider(monkeypatch, "all_met.json")
    user = world.add_person(engine)
    job = world.add_job(engine, "Role A")
    service.run(user, trigger="nightly", local_date=DAYS[0])
    with engine.begin() as c:  # a version made before ingest named changes: the note is NULL
        c.execute(text("UPDATE jobs SET content_version = 2 WHERE id = :j"), {"j": job.id})
    world.add_requirements(engine, job.id, 2, REQUIREMENTS)

    result = service.run(user, trigger="nightly", local_date=DAYS[1])

    assert result.shown == 1
    (second,) = [d for d in decisions(engine, user) if d.version == 2]
    assert (second.reasons, second.change_note) == (["top_ranked", "material_change"], service.CHANGED)
