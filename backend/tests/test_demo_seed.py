"""The demo seed (plan 04-06, task 3): `python -m app.cli demo seed-matches --email <address>`.

The candidate, the employer and the postings are invented (app/demo_data/README.md). The model is the real gateway on
the fake provider, answering from the demo's recording, so the whole chain runs: the account, the record, the postings,
the match run with its checker and its database guards, the brief, and the email the relay would send. Nothing reaches
a model or the network: the apply-link re-check is replaced, because it is the one step that would call out.

The seed commits (it opens its own connections), so the tables are emptied around each test.
"""

import json
import re
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app import cli, demo
from app.brief import mail
from app.ingest import api as ingest
from app.match import evidence
from app.match.validator import Accepted, VerdictSet, validate
from app.platform import config
from app.platform.relay import Outgoing
from app.profile import api as profile
from tests.match import factories
from tests.match.fixtures.run import world

POSTINGS = json.loads((demo.DATA / "postings.json").read_text(encoding="utf-8"))
RECORDED = json.loads(demo.RECORDING.read_text(encoding="utf-8"))
EXCLUDED = "Demo posting (invented): Onsite Support Engineer"
EMPTY = text("TRUNCATE users, jobs, job_sources, queue_jobs, llm_calls, outbox RESTART IDENTITY CASCADE")


@pytest.fixture(autouse=True)
def committed(engine, monkeypatch):
    """A clean database, the fake provider with the request log, no embedding model, and no apply-link request."""
    with engine.begin() as c:
        c.execute(EMPTY)
    world.no_embedder(monkeypatch)
    monkeypatch.setattr(ingest, "verify_live", lambda conn, job_id: None)  # "could not tell": the offline answer
    yield
    with engine.begin() as c:
        c.execute(EMPTY)


@pytest.fixture
def provider(monkeypatch):
    return world.use_provider(monkeypatch)  # the demo sets its own recording; this undoes it at the end of the test


def sign_in(engine, email=None):
    """The account a person's first sign-in makes: an address and the defaults, nothing else."""
    email = email or f"demo-{uuid4().hex[:8]}@example.com"
    with engine.begin() as c:
        c.execute(text("INSERT INTO users (email) VALUES (:e)"), {"e": email})
    return email


def fetch(engine, sql, **values):
    with engine.connect() as c:
        return c.execute(text(sql), values).all()


def scalar(engine, sql, **values):
    return fetch(engine, sql, **values)[0][0]


def settings_with(monkeypatch, **changes):
    current = config.get()  # taken now: the replacement below must not call itself
    monkeypatch.setattr(config, "get", lambda: replace(current, **changes))


# ---- the refusals ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["app", "app_demo", "chessai_prod"])
def test_it_refuses_a_database_whose_name_does_not_end_in_test(engine, provider, monkeypatch, name):
    email = sign_in(engine)
    settings = config.get()
    url = settings.database_url_migrate.rsplit("/", 1)[0] + f"/{name}"
    settings_with(monkeypatch, database_url_migrate=url)
    with pytest.raises(demo.SeedRefused, match=rf"database '{name}'.*does not end in _test"):
        demo.seed_matches(email)
    assert scalar(engine, "SELECT display_name FROM users WHERE lower(email) = :e", e=email) is None  # nothing written


@pytest.mark.parametrize("role", ["worker", "match"])
def test_it_refuses_when_any_of_the_logins_it_uses_points_at_a_real_database(engine, provider, monkeypatch, role):
    email = sign_in(engine)
    url = getattr(config.get(), f"database_url_{role}").rsplit("/", 1)[0] + "/app"
    settings_with(monkeypatch, **{f"database_url_{role}": url})
    with pytest.raises(demo.SeedRefused, match=f"the {role} login"):
        demo.seed_matches(email)


