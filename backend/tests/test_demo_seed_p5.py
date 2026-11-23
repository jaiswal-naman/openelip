"""The Phase 5 demo seed (plan 05-07, task 3): `python -m app.cli demo seed-tailor --email <address>`, and the default
recordings it goes with.

Everything is invented (backend/app/demo_data/phase5/README.md). After the Phase 4 seed and this one, the test drives
the whole roadmap demo in process on the fake provider's DEFAULT recordings (the files in app/platform/llm/recorded/,
which is what the demo stack's API and worker processes read): the scripted question through the chat routes, the
scripted second message with its one proposal, a tailored resume for the first match, and a packet. The fake provider
hands each recording's answers out in order, so the test resets it at the start and at the end.
"""

import hashlib
import json
from types import SimpleNamespace

import pytest

from app import cli, demo
from app.apply import api as apply
from app.apply import packet
from app.chat import loop, proposals
from app.ingest import api as ingest
from app.platform import config, db
from app.platform.llm import fake
from app.tailor import pipeline, service
from tests.chat.fixtures.service import world as chat_world
from tests.match.fixtures.run import world
from tests.test_demo_seed import EMPTY, fetch, scalar, settings_with, sign_in

RECORDED = demo.DATA.parent / "platform" / "llm" / "recorded"
MESSAGES = json.loads((demo.PHASE5 / "messages.json").read_text(encoding="utf-8"))
FIRST_MATCH = "Demo posting (invented): Quillscript Platform Engineer"


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    """A clean database, the fake provider on the default recordings, no embedding model, no apply-link request."""
    with engine.begin() as c:
        c.execute(EMPTY)
    world.no_embedder(monkeypatch)
    monkeypatch.setattr(ingest, "verify_live", lambda conn, job_id: None)
    world.use_provider(monkeypatch)
    fake.replay_from(None)
    yield
    fake.replay_from(None)
    with engine.begin() as c:
        c.execute(EMPTY)


def seeded_account(engine) -> tuple[str, demo.Seeded]:
    email = sign_in(engine)
    matches = demo.seed_matches(email)
    fake.replay_from(
        None
    )  # the matches seed leaves its own recording in place; the demo stack's processes never had it
    return email, matches


# ---- the refusals ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["app", "app_demo"])
def test_it_refuses_a_database_whose_name_does_not_end_in_test(engine, monkeypatch, name):
    email, _ = seeded_account(engine)
    url = config.get().database_url_migrate.rsplit("/", 1)[0] + f"/{name}"
    settings_with(monkeypatch, database_url_migrate=url)
    with pytest.raises(demo.SeedRefused, match=rf"database '{name}'.*does not end in _test"):
        demo.seed_tailor(email)


def test_it_refuses_a_live_model_provider(engine, monkeypatch):
    email, _ = seeded_account(engine)
    settings_with(monkeypatch, llm_provider="anthropic")
    with pytest.raises(demo.SeedRefused, match="LLM_PROVIDER"):
        demo.seed_tailor(email)
    assert scalar(engine, "SELECT count(*) FROM documents WHERE original_filename = :n", n=demo.RESUME5_FILENAME) == 0


def test_it_refuses_an_address_with_no_account():
    with pytest.raises(demo.SeedRefused, match="no active account for nobody@example.com"):
        demo.seed_tailor("nobody@example.com")


def test_it_refuses_an_account_the_matches_demo_has_not_seeded(engine):
    email = sign_in(engine)
    with pytest.raises(demo.SeedRefused, match="demo seed-matches"):
        demo.seed_tailor(email)
    assert scalar(engine, "SELECT count(*) FROM profile_facts") == 0


def test_the_command_is_registered_says_why_it_refused_and_exits_2(engine, capsys):
    args = cli.build_parser().parse_args(["demo", "seed-tailor", "--email", "a@example.com"])
    assert (args.group, args.command, args.email) == ("demo", "seed-tailor", "a@example.com")
    assert ("demo", "seed-tailor") in cli.COMMANDS
    assert cli.main(["demo", "seed-tailor", "--email", "nobody@example.com"]) == 2
    assert "refused: there is no active account for nobody@example.com" in capsys.readouterr().err


