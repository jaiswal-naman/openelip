"""Plan 07-01: the small read and link hooks campaigns use in apply, tailor and hiring."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.apply import api as apply_api
from app.hiring import api as hiring_api
from app.platform import db
from app.tailor import api as tailor_api
from app.tailor import service as tailor_service
from tests.campaigns.factories import make_campaign, make_world
from tests.hiring.factories import make_accepted, make_ask, make_candidate, make_open_role, make_org
from tests.match.factories import make_job, make_user
from tests.p5_factories import make_application, make_packet, make_tailorable_record, make_tailored_resume


def one(conn, sql, **params):
    return conn.execute(text(sql), params).scalar_one()


# ---- apply -----------------------------------------------------------------------------------------------------------


def test_link_campaign_sets_the_campaign_of_the_application_and_application_for_campaign_reads_it_back(conn):
    world = make_world(conn)
    application = make_application(conn, world.user, world.job)
    assert apply_api.application_for_campaign(conn, world.campaign) is None  # nothing linked yet
    assert apply_api.link_campaign(conn, application, world.campaign) is True
    assert one(conn, "SELECT campaign_id FROM applications WHERE id = :a", a=application) == world.campaign
    view = apply_api.application_for_campaign(conn, world.campaign)
    assert view is not None and view.id == application and view.job_id == world.job.id
    assert [event.status for event in view.events] == ["applied"]


def test_link_campaign_refuses_a_campaign_of_another_person_and_an_unknown_one(conn):
    world = make_world(conn)
    stranger = make_world(conn)
    application = make_application(conn, world.user, world.job)
    assert apply_api.link_campaign(conn, application, stranger.campaign) is False
    assert apply_api.link_campaign(conn, application, uuid4()) is False
    assert apply_api.link_campaign(conn, uuid4(), world.campaign) is False
    assert one(conn, "SELECT campaign_id FROM applications WHERE id = :a", a=application) is None


def test_open_application_for_job_returns_the_persons_application_for_that_job_only(conn):
    world = make_world(conn)
    other_job = make_job(
        conn, title="Other", description="Other.", requirements=[("Other", "required")], company_name="Other Demo"
    )
    assert apply_api.open_application_for_job(conn, world.user, world.job.id) is None
    application = make_application(conn, world.user, world.job, status="interview")
    view = apply_api.open_application_for_job(conn, world.user, world.job.id)
    assert view is not None and (view.id, view.status) == (application, "interview")
    assert apply_api.open_application_for_job(conn, world.user, other_job.id) is None
    assert apply_api.open_application_for_job(conn, make_user(conn), world.job.id) is None  # not another person's


def test_mark_packet_approved_approves_a_ready_packet_once_with_a_time(conn):
    world = make_world(conn)
    packet = make_packet(conn, world.user, world.job, status="ready")
    assert apply_api.mark_packet_approved(conn, packet) is True
    first = conn.execute(
        text("SELECT status::text AS state, approved_at AS at FROM application_packets WHERE id = :p"), {"p": packet}
    ).one()
    assert first.state == "approved" and first.at is not None
    assert apply_api.mark_packet_approved(conn, packet) is False  # a repeated approval changes nothing
    again = conn.execute(text("SELECT approved_at AS at FROM application_packets WHERE id = :p"), {"p": packet}).one()
    assert again.at == first.at


@pytest.mark.parametrize("status", ["draft", "superseded"])
def test_mark_packet_approved_leaves_a_packet_that_is_not_ready_alone(conn, status):
    world = make_world(conn)
    packet = make_packet(conn, world.user, world.job, status=status)
    assert apply_api.mark_packet_approved(conn, packet) is False
    assert one(conn, "SELECT status::text FROM application_packets WHERE id = :p", p=packet) == status
    assert apply_api.mark_packet_approved(conn, uuid4()) is False


def test_request_packet_and_draft_packet_are_the_packet_functions_themselves(conn):
    """Keyword arguments reach apply.packet unchanged because nothing stands between the caller and it."""
    from app.apply import packet

    assert apply_api.request_packet is packet.request and apply_api.draft_packet is packet.draft


# ---- tailor ----------------------------------------------------------------------------------------------------------


@pytest.fixture
def on_test_connection(conn, monkeypatch):
    """tailor.api.generate opens a transaction of its own; hand it the test's rolled-back connection instead."""

    @contextmanager
    def transaction(user_id=None, role="api"):
        assert role == "worker"
        yield conn

    monkeypatch.setattr(db, "transaction", transaction)