@pytest.mark.parametrize("name", ["anthropic", "openai", "openai_compatible"])
def test_it_refuses_a_live_model_provider(engine, provider, monkeypatch, name):
    email = sign_in(engine)
    settings_with(monkeypatch, llm_provider=name)
    with pytest.raises(demo.SeedRefused, match="LLM_PROVIDER"):
        demo.seed_matches(email)
    assert scalar(engine, "SELECT count(*) FROM jobs") == 0


def test_it_refuses_an_address_with_no_account(engine, provider):
    with pytest.raises(demo.SeedRefused, match="no active account for nobody@example.com"):
        demo.seed_matches("nobody@example.com")
    assert scalar(engine, "SELECT count(*) FROM jobs") == 0  # the postings were not added either


def test_it_refuses_an_index_that_holds_postings_that_are_not_the_demos(engine, provider):
    email = sign_in(engine)
    with engine.begin() as c:
        factories.make_job(
            c,
            title="Another role",
            description="Needs Python.",
            requirements=[("Python", "required")],
            company_name="Another Co",
        )
    with pytest.raises(demo.SeedRefused, match="holds 1 open postings that are not part of the demo"):
        demo.seed_matches(email)
    assert scalar(engine, "SELECT display_name FROM users WHERE lower(email) = :e", e=email) is None


def test_the_command_says_why_it_refused_and_exits_2(engine, provider, monkeypatch, capsys):
    settings_with(monkeypatch, llm_provider="anthropic")
    assert cli.main(["demo", "seed-matches", "--email", "a@example.com"]) == 2
    assert "refused: LLM_PROVIDER is 'anthropic'" in capsys.readouterr().err


def test_the_command_is_registered_and_needs_an_address():
    args = cli.build_parser().parse_args(["demo", "seed-matches", "--email", "a@example.com"])
    assert (args.group, args.command, args.email) == ("demo", "seed-matches", "a@example.com")
    assert ("demo", "seed-matches") in cli.COMMANDS
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["demo", "seed-matches"])


# ---- the seed -------------------------------------------------------------------------------------------------------


def test_the_account_becomes_a_demo_candidate_with_a_confirmed_record_and_the_three_consents(engine, provider):
    email = sign_in(engine)
    seeded = demo.seed_matches(email)

    (user,) = fetch(
        engine,
        """SELECT display_name, is_synthetic, timezone, brief_frequency::text AS frequency, brief_weekday,
                  record_version, record_confirmed_at, status::text AS status FROM users WHERE lower(email) = :e""",
        e=email,
    )
    assert (user.display_name, user.is_synthetic, user.frequency, user.brief_weekday) == (
        "Demo Candidate",
        False,
        "daily",
        None,
    )
    assert user.status == "active" and user.record_confirmed_at is not None
    now = datetime.now(timezone.utc)
    assert demo.in_window(user.timezone, now) and seeded.timezone == user.timezone  # morning in its own zone
    assert ZoneInfo(user.timezone).utcoffset(now).total_seconds() % 3600 == 0  # a whole-hour zone

    granted = fetch(
        engine,
        """SELECT purpose FROM consents g WHERE g.user_id = :u AND g.action = 'granted'
              AND NOT EXISTS (SELECT 1 FROM consents w WHERE w.withdraws_id = g.id) ORDER BY purpose""",
        u=seeded.user_id,
    )
    assert sorted(row.purpose for row in granted) == ["ai_processing", "brief_email", "terms"]

    # the record: a parsed DOCX, three confirmed facts that quote it, two hard constraints, one change-log row each
    assert scalar(engine, "SELECT status::text FROM documents WHERE user_id = :u", u=seeded.user_id) == "parsed"
    facts = fetch(
        engine,
        """SELECT f.kind::text AS kind, f.confirmed_at IS NOT NULL AS confirmed,
                  substr(d.extracted_text, lower(f.source_span) + 1,
                         upper(f.source_span) - lower(f.source_span)) = f.source_quote AS quotes
             FROM profile_facts f JOIN documents d ON d.id = f.document_id
            WHERE f.user_id = :u ORDER BY lower(f.source_span)""",
        u=seeded.user_id,
    )
    assert [(f.kind, f.confirmed, f.quotes) for f in facts] == [
        ("role", True, True),
        ("skill", True, True),
        ("achievement", True, True),
    ]
    constraints = fetch(
        engine,
        "SELECT kind::text AS kind, statement FROM constraints WHERE user_id = :u ORDER BY kind",
        u=seeded.user_id,
    )
    assert [(c.kind, c.statement) for c in constraints] == [
        ("location", "Only jobs in the United States."),
        ("remote_policy", "Remote or hybrid jobs only."),
    ]
    assert (
        user.record_version
        == 5
        == scalar(engine, "SELECT count(*) FROM profile_events WHERE user_id = :u", u=seeded.user_id)
    )


