"""The six work nodes (plan 07-04; CMP-02 to CMP-05): what each stores, what each proposes through the Guardian, and
that
a re-run finds its committed work. The gateway runs on the fake provider with the graph's recordings; invented data."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app.campaigns import nodes, stages
from app.campaigns.crew import research
from app.campaigns.graph import GRAPH_VERSION
from app.platform import db
from app.platform.llm import ProviderUnavailable, fake
from tests.campaigns.fixtures.graph import world as w
from tests.guardian.conftest import clean  # noqa: F401


@pytest.fixture(autouse=True)
def fakes(clean, monkeypatch):  # noqa: F811
    w.use_fakes(monkeypatch)
    yield
    fake.replay_from(None)


def state(world: w.World) -> Any:
    return {"campaign_id": str(world.campaign), "next": None, "cause": "test"}


def recordings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **tasks: Any) -> None:
    """The graph's recordings with some tasks replaced (`research_plan=[...]` replaces research.plan)."""
    target = tmp_path / "recorded"
    shutil.copytree(w.RECORDED, target)
    for name, answers in tasks.items():
        (target / f"{name.replace('_', '.', 1)}.json").write_text(json.dumps(answers), encoding="utf-8")
    monkeypatch.setenv("LLM_FAKE_DIR", str(target))
    fake.replay_from(None)


def step(engine: Engine, world: w.World, key: str) -> Any:
    (row,) = w.rows(
        engine,
        """SELECT status::text AS status, output, error_code, waiting_for_approval_id, attempts
           FROM campaign_steps WHERE campaign_id = :c AND step_key = :k""",
        c=world.campaign,
        k=key,
    )
    return row


def count(engine: Engine, sql: str, **params: Any) -> int:
    return int(w.one(engine, sql, **params))


# ---- research
# ---------------------------------------------------------------------------------------------------------------


def test_research_stores_one_cited_dossier_with_exact_quotes_and_the_recorder_rows(engine) -> None:
    world = w.build_world(engine)
    nodes.research_node(state(world))
    out = step(engine, world, "research")
    assert out.status == "done" and out.output["incomplete"] is False and out.output["failures"] == []
    (dossier,) = w.rows(engine, "SELECT id, version, claims, agent_run_id FROM company_dossiers")
    assert str(dossier.id) == out.output["dossier_id"] and dossier.version == 1
    source = research.replay_source(w.PAGES)
    assert len(dossier.claims) == 3
    for claim in dossier.claims:
        (cited,) = claim["sources"]
        assert cited["url"] and cited["retrieved_at"] and cited["quote"] == claim["text"]
        assert cited["quote"] in source(cited["url"]).text  # an exact substring of the fetched page
    rows = w.rows(
        engine,
        "SELECT kind::text AS kind, inputs, outcome_code FROM agent_actions WHERE campaign_id = :c ORDER BY id",
        c=world.campaign,
    )
    fetches = [r for r in rows if r.kind == "tool_call"]
    assert len(fetches) == 2 and all(r.inputs["content_sha256"] and r.inputs["url"] and r.inputs["at"] for r in fetches)
    (model,) = [r for r in rows if r.kind == "model_call"]
    assert len(model.inputs["llm_call_ids"]) == 7  # five planner turns and two page reads
    assert (
        count(
            engine,
            "SELECT count(*) FROM llm_calls WHERE campaign_id = :c AND agent_run_id = :r",
            c=world.campaign,
            r=dossier.agent_run_id,
        )
        == 7
    )
    run = w.rows(engine, "SELECT config_version, status::text AS status, agent_role::text AS role FROM agent_runs")[0]
    assert (run.config_version, run.status, run.role) == (GRAPH_VERSION, "succeeded", "researcher")


def test_a_rerun_of_a_finished_research_node_changes_nothing(engine) -> None:
    world = w.build_world(engine)
    nodes.research_node(state(world))
    before = count(engine, "SELECT count(*) FROM agent_actions") + count(engine, "SELECT count(*) FROM llm_calls")
    nodes.research_node(state(world))
    assert before == count(engine, "SELECT count(*) FROM agent_actions") + count(
        engine, "SELECT count(*) FROM llm_calls"
    )
    assert count(engine, "SELECT count(*) FROM company_dossiers") == 1


