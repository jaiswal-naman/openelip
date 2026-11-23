"""The two introduction emails (plan 06-04): introduction_accepted_v1 to the organization, introduction_answered_v1 to
the candidate. The resolvers run as the relay runs them, on a connection that sees every row."""

from uuid import uuid4

from sqlalchemy import text

import app.worker  # noqa: F401  (importing the worker is what registers its resolvers)
from app.hiring import introduction_mail
from app.platform import relay
from app.platform.relay import Outgoing, Refusal
from tests.hiring.factories import make_accepted, make_ask, make_candidate, make_member, make_open_role, make_org


def real(conn, user, email):
    """Give an invented person a deliverable address, so a resolver does not refuse them as synthetic."""
    conn.execute(text("UPDATE users SET email = :e, is_synthetic = false WHERE id = :u"), {"e": email, "u": user})


def accepted_scene(conn, *, timezone_name="Asia/Kolkata"):
    org = make_org(conn, timezone=timezone_name)
    role = make_open_role(conn, org)
    candidate = make_candidate(conn, name="Demo Mailed Candidate")
    return org, role, candidate, make_accepted(conn, role, candidate)


def accepted_payload(accepted):
    return {"introduction_id": str(accepted.id)}


def test_both_templates_are_registered_with_the_relay_and_the_worker_loads_them():
    assert relay.RESOLVERS["introduction_accepted_v1"] is introduction_mail.resolve_accepted
    assert relay.RESOLVERS["introduction_answered_v1"] is introduction_mail.resolve_answered


def test_the_notice_goes_to_the_roles_creator_with_the_role_the_due_time_and_a_link_but_no_candidate(conn):
    org, role, candidate, accepted = accepted_scene(conn)
    real(conn, org.owner, "owner@halden.example.com")
    real(conn, candidate.id, "asha@candidate.example.com")

    out = introduction_mail.resolve_accepted(conn, accepted_payload(accepted))

    assert isinstance(out, Outgoing) and out.to == "owner@halden.example.com"  # the owner created the role
    assert role.title in out.subject
    due = conn.execute(
        text("SELECT answer_due_at AT TIME ZONE 'Asia/Kolkata' FROM introductions WHERE id = :i"), {"i": accepted.id}
    ).scalar_one()
    assert f"{due:%A %d %B %Y, %H:%M} (Asia/Kolkata)" in out.body
    assert f"/hire/shortlist/{role.id}" in out.body
    for private in (candidate.name, "Demo Mailed", "asha@candidate.example.com", "candidate.example.com"):
        assert private not in out.body and private not in out.subject


def test_a_creator_who_left_means_the_earliest_joined_owner(conn):
    org, role, candidate, accepted = accepted_scene(conn)
    second_owner = make_member(conn, org, role="owner").user
    real(conn, org.owner, "creator@halden.example.com")
    real(conn, second_owner, "second@halden.example.com")
    # the creator is no longer a member; the earliest-joined active owner is the second one
    conn.execute(
        text("UPDATE org_members SET status = 'removed', removed_at = now() WHERE user_id = :u"), {"u": org.owner}
    )
    out = introduction_mail.resolve_accepted(conn, accepted_payload(accepted))
    assert isinstance(out, Outgoing) and out.to == "second@halden.example.com"


def test_with_no_active_owner_or_creator_nobody_is_written_to(conn):
    org, role, candidate, accepted = accepted_scene(conn)
    conn.execute(
        text("UPDATE org_members SET status = 'removed', removed_at = now() WHERE organization_id = :o"), {"o": org.id}
    )
    assert introduction_mail.resolve_accepted(conn, accepted_payload(accepted)) == Refusal("cancelled", "no_recipient")


def test_a_synthetic_recipient_a_missing_introduction_and_a_bad_payload_are_refused(conn):
    org, role, candidate, accepted = accepted_scene(conn)
    assert introduction_mail.resolve_accepted(conn, accepted_payload(accepted)) == Refusal("cancelled", "undeliverable")
    assert introduction_mail.resolve_accepted(conn, {"introduction_id": str(uuid4())}) == Refusal("cancelled", "stale")
    assert introduction_mail.resolve_accepted(conn, {}) == Refusal("failed", "bad_payload")
    assert introduction_mail.resolve_answered(conn, {"introduction_id": "nope"}) == Refusal("failed", "bad_payload")


def test_an_unaccepted_ask_has_no_notice(conn):
    org = make_org(conn)
    role = make_open_role(conn, org)
    ask = make_ask(conn, role, make_candidate(conn))
    real(conn, org.owner, "owner@halden.example.com")
    assert introduction_mail.resolve_accepted(conn, {"introduction_id": str(ask.id)}) == Refusal("cancelled", "stale")


def answer(conn, accepted, org, verdict="no", reason="We need someone who has\nled a team."):
    conn.execute(
        text(
            """UPDATE introductions SET employer_answer = CAST(:a AS employer_answer), employer_answer_reason = :r,
                   employer_answered_by = :u WHERE id = :i"""
        ),
        {"a": verdict, "r": reason, "u": org.owner, "i": accepted.id},
    )


def test_the_answer_goes_to_the_candidate_in_one_line(conn):
    org, role, candidate, accepted = accepted_scene(conn)
    real(conn, candidate.id, "asha@candidate.example.com")
    answer(conn, accepted, org)

    out = introduction_mail.resolve_answered(conn, accepted_payload(accepted))

    assert isinstance(out, Outgoing) and out.to == "asha@candidate.example.com"
    assert f"{org.name} answered no for {role.title}: We need someone who has led a team." in out.body
    assert "/app/today" in out.body


def test_the_answer_is_refused_before_it_exists_and_for_a_synthetic_or_example_test_person(conn):
    org, role, candidate, accepted = accepted_scene(conn)
    assert introduction_mail.resolve_answered(conn, accepted_payload(accepted)) == Refusal("cancelled", "stale")
    answer(conn, accepted, org, "yes", "Strong fit")
    assert introduction_mail.resolve_answered(conn, accepted_payload(accepted)) == Refusal("cancelled", "undeliverable")
    conn.execute(text("UPDATE users SET is_synthetic = false WHERE id = :u"), {"u": candidate.id})  # still example.test
    assert introduction_mail.resolve_answered(conn, accepted_payload(accepted)) == Refusal("cancelled", "undeliverable")