def test_four_invented_postings_each_with_four_requirements_are_in_the_index(engine, provider):
    demo.seed_matches(sign_in(engine))
    jobs = fetch(
        engine,
        """SELECT j.id, j.title, j.company_name, j.apply_url, j.description, s.is_active
             FROM jobs j JOIN job_sources s ON s.id = j.source_id ORDER BY j.id""",
    )
    assert len(jobs) == 4
    assert all(j.title.startswith("Demo posting (invented): ") and j.company_name == "Initech Demo" for j in jobs)
    assert all(re.fullmatch(r"https://apply\.initech-demo\.invalid/\d", j.apply_url) for j in jobs)  # can never resolve
    assert not any(j.is_active for j in jobs)  # the worker never fetches a board that does not exist
    for job in jobs:
        requirements = fetch(
            engine,
            """SELECT r.text,
                      substr(j.description, lower(r.span) + 1, upper(r.span) - lower(r.span)) = r.text AS verbatim
                 FROM job_requirements r JOIN jobs j ON j.id = r.job_id WHERE r.job_id = :j ORDER BY r.ordinal""",
            j=job.id,
        )
        assert len(requirements) == 4 and all(r.verbatim for r in requirements)


def test_the_match_run_shows_three_with_four_evidence_rows_and_files_the_excluded_posting_as_filtered(engine, provider):
    seeded = demo.seed_matches(sign_in(engine))

    assert (seeded.run_shown, len(seeded.shown), len(seeded.filtered)) == (3, 3, 1)
    assert [title for _, title in seeded.shown] == [
        "Demo posting (invented): Quillscript Platform Engineer",
        "Demo posting (invented): Data Reliability Engineer",
        "Demo posting (invented): Ledger Backend Engineer",
    ]
    assert [title for _, title in seeded.filtered] == [EXCLUDED]

    run = fetch(
        engine,
        "SELECT trigger::text AS trigger, status::text AS status, error_code FROM match_runs WHERE id = :r",
        r=seeded.run_id,
    )[0]
    assert (run.trigger, run.status, run.error_code) == (
        "on_demand",
        "completed",
        None,
    )  # the checker agreed with the filter
    assert [
        row.outcome
        for row in fetch(engine, "SELECT outcome::text AS outcome FROM match_decisions WHERE outcome <> 'filtered'")
    ] == ["shown"] * 3

    for decision, _ in seeded.shown:
        verdicts = fetch(
            engine,
            """SELECT e.verdict::text AS verdict
                 FROM match_evidence e JOIN job_requirements r ON r.id = e.job_requirement_id
                WHERE e.match_decision_id = :d ORDER BY r.ordinal""",
            d=decision,
        )
        assert [v.verdict for v in verdicts] == ["met", "met", "no_evidence", "partly_met"]  # four rows, as recorded
    (filtered,) = fetch(
        engine,
        "SELECT violated_kinds::text[] AS kinds, reason_codes::text[] AS reasons FROM match_decisions WHERE id = :d",
        d=seeded.filtered[0][0],
    )
    assert sorted(filtered.kinds) == ["location", "remote_policy"] and filtered.reasons == ["hard_constraint"]