def refuse_model(monkeypatch):
    def called(*args, **kwargs):
        raise AssertionError("a model call was made")

    monkeypatch.setattr(tailor_service.pipeline, "propose_and_verify", called)


@pytest.mark.parametrize("status", ["ready", "failed"])
def test_generate_on_a_tailored_resume_that_is_not_generating_calls_nothing_and_writes_nothing(
    conn, on_test_connection, monkeypatch, status
):
    world = make_world(conn)
    make_tailorable_record(conn, world.user)
    resume = make_tailored_resume(conn, world.user, world.job, status=status)
    before = conn.execute(text("SELECT to_jsonb(t) FROM tailored_resumes t WHERE id = :i"), {"i": resume}).scalar_one()
    refuse_model(monkeypatch)
    assert tailor_api.generate(resume) is None
    after = conn.execute(text("SELECT to_jsonb(t) FROM tailored_resumes t WHERE id = :i"), {"i": resume}).scalar_one()
    assert after == before
    assert one(conn, "SELECT count(*) FROM resume_edits WHERE tailored_resume_id = :i", i=resume) == 0


def test_generate_on_an_unknown_id_does_nothing(conn, on_test_connection, monkeypatch):
    refuse_model(monkeypatch)
    assert tailor_api.generate(uuid4()) is None


# ---- hiring ----------------------------------------------------------------------------------------------------------


def accepted_introduction(conn):
    org = make_org(conn)
    role = make_open_role(conn, org)
    candidate = make_candidate(conn)
    return role, candidate, make_accepted(conn, role, candidate)


def test_introduction_ref_names_the_people_the_job_the_status_and_the_answer_clock(conn):
    role, candidate, accepted = accepted_introduction(conn)
    ref = hiring_api.introduction_ref(conn, accepted.id)
    assert ref is not None
    assert (ref.user_id, ref.organization_id, ref.role_id, ref.job_id) == (candidate.id, role.org, role.id, role.job_id)
    assert (ref.status, ref.employer_answer) == ("accepted", None)
    assert ref.answer_due_at == accepted.answer_due_at


def test_introduction_ref_shows_the_employers_answer_and_a_plain_ask(conn):
    org = make_org(conn)
    role = make_open_role(conn, org)
    candidate = make_candidate(conn)
    ask = make_ask(conn, role, candidate)
    plain = hiring_api.introduction_ref(conn, ask.id)
    assert plain is not None and (plain.status, plain.answer_due_at, plain.employer_answer) == ("asked", None, None)
    accepted = make_accepted(conn, role, candidate, ask=ask)
    conn.execute(
        text("UPDATE introductions SET employer_answer = 'yes', employer_answer_reason = 'a fit' WHERE id = :i"),
        {"i": accepted.id},
    )
    answered = hiring_api.introduction_ref(conn, accepted.id)
    assert answered is not None and answered.employer_answer == "yes"


def test_introduction_ref_of_an_unknown_id_is_none(conn):
    assert hiring_api.introduction_ref(conn, uuid4()) is None


def test_the_candidate_reads_the_job_through_introduction_role_and_nobody_else_reads_it_at_all(conn):
    role, candidate, accepted = accepted_introduction(conn)
    stranger = make_user(conn)
    conn.execute(text("SET LOCAL ROLE app_api"))
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(candidate.id)})
    ref = hiring_api.introduction_ref(conn, accepted.id)
    assert ref is not None and ref.job_id == role.job_id  # the API login has no access to roles: the function answers
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(stranger)})
    assert hiring_api.introduction_ref(conn, accepted.id) is None  # row-level security hides another person's


def test_the_worker_reads_the_job_with_or_without_the_persons_id_set(conn):
    role, candidate, accepted = accepted_introduction(conn)
    conn.execute(text("SET LOCAL ROLE app_worker"))
    ref = hiring_api.introduction_ref(conn, accepted.id)
    assert ref is not None and ref.job_id == role.job_id
    conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(candidate.id)})
    again = hiring_api.introduction_ref(conn, accepted.id)
    assert again is not None and again.job_id == role.job_id


def test_a_campaign_can_start_at_applied_for_an_introductions_job(conn):
    """The shape 07-05 relies on: the ref gives the job, the campaign row takes the introduction."""
    role, candidate, accepted = accepted_introduction(conn)
    ref = hiring_api.introduction_ref(conn, accepted.id)
    assert ref is not None and ref.job_id is not None
    campaign = make_campaign(conn, ref.user_id, ref.job_id, stage="applied", introduction_id=accepted.id)
    assert one(conn, "SELECT introduction_id FROM campaigns WHERE id = :c", c=campaign) == accepted.id
