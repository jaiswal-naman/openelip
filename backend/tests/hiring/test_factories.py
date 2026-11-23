"""The shared hiring factories (plan 06-02): one call each builds rows that every database guard accepts, and every one
of them refuses a database whose name does not end in _test.

Each test builds on the `conn` fixture's rolled-back transaction and then looks at the rows the way the later plans'
code will: as the employer's member, as the candidate, or as the worker. Nothing here is a real person or company.
"""

import inspect
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text

from app.trust import api as trust
from tests.hiring import factories, world
from tests.hiring.factories import (
    make_accepted,
    make_ask,
    make_candidate,
    make_introduced_decision,
    make_member,
    make_open_role,
    make_org,
    make_role,
)
from tests.match.factories import Verdict


def one(conn, sql, **params):
    return conn.execute(text(sql), params).one()


def resolves(conn, role_id):
    """For each requirement of a role (in order): its text, importance, confirmed flag and whether the text is the
    description at its span, in code points, which is how PostgreSQL substr counts."""
    return [
        tuple(row)
        for row in conn.execute(
            text(
                """SELECT q.text, q.importance::text, q.is_confirmed,
                          substr(r.description, lower(q.span) + 1, upper(q.span) - lower(q.span)) = q.text
                   FROM role_requirements q JOIN roles r ON r.id = q.role_id
                   WHERE q.role_id = :r ORDER BY q.ordinal"""
            ),
            {"r": role_id},
        )
    ]


# ---- organizations and members ----------------------------------------


def test_an_organization_is_invented_synthetic_and_has_an_active_owner(conn):
    org = make_org(conn)
    row = one(conn, "SELECT name, is_synthetic, timezone, created_by FROM organizations WHERE id = :o", o=org.id)
    assert (row.name, row.is_synthetic, row.timezone, row.created_by) == ("Halden Demo", True, "UTC", org.owner)
    owner = one(conn, "SELECT email, display_name, is_synthetic FROM users WHERE id = :u", u=org.owner)
    assert owner.email.endswith("@example.test") and owner.display_name.startswith("Demo ") and owner.is_synthetic
    member = one(
        conn,
        "SELECT role::text AS role, status::text AS status, joined_at FROM org_members WHERE organization_id = :o",
        o=org.id,
    )
    assert (member.role, member.status) == ("owner", "active") and member.joined_at is not None


def test_the_owner_sees_their_organization_and_a_second_organization_sees_nothing_of_it(conn):
    first, second = make_org(conn), make_org(conn, "Brightwell Demo", timezone="America/Chicago")
    world.act_as(conn, "app_api", first.owner)
    assert world.count(conn, "organizations") == 1
    assert one(conn, "SELECT app_is_org_member(:o) AS member", o=second.id).member is False


def test_a_synthetic_organization_must_say_demo_and_a_real_one_may_not_pretend_otherwise(conn):
    with pytest.raises(ValueError, match="Demo"):
        make_org(conn, "Halden Holdings")
    real = make_org(conn, "Halden Holdings", is_synthetic=False)
    assert one(conn, "SELECT is_synthetic FROM organizations WHERE id = :o", o=real.id).is_synthetic is False


def test_a_member_is_active_with_the_role_asked_for_and_joins_after_the_owner(conn):
    org = make_org(conn)
    admin = make_member(conn, org, role="admin")
    assert (admin.org, admin.role, admin.status) == (org.id, "admin", "active") and admin.user is not None
    row = one(
        conn,
        "SELECT role::text AS role, status::text AS status, joined_at FROM org_members WHERE id = :m",
        m=admin.id,
    )
    assert (row.role, row.status) == ("admin", "active")
    owner_joined = one(conn, "SELECT joined_at FROM org_members WHERE organization_id = :o AND role = 'owner'", o=org.id)
    assert owner_joined.joined_at < row.joined_at  # one transaction, but the clock keeps counting: ordering is testable
    world.act_as(conn, "app_api", admin.user)
    assert one(conn, "SELECT app_is_org_admin(:o) AS admin", o=org.id).admin is True


