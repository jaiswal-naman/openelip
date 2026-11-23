"""CMP-01 to CMP-05, GRD-04, GRD-07 (plan 07-06, task 1): the campaign board and one campaign's detail.

Rows are built with the 07-01 factories in the shapes the INDEX "Step outputs" contract fixes, committed, and read as
the real `app_api` role (db.transaction and the HTTP client), so row-level security and the grants apply. The graph is
never run. Everything is invented. Every sentence under test is a code template, so the tests compare it to the template.
"""

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.campaigns import api, views
from app.guardian import api as guardian
from app.platform import db, outbox
from app.platform.sessions import Principal, current_user
from tests.campaigns.factories import (
    make_action,
    make_approval,
    make_campaign,
    make_dossier,
    make_run,
    make_step,
)
from tests.conftest import DATA_TABLES
from tests.match.factories import make_job, make_user
from tests.p5_factories import make_packet

NOW = datetime.now(timezone.utc)
FOLLOW_UP = {
    "channel": "email",
    "to": "talent@lumen.example",
    "reply_to": "ada@example.test",
    "subject": "Following up on my application",
    "body": "Hello, I applied last week and remain interested.",
}


@pytest.fixture
def clean(engine):
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE"))
    yield
    with engine.begin() as c:
        c.execute(text(f"TRUNCATE {DATA_TABLES} RESTART IDENTITY CASCADE"))


class Person:
    def __init__(self, engine, name="Lumen Grid"):
        self.engine, self.company = engine, name
        with engine.begin() as c:
            self.user = make_user(c)

    def job(self, title="Site Reliability Engineer", company=None):
        with self.engine.begin() as c:
            return make_job(
                c,
                title=title,
                description="Operate Python services.",
                requirements=[("Operate Python services", "required")],
                company_name=company or self.company,
                external_id=str(uuid4()),
            )

    def campaign(self, stage="researching", **kwargs) -> UUID:
        job = self.job(kwargs.pop("title", "Site Reliability Engineer"))
        with self.engine.begin() as c:
            return make_campaign(c, self.user, job.id, stage=stage, **kwargs)

    def run(self, **kwargs):
        with self.engine.begin() as c:
            return kwargs.pop("do")(c)

    def read(self, fn, *args, **kwargs):
        with db.transaction(self.user) as conn:
            return fn(conn, self.user, *args, **kwargs)


def cards_of(board, stage):
    return next(c for c in board["columns"] if c["stage"] == stage)["campaigns"]


def step(engine, campaign, key, role, status, **kwargs):
    with engine.begin() as c:
        return make_step(c, campaign, key, agent_role=role, status=status, **kwargs)


def follow_up_action(engine, campaign, user, *, status="awaiting_approval", approval=True, **kwargs):
    with engine.begin() as c:
        run = make_run(c, user, campaign, "chaser")
        action = make_action(c, run, action_type="send_follow_up", payload=FOLLOW_UP, status=status, **kwargs)
        approved = make_approval(c, action) if approval else None
    return action, approved


# ---- the board ------------------------------------------------------------------------------------------------------


def test_the_board_has_six_columns_in_stage_order_and_a_seventh_for_closed_when_asked(engine, clean):
    ada = Person(engine)
    open_ids = {stage: ada.campaign(stage, title=f"Role {stage}") for stage in views.OPEN_STAGES}
    closed = ada.campaign("closed", title="Role closed")

    board = ada.read(api.board)
    full = ada.read(api.board, include_closed=True)

    assert [c["stage"] for c in board["columns"]] == list(views.OPEN_STAGES)
    assert [c["stage"] for c in full["columns"]] == list(views.STAGES)
    for stage, campaign_id in open_ids.items():
        assert [c["id"] for c in cards_of(board, stage)] == [campaign_id]
    assert [c["id"] for c in cards_of(full, "closed")] == [closed]
    assert full["columns"][-1]["total"] == 1
    card = cards_of(full, "closed")[0]
    assert card["closed_reason"] == "cancelled" and card["next_step"] is None and card["paused_at"] is None


