"""CMP-03 (plan 07-02): a campaign's packet may cite the company's dossier claims (d1..dK); a packet without a campaign
behaves as in Phase 5. Everything is invented; the gateway runs on the fake provider with recordings written here."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.apply import packet
from app.platform import db
from app.platform.llm import fake
from tests.p5_factories import make_job
from tests.tailor.fixtures.service import harness
from tests.tailor.fixtures.service.harness import write_answers

POSTING = "Build reporting pipelines in Python. Experience with PostgreSQL."
REQUIREMENTS = (("Build reporting pipelines in Python", "required"), ("Experience with PostgreSQL", "required"))
DOSSIER = [
    {
        "text": "Lumen Freight builds route planning software for regional trucking companies.",
        "topic": "product",
        "sources": [{"url": "https://lumen.example/about", "quote": "x", "retrieved_at": "2026-11-01T09:00:00+00:00"}],
        "injection_suspected": False,
    },
    {"text": "The company has 85 employees.", "topic": "size", "sources": [], "injection_suspected": False},
]


def s(text_, facts=(), claims=()):
    return {"text": text_, "facts": list(facts), "dossier_claims": list(claims)}


@pytest.fixture
def w(engine, monkeypatch):
    harness.empty_tables(engine)
    harness.use_provider(monkeypatch)
    world = harness.world(engine)
    with engine.begin() as c:
        world.job = make_job(c, title="Data engineer", description=POSTING, requirements=REQUIREMENTS, country="IN")
    world.engine, world.monkeypatch = engine, monkeypatch
    yield world
    fake.replay_from(None)
    harness.empty_tables(engine)


def start(w, campaign_id: UUID | None):
    """A packet request in its own transaction. The campaigns table is Phase 7's: where its foreign key exists, the
    test drops it inside the transaction (rolled back), because this test is about packets, not campaigns."""
    with w.engine.connect() as c:
        with c.begin():
            for name in (
                c.execute(
                    text(
                        "SELECT conname FROM pg_constraint WHERE conrelid = 'application_packets'::regclass "
                        "AND contype = 'f' AND conkey = ARRAY(SELECT attnum FROM pg_attribute "
                        "WHERE attrelid = 'application_packets'::regclass AND attname = 'campaign_id')"
                    )
                )
                .scalars()
                .all()
            ):
                c.execute(text(f'ALTER TABLE application_packets DROP CONSTRAINT "{name}"'))
            view = packet.request(c, w.ada, w.job.id, campaign_id=campaign_id)
    return view


def stored(w):
    with w.engine.connect() as c:
        return list(
            c.execute(
                text("SELECT id, campaign_id, status::text AS status FROM application_packets ORDER BY created_at")
            )
        )


def draft(w, packet_id, claims):
    with db.transaction(role="worker") as conn:
        packet.draft(conn, {"packet_id": str(packet_id)}, dossier_claims=claims)


def view(w, packet_id):
    with db.transaction(w.ada, role="api") as conn:
        return packet.get(conn, w.ada, packet_id)


def record(w, tmp_path, *answers):
    return harness.use_provider(w.monkeypatch, write_answers(tmp_path, *answers))


def writer_answer():
    return {
        "answers": [
            {
                "question_id": "why_role",
                "status": "drafted",
                "sentences": [
                    s(
                        "I like that Lumen Freight builds route planning software for regional trucking companies.",
                        [],
                        ["d1"],
                    ),
                    s("I like that Lumen Freight raised 12 million dollars.", [], ["d1"]),  # a number d1 lacks
                    s("I built nightly Python and PostgreSQL pipelines.", ["f2"]),
                ],
            },
            {
                "question_id": "relevant_experience",
                "status": "drafted",
                "sentences": [
                    s("I led a team of three engineers on the reporting platform.", ["f3"]),
                    s("I admire the work of Zenith Corp.", [], ["d9"]),  # no such claim
                ],
            },
            {"question_id": "expected_pay", "status": "drafted", "sentences": [s("I want 100 thousand.", ["f2"])]},
            {
                "question_id": "start_date",
                "status": "drafted",
                "sentences": [s("I can start on Monday.", ["f2"], ["d2"])],
            },
        ],
        "note": [s("I built nightly Python and PostgreSQL pipelines and I like Lumen Freight.", ["f2"], ["d1"])],
    }


def entail(n):
    return {"items": [{"edit": f"e{i}", "entailed": True} for i in range(1, n + 1)]}


def test_a_campaign_packet_cites_dossier_claims_and_removes_the_ones_that_do_not_hold(w, tmp_path):
    calls = record(w, tmp_path, writer_answer(), entail(4))
    created = start(w, uuid4()).id
    draft(w, created, DOSSIER)
    got = view(w, created)
    answers = {a.question_id: a for a in got.answers}
    assert got.status == "ready"
    assert answers["why_role"].answer == (
        "I like that Lumen Freight builds route planning software for regional trucking companies. "
        "I built nightly Python and PostgreSQL pipelines."
    )
    assert answers["why_role"].removed_sentences == 1  # the changed number
    assert answers["relevant_experience"].removed_sentences == 1  # the unknown d9
    assert answers["why_role"].fact_ids == [w.record.facts["bullet_initech_1"]]
    assert answers["relevant_experience"].answer == "I led a team of three engineers on the reporting platform."
    assert got.cover_note == "I built nightly Python and PostgreSQL pipelines and I like Lumen Freight."
    assert [c[0] for c in calls] == ["writer.packet", "verify.entail"]
    with w.engine.connect() as c:
        stored_answers = c.execute(
            text("SELECT answers FROM application_packets WHERE id = :i"), {"i": created}
        ).scalar_one()
    by_id = {a["question_id"]: a for a in stored_answers}
    assert by_id["why_role"]["dossier_claim_indexes"] == [0]  # the dossier claim the answer rests on
    assert by_id["relevant_experience"]["dossier_claim_indexes"] == []


def test_pay_notice_start_date_and_work_authorisation_stay_blank_in_a_campaign_packet_too(w, tmp_path):
    record(w, tmp_path, writer_answer(), entail(4))
    created = start(w, uuid4()).id
    draft(w, created, DOSSIER)
    for answer in view(w, created).answers:
        if answer.question_id in ("work_authorization", "expected_pay", "notice_period", "start_date"):
            assert (answer.status, answer.answer, answer.fact_ids) == ("candidate_must_answer", "", [])


def test_a_packet_without_dossier_claims_removes_any_sentence_that_cites_one_as_in_phase_5(w, tmp_path):
    calls = record(w, tmp_path, writer_answer(), entail(3))
    created = start(w, None).id
    draft(w, created, None)
    got = view(w, created)
    answers = {a.question_id: a for a in got.answers}
    assert answers["why_role"].answer == "I built nightly Python and PostgreSQL pipelines."
    assert answers["why_role"].removed_sentences == 2  # both cite a dossier this packet was never given
    assert got.cover_note == ""  # the note cites d1, so it is removed whole
    assert "dossier_claim_indexes" not in answers["why_role"].model_dump()
    assert [c[0] for c in calls] == ["writer.packet", "verify.entail"]


def test_the_request_gives_the_claim_texts_as_a_separate_data_message_and_nothing_else_about_them(w):
    facts = [harness_fact(w)]
    plain = packet.build_messages(facts, ["Build reporting pipelines in Python"])
    with_claims = packet.build_messages(facts, ["Build reporting pipelines in Python"], dossier_claims=DOSSIER)
    assert len(plain) == 2 and len(with_claims) == 3 and with_claims[:2] == plain
    data = with_claims[2]["content"]
    assert data.splitlines()[1:] == [
        "[d1] Lumen Freight builds route planning software for regional trucking companies.",
        "[d2] The company has 85 employees.",
    ]
    assert "https://" not in data and "retrieved" not in data


def harness_fact(w):
    from app.profile import api as profile
    from app.tailor import api as tailor

    with w.engine.connect() as c:
        return tailor.to_fact(profile.usable_facts(c, w.ada)[1])


def test_a_campaign_request_enqueues_no_job_and_supersedes_only_its_own_campaigns_packet(w):
    first_plain = start(w, None).id  # the job's campaign-less packet (a Phase 5 request): one apply.packet job
    campaign = uuid4()
    one = start(w, campaign).id
    two = start(w, campaign).id
    rows = {r.id: r for r in stored(w)}
    assert (rows[first_plain].status, rows[one].status, rows[two].status) == ("draft", "superseded", "draft")
    assert rows[two].campaign_id == campaign and rows[first_plain].campaign_id is None
    with w.engine.connect() as c:
        jobs = c.execute(text("SELECT unique_key FROM queue_jobs WHERE kind = 'apply.packet'")).scalars().all()
    assert jobs == [f"apply.packet:{first_plain}"]  # none for either campaign packet


def test_drafting_a_packet_that_is_not_draft_makes_no_model_call(w, tmp_path):
    calls = record(w, tmp_path, writer_answer(), entail(4), writer_answer(), entail(4))
    created = start(w, uuid4()).id
    draft(w, created, DOSSIER)
    assert len(calls) == 2 and view(w, created).status == "ready"
    draft(w, created, DOSSIER)  # a second run of the same node
    assert len(calls) == 2
    with w.engine.begin() as c:
        c.execute(text("UPDATE application_packets SET status = 'superseded' WHERE id = :i"), {"i": created})
    draft(w, created, DOSSIER)
    assert len(calls) == 2