def test_an_invitation_is_an_address_with_no_person_that_the_invited_account_can_list(conn):
    org = make_org(conn)
    invitation = make_member(conn, org, invited=True, email="demo.invitee@example.test")
    row = one(
        conn,
        "SELECT user_id, invited_email, status::text AS status, role::text AS role, invited_by, joined_at "
        "FROM org_members WHERE id = :m",
        m=invitation.id,
    )
    assert (row.user_id, row.invited_email, row.status, row.role) == (None, "demo.invitee@example.test", "invited", "member")
    assert (row.invited_by, row.joined_at) == (org.owner, None)
    assert (invitation.user, invitation.status, invitation.email) == (None, "invited", "demo.invitee@example.test")
    invitee = make_candidate(conn, email="demo.invitee@example.test")
    world.act_as(conn, "app_api", invitee.id)
    listed = conn.execute(text("SELECT member_id, organization_id FROM my_org_invitations()")).all()
    assert [tuple(r) for r in listed] == [(invitation.id, org.id)]


# ---- roles ----------------------------------------


def test_a_draft_role_has_unconfirmed_requirements_whose_spans_resolve(conn):
    org = make_org(conn)
    role = make_role(conn, org)
    row = one(
        conn,
        "SELECT status::text AS status, job_id, requirements_confirmed_at, salary_min, created_by, opened_at "
        "FROM roles WHERE id = :r",
        r=role.id,
    )
    assert (row.status, row.job_id, row.requirements_confirmed_at, row.salary_min) == ("draft", None, None, None)
    assert (row.created_by, row.opened_at) == (org.owner, None)
    assert resolves(conn, role.id) == [
        ("Python is required.", "required", False, True),
        ("SQL is required.", "required", False, True),
        ("Experience with Airflow is a plus.", "preferred", False, True),
    ]
    assert (role.job_id, role.job_requirement_ids, len(role.requirement_ids)) == (None, (), 3)


def test_a_draft_can_be_made_ready_to_publish_with_a_pay_range_and_every_item_confirmed(conn):
    role = make_role(conn, make_org(conn), confirmed=True, pay=(70_000, 90_000, "EUR"))
    row = one(
        conn,
        "SELECT status::text AS status, requirements_confirmed_at, salary_min, salary_max, salary_currency "
        "FROM roles WHERE id = :r",
        r=role.id,
    )
    assert (row.status, row.salary_min, row.salary_max, row.salary_currency) == ("draft", 70_000, 90_000, "EUR")
    assert row.requirements_confirmed_at is not None
    assert all(item[2] for item in resolves(conn, role.id))


def test_spans_are_counted_in_code_points_so_an_emoji_before_a_requirement_does_not_shift_them(conn):
    description = "Café Halden Demo ☕ is hiring 🚀🚀.\nPython is required."
    role = make_role(conn, make_org(conn), description=description, requirements=[("Python is required.", "required")])
    assert resolves(conn, role.id) == [("Python is required.", "required", False, True)]
    start = one(conn, "SELECT lower(span) AS start FROM role_requirements WHERE role_id = :r", r=role.id).start
    assert start == description.index("Python") and start != len(description[: start].encode("utf-16-le")) // 2


def test_a_requirement_that_is_not_in_the_description_is_refused_at_once(conn):
    with pytest.raises(ValueError, match="Rust"):
        make_role(conn, make_org(conn), requirements=[("Rust is required.", "required")])


def test_an_open_role_is_published_as_an_unlisted_direct_job_with_requirements_and_spans(conn):
    org = make_org(conn)
    role = make_open_role(conn, org)
    row = one(
        conn,
        "SELECT status::text AS status, salary_min, salary_max, salary_currency, requirements_confirmed_at, job_id, "
        "opened_at, created_by FROM roles WHERE id = :r",
        r=role.id,
    )
    assert (row.status, row.salary_min, row.salary_max, row.salary_currency) == ("open", 80_000, 120_000, "USD")
    assert (row.job_id, row.created_by) == (role.job_id, org.owner)
    assert row.requirements_confirmed_at is not None and row.opened_at is not None
    assert all(item[2] for item in resolves(conn, role.id))  # every item confirmed

    job = one(
        conn,
        """SELECT j.is_listed, j.status::text AS status, j.external_id, j.dedupe_key, j.content_version,
                  j.requirements_version, j.company_name, j.title, j.salary_min, j.apply_url, s.ats::text AS ats,
                  s.is_active, s.organization_id, s.token
           FROM jobs j JOIN job_sources s ON s.id = j.source_id WHERE j.id = :j""",
        j=role.job_id,
    )
    assert (job.is_listed, job.status, job.external_id, job.dedupe_key) == (False, "open", str(role.id), f"direct:{role.id}")
    assert (job.content_version, job.requirements_version, job.company_name) == (1, 1, "Halden Demo")
    assert (job.ats, job.is_active, job.organization_id, job.token) == ("direct", False, org.id, str(org.id))
    assert job.apply_url.endswith("/app/today") and job.salary_min == 80_000

    requirements = conn.execute(
        text(
            """SELECT r.id, r.ordinal, r.importance::text AS importance, r.prompt_version,
                      substr(j.description, lower(r.span) + 1, upper(r.span) - lower(r.span)) = r.text AS resolves
               FROM job_requirements r JOIN jobs j ON j.id = r.job_id WHERE r.job_id = :j ORDER BY r.ordinal"""
        ),
        {"j": role.job_id},
    ).all()
    assert [r.id for r in requirements] == list(role.job_requirement_ids)
    assert [(r.ordinal, r.importance, r.resolves) for r in requirements] == [
        (1, "required", True),
        (2, "required", True),
        (3, "preferred", True),
    ]
    assert {r.prompt_version for r in requirements} == {"employer-confirmed"}