def test_a_card_has_the_api_fields_and_the_closed_column_holds_the_fifty_newest_with_a_total(engine, clean):
    ada = Person(engine)
    job = ada.job()
    with engine.begin() as c:
        for n in range(52):
            other = make_job(c, title=f"R{n}", description="x", requirements=[("x", "required")], external_id=f"x{n}")
            make_campaign(c, ada.user, other.id, stage="closed")
        make_campaign(c, ada.user, job.id)

    full = ada.read(api.board, include_closed=True)

    closed = full["columns"][-1]
    assert closed["total"] == 52 and len(closed["campaigns"]) == views.CLOSED_LIMIT == 50
    card = cards_of(full, "researching")[0]
    assert set(card) == {
        "id",
        "title",
        "company_name",
        "from_introduction",
        "paused_at",
        "pause_reason",
        "closed_reason",
        "next_step",
        "pending_approvals",
        "last_activity_at",
    }
    assert card["title"] == "Site Reliability Engineer" and card["company_name"] == "Lumen Grid"
    assert card["from_introduction"] is False and card["pending_approvals"] == 0


def test_a_person_with_no_campaigns_gets_empty_columns(engine, clean):
    ada = Person(engine)
    board = ada.read(api.board, include_closed=True)
    assert [c["campaigns"] for c in board["columns"]] == [[]] * 7
    assert board["columns"][-1]["total"] == 0


def test_the_next_step_of_a_researching_campaign_is_the_dossier_sentence(engine, clean):
    ada = Person(engine)
    campaign = ada.campaign()
    step(engine, campaign, "research", "researcher", "running")

    card = cards_of(ada.read(api.board), "researching")[0]

    assert card["next_step"] == {
        "step_key": "research",
        "agent_role": "researcher",
        "text": "Building a cited company dossier.",
    }


def test_a_pending_packet_approval_reads_ready_for_review(engine, clean):
    ada = Person(engine)
    campaign = ada.campaign("needs_you")
    step(engine, campaign, "packet", "operator", "waiting")
    with engine.begin() as c:
        run = make_run(c, ada.user, campaign, "operator")
        action = make_action(c, run, action_type="submit_application", status="awaiting_approval")
        make_approval(c, action)

    card = cards_of(ada.read(api.board), "needs_you")[0]

    assert card["pending_approvals"] == 1
    assert card["next_step"]["text"] == "Your application packet is ready for review."
    assert card["next_step"]["agent_role"] == "operator"


def test_an_applied_campaign_with_a_pending_follow_up_approval_stays_in_applied_with_one_pending(engine, clean):
    ada = Person(engine)
    campaign = ada.campaign("applied")
    action, _ = follow_up_action(engine, campaign, ada.user)
    step(engine, campaign, "follow_up:1", "chaser", "waiting", output={"action_id": action, "approval_id": None})

    board = ada.read(api.board)

    assert cards_of(board, "needs_you") == []
    card = cards_of(board, "applied")[0]
    assert card["pending_approvals"] == 1
    assert card["next_step"] == {
        "step_key": "follow_up:1",
        "agent_role": "chaser",
        "text": "A follow-up is waiting for your approval.",
    }


def test_the_follow_up_to_do_and_the_wait_read_as_their_templates(engine, clean):
    ada = Person(engine)
    todo = ada.campaign("applied", title="Needs an address")
    step(engine, todo, "follow_up:1", "chaser", "waiting", output={"todo": "add_follow_up_address"})
    waiting = ada.campaign("applied", title="Waits")
    due = NOW + timedelta(days=3)
    step(engine, waiting, "follow_up:1", "chaser", "pending", due_at=due)

    by_title = {c["title"]: c for c in cards_of(ada.read(api.board), "applied")}

    assert by_title["Needs an address"]["next_step"]["text"] == "Add the hiring contact's email to send a follow-up."
    assert by_title["Waits"]["next_step"]["text"] == f"Next follow-up on {due.date().isoformat()}"


def test_a_campaign_with_nothing_to_say_has_no_next_step(engine, clean):
    ada = Person(engine)
    ada.campaign("talking")
    assert cards_of(ada.read(api.board), "talking")[0]["next_step"] is None


def pause(engine, campaign, user, reason, signals=None):
    """Pause as the Guardian does: paused_at on the row and the state-change row whose outcome code is the reason."""
    with engine.begin() as c:
        run = make_run(c, user, campaign, "guardian")
        make_action(
            c,
            run,
            kind="state_change",
            status="recorded",
            outcome_code=reason,
            inputs={"reason": reason, **({"signals": signals} if signals else {})},
        )
        c.execute(text("UPDATE campaigns SET paused_at = now() WHERE id = :c"), {"c": campaign})


