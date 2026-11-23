"""The hiring desk demo seed (plan 06-07): `python -m app.cli demo seed-hiring --employer-email A --candidate-email B`.

The organization, the candidates and the role are invented (app/demo_data/hiring/README.md). The model is the real
gateway on the fake provider, answering from the demo's own recordings through LLM_FAKE_DIR, so the whole chain
runs: the seed, the role read and confirmed and published, the ask job with its role run and its database guards, the
accept of the consent sheet, and the employer's answer. Nothing reaches a model or the network.

The seed commits (it opens its own connections), so the tables are emptied around each test.
"""

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app import cli, demo, demo_hiring
from app.hiring import ask_jobs, introductions, roles
from app.hiring.roles import RequirementInput
from app.platform import config, db
from tests.hiring import asks_world
from tests.match.fixtures.run import world

FAKE_DIR = demo_hiring.DATA / "recorded"
ROLE_TEXT = (demo_hiring.DATA / "role.txt").read_text(encoding="utf-8")
EMPTY = asks_world.EMPTY
VERSION = "introduction-2026-11"


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    yield from asks_world.committed(engine, monkeypatch)


@pytest.fixture
def provider(monkeypatch):
    calls = world.use_provider(monkeypatch)  # the scripted source is cleared: the demo directory answers
    monkeypatch.setenv("LLM_FAKE_DIR", str(FAKE_DIR))
    return calls


def sign_in(engine, label):
    email = f"demo-{label}-{uuid4().hex[:8]}@example.com"
    with engine.begin() as c:
        c.execute(text("INSERT INTO users (email) VALUES (:e)"), {"e": email})
    return email


def accounts(engine):
    return sign_in(engine, "employer"), sign_in(engine, "candidate")


def fetch(engine, sql, **values):
    with engine.connect() as c:
        return c.execute(text(sql), values).all()


def scalar(engine, sql, **values):
    return fetch(engine, sql, **values)[0][0]


def settings_with(monkeypatch, **changes):
    current = config.get()
    monkeypatch.setattr(config, "get", lambda: replace(current, **changes))


# ---- the refusals ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["migrate", "worker", "match"])
def test_it_refuses_when_any_login_it_uses_points_at_a_real_database(engine, provider, monkeypatch, role):
    employer, candidate = accounts(engine)
    url = getattr(config.get(), f"database_url_{role}").rsplit("/", 1)[0] + "/app"
    settings_with(monkeypatch, **{f"database_url_{role}": url})
    with pytest.raises(demo.SeedRefused, match=f"the {role} login"):
        demo_hiring.seed_hiring(employer, candidate)
    assert scalar(engine, "SELECT count(*) FROM organizations") == 0


def test_it_refuses_a_url_that_names_a_scratch_database_but_reaches_another(engine, provider, monkeypatch):
    """06-VERIFICATION W3: a `dbname` query parameter overrides the URL's path, so the name check alone passed a login
    that reached a real database. The server's answer to current_database() refuses before a row is written."""
    employer, candidate = accounts(engine)
    url = config.get().database_url_migrate.split("?", 1)[0] + "?dbname=postgres"
    settings_with(monkeypatch, database_url_migrate=url)
    monkeypatch.setattr(db, "_engines", {})  # engines are made from the settings once: make this role's again
    try:
        with pytest.raises(demo.SeedRefused, match=r"connected to database 'postgres', which does not end in _test"):
            demo_hiring.seed_hiring(employer, candidate)
    finally:
        for made in db._engines.values():
            made.dispose()
    assert scalar(engine, "SELECT count(*) FROM organizations") == 0


@pytest.mark.parametrize("name", ["anthropic", "openai", "openai_compatible"])
def test_it_refuses_a_live_model_provider(engine, provider, monkeypatch, name):
    employer, candidate = accounts(engine)
    settings_with(monkeypatch, llm_provider=name)
    with pytest.raises(demo.SeedRefused, match="LLM_PROVIDER"):
        demo_hiring.seed_hiring(employer, candidate)
    assert scalar(engine, "SELECT count(*) FROM organizations") == 0