def test_a_second_role_of_the_organization_shares_its_direct_source_and_another_organization_has_its_own(conn):
    org, other = make_org(conn), make_org(conn, "Brightwell Demo")
    first, second, elsewhere = make_open_role(conn, org), make_open_role(conn, org, title="Analyst"), make_open_role(conn, other)
    sources = {
        r.job_id: r.source_id
        for r in conn.execute(text("SELECT id AS job_id, source_id FROM jobs WHERE id = ANY(:j)"), {"j": [first.job_id, second.job_id, elsewhere.job_id]})
    }
    assert sources[first.job_id] == sources[second.job_id] != sources[elsewhere.job_id]
    assert world.count(conn, "job_sources WHERE ats = 'direct'") == 2
    assert world.count(conn, "job_sources WHERE ats = 'direct' AND is_active") == 0  # the index fetch never touches them
    assert world.count(conn, "jobs WHERE is_listed") == 0  # and search lists none of them


def test_an_open_role_can_carry_the_place_and_kind_of_job_a_candidate_s_constraints_are_checked_against(conn):
    role = make_open_role(
        conn, make_org(conn), country="DE", city="Berlin", arrangements=("hybrid",), seniority="senior", employment_type="full_time"
    )
    job = one(
        conn,
        "SELECT country, city, work_arrangements::text[] AS arrangements, seniority::text AS seniority, "
        "employment_type::text AS employment_type FROM jobs WHERE id = :j",
        j=role.job_id,
    )
    assert tuple(job) == ("DE", "Berlin", ["hybrid"], "senior", "full_time")
    role_row = one(conn, "SELECT country, city, work_arrangements::text[] AS a FROM roles WHERE id = :r", r=role.id)
    assert tuple(role_row) == ("DE", "Berlin", ["hybrid"])


# ---- candidates ----------------------------------------


def test_a_candidate_is_an_invented_person_with_a_confirmed_record_and_the_consents_that_open_them_to_asks(conn):
    candidate = make_candidate(conn)
    user = one(
        conn,
        "SELECT email, display_name, is_synthetic, record_confirmed_at, status::text AS status FROM users WHERE id = :u",
        u=candidate.id,
    )
    assert user.email.endswith("@example.test") and user.display_name == "Demo Candidate"
    assert (user.is_synthetic, user.status) == (True, "active") and user.record_confirmed_at is not None
    assert (candidate.name, candidate.email) == ("Demo Candidate", user.email)
    facts = conn.execute(
        text("SELECT id FROM profile_facts WHERE user_id = :u AND confirmed_at IS NOT NULL ORDER BY created_at, id"),
        {"u": candidate.id},
    ).scalars().all()
    assert len(facts) == 3 and set(candidate.record.fact_ids) == set(facts)
    assert one(conn, "SELECT has_active_consent(:u, 'open_to_introductions') AS open", u=candidate.id).open is True
    trust.require_consent(conn, candidate.id, "ai_processing")  # does not raise


def test_a_candidate_can_be_closed_to_introductions_or_stand_in_for_a_real_person(conn):
    closed = make_candidate(conn, open_to_introductions=False)
    assert one(conn, "SELECT has_active_consent(:u, 'open_to_introductions') AS open", u=closed.id).open is False
    trust.require_consent(conn, closed.id, "ai_processing")  # only the introductions consent is left out
    real = make_candidate(conn, email="demo.person@example.com", name="Demo Person", is_synthetic=False)
    row = one(conn, "SELECT email, display_name, is_synthetic FROM users WHERE id = :u", u=real.id)
    assert tuple(row) == ("demo.person@example.com", "Demo Person", False)


