"""The campaign demo (plan 07-07, task 3): `demo seed-campaign` and `demo age-campaign`, and the default recordings they
go with. Everything is invented (backend/app/demo_data/campaign/README.md).

After the Phase 4 and 5 seeds and this one, the test drives the roadmap demo in process on the fake provider's DEFAULT
recordings (app/platform/llm/recorded/, which is what the demo stack's worker reads) and the invented company pages:
start, research, the packet, applied, a follow-up approved and undone, the dial set to Never, a blocked follow-up, the
interview and its prep. It then runs that flow and the Phase 5 packet flow in one process in both orders, because the
fake provider hands out its answers once per process and both demos share the Writer's and the checker's recordings.
The seed commits, so the tables are emptied around each test."""

import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from app import cli, demo
from app.apply import api as apply
from app.campaigns import jobs as campaign_jobs  # noqa: F401  (registers the campaign handlers)
from app.campaigns.crew import chaser, coach, research
from app.guardian import jobs as guardian_jobs  # noqa: F401  (registers guardian.release)
from app.guardian import runtime
from app.platform import config, db
from app.platform.llm import fake
from app.tailor import pipeline, service
from tests.campaigns.fixtures.graph import world as w
from tests.chat.fixtures.service import world as chat_world
from tests.guardian.conftest import clean  # noqa: F401  (the fixture that empties the tables around a committing test)
from tests.match.fixtures.run import world as match_world
from tests.test_demo_seed import fetch, scalar, settings_with, sign_in
from tests.test_demo_seed_p5 import FIRST_MATCH

RECORDED = demo.DATA.parent / "platform" / "llm" / "recorded"
PAGES = demo.CAMPAIGN_DATA / "pages"
TASKS = ("research.plan", "research.read_page", "chaser.draft", "coach.prep", "writer.packet", "verify.entail")
TYPED = "hiring@apply.initech-demo.invalid"


@pytest.fixture(autouse=True)
def demo_world(clean, monkeypatch):  # noqa: F811
    """A clean database, the fake provider on the default recordings, the invented pages, no embedding model."""
    runtime.close()
    match_world.no_embedder(monkeypatch)
    w.use_fakes(monkeypatch, pages=PAGES, recorded=None)
    monkeypatch.delenv("LLM_FAKE_DIR", raising=False)
    fake.replay_from(None)
    yield
    fake.replay_from(None)
    runtime.close()


# ---- the refusals ----------------------------------------------------------------------------------------------------


def seeded_account(engine) -> str:
    return sign_in(engine)


COMMANDS = [
    pytest.param(lambda email: demo.seed_campaign(email), id="seed-campaign"),
    pytest.param(lambda email: demo.age_campaign(email, 8), id="age-campaign"),
]


def nothing_written(engine) -> None:
    for table in ("documents", "jobs", "tailored_resumes", "campaigns", "profile_facts"):
        assert scalar(engine, f"SELECT count(*) FROM {table}") == 0, table


@pytest.mark.parametrize("name", ["app", "app_demo", "app_test2"])
@pytest.mark.parametrize("command", COMMANDS)
def test_both_commands_refuse_a_database_whose_name_does_not_end_in_test(engine, monkeypatch, name, command):
    email = seeded_account(engine)
    url = config.get().database_url_migrate.rsplit("/", 1)[0] + f"/{name}"
    settings_with(monkeypatch, database_url_migrate=url)
    with pytest.raises(demo.SeedRefused, match=rf"database '{name}'.*does not end in _test"):
        command(email)
    nothing_written(engine)


@pytest.mark.parametrize("command", COMMANDS)
def test_both_commands_refuse_a_login_connected_to_a_database_that_does_not_end_in_test(engine, monkeypatch, command):
    """The URL names a scratch database but the server says the login is connected to another: `current_database()`
    refuses, before a row is written."""
    email = seeded_account(engine)
    monkeypatch.setattr(demo, "_refuse_unless_scratch_and_fake", lambda: None)
    url = config.get().database_url_migrate.rsplit("/", 1)[0] + "/postgres"
    settings_with(monkeypatch, database_url_migrate=url)
    monkeypatch.setattr(db, "_engines", {})
    with pytest.raises(demo.SeedRefused, match=r"which does not end in _test"):
        command(email)
    nothing_written(engine)