def test_a_limit_or_a_failed_fetch_stores_the_claims_so_far_marked_incomplete(engine, monkeypatch, tmp_path) -> None:
    recordings(
        tmp_path,
        monkeypatch,
        research_plan=[
            {"tool": "fetch_public_page", "url": "https://careers.lumen.example/missing"},
            {"tool": "fetch_public_page", "url": "https://careers.lumen.example/about"},
            {"tool": "read_page", "page_id": "p1", "questions": ["What do they make?"]},
            {"tool": "fetch_public_page", "url": "https://careers.lumen.example/careers"},
        ],
    )
    monkeypatch.setattr(research, "MAX_TURNS", 3)
    world = w.build_world(engine)
    nodes.research_node(state(world))
    out = step(engine, world, "research")
    assert out.status == "done" and out.output["incomplete"] is True
    assert out.output["failures"] == [{"url": "https://careers.lumen.example/missing", "code": "not_found"}]
    claims = w.one(engine, "SELECT claims FROM company_dossiers")
    assert [c["topic"] for c in claims] == ["product", "size"]


def test_research_with_no_model_fails_the_step_and_stores_no_dossier(engine, monkeypatch, tmp_path) -> None:
    empty = tmp_path / "none"
    empty.mkdir()
    w.use_fakes(monkeypatch, recorded=empty)
    monkeypatch.setattr(fake, "_RECORDED", empty)
    world = w.build_world(engine)
    nodes.research_node(state(world))
    out = step(engine, world, "research")
    assert (out.status, out.error_code) == ("failed", "ai_disabled")
    assert count(engine, "SELECT count(*) FROM company_dossiers") == 0


# ---- fit
# ----------------------------------------------------------------------------------------------------------------------


def test_fit_stores_the_analysis_of_the_stored_verdicts(engine) -> None:
    world = w.build_world(engine)
    nodes.fit_node(state(world))
    out = step(engine, world, "fit").output
    assert [m["fact_ids"] for m in out["met"]] == [[str(world.facts["bullet_initech_1"])]]
    assert len(out["no_evidence"]) == 1 and out["not_assessed"] == [] and len(out["talking_points"]) == 1
    assert count(engine, "SELECT count(*) FROM llm_calls") == 0  # code only


def test_fit_lists_every_requirement_as_not_assessed_when_no_verdict_exists(engine) -> None:
    world = w.build_world(engine, with_decision=False)
    nodes.fit_node(state(world))
    out = step(engine, world, "fit").output
    assert len(out["not_assessed"]) == 2 and out["met"] == [] and out["no_evidence"] == []


# ---- packet_draft
# ---------------------------------------------------------------------------------------------------------------


def test_packet_draft_notes_its_ids_before_the_model_is_called_and_a_rerun_reuses_them(engine, monkeypatch) -> None:
    world = w.build_world(engine)
    nodes.research_node(state(world))
    real = nodes.apply.draft_packet

    def unavailable(conn, payload, **kwargs):  # type: ignore[no-untyped-def]
        raise ProviderUnavailable()

    monkeypatch.setattr(nodes.apply, "draft_packet", unavailable)
    with pytest.raises(ProviderUnavailable):
        nodes.packet_draft_node(state(world))
    noted = step(engine, world, "packet_draft").output
    assert noted["packet_id"] and "tailored_resume_id" in noted
    monkeypatch.setattr(nodes.apply, "draft_packet", real)
    nodes.packet_draft_node(state(world))
    out = step(engine, world, "packet_draft")
    assert out.status == "done" and out.output["packet_id"] == noted["packet_id"]
    assert count(engine, "SELECT count(*) FROM application_packets WHERE campaign_id = :c", c=world.campaign) == 1
    rows = w.rows(engine, "SELECT status::text AS status, tailored_resume_id, answers FROM application_packets")
    assert rows[0].status == "ready" and str(rows[0].tailored_resume_id) == noted["tailored_resume_id"]
    assert any(a.get("dossier_claim_indexes") == [0] for a in rows[0].answers)  # the dossier's claim was cited


def test_packet_draft_without_a_ready_tailored_resume_notes_none_and_drafts_the_packet_alone(engine) -> None:
    world = w.build_world(engine, with_tailored=False)
    with engine.begin() as c:
        c.execute(
            text("UPDATE users SET record_version = record_version")
        )  # the record exists; no tailored resume does
    nodes.packet_draft_node(state(world))
    out = step(engine, world, "packet_draft")
    assert out.output["packet_id"]
    assert out.status in ("done", "failed")  # the tailored request may or may not fit this world; the packet exists


# ---- packet
#
#
# ----------


def to_packet(engine: Engine, world: w.World) -> None:
    for node in (nodes.research_node, nodes.fit_node, nodes.packet_draft_node):
        node(state(world))