# ---- role runs, decisions, asks ----------------------------------------


def test_an_introduced_decision_has_a_completed_role_run_and_one_evidence_row_per_requirement(conn):
    org, candidate = make_org(conn), make_candidate(conn)
    role = make_open_role(conn, org)
    decision = make_introduced_decision(conn, role, candidate)
    run = one(
        conn,
        "SELECT trigger::text AS trigger, role_id, user_id, status::text AS status, finished_at FROM match_runs WHERE id = :r",
        r=decision.run,
    )
    assert (run.trigger, run.role_id, run.user_id, run.status) == ("role", role.id, None, "completed")
    assert run.finished_at is not None
    row = one(
        conn,
        "SELECT user_id, job_id, outcome::text AS outcome, reason_codes::text[] AS reasons, summary, match_run_id "
        "FROM match_decisions WHERE id = :d",
        d=decision.id,
    )
    assert (row.user_id, row.job_id, row.outcome, row.reasons) == (candidate.id, role.job_id, "introduced", ["selected_for_role"])
    assert row.summary and row.match_run_id == decision.run
    evidence = conn.execute(
        text(
            """SELECT e.job_requirement_id, e.verdict::text AS verdict, e.fact_ids
               FROM match_evidence e JOIN job_requirements r ON r.id = e.job_requirement_id
               WHERE e.match_decision_id = :d ORDER BY r.ordinal"""
        ),
        {"d": decision.id},
    ).all()
    assert [e.job_requirement_id for e in evidence] == list(role.job_requirement_ids)
    assert [e.verdict for e in evidence] == ["met", "met", "no_evidence"]  # every required item met, the plus unsupported
    assert [len(e.fact_ids) for e in evidence] == [1, 1, 0]
    assert (decision.user, decision.role, decision.outcome) == (candidate.id, role.id, "introduced")


def test_a_withheld_decision_joins_the_same_run_and_has_no_evidence(conn):
    role = make_open_role(conn, make_org(conn))
    first, second = make_candidate(conn), make_candidate(conn)
    introduced = make_introduced_decision(conn, role, first)
    withheld = make_introduced_decision(conn, role, second, run=introduced.run, outcome="withheld")
    assert withheld.run == introduced.run and withheld.outcome == "withheld"
    row = one(
        conn, "SELECT outcome::text AS outcome, reason_codes::text[] AS reasons, summary FROM match_decisions WHERE id = :d", d=withheld.id
    )
    assert (row.outcome, row.reasons, row.summary) == ("withheld", ["required_not_met"], None)
    assert world.count(conn, "match_evidence WHERE match_decision_id = :d", d=withheld.id) == 0
    assert world.count(conn, "match_runs WHERE role_id = :r", r=role.id) == 1


def test_verdicts_can_be_given_and_an_incomplete_set_fails_in_the_factory_not_in_silence(conn):
    role = make_open_role(conn, make_org(conn))
    candidate = make_candidate(conn)
    fact = candidate.record.fact_ids[1]
    custom = [Verdict("partly_met", (fact,), "A close one."), Verdict("met", (fact,)), Verdict("no_evidence")]
    decision = make_introduced_decision(conn, role, candidate, verdicts=custom)
    got = conn.execute(
        text("SELECT verdict::text, note FROM match_evidence WHERE match_decision_id = :d ORDER BY id"), {"d": decision.id}
    ).all()
    assert [tuple(g) for g in got] == [("partly_met", "A close one."), ("met", None), ("no_evidence", None)]
    with pytest.raises(AssertionError, match="one verdict per requirement"):
        make_introduced_decision(conn, role, make_candidate(conn), verdicts=custom[:2])