@pytest.mark.parametrize("command", COMMANDS)
def test_both_commands_refuse_a_live_provider_and_a_missing_page_directory_and_an_unknown_address(
    engine, monkeypatch, command
):
    email = seeded_account(engine)
    settings_with(monkeypatch, llm_provider="anthropic")
    with pytest.raises(demo.SeedRefused, match="LLM_PROVIDER"):
        command(email)
    settings_with(monkeypatch, llm_provider="fake", research_pages_dir=None)
    with pytest.raises(demo.SeedRefused, match="RESEARCH_PAGES_DIR"):
        command(email)
    settings_with(monkeypatch, llm_provider="fake", research_pages_dir=str(PAGES))
    with pytest.raises(demo.SeedRefused, match="no active account"):
        command("nobody@example.com")
    nothing_written(engine)


def test_the_commands_are_registered_and_say_why_they_refused_and_exit_2(engine, capsys, monkeypatch):
    settings_with(monkeypatch, research_pages_dir=None)
    email = seeded_account(engine)
    assert cli.main(["demo", "seed-campaign", "--email", email]) == 2
    assert "RESEARCH_PAGES_DIR" in capsys.readouterr().err
    assert cli.main(["demo", "age-campaign", "--email", email, "--days", "8"]) == 2
    assert "RESEARCH_PAGES_DIR" in capsys.readouterr().err


# ---- the demo, in process --------------------------------------------------------------------------------------------

_key = iter(range(10**6))


def call(client, method: str, path: str, *, status: int = 200, **kwargs):
    """One API call with a fresh Idempotency-Key; the data of the envelope."""
    headers = {"Idempotency-Key": f"demo-key-{next(_key):06d}", **kwargs.pop("headers", {})}
    response = client.request(method, path, headers=headers, **kwargs)
    assert response.status_code == status, response.text
    return response.json()["data"]


def detail(client, campaign_id: str) -> dict:
    return call(client, "GET", f"/v1/campaigns/{campaign_id}")


def seed_in_this_process(email: str) -> demo.SeededCampaign:
    """The seeds replay their own recordings and so reset the fake's counters; the demo stack's worker never runs them,
    so the counters of this process are kept across the seed."""
    given = dict(fake._given)
    seeded = demo.seed_campaign(email)
    fake.replay_from(None)
    fake._given.update(given)
    return seeded


def start_campaign(engine, api_client) -> tuple[demo.SeededCampaign, str, w.World]:
    email = sign_in(engine)
    seeded = seed_in_this_process(email)
    chat_world.sign_in(api_client, seeded.user_id)
    started = call(api_client, "POST", "/v1/campaigns", status=201, json={"job_id": seeded.job_id})
    assert started["stage"] == "researching"
    return seeded, email, w.World(seeded.user_id, seeded.job_id, started["id"], {})