def test_it_refuses_an_address_with_no_account_and_writes_nothing(engine, provider):
    employer, candidate = accounts(engine)
    with pytest.raises(demo.SeedRefused, match="no active account for nobody@example.com"):
        demo_hiring.seed_hiring(employer, "nobody@example.com")
    with pytest.raises(demo.SeedRefused, match="no active account for nobody@example.com"):
        demo_hiring.seed_hiring("nobody@example.com", candidate)
    assert scalar(engine, "SELECT count(*) FROM organizations") == 0
    assert scalar(engine, "SELECT count(*) FROM users WHERE is_synthetic") == 0


def test_it_refuses_one_account_as_both(engine, provider):
    employer, _ = accounts(engine)
    with pytest.raises(demo.SeedRefused, match="two different accounts"):
        demo_hiring.seed_hiring(employer, employer.upper())


def test_the_command_says_why_it_refused_and_exits_2(engine, provider, monkeypatch, capsys):
    settings_with(monkeypatch, llm_provider="anthropic")
    assert (
        cli.main(["demo", "seed-hiring", "--employer-email", "a@example.com", "--candidate-email", "b@example.com"])
        == 2
    )
    assert "refused: LLM_PROVIDER is 'anthropic'" in capsys.readouterr().err


def test_the_command_is_registered_and_needs_both_addresses():
    args = cli.build_parser().parse_args(
        ["demo", "seed-hiring", "--employer-email", "a@x.test", "--candidate-email", "b@x.test"]
    )
    assert (args.group, args.command, args.employer_email, args.candidate_email) == (
        "demo",
        "seed-hiring",
        "a@x.test",
        "b@x.test",
    )
    assert ("demo", "seed-hiring") in cli.COMMANDS
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["demo", "seed-hiring", "--employer-email", "a@x.test"])


# ---- lesson of the Phase 5 demo: the seed runs in the worker container, which has no API login ------------------


def test_the_seed_opens_connections_only_with_the_logins_its_scratch_check_covers(engine, provider, monkeypatch):
    employer, candidate = accounts(engine)
    opened = []
    real = db.engine
    monkeypatch.setattr(db, "engine", lambda role: opened.append(role) or real(role))

    demo_hiring.seed_hiring(employer, candidate)

    assert opened, "the spy saw no connection: it would pass for any seed"
    assert set(opened) <= {"migrate", "worker", "match"}, (
        f"the seed opened a login its scratch check does not cover: {opened}"
    )
    assert "api" not in opened
    assert set(demo_hiring.SEED_LOGINS) <= {"migrate", "worker", "match"}


def test_the_seed_works_when_the_api_login_cannot_connect_at_all(engine, provider, monkeypatch):
    """The worker container has no DATABASE_URL_API, so the default is a localhost URL: nothing may use it."""
    employer, candidate = accounts(engine)
    settings_with(monkeypatch, database_url_api="postgresql://nobody:nothing@127.0.0.1:1/app")
    monkeypatch.setattr(db, "_engines", {})
    demo_hiring.seed_hiring(employer, candidate)
    assert scalar(engine, "SELECT count(*) FROM organizations WHERE is_synthetic") == 1


# ---- the seed -------------------------------------------------------------------------------------------------------