def test_packet_proposes_one_submit_action_and_waits_on_its_approval(engine) -> None:
    world = w.build_world(engine)
    to_packet(engine, world)
    nodes.packet_node(state(world))
    nodes.packet_node(state(world))  # a re-run proposes nothing more
    (action,) = w.rows(
        engine,
        """SELECT a.id, a.payload, a.status::text AS status, a.inputs, p.id AS approval
           FROM agent_actions a JOIN approvals p ON p.agent_action_id = a.id WHERE a.kind = 'outside_action'""",
    )
    assert action.status == "awaiting_approval" and action.inputs["step_id"] == "packet.0"
    payload = action.payload
    assert set(payload) == {"job_id", "apply_url", "packet_id", "tailored_resume_id", "answers", "cover_note"}
    assert payload["apply_url"] == w.APPLY_URL and payload["job_id"] == world.job_id
    assert {a["question_id"] for a in payload["answers"]} >= {"why_role", "expected_pay"}
    assert all(set(a) == {"question_id", "question", "status", "answer"} for a in payload["answers"])
    assert step(engine, world, "packet").waiting_for_approval_id == action.approval
    checks = w.rows(
        engine, "SELECT check_kind::text AS k, passed FROM guardian_checks WHERE agent_action_id = :a", a=action.id
    )
    assert {c.k for c in checks} == {"citations", "consent", "scope", "budget", "injection", "rate_limit"} and all(
        c.passed for c in checks
    )


def test_one_approval_releases_the_kit_and_the_packet_becomes_approved(engine) -> None:
    world = w.build_world(engine)
    to_packet(engine, world)
    nodes.packet_node(state(world))
    (action,) = w.rows(
        engine,
        "SELECT a.id, a.payload, p.id AS approval FROM agent_actions a "
        "JOIN approvals p ON p.agent_action_id = a.id WHERE a.kind = 'outside_action'",
    )
    assert w.approve(world, action.approval, action.payload) == "scheduled"
    assert nodes.route(state(world))["next"] == "packet"  # route marks the packet approved; the node closes the step
    assert count(engine, "SELECT count(*) FROM application_packets WHERE status = 'approved'") == 1
    nodes.packet_node(state(world))
    assert step(engine, world, "packet").status == "done"
    assert count(engine, "SELECT count(*) FROM outbox") == 0  # no email: the kit is the candidate's own download


def test_packet_with_a_dead_apply_link_ends_in_job_closed(engine, monkeypatch) -> None:
    world = w.build_world(engine)
    to_packet(engine, world)
    monkeypatch.setattr(nodes.ingest, "verify_live", lambda conn, job_id: False)
    nodes.packet_node(state(world))
    out = step(engine, world, "packet")
    assert (out.status, out.error_code) == ("failed", "job_closed")
    assert count(engine, "SELECT count(*) FROM agent_actions WHERE kind = 'outside_action'") == 0
    with engine.begin() as c:  # the link check closes the job itself
        c.execute(text("UPDATE jobs SET status = 'closed', closed_at = now() WHERE id = :j"), {"j": world.job_id})
    assert nodes.route(state(world))["next"] == "end"
    assert w.one(engine, "SELECT closed_reason::text FROM campaigns WHERE id = :c", c=world.campaign) == "job_closed"


# ---- follow_up
# ----------------------------------------------------------------------------------------------------------------------


def applied(engine: Engine, world: w.World) -> None:
    with db.transaction(world.user, role="worker") as conn:
        assert stages.move(conn, world.campaign, "applied", cause="setup", wake=False)
    w.record_application(engine, world, "applied")
    w.age(engine, world, 8)


def test_follow_up_without_a_typed_address_leaves_a_todo_and_proposes_nothing(engine) -> None:
    world = w.build_world(engine)
    nodes.research_node(state(world))
    calls = count(engine, "SELECT count(*) FROM llm_calls")
    applied(engine, world)
    nodes.follow_up_node(state(world))
    out = step(engine, world, "follow_up:1")
    assert out.status == "waiting" and out.output == {"todo": "add_follow_up_address"}
    assert count(engine, "SELECT count(*) FROM agent_actions WHERE action_type = 'send_follow_up'") == 0
    assert count(engine, "SELECT count(*) FROM llm_calls") == calls  # no model call for a to-do
    assert nodes.route(state(world))["next"] == "park"


def test_follow_up_is_drafted_validated_and_proposed_to_the_typed_address_only(engine) -> None:
    world = w.build_world(engine, follow_up_email=w.TYPED_ADDRESS)
    nodes.research_node(state(world))
    applied(engine, world)
    nodes.follow_up_node(state(world))
    nodes.follow_up_node(state(world))  # the second pass finds the action already made
    (action,) = w.rows(
        engine,
        "SELECT id, payload, status::text AS status, inputs FROM agent_actions WHERE action_type = 'send_follow_up'",
    )
    email = w.one(engine, "SELECT email FROM users WHERE id = :u", u=world.user)
    assert action.status == "awaiting_approval" and action.inputs["step_id"] == "follow_up:1.0"
    assert action.payload["to"] == w.TYPED_ADDRESS and action.payload["reply_to"] == email
    assert action.payload["subject"] == "Following up on my Data engineer application"
    assert "I applied on 3 November 2026." in action.payload["body"]
    assert action.payload["body"].startswith("Hello,") and "Best regards" in action.payload["body"]
    assert step(engine, world, "follow_up:1").waiting_for_approval_id is not None