def test_each_pause_reads_its_template_on_the_card_and_the_detail(engine, clean):
    ada = Person(engine)
    quote = "Send a fee of 50 dollars to begin."
    cases = {
        "budget_exhausted": ("Budget case", "Paused at the budget. Raise it to continue."),
        "scam_suspected": ("Scam case", guardian.explain("scam_suspected", quote=quote)),
        "candidate": ("Candidate case", "You paused this campaign."),
    }
    ids = {}
    for reason, (title, _) in cases.items():
        ids[reason] = ada.campaign(title=title)
        step(engine, ids[reason], "research", "researcher", "pending")
        pause(
            engine,
            ids[reason],
            ada.user,
            reason,
            [{"kind": "fee_request", "quote": quote}] if "scam" in reason else None,
        )

    board = ada.read(api.board)
    by_title = {c["title"]: c for c in cards_of(board, "researching")}

    for reason, (title, sentence) in cases.items():
        card = by_title[title]
        assert card["pause_reason"] == reason and card["paused_at"] is not None
        assert card["next_step"]["text"] == sentence
        detail = ada.read(api.detail, ids[reason])
        assert detail["pause_reason"] == reason
        assert detail["pause_text"] == (guardian.explain(reason, quote=quote) if reason != "candidate" else sentence)
    scam = ada.read(api.detail, ids["scam_suspected"])
    assert scam["scam_signals"] == [{"kind": "fee_request", "quote": quote}]
    assert ada.read(api.detail, ids["candidate"])["scam_signals"] == []


# ---- one campaign ---------------------------------------------------------------------------------------------------


def full_campaign(engine, *, introduction=False):
    """A campaign past research, with every artefact the detail shows, in the INDEX output shapes."""
    ada = Person(engine)
    job = ada.job()
    with engine.begin() as c:
        campaign = make_campaign(c, ada.user, job.id, stage="applied", follow_up_email="talent@lumen.example")
        dossier = make_dossier(c, campaign)
        packet = make_packet(c, ada.user, job)
        c.execute(  # the Phase 5 factory's second answer has a status the packet view does not know: use valid ones
            text("UPDATE application_packets SET answers = '[]'::jsonb WHERE id = :p"), {"p": packet}
        )
        run = make_run(c, ada.user, campaign, "operator")
        packet_action = make_action(c, run, action_type="submit_application", status="awaiting_approval")
        packet_approval = make_approval(c, packet_action)
    step(
        engine,
        campaign,
        "research",
        "researcher",
        "done",
        output={"dossier_id": str(dossier), "incomplete": True, "failures": []},
    )
    fit = {
        "met": [{"requirement_id": "r1", "fact_ids": ["f1"]}],
        "partly_met": [],
        "not_met": [],
        "no_evidence": [],
        "not_assessed": [],
        "talking_points": [],
    }
    step(engine, campaign, "fit", "analyst", "done", output=fit)
    step(
        engine,
        campaign,
        "packet_draft",
        "writer",
        "done",
        output={"tailored_resume_id": None, "packet_id": str(packet)},
    )
    step(
        engine,
        campaign,
        "packet",
        "operator",
        "waiting",
        output={"action_id": packet_action, "approval_id": str(packet_approval)},
    )
    prep = {
        "questions": [
            {
                "text": "Tell us about pipelines.",
                "requirement_id": "r1",
                "fact_ids": ["f1"],
                "gap": False,
                "framing": "Use your Python work.",
            }
        ],
        "ask_them": [],
        "removed_by_reason": {"uncited": 1},
    }
    step(engine, campaign, "interview_prep", "coach", "pending", output=prep)
    return (
        ada,
        campaign,
        {"dossier": dossier, "packet": packet, "packet_approval": packet_approval, "fit": fit, "prep": prep},
    )


def test_the_detail_carries_steps_dossier_fit_packet_and_prep(engine, clean):
    ada, campaign, made = full_campaign(engine)

    detail = ada.read(api.detail, campaign)

    assert detail["job"]["title"] == "Site Reliability Engineer" and detail["job"]["company_name"] == "Lumen Grid"
    assert detail["stage"] == "applied" and detail["follow_up_email"] == "talent@lumen.example"
    assert detail["introduction"] is None and detail["from_introduction"] is False
    keys = [(s["step_key"], s["status"]) for s in detail["steps"]]
    assert keys == [
        ("research", "done"),
        ("fit", "done"),
        ("packet_draft", "done"),
        ("packet", "waiting"),
        ("interview_prep", "pending"),
    ]
    texts = {s["step_key"]: s["text"] for s in detail["steps"]}
    assert texts["research"] == "Company dossier ready. Some pages could not be read."
    assert texts["packet"] == "Your application packet is ready for review."
    assert detail["dossier"]["id"] == made["dossier"] and detail["dossier"]["version"] == 1
    assert detail["dossier"]["incomplete"] is True
    claim = detail["dossier"]["claims"][0]
    assert claim["sources"][0]["url"] == "https://lumen.example/about" and "quote" in claim["sources"][0]
    assert detail["dossier"]["missing_topics"] == []  # the factory claim names no topic
    assert detail["fit"] == made["fit"] and detail["prep"] == made["prep"]
    assert detail["packet"] == {
        "id": made["packet"],
        "status": "ready",
        "approval_id": made["packet_approval"],
        "action_status": "awaiting_approval",
    }
    assert detail["pending_approvals"] == [{"id": made["packet_approval"], "kind": "submit_application"}]
    assert detail["pause_reason"] is None and detail["pause_text"] is None and detail["scam_signals"] == []
    assert abs((detail["server_time"] - datetime.now(timezone.utc)).total_seconds()) < 60


