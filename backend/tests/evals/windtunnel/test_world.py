"""The world builder: invented, synthetic, independent per attempt, and written only to a test database."""

import pytest
from sqlalchemy import text

from app.evals.windtunnel import guard, scenario, world
from app.hiring import share
from app.platform.llm import fake
from tests.evals.windtunnel import scenario_text
from tests.evals.windtunnel.conftest import empty


@pytest.fixture(autouse=True)
def tables(engine):
    empty(engine)
    yield
    empty(engine)
    fake.replay_from(None)


def build(engine, tmp_path, start="match", attempt=1, key="unit-a", host="lumen.example"):
    loaded = scenario.load(scenario_text.write(tmp_path, key, start=start, host=host))
    with engine.begin() as conn:
        return loaded, world.build(conn, loaded, attempt, "t1")


def count(engine, sql, **values):
    with engine.connect() as c:
        return c.execute(text(sql), values).scalar_one()


def test_a_match_start_world_has_a_synthetic_candidate_a_record_a_job_and_a_shown_match(engine, tmp_path):
    loaded, made = build(engine, tmp_path)
    assert made.candidate_email == "wt-t1-unit-a-1@example.test" and made.employer_id is None
    user = count(
        engine,
        "SELECT row(is_synthetic, undo_window_minutes, record_confirmed_at IS NOT NULL)::text FROM users WHERE id = :u",
        u=made.user_id,
    )
    assert user == "(t,1,t)"
    assert (
        count(
            engine, "SELECT count(*) FROM profile_facts WHERE user_id = :u AND confirmed_at IS NOT NULL", u=made.user_id
        )
        == 2
    )
    assert count(engine, "SELECT count(*) FROM constraints WHERE user_id = :u", u=made.user_id) == 2
    assert (
        count(engine, "SELECT count(*) FROM consents WHERE user_id = :u AND purpose = 'ai_processing'", u=made.user_id)
        >= 1
    )
    assert (
        count(engine, "SELECT apply_url FROM jobs WHERE id = :j", j=made.job_id)
        == "https://careers.lumen.example/apply/1"
    )
    assert count(engine, "SELECT count(*) FROM job_requirements WHERE job_id = :j", j=made.job_id) == 2
    assert (
        count(engine, "SELECT count(*) FROM match_decisions WHERE user_id = :u AND outcome = 'shown'", u=made.user_id)
        == 1
    )
    assert count(engine, "SELECT count(*) FROM match_evidence WHERE user_id = :u", u=made.user_id) == 2
    assert (
        count(engine, "SELECT count(*) FROM tailored_resumes WHERE user_id = :u AND status = 'ready'", u=made.user_id)
        == 1
    )
    assert made.typed_address == "talent@lumen.example" and made.canaries["phone"] == "+1 415 555 0100"
    assert fake.is_registered(made.user_id)
    assert loaded.recorded.name == "recorded"


def test_the_start_path_finds_the_match_through_the_real_decision_view(engine, tmp_path):
    from app.match import api as match
    from app.platform import db

    _, made = build(engine, tmp_path)
    with db.transaction(made.user_id) as conn:
        view = match.decision_for_job(conn, made.user_id, made.job_id)
    assert view is not None and view.outcome == "shown"


def test_an_introduction_start_world_adds_an_employer_an_organization_a_role_and_an_asked_introduction(
    engine, tmp_path
):
    _, made = build(engine, tmp_path, start="introduction")
    assert made.employer_id and made.org_id and made.role_id and made.introduction_id
    assert count(engine, "SELECT is_synthetic FROM users WHERE id = :u", u=made.employer_id)
    assert count(engine, "SELECT status::text FROM org_members WHERE user_id = :u", u=made.employer_id) == "active"
    assert count(engine, "SELECT status::text FROM roles WHERE id = :r", r=made.role_id) == "open"
    row = count(engine, "SELECT status::text FROM introductions WHERE id = :i", i=made.introduction_id)
    assert row == "asked"
    preview = count(engine, "SELECT share_preview FROM introductions WHERE id = :i", i=made.introduction_id)
    assert share.preview_hash(preview) == made.share_preview_hash
    assert preview["on_employer_yes"]["email"] == made.candidate_email
    assert [e["verdict"] for e in preview["on_accept"]["evidence"]] == ["met", "met"]
    assert (
        count(
            engine,
            "SELECT count(*) FROM consents WHERE user_id = :u AND purpose = 'open_to_introductions'",
            u=made.user_id,
        )
        >= 1
    )
    assert (
        count(engine, "SELECT count(*) FROM match_decisions WHERE user_id = :u AND outcome = 'shown'", u=made.user_id)
        == 0
    )


def test_two_attempts_share_no_row(engine, tmp_path):
    loaded, first = build(engine, tmp_path, attempt=1)
    with engine.begin() as conn:
        second = world.build(conn, loaded, 2, "t1")
    assert first.user_id != second.user_id and first.job_id != second.job_id
    assert first.candidate_email != second.candidate_email
    for table in ("profile_facts", "constraints", "match_decisions", "tailored_resumes"):
        both = count(
            engine,
            f"SELECT count(DISTINCT user_id) FROM {table} WHERE user_id = ANY(:ids)",
            ids=[first.user_id, second.user_id],
        )
        assert both == 2, table
    assert count(engine, "SELECT count(*) FROM jobs WHERE id = ANY(:ids)", ids=[first.job_id, second.job_id]) == 2


def test_release_returns_the_candidate_to_the_ordinary_rules(engine, tmp_path):
    _, made = build(engine, tmp_path)
    assert fake.is_registered(made.user_id)
    world.release(made)
    assert not fake.is_registered(made.user_id)


def test_it_writes_nothing_outside_a_test_database(monkeypatch, tmp_path, engine):
    loaded = scenario.load(scenario_text.write(tmp_path))

    class NotTest:
        def execute(self, *args, **kwargs):
            class Result:
                def scalar_one(self):
                    return "app"

            return Result()

    with pytest.raises(guard.NotATestDatabase):
        world.build(NotTest(), loaded, 1, "t1")  # type: ignore[arg-type]
    assert count(engine, "SELECT count(*) FROM users") == 0