def campaign_flow(engine, api_client) -> None:
    seeded, email, world = start_campaign(engine, api_client)
    campaign = str(world.campaign)
    w.run_jobs()
    # research and the packet ran by themselves; the packet waits for the candidate
    found = detail(api_client, campaign)
    assert found["stage"] == "needs_you"
    dossier = found["dossier"]
    assert dossier is not None and not dossier["incomplete"] and len(dossier["claims"]) == 3
    page_text = {
        path: path.read_text(encoding="utf-8") for path in (PAGES / "apply.initech-demo.invalid").glob("*.html")
    }
    for claim in dossier["claims"]:
        source = claim["sources"][0]
        assert source["url"].startswith("https://apply.initech-demo.invalid/")
        assert any(source["quote"] in body for body in page_text.values())  # the quote is exact
    assert found["packet"]["action_status"] == "awaiting_approval"
    # approve the packet and run the release: the kit is scheduled for the candidate to use
    approval = found["packet"]["approval_id"]
    shown = call(api_client, "GET", f"/v1/approvals/{approval}")
    call(api_client, "POST", f"/v1/approvals/{approval}/approve", json={"payload_hash": shown["payload_hash"]})
    w.run_jobs()
    after = detail(api_client, campaign)
    assert after["packet"]["action_status"] == "scheduled" and after["packet"]["status"] == "approved"
    # the candidate applies elsewhere and records it; the follow-up address is typed
    packet = after["packet"]["id"]
    call(
        api_client,
        "POST",
        "/v1/applications",
        status=201,
        json={"job_id": seeded.job_id, "application_packet_id": packet},
    )
    w.run_jobs()
    assert detail(api_client, campaign)["stage"] == "applied"
    call(api_client, "PATCH", f"/v1/campaigns/{campaign}", json={"follow_up_email": TYPED})
    w.run_jobs()
    # eight quiet days later the Chaser has drafted one follow-up to the typed address, and it waits for approval
    aged = demo.age_campaign(email, 8)
    assert aged.campaigns == 1 and aged.wake_jobs >= 1
    w.run_jobs()
    follow = detail(api_client, campaign)["follow_ups"]
    assert [f["status"] for f in follow] == ["awaiting_approval"] and follow[0]["to"] == TYPED
    assert "Built the nightly Quillscript billing pipeline." in follow[0]["body"]
    # approve it, run the release, and undo it inside the window: nothing is sent
    shown = call(api_client, "GET", f"/v1/approvals/{follow[0]['approval_id']}")
    call(
        api_client,
        "POST",
        f"/v1/approvals/{follow[0]['approval_id']}/approve",
        json={"payload_hash": shown["payload_hash"]},
    )
    w.run_jobs()
    follow = detail(api_client, campaign)["follow_ups"]
    assert follow[0]["status"] == "scheduled" and follow[0]["undo_until"] is not None
    action = follow[0]["action_id"]
    undone = call(api_client, "POST", f"/v1/control/actions/{action}/undo")
    assert undone["action"]["status"] == "cancelled" and undone["outbox_cancelled"] == 1
    w.run_jobs()
    assert scalar(engine, "SELECT status::text FROM agent_actions WHERE id = :a", a=action) == "cancelled"
    assert fetch(engine, "SELECT status::text FROM outbox WHERE agent_action_id = :a", a=action) == [("cancelled",)]
    assert scalar(engine, "SELECT count(*) FROM outbox WHERE status = 'sent'") == 0
    # the candidate sets follow-ups to Never; eight days later the next one is blocked with its reason
    current = call(api_client, "GET", "/v1/autonomy")
    call(
        api_client,
        "PUT",
        "/v1/autonomy",
        headers={"If-Match": f'"{current["version"]}"'},
        json={"actions": {"send_follow_up": {"level": "never"}}},
    )
    demo.age_campaign(email, 8)
    w.run_jobs()
    blocked = fetch(
        engine,
        """SELECT status::text, outcome_code FROM agent_actions
            WHERE campaign_id = :c AND action_type = 'send_follow_up' AND status = 'blocked'""",
        c=world.campaign,
    )
    assert blocked == [("blocked", "dial_never")]
    assert scalar(engine, "SELECT status::text FROM campaign_steps WHERE step_key = 'follow_up:2'") == "failed"
    # the recorder shows every step, the blocked one with its reason
    rows = call(api_client, "GET", "/v1/control/recorder", params={"campaign_id": campaign})
    assert {"model_call", "tool_call", "state_change", "outside_action"} <= {r["kind"] for r in rows}
    assert [r["outcome_code"] for r in rows if r["status"] == "blocked"] == ["dial_never"]
    # the candidate records an interview: the Coach's prep is stored
    application = call(api_client, "GET", "/v1/applications")[0]["id"]
    call(
        api_client,
        "POST",
        f"/v1/applications/{application}/status",
        status=201,
        json={"status": "interview", "occurred_at": datetime.now(UTC).isoformat()},
    )
    w.run_jobs()
    final = detail(api_client, campaign)
    assert final["stage"] == "interview" and final["prep"]["questions"] and final["prep"]["ask_them"]
    assert final["prep"]["removed_by_reason"] == {} or final["prep"]["removed_by_reason"] is not None
    assert scalar(engine, "SELECT count(*) FROM llm_calls WHERE campaign_id = :c", c=world.campaign) > 5


def test_the_seed_is_idempotent_and_the_default_recordings_drive_the_campaign_demo(engine, api_client):
    campaign_flow(engine, api_client)
    again = demo.seed_campaign(fetch(engine, "SELECT email FROM users")[0][0])
    assert again.already and scalar(engine, "SELECT count(*) FROM tailored_resumes WHERE status = 'ready'") == 1