def test_the_employer_owns_halden_demo_and_the_candidates_are_invented_and_ready(engine, provider):
    employer, candidate = accounts(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE users SET display_name = 'Pat Chosen' WHERE email = :e"), {"e": candidate})

    seeded = demo_hiring.seed_hiring(employer, candidate)

    org = fetch(engine, "SELECT id, name, timezone, is_synthetic FROM organizations")
    assert [(o.name, o.timezone, o.is_synthetic) for o in org] == [("Halden Demo", "Europe/Berlin", True)]
    owner = fetch(
        engine,
        "SELECT m.role::text AS role, m.status::text AS status, u.email "
        "FROM org_members m JOIN users u ON u.id = m.user_id",
    )
    assert [(o.role, o.status, o.email) for o in owner] == [("owner", "active", employer)]

    synthetic = fetch(engine, "SELECT email, display_name FROM users WHERE is_synthetic ORDER BY email")
    assert [s.display_name for s in synthetic] == [
        "Demo Blocked Candidate",
        *(f"Demo Candidate {n}" for n in range(1, 7)),
    ]
    assert all(s.email.endswith("@example.test") for s in synthetic)
    ordinary = fetch(engine, "SELECT email, display_name FROM users WHERE NOT is_synthetic ORDER BY email")
    assert {o.email for o in ordinary} == {
        employer,
        candidate,
    }  # the signed-in accounts stay ordinary: Mailpit gets their mail
    assert scalar(engine, "SELECT display_name FROM users WHERE email = :e", e=candidate) == "Pat Chosen"  # kept
    assert scalar(engine, "SELECT display_name FROM users WHERE email = :e", e=employer) is None  # not touched

    # seven candidates (B and six invented) have the same confirmed record and live consents; the employer has none
    people = [candidate, *(f"demo.candidate{n}@example.test" for n in range(1, 7))]
    for email in people:
        user = scalar(engine, "SELECT id FROM users WHERE email = :e", e=email)
        facts = fetch(
            engine,
            """SELECT f.kind::text AS kind, f.confirmed_at IS NOT NULL AS confirmed,
                      substr(d.extracted_text, lower(f.source_span) + 1, upper(f.source_span) - lower(f.source_span))
                          = f.source_quote AS quotes
                 FROM profile_facts f JOIN documents d ON d.id = f.document_id
                WHERE f.user_id = :u ORDER BY lower(f.source_span)""",
            u=user,
        )
        assert [(f.kind, f.confirmed, f.quotes) for f in facts] == [
            ("role", True, True),
            ("skill", True, True),
            ("achievement", True, True),
            ("education", True, True),
        ]
        assert scalar(engine, "SELECT record_confirmed_at IS NOT NULL FROM users WHERE id = :u", u=user)
        assert scalar(
            engine,
            "SELECT has_active_consent(:u, 'open_to_introductions') AND has_active_consent(:u, 'ai_processing')",
            u=user,
        )
    assert (
        scalar(
            engine,
            "SELECT count(*) FROM profile_facts WHERE user_id = (SELECT id FROM users WHERE email = :e)",
            e=employer,
        )
        == 0
    )

    # the seventh invented account blocked the organization, through the constraint a candidate would set
    blocked = fetch(
        engine,
        "SELECT c.kind::text AS kind, c.company_names FROM constraints c JOIN users u ON u.id = c.user_id "
        "WHERE u.email = 'demo.blocked@example.test'",
    )
    assert [(b.kind, b.company_names) for b in blocked] == [("excluded_company", ["Halden Demo"])]
    assert scalar(engine, "SELECT count(*) FROM constraints") == 1
    assert seeded.created and seeded.synthetic_candidates == 7
    assert "Seeded the invented hiring demo." in seeded.report()


def test_running_it_twice_changes_nothing_the_second_time(engine, provider):
    employer, candidate = accounts(engine)
    demo_hiring.seed_hiring(employer, candidate)
    tables = [
        "organizations",
        "org_members",
        "users",
        "documents",
        "profile_facts",
        "profile_events",
        "constraints",
        "consents",
    ]
    before = {t: scalar(engine, f"SELECT count(*) FROM {t}") for t in tables}
    version = scalar(engine, "SELECT record_version FROM users WHERE email = :e", e=candidate)

    again = demo_hiring.seed_hiring(employer, candidate)

    assert not again.created and "Already seeded" in again.report()
    assert {t: scalar(engine, f"SELECT count(*) FROM {t}") for t in tables} == before
    assert scalar(engine, "SELECT record_version FROM users WHERE email = :e", e=candidate) == version


def test_the_recordings_are_what_the_readme_says(engine):
    extract = json.loads((FAKE_DIR / "job.requirements.extract.json").read_text(encoding="utf-8"))
    evidence = json.loads((FAKE_DIR / "match.evidence.score.json").read_text(encoding="utf-8"))
    assert len(extract) >= 2 and all(a == extract[0] for a in extract)
    assert len(evidence) >= 21 and all(a == evidence[0] for a in evidence)  # identical: call order cannot matter
    lines = [line for line in ROLE_TEXT.splitlines() if line.strip()]
    assert len(lines) == 6 and lines[2] == "Python and SQL services in production."


# ---- the whole flow, in process -----------------------------------------------------------------------------


def test_post_the_role_ask_seven_accept_as_the_candidate_and_answer_yes_as_the_employer(engine, provider):
    employer_email, candidate_email = accounts(engine)
    demo_hiring.seed_hiring(employer_email, candidate_email)
    employer = scalar(engine, "SELECT id FROM users WHERE email = :e", e=employer_email)
    candidate = scalar(engine, "SELECT id FROM users WHERE email = :e", e=candidate_email)
    org = scalar(engine, "SELECT id FROM organizations")

    # as the employer: describe the role, confirm every extracted requirement, set the pay range, publish
    with db.transaction(employer) as conn:
        made = roles.create(conn, employer, org_id=org, title="Backend engineer", description=ROLE_TEXT)
    assert made["extraction"] == {"state": "extracted", "dropped": 0}
    assert [r["text"] for r in made["requirements"]] == [
        "Python and SQL services in production.",
        "3+ years as a backend engineer.",
        "Experience leading a small team is a plus.",
    ]
    items = [
        RequirementInput(r["kind"], r["text"], r["importance"], True, (r["span"]["start"], r["span"]["end"]))
        for r in made["requirements"]
    ]
    with db.transaction(employer) as conn:
        roles.set_requirements(conn, employer, made["id"], items)
        ready = roles.update(
            conn, employer, made["id"], {"pay_range": {"min": 70_000, "max": 95_000, "currency": "EUR"}}
        )
    assert ready["can_publish"] is True, ready["blockers"]
    roles.publish(employer, made["id"])

    # the worker's ask job reads the queue payload the publish left
    (job,) = fetch(engine, "SELECT payload FROM queue_jobs WHERE kind = 'hiring.ask_candidates'")
    asks_world.worker(ask_jobs.ask_candidates, job.payload)

    # seven pass the hard filter (the blocked account does not) and all seven qualify; the counts show no one
    with db.transaction(employer) as conn:
        preview = conn.execute(text("SELECT role_preview(:r)"), {"r": made["id"]}).scalar_one()
    assert (preview["eligible_candidates"], preview["selected_to_ask"]) == (7, 7)
    asked = fetch(engine, "SELECT user_id FROM introductions")
    assert len(asked) == 7
    blocked = scalar(engine, "SELECT id FROM users WHERE email = 'demo.blocked@example.test'")
    assert blocked not in {a.user_id for a in asked}
    evidence_calls = [c for c in provider if c[0] == "match.evidence.score"]
    assert len(evidence_calls) == 7  # one answer each: 21 recorded is three times what a run needs

    # as the candidate: the ask, the exact preview and its hash, accept
    (mine,) = fetch(engine, "SELECT id FROM introductions WHERE user_id = :u", u=candidate)
    with db.transaction(candidate) as conn:
        shown = introductions.get(conn, candidate, mine.id)
    assert shown["status"] == "asked" and shown["organization"]["name"] == "Halden Demo"
    assert shown["organization"]["is_synthetic"] is True and shown["role"]["pay_range"]["currency"] == "EUR"
    evidence = shown["share_preview"]["on_accept"]["evidence"]
    assert [row["verdict"] for row in evidence] == ["met", "met", "partly_met"]
    assert all(row["record_quotes"] for row in evidence)
    with db.transaction(candidate) as conn:
        introductions.accept(conn, candidate, mine.id, shown["share_preview_hash"], shown["consent_text_version"])

    # as the employer: one said-yes entry with the evidence, then the answer
    from app.hiring import answers

    with db.transaction(employer) as conn:
        shortlist = answers.shortlist(conn, employer, made["id"], limit=20, cursor=None)
    entries = shortlist[0] if isinstance(shortlist, tuple) else shortlist
    assert len(entries) == 1 and entries[0]["email"] is None  # no email before a yes
    with db.transaction(employer) as conn:
        answers.answer(conn, employer, mine.id, "yes", "Strong fit for the role.")
    with db.transaction(candidate) as conn:
        after = introductions.get(conn, candidate, mine.id)
    assert (after["employer_answer"], after["employer_answer_reason"]) == ("yes", "Strong fit for the role.")
    assert after["status"] == "accepted"