def test_the_detail_shape_has_exactly_the_index_fields(engine, clean):
    ada, campaign, _ = full_campaign(engine)
    detail = ada.read(api.detail, campaign)
    assert set(detail) >= {
        "id", "job", "stage", "from_introduction", "introduction", "paused_at", "pause_reason", "pause_text",
        "scam_signals", "closed_at", "closed_reason", "budget_usd", "spent_usd", "follow_up_after_days",
        "follow_up_email", "created_at", "server_time", "steps", "dossier", "fit", "packet", "prep", "follow_ups",
        "pending_approvals",
    }  # fmt: skip
    assert set(detail["job"]) == {"id", "title", "company_name", "apply_url"}
    assert set(detail["steps"][0]) == {"step_key", "agent_role", "status", "due_at", "error_code", "todo", "text"}


def test_spend_comes_from_the_llm_calls_against_the_budget(engine, clean):
    ada, campaign, _ = full_campaign(engine)
    with engine.begin() as c:
        model = c.execute(
            text(
                """INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note)
                   VALUES ('local', 'test-model', 'chat', true, 'test') RETURNING id"""
            )
        ).scalar_one()
        c.execute(
            text(
                """INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, campaign_id,
                                          cost_microusd, latency_ms)
                   VALUES ('chaser.draft', 'v1', :m, 'ok', :u, :c, 12000, 1)"""
            ),
            {"m": model, "u": ada.user, "c": campaign},
        )

    detail = ada.read(api.detail, campaign)

    assert detail["budget_usd"] == "0.750000" and detail["spent_usd"] == "0.012000"


