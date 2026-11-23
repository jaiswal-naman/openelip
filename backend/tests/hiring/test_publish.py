"""HIR-02 and the publish half of HIR-06: the role service (plan 06-03) -- describe, edit, confirm, publish and close.

The service runs as the real roles: drafts as app_api through db.transaction (row-level security in force), publish and
close in their own worker transaction. Those transactions commit, so the rows are built with the 06-02 factories on a
committing connection and the tables are emptied around each test. The model is the fake provider answering from the
invented recording of 06-02. Everything here is invented.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import text

from app.hiring import roles
from app.hiring.roles import RequirementInput
from app.platform import config, db
from app.platform.errors import ApiError
from app.platform.llm import fake
from tests.conftest import _empty_tables
from tests.hiring import world
from tests.hiring.factories import make_accepted, make_candidate, make_member, make_open_role, make_org, make_role

RECORDING = Path(__file__).parent / "fixtures" / "intake" / "invented_answer_three.json"
DESCRIPTION = (
    "Café Halden Demo ☕ is hiring a data engineer 🚀.\n"
    "What you'll need\n"
    "3+ years of Python.\n"
    "Strong SQL skills.\n"
    "Experience with Airflow is a plus.\n"
    "We offer free lunch."
)
PAY = {"min": 80_000, "max": 120_000, "currency": "USD"}


@pytest.fixture
def clean(engine):
    _empty_tables(engine)
    yield engine
    _empty_tables(engine)


@pytest.fixture
def provider(monkeypatch):
    """The real gateway on the fake provider, which plays the recording of invented_answer_three.json."""
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": ""}
    settings = config.load({**os.environ, **blank, "APP_ENV": "test", "LLM_PROVIDER": "fake"})
    monkeypatch.setattr(config, "get", lambda: settings)
    fake.replay_from(RECORDING)
    yield
    fake.replay_from(None)


def api(user):
    return db.transaction(user)


def refused(call, status, code):
    with pytest.raises(ApiError) as error:
        call()
    assert (error.value.status, error.value.code) == (status, code), error.value.details
    return error.value


def build(engine, **over):
    """An organization and a ready draft: confirmed requirements and a pay range."""
    with engine.begin() as conn:
        org = make_org(conn)
        role = make_role(conn, org, confirmed=True, pay=(80_000, 120_000, "USD"), **over)
    return org, role


def one(engine, sql, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).all()


# ---- publish ----------------------------------------------------------------------------------------------------


def test_publishing_a_ready_draft_opens_it_as_one_unlisted_job_and_queues_the_ask_once(clean):
    org, role = build(clean)

    roles.publish(org.owner, role.id)

    opened = one(clean, "SELECT status::text, job_id, opened_at FROM roles WHERE id = :id", id=role.id)[0]
    assert opened.status == "open" and opened.job_id is not None and opened.opened_at is not None
    job = one(
        clean, "SELECT is_listed, status::text, company_name, external_id FROM jobs WHERE id = :id", id=opened.job_id
    )
    assert [tuple(row) for row in job] == [(False, "open", org.name, str(role.id))]
    queued = one(clean, "SELECT kind, unique_key, queue, payload FROM queue_jobs WHERE kind = 'hiring.ask_candidates'")
    assert [(q.unique_key, q.queue, q.payload) for q in queued] == [
        (f"hiring.ask:{role.id}:published", "llm", {"role_id": str(role.id)})
    ]
    audit = one(
        clean, "SELECT event_type, organization_id, details FROM audit_log WHERE event_type = 'hiring.role_published'"
    )
    assert [(a.event_type, a.organization_id) for a in audit] == [("hiring.role_published", org.id)]
    assert set(audit[0].details) == {"role_id", "job_id"}  # ids only


def test_the_jobs_requirements_are_the_roles_with_spans_that_resolve_and_typed_items_are_appended(clean):
    org, role = build(clean)
    typed = RequirementInput("authorization", "Right to work in India", "required", True)
    with api(org.owner) as conn:
        current = roles.get(conn, org.owner, role.id)["requirements"]
        items = [
            RequirementInput(r["kind"], r["text"], r["importance"], True, (r["span"]["start"], r["span"]["end"]))
            for r in current
        ]
        roles.set_requirements(conn, org.owner, role.id, [*items, typed])

    roles.publish(org.owner, role.id)

    job = one(
        clean, "SELECT j.id, j.description FROM jobs j JOIN roles r ON r.job_id = j.id WHERE r.id = :id", id=role.id
    )[0]
    rows = one(
        clean,
        "SELECT ordinal, text, importance::text AS importance, lower(span) AS s, upper(span) AS e "
        "FROM job_requirements "
        "WHERE job_id = :id ORDER BY ordinal",
        id=job.id,
    )
    assert [r.ordinal for r in rows] == [1, 2, 3, 4]
    assert all(job.description[r.s : r.e] == r.text for r in rows)
    assert rows[3].text == "Right to work in India" and rows[3].s > job.description.index(
        "Requirements added by the employer"
    )
    assert job.description.startswith(role.description.rstrip())  # extracted items keep their spans in the role's text
    assert [r.s for r in rows[:3]] == [role.description.index(r.text) for r in rows[:3]]


def test_publishing_twice_is_a_conflict_in_state_open_and_queues_nothing_more(clean):
    org, role = build(clean)
    roles.publish(org.owner, role.id)

    error = refused(lambda: roles.publish(org.owner, role.id), 409, "conflict")

    assert error.details == {"state": "open"}
    assert len(one(clean, "SELECT 1 FROM queue_jobs WHERE kind = 'hiring.ask_candidates'")) == 1
    assert len(one(clean, "SELECT 1 FROM jobs WHERE dedupe_key LIKE 'direct:%'")) == 1


def test_without_a_pay_range_publishing_says_so_and_lists_every_blocker(clean):
    with clean.begin() as conn:
        org = make_org(conn)
        role = make_role(conn, org)  # no pay, nothing confirmed

    error = refused(lambda: roles.publish(org.owner, role.id), 422, "pay_range_required")

    assert error.details == {"blockers": ["pay_range_required", "requirements_unconfirmed"]}
    with api(org.owner) as conn:
        assert roles.blockers(conn, org.owner, role.id) == ["pay_range_required", "requirements_unconfirmed"]
    assert one(clean, "SELECT status::text FROM roles WHERE id = :id", id=role.id)[0][0] == "draft"
    assert one(clean, "SELECT 1 FROM jobs") == [] and one(clean, "SELECT 1 FROM queue_jobs") == []


def test_with_a_pay_range_but_unconfirmed_requirements_the_code_is_requirements_unconfirmed(clean):
    with clean.begin() as conn:
        org = make_org(conn)
        role = make_role(conn, org, pay=(80_000, 120_000, "USD"))

    error = refused(lambda: roles.publish(org.owner, role.id), 422, "requirements_unconfirmed")

    assert error.details == {"blockers": ["requirements_unconfirmed"]}


def test_one_unconfirmed_item_blocks_even_when_the_list_was_confirmed_before(clean):
    org, role = build(clean)
    with clean.begin() as conn:  # the database lets a row be flipped by hand; the service re-checks the items
        conn.execute(
            text("UPDATE role_requirements SET is_confirmed = false WHERE id = :id"), {"id": role.requirement_ids[0]}
        )

    refused(lambda: roles.publish(org.owner, role.id), 422, "requirements_unconfirmed")


def test_an_overdue_answer_blocks_publishing_with_a_conflict_and_changes_nothing(clean):
    with clean.begin() as conn:
        org = make_org(conn)
        owed = make_accepted(conn, make_open_role(conn, org), make_candidate(conn))
        world.overdue(conn, owed.id)
        draft = make_role(conn, org, confirmed=True, pay=(80_000, 120_000, "USD"))

    error = refused(lambda: roles.publish(org.owner, draft.id), 409, "conflict")

    assert error.details == {"state": "answers_overdue"}
    assert one(clean, "SELECT status::text FROM roles WHERE id = :id", id=draft.id)[0][0] == "draft"
    assert one(clean, "SELECT 1 FROM queue_jobs WHERE kind = 'hiring.ask_candidates'") == []


def test_a_member_of_another_organization_gets_not_found_from_every_function(clean):
    org, role = build(clean)
    with clean.begin() as conn:
        stranger = make_org(conn, "Brightwell Demo").owner
    with api(stranger) as conn:
        refused(lambda: roles.get(conn, stranger, role.id), 404, "not_found")
        refused(lambda: roles.blockers(conn, stranger, role.id), 404, "not_found")
        refused(lambda: roles.update(conn, stranger, role.id, {"title": "Taken"}), 404, "not_found")
        refused(lambda: roles.set_requirements(conn, stranger, role.id, []), 404, "not_found")
        refused(lambda: roles.list_for_org(conn, stranger, org.id), 404, "not_found")
        refused(
            lambda: roles.create(conn, stranger, org_id=org.id, title="Taken", description="Taken."), 404, "not_found"
        )
    refused(lambda: roles.publish(stranger, role.id), 404, "not_found")
    refused(lambda: roles.close(stranger, role.id), 404, "not_found")
    assert one(clean, "SELECT status::text, title FROM roles WHERE id = :id", id=role.id)[0].title == role.title


def test_a_removed_member_can_no_longer_publish(clean):
    org, role = build(clean)
    with clean.begin() as conn:
        colleague = make_member(conn, org)
        conn.execute(
            text("UPDATE org_members SET status = 'removed', removed_at = now() WHERE id = :id"), {"id": colleague.id}
        )

    refused(lambda: roles.publish(colleague.user, role.id), 404, "not_found")


# ---- close ------------------------------------------------------------------------------------------------------


def test_closing_withdraws_open_asks_keeps_accepted_ones_and_closes_the_job(clean):
    with clean.begin() as conn:
        org = make_org(conn)
        role = make_open_role(conn, org)
        asked = make_candidate(conn)
        waiting = world_ask(conn, role, asked)
        accepted = make_accepted(conn, role, make_candidate(conn))

    roles.close(org.owner, role.id)

    states = dict(one(clean, "SELECT id, status::text FROM introductions"))
    assert states == {waiting: "withdrawn", accepted.id: "accepted"}
    closed = one(clean, "SELECT status::text, closed_at FROM roles WHERE id = :id", id=role.id)[0]
    assert closed.status == "closed" and closed.closed_at is not None
    job = one(clean, "SELECT status::text, closed_at FROM jobs WHERE id = :id", id=role.job_id)[0]
    assert job.status == "closed" and job.closed_at is not None
    audit = one(clean, "SELECT organization_id, details FROM audit_log WHERE event_type = 'hiring.role_closed'")
    assert [(a.organization_id, a.details["withdrawn"]) for a in audit] == [(org.id, 1)]
    # the accepted introduction still owes an answer: the clock keeps running after the role closes
    owed = one(
        clean,
        "SELECT 1 FROM introductions WHERE status = 'accepted' AND employer_answer IS NULL "
        "AND answer_due_at IS NOT NULL",
    )
    assert len(owed) == 1


def test_closing_a_closed_role_is_a_conflict_in_state_closed_and_a_draft_has_nothing_to_close(clean):
    org, draft = build(clean)
    with clean.begin() as conn:
        role = make_open_role(conn, org)
    roles.close(org.owner, role.id)

    assert refused(lambda: roles.close(org.owner, role.id), 409, "conflict").details == {"state": "closed"}
    assert refused(lambda: roles.close(org.owner, draft.id), 409, "conflict").details == {"state": "draft"}


def world_ask(conn, role, candidate):
    from tests.hiring.factories import make_ask

    return make_ask(conn, role, candidate).id


# ---- drafts: create, edit, confirm -------------------------------------------------------------------------------


def test_create_stores_a_draft_with_the_extracted_requirements_and_their_spans(clean, provider):
    with clean.begin() as conn:
        org = make_org(conn)

    with api(org.owner) as conn:
        made = roles.create(conn, org.owner, org_id=org.id, title="Data engineer", description=DESCRIPTION)

    assert made["status"] == "draft" and made["pay_range"] is None and made["can_publish"] is False
    assert made["blockers"] == ["pay_range_required", "requirements_unconfirmed"]
    assert made["extraction"] == {"state": "extracted", "dropped": 0}
    assert [r["text"] for r in made["requirements"]] == [
        "3+ years of Python.",
        "Strong SQL skills",
        "Airflow is a plus",
    ]
    assert all(not r["is_confirmed"] and r["quote"] == r["text"] for r in made["requirements"])
    assert all(DESCRIPTION[r["span"]["start"] : r["span"]["end"]] == r["text"] for r in made["requirements"])
    assert one(clean, "SELECT created_by FROM roles")[0][0] == org.owner


def test_with_no_model_the_role_is_stored_with_no_requirements_and_says_ai_disabled(clean, monkeypatch):
    blank = {"ANTHROPIC_API_KEY": "", "OPENAI_API_KEY": "", "LLM_BASE_URL": "", "LLM_PROVIDER": ""}
    monkeypatch.setattr(config, "get", lambda: config.load({**os.environ, **blank, "APP_ENV": "test"}))
    with clean.begin() as conn:
        org = make_org(conn)

    with api(org.owner) as conn:
        made = roles.create(conn, org.owner, org_id=org.id, title="Data engineer", description=DESCRIPTION)

    assert made["extraction"] == {"state": "ai_disabled", "dropped": 0} and made["requirements"] == []


def test_a_description_change_reads_again_keeps_typed_items_and_clears_every_confirmation(clean, provider):
    with clean.begin() as conn:
        org = make_org(conn)
    with api(org.owner) as conn:
        made = roles.create(conn, org.owner, org_id=org.id, title="Data engineer", description=DESCRIPTION)
        items = [
            RequirementInput(r["kind"], r["text"], r["importance"], True, (r["span"]["start"], r["span"]["end"]))
            for r in made["requirements"]
        ]
        typed = RequirementInput("authorization", "Right to work in India", "required", True)
        confirmed = roles.set_requirements(conn, org.owner, made["id"], [*items, typed])
    assert confirmed["blockers"] == ["pay_range_required"]
    fake.replay_from(RECORDING)

    with api(org.owner) as conn:
        changed = roles.update(conn, org.owner, made["id"], {"description": DESCRIPTION + "\nNo travel."})

    assert changed["extraction"] == {"state": "extracted", "dropped": 0}
    texts = [r["text"] for r in changed["requirements"]]
    assert texts == ["3+ years of Python.", "Strong SQL skills", "Airflow is a plus", "Right to work in India"]
    assert [r["span"] is None for r in changed["requirements"]] == [False, False, False, True]
    assert not any(r["is_confirmed"] for r in changed["requirements"])
    assert changed["blockers"] == ["pay_range_required", "requirements_unconfirmed"]
    assert one(clean, "SELECT requirements_confirmed_at FROM roles")[0][0] is None
    assert [r.ordinal for r in one(clean, "SELECT ordinal FROM role_requirements ORDER BY ordinal")] == [1, 2, 3, 4]


def test_a_title_or_pay_change_does_not_read_again_and_keeps_the_confirmation(clean):
    org, role = build(clean)

    with api(org.owner) as conn:
        changed = roles.update(
            conn, org.owner, role.id, {"title": "Staff engineer", "pay_range": {**PAY, "max": 150_000}}
        )

    assert changed["extraction"] is None and changed["title"] == "Staff engineer"
    assert changed["pay_range"] == {"min": 80_000, "max": 150_000, "currency": "USD"}
    assert changed["can_publish"] is True


def test_a_pay_range_can_be_cleared_and_the_role_is_blocked_again(clean):
    org, role = build(clean)

    with api(org.owner) as conn:
        changed = roles.update(conn, org.owner, role.id, {"pay_range": None})

    assert changed["pay_range"] is None and changed["blockers"] == ["pay_range_required"]


def test_an_open_or_closed_role_cannot_be_changed(clean):
    org, role = build(clean)
    roles.publish(org.owner, role.id)
    with api(org.owner) as conn:
        assert refused(lambda: roles.update(conn, org.owner, role.id, {"title": "New"}), 409, "conflict").details == {
            "state": "open"
        }
        refused(lambda: roles.set_requirements(conn, org.owner, role.id, []), 409, "conflict")
    roles.close(org.owner, role.id)
    with api(org.owner) as conn:
        assert refused(lambda: roles.update(conn, org.owner, role.id, {"title": "New"}), 409, "conflict").details == {
            "state": "closed"
        }


def test_an_item_that_is_no_longer_its_span_loses_the_span_and_the_list_is_confirmed_only_when_whole(clean):
    org, role = build(clean)
    with api(org.owner) as conn:
        current = roles.get(conn, org.owner, role.id)["requirements"]
        first = current[0]
        span = (first["span"]["start"], first["span"]["end"])
        edited = RequirementInput("skill", "Python, any version", "required", True, span)
        kept = RequirementInput(
            "skill", current[1]["text"], "required", True, (current[1]["span"]["start"], current[1]["span"]["end"])
        )
        done = roles.set_requirements(conn, org.owner, role.id, [edited, kept])

        assert [r["span"] is None for r in done["requirements"]] == [True, False]  # edited text, so no span
        assert done["can_publish"] is True and [r["quote"] for r in done["requirements"]][0] is None

        partly = roles.set_requirements(
            conn, org.owner, role.id, [edited, RequirementInput("skill", "SQL", "required", False)]
        )
        assert partly["blockers"] == ["requirements_unconfirmed"]

        empty = roles.set_requirements(conn, org.owner, role.id, [])
        assert empty["requirements"] == [] and empty["blockers"] == ["requirements_unconfirmed"]


def test_more_than_thirty_requirements_are_refused(clean):
    org, role = build(clean)
    too_many = [RequirementInput("skill", f"Skill {n}", "preferred", True) for n in range(31)]

    with api(org.owner) as conn:
        refused(lambda: roles.set_requirements(conn, org.owner, role.id, too_many), 422, "validation_failed")


def test_the_list_shows_each_role_with_what_is_owed(clean):
    with clean.begin() as conn:
        org = make_org(conn)
        role = make_open_role(conn, org, title="Platform engineer")
        owed = make_accepted(conn, role, make_candidate(conn))
        late = make_accepted(conn, role, make_candidate(conn))
        world.overdue(conn, late.id)
        make_role(conn, org, title="Draft role")

    with api(org.owner) as conn:
        listed = roles.list_for_org(conn, org.owner, org.id)

    by_title = {r["title"]: r for r in listed}
    assert by_title["Draft role"]["status"] == "draft" and by_title["Draft role"]["said_yes"] == 0
    platform = by_title["Platform engineer"]
    assert (platform["status"], platform["said_yes"], platform["answers_owed"], platform["answers_overdue"]) == (
        "open",
        2,
        2,
        1,
    )
    assert owed.id is not None
