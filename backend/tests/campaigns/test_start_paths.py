"""Plan 07-05, CMP-01 (Phase 7 acceptance SC1): the two ways a campaign starts.

`POST /v1/campaigns` after the candidate approves a match makes a `researching` campaign; accepting an introduction
makes one at `applied` a few seconds later, by the `introduction.accepted` job. Both queue the `start` resume job.
Rows are invented; the graph is not run.
"""

from tests.campaigns.test_routes_campaigns import key, ok, refused, rows, scalar, sign_in, start, world  # noqa: F401
from tests.campaigns.test_subscribers import accept_introduction, run_jobs, scene_for_campaign


def test_a_start_from_a_shown_match_stores_the_decision(world):  # noqa: F811
    from tests.match.factories import Verdict, make_record, make_run, make_shown_decision

    with world.engine.begin() as conn:
        make_record(conn, world.ada)
        run = make_run(conn, world.ada)
        decision = make_shown_decision(conn, run, world.ada, world.job, [Verdict("no_evidence")])

    data = ok(start(world), 201)

    stored = rows(world.engine, "SELECT match_decision_id FROM campaigns WHERE id = :c", c=data["id"])
    assert stored[0][0] == decision


def test_a_start_with_no_shown_decision_stores_none(world):  # noqa: F811
    data = ok(start(world), 201)
    assert scalar(world.engine, "SELECT match_decision_id FROM campaigns WHERE id = :c", c=data["id"]) is None


def test_an_application_recorded_before_the_start_is_linked_to_the_new_campaign(world):  # noqa: F811
    from tests.p5_factories import make_application

    with world.engine.begin() as conn:
        application = make_application(conn, world.ada, world.job)

    data = ok(start(world), 201)

    linked = scalar(world.engine, "SELECT campaign_id FROM applications WHERE id = :a", a=application)
    assert str(linked) == data["id"]


def test_a_start_leaves_another_persons_application_for_the_job_alone(world):  # noqa: F811
    from tests.p5_factories import make_application

    with world.engine.begin() as conn:
        theirs = make_application(conn, world.bob, world.job)
    ok(start(world), 201)
    assert scalar(world.engine, "SELECT campaign_id FROM applications WHERE id = :a", a=theirs) is None


def test_accepting_an_introduction_starts_a_campaign_at_applied_that_wakes_with_cause_start(engine, api_client):
    s = scene_for_campaign(engine)
    accept_introduction(api_client, s)

    run_jobs(engine, "campaign.from_introduction")

    stored = rows(engine, "SELECT user_id, job_id, introduction_id, stage::text, budget_microusd FROM campaigns")
    assert [tuple(r) for r in stored] == [(s.candidate.id, s.role.job_id, s.ask.id, "applied", 750_000)]
    campaign = scalar(engine, "SELECT id FROM campaigns")
    keys = rows(engine, "SELECT unique_key FROM queue_jobs WHERE kind = 'campaign.resume'")
    assert [k[0] for k in keys] == [f"campaign.resume:{campaign}:start"]