def test_a_follow_up_shows_its_delivery_undo_time_and_a_blocked_one_its_reason(engine, clean):
    ada = Person(engine)
    campaign = ada.campaign("applied", follow_up_email="talent@lumen.example")
    undo = NOW + timedelta(minutes=8)
    sent_action, _ = follow_up_action(engine, campaign, ada.user, status="scheduled", approval=False, undo_until=undo)
    with engine.begin() as c:
        outbox.add(
            c,
            kind="follow_up_v1",
            dedupe_key=f"action:{sent_action}",
            user_id=ada.user,
            payload={"agent_action_id": sent_action},
            not_before=undo,
            agent_action_id=sent_action,
        )
    quote = "I led a team of forty."
    blocked, _ = follow_up_action(
        engine, campaign, ada.user, status="blocked", approval=False, outcome_code="unsupported_claim"
    )
    with engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO guardian_checks (agent_action_id, user_id, check_kind, passed, reason_code, details,
                                                guardian_version)
                   SELECT id, user_id, 'citations', false, 'unsupported_claim', CAST(:d AS jsonb), 'test-1'
                   FROM agent_actions WHERE id = :a"""
            ),
            {"a": blocked, "d": '{"quote": "%s"}' % quote},
        )
    step(engine, campaign, "follow_up:1", "chaser", "done", output={"action_id": sent_action, "approval_id": None})
    step(engine, campaign, "follow_up:2", "chaser", "failed", output={"action_id": blocked, "approval_id": None})

    detail = ada.read(api.detail, campaign)

    first, second = detail["follow_ups"]
    assert (first["step_key"], first["action_id"], first["status"], first["delivery"]) == (
        "follow_up:1",
        sent_action,
        "scheduled",
        "pending",
    )
    assert (
        first["to"] == "talent@lumen.example"
        and first["subject"] == FOLLOW_UP["subject"]
        and first["body"] == FOLLOW_UP["body"]
    )
    assert abs((first["undo_until"] - undo).total_seconds()) < 1
    assert first["outcome_text"] is None
    assert second["status"] == "blocked" and second["delivery"] is None
    assert second["outcome_code"] == "unsupported_claim"
    assert second["outcome_text"] == guardian.explain("unsupported_claim", quote=quote)


def test_an_edited_original_is_not_listed_and_its_replacement_keeps_the_step(engine, clean):
    ada = Person(engine)
    campaign = ada.campaign("applied", follow_up_email="talent@lumen.example")
    original, _ = follow_up_action(engine, campaign, ada.user, status="rejected", approval=False, outcome_code="edited")
    replacement, _ = follow_up_action(engine, campaign, ada.user, approval=True, inputs={"replaces": original})
    step(engine, campaign, "follow_up:1", "chaser", "waiting", output={"action_id": original, "approval_id": None})

    listed = ada.read(api.detail, campaign)["follow_ups"]

    assert [(f["action_id"], f["step_key"]) for f in listed] == [(replacement, "follow_up:1")]


def test_an_introduction_campaign_carries_the_employers_answer_and_due_time(engine, clean):
    from tests.hiring import factories as hiring

    ada = Person(engine)
    job = ada.job()
    with engine.begin() as c:
        candidate = hiring.make_candidate(c, name="Ada Example", email="ada-intro@mail.test")
        role = hiring.make_open_role(c, hiring.make_org(c, "Halden Demo"), title="Data engineer")
        ask = hiring.make_accepted(c, role, candidate)
        campaign = make_campaign(c, candidate.id, job.id, stage="applied", introduction_id=ask.id)
    with db.transaction(candidate.id) as conn:
        detail = api.detail(conn, candidate.id, campaign)
        board = api.board(conn, candidate.id)

    assert detail["from_introduction"] is True
    assert detail["introduction"]["id"] == ask.id and detail["introduction"]["answer_due_at"] is not None
    assert set(detail["introduction"]) == {"id", "employer_answer", "answer_due_at"}
    assert cards_of(board, "applied")[0]["from_introduction"] is True


def test_the_detail_works_on_a_campaign_no_step_has_written_yet(engine, clean):
    ada = Person(engine)
    campaign = ada.campaign()

    detail = ada.read(api.detail, campaign)

    assert detail["steps"] == [] and detail["dossier"] is None and detail["fit"] is None
    assert detail["packet"] is None and detail["prep"] is None and detail["follow_ups"] == []
    assert detail["next_step"] is None


# ---- other people, and the connection -------------------------------------------------------------------------------


def test_another_persons_campaign_is_not_found_and_not_on_the_board(engine, clean):
    ada, bob = Person(engine), Person(engine, "Other Co")
    mine = ada.campaign(title="Mine")
    theirs = bob.campaign(title="Theirs")

    assert ada.read(api.detail, theirs) is None
    board = ada.read(api.board, include_closed=True)
    assert [c["id"] for col in board["columns"] for c in col["campaigns"]] == [mine]
    assert bob.read(api.detail, mine) is None


def test_the_routes_return_the_envelope_and_404_for_anothers_campaign(engine, api_client):
    ada, bob = Person(engine), Person(engine, "Other Co")
    mine, theirs = ada.campaign(title="Mine"), bob.campaign(title="Theirs")
    api_client.app.dependency_overrides[current_user] = lambda: Principal(ada.user)

    board = api_client.get("/v1/campaigns")
    detail = api_client.get(f"/v1/campaigns/{mine}")
    other = api_client.get(f"/v1/campaigns/{theirs}")
    with_closed = api_client.get("/v1/campaigns?include_closed=true")

    assert board.status_code == 200 and [c["stage"] for c in board.json()["data"]["columns"]] == list(views.OPEN_STAGES)
    assert detail.status_code == 200 and detail.json()["data"]["id"] == str(mine)
    assert other.status_code == 404 and other.json()["error"]["code"] == "not_found"
    assert "Theirs" not in board.text + detail.text
    assert with_closed.json()["data"]["columns"][-1]["stage"] == "closed"
    assert api_client.get("/v1/campaigns/not-a-uuid").status_code == 422


def test_no_read_opens_a_worker_or_match_connection(engine, api_client, monkeypatch):
    """The API process holds only the API login: the unit tests hold all four URLs, so this test forbids the others."""
    ada, _, _ = full_campaign(engine)
    real = db.transaction

    def api_only(*args, **kwargs):
        assert kwargs.get("role", "api") == "api", "a read route opened a non-API connection"
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "transaction", api_only)
    api_client.app.dependency_overrides[current_user] = lambda: Principal(ada.user)
    for_job = api_client.get("/v1/campaigns?include_closed=true")
    assert for_job.status_code == 200
    campaign = for_job.json()["data"]["columns"][2]["campaigns"][0]["id"]
    assert api_client.get(f"/v1/campaigns/{campaign}").status_code == 200