# ---- the seed and the recordings ------------------------------------------------------------------------------------


def test_the_manifest_names_every_default_recording_with_its_current_prompt_version():
    manifest = json.loads((demo.PHASE5 / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == {
        "chat.turn.json": {"chat.turn": loop.PROMPT_VERSION},
        "record.propose.json": {"record.propose": proposals.PROMPT_VERSION},
        "tailor.propose.json": {"tailor.propose": pipeline.PROPOSE_VERSION},
        "verify.entail.json": {"verify.entail": pipeline.ENTAIL_VERSION},
        "writer.packet.json": {"writer.packet": packet.PROMPT_VERSION},
    }
    for name in manifest:
        assert isinstance(json.loads((RECORDED / name).read_text(encoding="utf-8")), list)  # the fake's plain format
    assert hashlib.sha256(loop.PROMPT.encode()).hexdigest()[:12] == loop.PROMPT_VERSION


def test_the_seed_adds_invented_confirmed_facts_with_spans_and_a_second_run_changes_nothing(engine, capsys):
    email, matches = seeded_account(engine)
    before = scalar(engine, "SELECT record_version FROM users WHERE id = :u", u=matches.user_id)
    seeded = demo.seed_tailor(email)

    assert seeded.report().splitlines()[0] == f"Seeded the invented tailoring demo for {email}."
    kinds = [kind for kind, _, _ in seeded.facts]
    assert kinds == ["role", "achievement", "achievement", "role", "achievement", "achievement"] + ["skill"] * 5
    assert [s for _, s, _ in seeded.facts][-2:] == ["Docker", "Grafana"]
    assert scalar(engine, "SELECT record_version FROM users WHERE id = :u", u=matches.user_id) == before + 11

    quoted = fetch(
        engine,
        """SELECT f.statement, substr(d.extracted_text, lower(f.source_span) + 1,
                                       upper(f.source_span) - lower(f.source_span)) = f.source_quote AS quotes
             FROM profile_facts f JOIN documents d ON d.id = f.document_id
            WHERE f.user_id = :u AND d.original_filename = :n""",
        u=matches.user_id,
        n=demo.RESUME5_FILENAME,
    )
    assert len(quoted) == 9 and all(row.quotes for row in quoted)
    parents = fetch(
        engine,
        "SELECT count(*) AS n FROM profile_facts WHERE user_id = :u AND parent_fact_id IS NOT NULL",
        u=matches.user_id,
    )
    assert parents[0].n == 4
    said = fetch(engine, "SELECT statement FROM profile_facts WHERE source = 'confirmed_proposal' ORDER BY created_at")
    assert [row.statement for row in said] == ["Docker", "Grafana"]

    again = demo.seed_tailor(email)
    assert again.already and "already seeded" in again.report()
    assert scalar(engine, "SELECT record_version FROM users WHERE id = :u", u=matches.user_id) == before + 11
    assert scalar(engine, "SELECT count(*) FROM profile_facts WHERE user_id = :u", u=matches.user_id) == 3 + 11


# ---- the whole demo in process ---------------------------------------------------------------------------------------


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json()["data"]


def test_the_default_recordings_drive_the_demo_end_to_end(engine, api_client, monkeypatch):
    with engine.begin() as c:
        c.execute(EMPTY)  # api_client emptied the tables when it started: sign in after it
    email, matches = seeded_account(engine)
    seeded = demo.seed_tailor(email)
    user = matches.user_id
    chat_world.reset_hub(monkeypatch)
    chat_world.sign_in(api_client, user)
    w = SimpleNamespace(client=api_client, engine=engine)

    # message 1: the scripted question, answered with a citation to the decision the constraints filtered
    session = chat_world.open_session(w)
    first = chat_world.ok(chat_world.say(w, session, MESSAGES["question"]), 202)["data"]
    chat_world.wait_done(first["message_id"])
    rows = chat_world.transcript(w, session).json()["data"]
    reply = rows[-1]
    assert reply["role"] == "assistant" and reply["finish_reason"] == "complete"
    (filtered,) = [decision for decision, _ in matches.filtered]
    assert [(c["alias"], c["kind"], c["decision_id"]) for c in reply["citations"]] == [
        ("d1", "decision", str(filtered))
    ]
    assert "[[d1]]" in reply["text"] and "United States" in reply["text"]

    # message 2: one proposal, and the record is as it was until it is confirmed
    version = scalar(engine, "SELECT record_version FROM users WHERE id = :u", u=user)
    second = chat_world.ok(chat_world.say(w, session, MESSAGES["proposal"]), 202)["data"]
    chat_world.wait_done(second["message_id"])
    reply = chat_world.transcript(w, session).json()["data"][-1]
    assert reply["finish_reason"] == "complete" and len(reply["proposals"]) == 1
    proposal = reply["proposals"][0]
    assert (proposal["entity"], proposal["operation"], proposal["after"]["statement"]) == ("fact", "create", "Rust")
    assert scalar(engine, "SELECT record_version FROM users WHERE id = :u", u=user) == version
    assert scalar(engine, "SELECT count(*) FROM profile_facts WHERE statement = 'Rust'") == 0
    chat_world.finish(engine)

    # a tailored resume for the first match: five edits, three shown, two discarded, nothing rephrased
    job_id = scalar(engine, "SELECT job_id FROM match_decisions WHERE id = :d", d=matches.shown[0][0])
    assert scalar(engine, "SELECT title FROM jobs WHERE id = :j", j=job_id) == FIRST_MATCH
    key = {"Idempotency-Key": "demo-tailor-1"}
    tailored = ok(api_client.post("/v1/tailored-resumes", json={"job_id": job_id}, headers=key), 202)
    with db.transaction(role="worker") as conn:
        service.generate(conn, {"tailored_resume_id": tailored["id"]})
    read = ok(api_client.get(f"/v1/tailored-resumes/{tailored['id']}"))
    assert read["status"] == "ready" and read["discarded_count"] == 2
    assert [e["op"] for e in read["edits"]] == ["reorder", "remove", "add_from_fact"]
    assert read["edits"][2]["after_text"] == "Docker"
    assert read["discarded_by_reason"] == {"new_entity": 1, "no_supporting_fact": 1}

    # a packet: ready, with sentences that passed both layers, and a PDF
    made = ok(
        api_client.post(
            "/v1/applications/packets", json={"job_id": job_id}, headers={"Idempotency-Key": "demo-packet-1"}
        ),
        202,
    )
    with db.transaction(role="worker") as conn:
        apply.draft_packet(conn, {"packet_id": made["id"]})
    view = ok(api_client.get(f"/v1/applications/packets/{made['id']}"))
    assert view["status"] == "ready" and view["tailored_resume_id"] == tailored["id"]
    answers = {a["question_id"]: a for a in view["answers"]}
    assert (
        answers["why_role"]["answer"]
        == "Built the nightly Quillscript billing pipeline. Ran the PostgreSQL failover rehearsals."
    )
    assert answers["relevant_experience"]["removed_sentences"] == 1
    assert answers["expected_pay"]["status"] == "candidate_must_answer"
    pdf = api_client.get(f"/v1/applications/packets/{made['id']}/file", params={"format": "pdf"})
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")

    # the answers of each default recording were all used and no task needed one it does not hold
    for name, expected in (("chat.turn.json", 4), ("record.propose.json", 1), ("tailor.propose.json", 1)):
        assert fake._given[RECORDED / name] == expected
    assert fake._given[RECORDED / "verify.entail.json"] == 1 and fake._given[RECORDED / "writer.packet.json"] == 1
    assert len(seeded.facts) == 11