def test_an_ask_is_an_asked_introduction_with_a_preview_of_exactly_what_the_candidate_would_share(conn):
    org, candidate = make_org(conn), make_candidate(conn)
    role = make_open_role(conn, org)
    ask = make_ask(conn, role, candidate)
    row = one(
        conn,
        "SELECT status::text AS status, organization_id, role_id, user_id, match_decision_id, asked_at, expires_at, "
        "responded_at, answer_due_at, employer_answer, share_preview FROM introductions WHERE id = :i",
        i=ask.id,
    )
    assert (row.status, row.organization_id, row.role_id, row.user_id) == ("asked", org.id, role.id, candidate.id)
    assert (row.responded_at, row.answer_due_at, row.employer_answer) == (None, None, None)
    assert row.match_decision_id == ask.decision and (row.asked_at, row.expires_at) == (ask.asked_at, ask.expires_at)
    assert one(
        conn,
        "SELECT CAST(:e AS timestamptz) = CAST(:a AS timestamptz) + interval '7 days' AS seven",
        e=ask.expires_at,
        a=ask.asked_at,
    ).seven
    preview = row.share_preview
    assert preview == ask.preview and set(preview) == {"on_accept", "on_employer_yes"}
    assert set(preview["on_accept"]) == {"name", "headline", "evidence", "facts"}
    assert preview["on_employer_yes"] == {"email": candidate.email}
    assert (preview["on_accept"]["name"], preview["on_accept"]["headline"]) == (
        "Demo Candidate",
        "Senior data engineer at Initech Demo, 2019 to 2024.",
    )
    evidence = preview["on_accept"]["evidence"]
    assert [(e["requirement"], e["importance"], e["verdict"]) for e in evidence] == [
        ("Python is required.", "required", "met"),
        ("SQL is required.", "required", "met"),
        ("Experience with Airflow is a plus.", "preferred", "no_evidence"),
    ]
    assert evidence[0]["record_quotes"] == ["Built nightly Python and SQL pipelines that cut report time by 40 percent."]
    assert evidence[2]["record_quotes"] == []
    assert preview["on_accept"]["facts"] == [
        {"kind": "role", "statement": "Senior data engineer at Initech Demo, 2019 to 2024."}
    ]
    assert json.loads(json.dumps(preview)) == preview


def test_the_ask_is_inserted_as_the_worker_and_the_connection_goes_back_to_the_login_it_had(conn):
    role, candidate = make_open_role(conn, make_org(conn)), make_candidate(conn)
    statements: list[str] = []

    def remember(connection, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.split()))

    event.listen(conn, "before_cursor_execute", remember)
    try:
        make_ask(conn, role, candidate)
    finally:
        event.remove(conn, "before_cursor_execute", remember)
    at = next(i for i, s in enumerate(statements) if s.startswith("INSERT INTO introductions"))
    assert "SET LOCAL ROLE app_worker" in statements[:at]
    assert "RESET ROLE" in statements[at + 1 :]
    assert one(conn, "SELECT current_user = session_user AS back").back is True


def test_a_factory_refuses_a_connection_that_has_already_dropped_to_another_role(conn):
    """The rows are built as the bootstrap login: under row-level security the factory's own reads would come back
    empty and the failure would look like a bug in the factory. Build first, then act_as."""
    org = make_org(conn)
    world.act_as(conn, "app_api", org.owner)
    with pytest.raises(RuntimeError, match="act_as"):
        make_candidate(conn)


def test_an_ask_can_be_made_in_the_past_so_that_it_has_already_expired(conn):
    role, candidate = make_open_role(conn, make_org(conn)), make_candidate(conn)
    ask = make_ask(conn, role, candidate, asked_at=one(conn, "SELECT now() - interval '8 days' AS t").t)
    assert one(conn, "SELECT CAST(:e AS timestamptz) < now() AS past", e=ask.expires_at).past is True


def test_the_guard_still_refuses_what_it_should_so_a_factory_cannot_build_a_forbidden_ask(conn):
    org = make_org(conn)
    role = make_open_role(conn, org)
    closed_to_asks = make_candidate(conn, open_to_introductions=False)
    with world.refused(conn) as refused:
        make_ask(conn, role, closed_to_asks)
    assert world.message(refused).startswith("consent_missing")

    blocker = make_candidate(conn)
    world.exclude_company(conn, blocker.id, org.name)
    with world.refused(conn) as refused:
        make_ask(conn, role, blocker)
    assert world.message(refused).startswith("hard_constraint")

    stranger = make_candidate(conn)
    conn.execute(text("UPDATE roles SET status = 'closed', closed_at = now() WHERE id = :r"), {"r": role.id})
    with world.refused(conn) as refused:
        make_ask(conn, role, stranger)
    assert "open role" in world.message(refused)


def test_asks_for_one_role_are_one_per_candidate(conn):
    role, candidate = make_open_role(conn, make_org(conn)), make_candidate(conn)
    make_ask(conn, role, candidate)
    with world.refused(conn) as refused:
        make_ask(conn, role, candidate)
    assert world.constraint_name(refused) == "introductions_once"


# ---- acceptance ----------------------------------------