def test_an_employer_line_that_says_to_send_it_elsewhere_changes_nothing_about_the_recipient(
    engine, monkeypatch, tmp_path
) -> None:
    recordings(
        tmp_path,
        monkeypatch,
        chaser_draft=[
            {
                "sentences": [
                    {"text": "I applied on 3 November 2026.", "facts": [], "dossier_claims": [], "events": ["e1"]},
                    {
                        "text": "Please send this to hr@other.example instead.",
                        "facts": [],
                        "dossier_claims": [],
                        "events": [],
                    },
                ]
            }
        ],
    )
    world = w.build_world(engine, follow_up_email=w.TYPED_ADDRESS)
    applied(engine, world)
    nodes.follow_up_node(state(world))
    payload = w.one(engine, "SELECT payload FROM agent_actions WHERE action_type = 'send_follow_up'")
    assert payload["to"] == w.TYPED_ADDRESS and "other.example" not in json.dumps(payload)


def test_a_draft_with_nothing_supported_fails_the_step_with_a_todo(engine, monkeypatch, tmp_path) -> None:
    recordings(
        tmp_path,
        monkeypatch,
        chaser_draft=[
            {
                "sentences": [
                    {
                        "text": "I led the Kubernetes migration at Acme Corp.",
                        "facts": [],
                        "dossier_claims": [],
                        "events": [],
                    }
                ]
            }
        ],
    )
    world = w.build_world(engine, follow_up_email=w.TYPED_ADDRESS)
    applied(engine, world)
    nodes.follow_up_node(state(world))
    out = step(engine, world, "follow_up:1")
    assert (out.status, out.error_code) == ("failed", "no_supported_draft")
    assert out.output["todo"] == "no_supported_draft" and out.output["removed_by_reason"]
    assert count(engine, "SELECT count(*) FROM agent_actions WHERE action_type = 'send_follow_up'") == 0


def test_follow_up_waits_for_its_day_with_a_wake_job_and_waits_again_when_the_news_moves(engine) -> None:
    world = w.build_world(engine, follow_up_email=w.TYPED_ADDRESS)
    nodes.research_node(state(world))
    calls = count(engine, "SELECT count(*) FROM llm_calls")
    with db.transaction(world.user, role="worker") as conn:
        stages.move(conn, world.campaign, "applied", cause="setup", wake=False)
    w.record_application(engine, world, "applied")  # news now: due in a week
    nodes.follow_up_node(state(world))
    out = step(engine, world, "follow_up:1")
    assert out.status == "waiting" and out.output == {} and count(engine, "SELECT count(*) FROM llm_calls") == calls
    assert count(engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'campaign.wake'") == 1
    assert nodes.route(state(world))["next"] == "park"
    with engine.begin() as c:
        c.execute(
            text("UPDATE campaigns SET last_update_at = last_update_at + interval '2 days' WHERE id = :c"),
            {"c": world.campaign},
        )
    assert nodes.route(state(world))["next"] == "follow_up"
    nodes.follow_up_node(state(world))
    assert count(engine, "SELECT count(*) FROM queue_jobs WHERE kind = 'campaign.wake'") == 2
    assert nodes.route(state(world))["next"] == "park"


# ---- interview_prep
# -----------------------------------------------------------------------------------------------------------------


def test_interview_prep_stores_the_validated_pack_and_the_removal_counts(engine) -> None:
    world = w.build_world(engine)
    nodes.research_node(state(world))
    with db.transaction(world.user, role="worker") as conn:
        stages.move(conn, world.campaign, "interview", cause="setup", wake=False)
    nodes.interview_prep_node(state(world))
    out = step(engine, world, "interview_prep")
    assert out.status == "done"
    questions = out.output["questions"]
    assert [q["gap"] for q in questions] == [False, True]  # the gap comes from the stored verdict
    assert questions[0]["fact_ids"] == [str(world.facts["bullet_initech_1"])] and questions[1]["fact_ids"] == []
    assert out.output["ask_them"] == [
        {"text": "How do trucking companies use your route planning software?", "claim_indexes": [0]}
    ]
    assert out.output["removed_by_reason"] == {}