def test_one_provider_request_is_made_for_each_posting_that_is_scored_and_none_for_the_filtered_one(engine, provider):
    demo.seed_matches(sign_in(engine))
    assert [call[0] for call in provider] == ["match.evidence.score"] * 3
    assert len(RECORDED) == 3  # the recording holds exactly the answers the run asks for


def test_the_recorded_answer_is_valid_for_every_eligible_posting_whatever_the_order_they_are_scored_in(
    engine, provider
):
    """The recording is replayed in the order the run scores postings, which the search ranking decides. So each answer
    must pass the citation validator against any of the three postings, not only the one it happens to land on."""
    seeded = demo.seed_matches(sign_in(engine))
    with engine.connect() as c:
        facts = profile.usable_facts(c, seeded.user_id)
        job_ids = [r.id for r in c.execute(text("SELECT id FROM jobs WHERE title <> :t ORDER BY id"), {"t": EXCLUDED})]
        for job_id in job_ids:
            job = ingest.get_job(c, job_id)
            request = evidence.build_request(
                facts, job["requirements"], description=job["description"], names=("Demo Candidate",)
            )
            for answer in RECORDED:
                accepted = validate(VerdictSet.model_validate(answer), request, career_months=request.career_months)
                assert isinstance(accepted, Accepted) and not accepted.weak_fit
    assert len(job_ids) == 3


def test_the_brief_is_composed_once_with_the_three_matches_and_its_email_is_due_now(engine, provider):
    seeded = demo.seed_matches(sign_in(engine))

    briefs = fetch(
        engine,
        "SELECT channel::text AS channel, status::text AS status FROM briefs WHERE user_id = :u ORDER BY channel",
        u=seeded.user_id,
    )
    assert [(b.channel, b.status) for b in briefs] == [("email", "queued"), ("in_app", "composed")]
    items = fetch(
        engine, "SELECT match_decision_id FROM brief_items WHERE brief_id = :b ORDER BY position", b=seeded.brief_id
    )
    assert [i.match_decision_id for i in items] == [decision for decision, _ in seeded.shown]

    (row,) = fetch(
        engine,
        """SELECT template, status::text AS status, not_before <= now() AS due, expires_at > now() AS live, user_id,
                  payload->>'brief_id' AS brief
             FROM outbox""",
    )
    assert (row.template, row.status, row.due, row.live, row.user_id) == (
        "brief_v1",
        "pending",
        True,
        True,
        seeded.user_id,
    )
    assert row.brief == str(seeded.brief_id)
    assert seeded.outbox_id is not None and seeded.email_brief_id is not None


def test_the_email_the_relay_would_send_names_the_three_matches_and_not_the_excluded_posting(engine, provider):
    email = sign_in(engine)
    seeded = demo.seed_matches(email)
    (payload,) = fetch(engine, "SELECT payload FROM outbox")

    with engine.begin() as c:
        sent = mail.resolve(c, payload.payload)

    # not refused as undeliverable: the account is not synthetic and the address is not example.test
    assert isinstance(sent, Outgoing)
    assert sent.to == email and sent.subject == "Your morning brief: 3 matches"
    assert re.findall(r"/app/matches/([0-9a-f-]{36})", sent.body) == [str(decision) for decision, _ in seeded.shown]
    assert "Stop this email: " in sent.body and "/unsubscribe?t=" in sent.body
    assert EXCLUDED not in sent.body
    assert sent.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_the_apply_links_are_not_asked_for_over_the_network(engine, provider, monkeypatch):
    asked = []
    monkeypatch.setattr(
        ingest, "verify_live", lambda conn, job_id: asked.append(job_id)
    )  # the stub of the fixture, with a log
    demo.seed_matches(sign_in(engine))
    assert len(asked) == 3  # compose checks each shown match once; the stub answers without a request


def test_the_report_names_every_decision_and_the_brief(engine, provider):
    seeded = demo.seed_matches(sign_in(engine))
    report = seeded.report()
    for decision, title in (*seeded.shown, *seeded.filtered):
        assert str(decision) in report and title in report
    assert str(seeded.brief_id) in report and str(seeded.outbox_id) in report