def test_an_accepted_introduction_has_its_scoped_consent_a_due_time_and_a_shortlist_entry_equal_to_the_preview(conn):
    org, candidate = make_org(conn, timezone="America/Chicago"), make_candidate(conn)
    role = make_open_role(conn, org)
    accepted = make_accepted(conn, role, candidate)
    row = one(
        conn,
        "SELECT status::text AS status, responded_at, answer_due_at, employer_answer, share_preview "
        "FROM introductions WHERE id = :i",
        i=accepted.id,
    )
    assert row.status == "accepted" and row.responded_at is not None and row.employer_answer is None
    assert row.answer_due_at == accepted.answer_due_at
    assert one(
        conn,
        "SELECT add_business_days(CAST(:r AS timestamptz), 5, 'America/Chicago') = CAST(:d AS timestamptz) AS five",
        r=row.responded_at,
        d=row.answer_due_at,
    ).five

    assert [tuple(c) for c in conn.execute(text("SELECT purpose::text, organization_id, role_id, text_version FROM consents WHERE id = :c"), {"c": accepted.consent})] == [
        ("introduction", org.id, role.id, "introduction-2026-11")
    ]
    assert one(conn, "SELECT has_active_consent(:u, 'introduction', :o, :r) AS live", u=candidate.id, o=org.id, r=role.id).live

    entry = one(
        conn,
        "SELECT organization_id, role_id, introduction_id, user_id, shared_payload, contact_released_at "
        "FROM shortlist_entries WHERE id = :s",
        s=accepted.shortlist,
    )
    assert (entry.organization_id, entry.role_id, entry.introduction_id, entry.user_id) == (org.id, role.id, accepted.id, candidate.id)
    assert entry.shared_payload == row.share_preview == accepted.ask.preview and entry.contact_released_at is not None


def test_an_accepted_introduction_can_be_built_from_an_ask_that_already_exists(conn):
    role, candidate = make_open_role(conn, make_org(conn)), make_candidate(conn)
    ask = make_ask(conn, role, candidate)
    accepted = make_accepted(conn, role, candidate, ask=ask)
    assert accepted.ask == ask and accepted.id == ask.id
    assert world.count(conn, "introductions WHERE role_id = :r", r=role.id) == 1


def test_the_employer_sees_the_accepted_candidate_and_not_the_one_who_was_only_asked(conn):
    org = make_org(conn)
    role = make_open_role(conn, org)
    yes, silent = make_candidate(conn), make_candidate(conn)
    accepted = make_accepted(conn, role, yes)
    asked = make_ask(conn, role, silent)
    world.act_as(conn, "app_api", org.owner)
    visible = conn.execute(text("SELECT id FROM introductions")).scalars().all()
    assert visible == [accepted.id] and asked.id not in visible
    assert world.count(conn, "shortlist_entries") == 1
    world.act_as(conn, "app_api", silent.id)
    assert conn.execute(text("SELECT id FROM introductions")).scalars().all() == [asked.id]


def test_the_counts_a_member_sees_come_from_these_rows(conn):
    org = make_org(conn)
    role = make_open_role(conn, org)
    a, b, c = make_candidate(conn), make_candidate(conn), make_candidate(conn)
    first = make_introduced_decision(conn, role, a)
    make_ask(conn, role, a, decision=first)
    make_introduced_decision(conn, role, b, run=first.run, outcome="withheld")
    make_accepted(conn, role, c, ask=make_ask(conn, role, c, decision=make_introduced_decision(conn, role, c, run=first.run)))
    world.act_as(conn, "app_api", org.owner)
    preview = conn.execute(text("SELECT role_preview(:r)"), {"r": role.id}).scalar_one()
    assert preview["eligible_candidates"] == "fewer than 5" and preview["selected_to_ask"] == "fewer than 5"
    assert (preview["waiting_for_reply"], preview["said_yes"], preview["said_no_or_expired"]) == ("fewer than 5", 1, 0)
    assert preview["as_of"] is not None


# ---- the database name check ----------------------------------------


class Elsewhere:
    """A connection to a database called app: the one place invented rows must never go."""

    def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalar_one=lambda: "app")


def test_every_factory_refuses_a_database_whose_name_does_not_end_in_test():
    names = sorted(name for name in dir(factories) if name.startswith("make_"))
    assert len(names) == 8  # a factory added later is covered too: it is found by its name
    for name in names:
        function = getattr(factories, name)
        required = [
            p for p in inspect.signature(function).parameters.values() if p.default is p.empty and p.kind is p.POSITIONAL_OR_KEYWORD
        ]
        with pytest.raises(RuntimeError, match="_test"):
            function(Elsewhere(), *[None] * (len(required) - 1))