def phase5_packet_flow(engine, api_client) -> None:
    """The Phase 5 demo's tailoring and packet, as tests/test_demo_seed_p5.py drives them (without the chat messages and
    without its count of answers used, which another demo in the same process changes)."""
    email = sign_in(engine)
    given = dict(fake._given)
    matches = demo.seed_matches(email)
    fake.replay_from(None)
    fake._given.update(given)
    demo.seed_tailor(email)
    chat_world.sign_in(api_client, matches.user_id)
    job_id = scalar(engine, "SELECT job_id FROM match_decisions WHERE id = :d", d=matches.shown[0][0])
    assert scalar(engine, "SELECT title FROM jobs WHERE id = :j", j=job_id) == FIRST_MATCH
    tailored = call(api_client, "POST", "/v1/tailored-resumes", status=202, json={"job_id": job_id})
    with db.transaction(role="worker") as conn:
        service.generate(conn, {"tailored_resume_id": tailored["id"]})
    read = call(api_client, "GET", f"/v1/tailored-resumes/{tailored['id']}")
    assert read["status"] == "ready" and read["discarded_count"] == 2
    made = call(api_client, "POST", "/v1/applications/packets", status=202, json={"job_id": job_id})
    with db.transaction(role="worker") as conn:
        apply.draft_packet(conn, {"packet_id": made["id"]})
    view = call(api_client, "GET", f"/v1/applications/packets/{made['id']}")
    assert view["status"] == "ready"
    answers = {a["question_id"]: a for a in view["answers"]}
    assert (
        answers["why_role"]["answer"]
        == "Built the nightly Quillscript billing pipeline. Ran the PostgreSQL failover rehearsals."
    )
    assert answers["relevant_experience"]["removed_sentences"] == 1


def empty_tables(engine) -> None:
    with engine.begin() as c:
        c.execute(text("TRUNCATE users, jobs, job_sources, queue_jobs, llm_calls, outbox RESTART IDENTITY CASCADE"))


@pytest.mark.parametrize("campaign_first", [True, False], ids=["campaign-then-phase5", "phase5-then-campaign"])
def test_both_demos_pass_in_one_process_in_either_order(engine, api_client, campaign_first):
    """The fake provider hands out each recording's answers once per process, and both demos use the Writer's and the
    checker's. Run one after the other without resetting it, in both orders (INDEX decision 30)."""
    fake.replay_from(None)
    flows = [campaign_flow, phase5_packet_flow]
    for flow in flows if campaign_first else reversed(flows):
        empty_tables(engine)
        runtime.close()
        flow(engine, api_client)
    fake.replay_from(None)


def test_the_manifest_names_the_default_recordings_with_their_current_prompt_versions():
    manifest = json.loads((demo.CAMPAIGN_DATA / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "research.plan.json": {"research.plan": research.PLAN_VERSION},
        "research.read_page.json": {"research.read_page": research.READ_VERSION},
        "chaser.draft.json": {"chaser.draft": chaser.PROMPT_VERSION},
        "coach.prep.json": {"coach.prep": coach.PROMPT_VERSION},
        "writer.packet.json": {"writer.packet": apply_packet_version()},
        "verify.entail.json": {"verify.entail": pipeline.ENTAIL_VERSION},
    }
    assert set(TASKS) == {task for entry in manifest.values() for task in entry}
    for name in manifest:
        answers = json.loads((RECORDED / name).read_text(encoding="utf-8"))
        assert isinstance(answers, list) and answers  # the fake's plain format
    assert hashlib.sha256(chaser.PROMPT.encode()).hexdigest()[:12] == chaser.PROMPT_VERSION
    # the two shared recordings hold the Phase 5 answer repeated, so either demo may take any of them
    writer = json.loads((RECORDED / "writer.packet.json").read_text(encoding="utf-8"))
    assert len(writer) >= 2 and all(answer == writer[0] for answer in writer)
    entail = json.loads((RECORDED / "verify.entail.json").read_text(encoding="utf-8"))
    assert all(answer == entail[0] for answer in entail) and all(item["entailed"] for item in entail[0]["items"])


def apply_packet_version() -> str:
    from app.apply import packet

    return packet.PROMPT_VERSION