# ---- running it again ------------------------------------------------------------------------------------------------


def test_running_it_twice_for_one_account_adds_no_second_brief_no_second_email_and_no_second_shown_match(
    engine, provider
):
    email = sign_in(engine)
    first = demo.seed_matches(email)
    counts = (
        "SELECT (SELECT count(*) FROM briefs) AS briefs, (SELECT count(*) FROM outbox) AS outbox, "
        "(SELECT count(*) FROM match_decisions WHERE outcome = 'shown') AS shown, "
        "(SELECT count(*) FROM profile_facts) AS facts, "
        "(SELECT count(*) FROM constraints) AS constraints, (SELECT count(*) FROM profile_events) AS events, "
        "(SELECT count(*) FROM documents) AS documents, (SELECT count(*) FROM jobs) AS jobs, "
        "(SELECT count(*) FROM job_requirements) AS requirements, (SELECT count(*) FROM consents) AS consents"
    )
    before = fetch(engine, counts)[0]

    second = demo.seed_matches(email)

    assert fetch(engine, counts)[0] == before
    assert (before.briefs, before.outbox, before.shown) == (2, 1, 3)
    assert second.brief_id == first.brief_id and second.outbox_id == first.outbox_id
    assert second.timezone == first.timezone  # the morning zone is kept, so the local date does not move
    assert second.run_shown == 0 and [d for d, _ in second.shown] == [d for d, _ in first.shown]


def test_a_second_account_gets_its_own_matches_from_the_same_four_postings(engine, provider):
    first = demo.seed_matches(sign_in(engine))
    second = demo.seed_matches(sign_in(engine))
    assert first.user_id != second.user_id and second.run_shown == 3
    assert scalar(engine, "SELECT count(*) FROM jobs") == 4  # the postings are shared, not copied
    assert {d for d, _ in first.shown}.isdisjoint({d for d, _ in second.shown})


# ---- the time zone ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("hour", range(24))
def test_there_is_a_whole_hour_zone_in_which_it_is_nine_in_the_morning_whatever_the_hour(hour):
    now = datetime(2026, 11, 23, hour, 30, tzinfo=timezone.utc)
    zone = demo.demo_zone(now)
    assert zone.startswith("Etc/GMT")
    assert now.astimezone(ZoneInfo(zone)).hour == 9 and demo.in_window(zone, now)


def test_a_zone_that_is_already_in_the_morning_is_kept_and_one_that_is_not_known_is_not_in_the_window():
    now = datetime(2026, 11, 23, 4, 30, tzinfo=timezone.utc)  # 10:00 in Asia/Kolkata
    assert demo.in_window("Asia/Kolkata", now) and not demo.in_window("America/New_York", now)
    assert not demo.in_window("Not/AZone", now)


# ---- invented, and labelled so ---------------------------------------------------------------------------------------


def test_every_demo_file_says_it_is_invented_and_every_title_and_link_is_marked_so():
    assert "invented" in (demo.DATA / "resume.txt").read_text(encoding="utf-8").splitlines()[0]
    assert "INVENTED DEMO DATA" in POSTINGS["note"]
    assert "invented" in (demo.DATA / "README.md").read_text(encoding="utf-8").lower()
    for posting in POSTINGS["postings"]:
        assert posting["title"].startswith("Demo posting (invented): ")
        assert posting["apply_url"].split("/")[2].endswith(".invalid")
        assert all(r["text"] in posting["description"] for r in posting["requirements"])
        assert len(posting["requirements"]) == 4


def test_the_demo_resume_lines_are_the_facts_the_seed_confirms():
    resume = (demo.DATA / "resume.txt").read_text(encoding="utf-8")
    assert all(statement in resume for _, statement, _ in demo.FACTS)
    assert "@" not in resume and "http" not in resume  # no address and no link: nothing in it names a real person
