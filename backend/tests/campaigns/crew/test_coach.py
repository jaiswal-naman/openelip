"""The Coach (plan 07-02, CMP-05): prep validation, counted removals, gaps from the stored verdicts. Invented data."""

import pytest

from app.campaigns.crew import coach
from app.platform.llm import fake
from app.tailor import api as tailor
from tests.tailor.fixtures.service.harness import use_provider, write_answers

FACTS = [
    tailor.Fact("fact-1", "achievement", "Built reporting pipelines in Python for a logistics team."),
    tailor.Fact("fact-2", "skill", "SQL", {"name": "SQL"}),
]
REQUIREMENTS = [
    tailor.Requirement(11, "Build data pipelines in Python", "required"),
    tailor.Requirement(12, "Experience with Kubernetes", "required"),
    tailor.Requirement(13, "Communicate with stakeholders", "preferred"),
]
VERDICTS = {11: "met", 12: "no_evidence", 13: "partly_met"}
CLAIMS = ["In 2024 we raised a Series A of 12 million dollars.", "The company has 85 employees."]


@pytest.fixture(autouse=True)
def _reset_fake():
    yield
    fake.replay_from(None)


def q(text, requirement, facts, framing, gap=False):
    return coach.PrepQuestion(text=text, requirement=requirement, facts=facts, gap=gap, framing=framing)


def validate(pack, tmp_path, monkeypatch, *entailed):
    items = [{"edit": f"e{n}", "entailed": ok} for n, ok in enumerate(entailed, 1)]
    calls = use_provider(monkeypatch, write_answers(tmp_path, {"items": items}))
    out, removed = coach.validate(
        pack,
        facts=FACTS,
        requirements=REQUIREMENTS,
        verdicts=VERDICTS,
        claims=CLAIMS,
        skills=["Python", "SQL"],
        job_terms=[r.text for r in REQUIREMENTS],
        principal=None,
    )
    return out, removed, calls


def test_validate_keeps_supported_items_and_counts_what_it_removes(tmp_path, monkeypatch):
    pack = coach.PrepPack(
        questions=[
            q(
                "Tell us about a pipeline you built.",
                "r1",
                ["f1"],
                "Tell the story of the Python pipelines you built for a logistics team.",
            ),
            q(
                "Have you run Kubernetes?",
                "r2",
                [],
                "Be honest that you have not used Kubernetes yet and say how you would learn it.",
            ),
            q("Tell us about a pipeline you built.", "r1", ["f1"], "Tell the story again."),  # a repeat
            q("Walk me through a design.", "r1", [], "Tell the story of a design."),  # a story without a fact
            q(
                "Have you run Kubernetes at scale?",
                "r2",
                ["f1"],
                "Say you led the Kubernetes migrations at a logistics team.",
            ),  # entailed rejects
            q("How many clusters?", "r2", ["f1"], "You ran 40 Kubernetes clusters."),  # a number the facts lack
            q("Anything on dbt?", "r9", ["f1"], "Say something."),  # no such requirement
            q("Anything on Spark?", "r1", ["f7"], "Say something."),  # no such fact
            q("   ", "r1", ["f1"], "x"),
        ],
        ask_them=[
            coach.AskThem(text="How is the Series A shaping the team?", dossier_claims=["d1"]),
            coach.AskThem(text="What is the culture like?", dossier_claims=[]),
            coach.AskThem(text="How is the team doing?", dossier_claims=["d7"]),
            coach.AskThem(text="Is it true you raised 50 million dollars?", dossier_claims=["d1"]),
        ],
    )
    out, removed, calls = validate(pack, tmp_path, monkeypatch, True, True, False)
    assert [(x.requirement, x.gap) for x in out.questions] == [("r1", False), ("r2", True)]
    assert [a.text for a in out.ask_them] == ["How is the Series A shaping the team?"]
    assert removed == {
        "duplicate": 1,
        "story_without_fact": 1,
        "framing_not_entailed": 1,
        "framing_changed_number": 1,
        "unknown_requirement": 1,
        "unknown_alias": 1,
        "bad_length": 1,
        "ask_without_claim": 1,
        "unknown_claim": 1,
        "ask_changed_number": 1,
    }
    assert [c[0] for c in calls] == ["verify.entail"]


def test_gap_comes_from_the_stored_verdict_not_from_the_model(tmp_path, monkeypatch):
    pack = coach.PrepPack(
        questions=[
            q(
                "Have you built stakeholder reports?",
                "r3",
                ["f1"],
                "Tell the story of the Python pipelines you built.",
                gap=False,
            ),
            q("Tell us about a pipeline you built.", "r1", ["f1"], "Tell the story of the Python pipelines.", gap=True),
        ],
        ask_them=[],
    )
    out, _, _ = validate(pack, tmp_path, monkeypatch, True, True)
    assert [(x.requirement, x.gap) for x in out.questions] == [("r3", True), ("r1", False)]


def test_no_pack_when_the_checker_never_answers(tmp_path, monkeypatch):
    pack = coach.PrepPack(
        questions=[q("Tell us about a pipeline.", "r1", ["f1"], "Tell the story of the Python pipelines.")], ask_them=[]
    )
    use_provider(monkeypatch, write_answers(tmp_path, {"items": []}))
    out, removed = coach.validate(
        pack,
        facts=FACTS,
        requirements=REQUIREMENTS,
        verdicts=VERDICTS,
        claims=CLAIMS,
        skills=["Python"],
        job_terms=[],
        principal=None,
    )
    assert out.questions == [] and removed == {"framing_not_entailed": 1}


def test_to_output_gives_ids_indexes_and_counts_only(tmp_path, monkeypatch):
    pack = coach.PrepPack(
        questions=[q("Tell us about a pipeline.", "r1", ["f1", "f2"], "Tell the story of the Python pipelines.")],
        ask_them=[coach.AskThem(text="How is the Series A shaping the team?", dossier_claims=["d1", "d2"])],
    )
    out = coach.to_output(
        pack, facts=FACTS, requirements=REQUIREMENTS, removed_by_reason={"duplicate": 2, "bad_length": 1}
    )
    assert out == {
        "questions": [
            {
                "text": "Tell us about a pipeline.",
                "requirement_id": 11,
                "fact_ids": ["fact-1", "fact-2"],
                "gap": False,
                "framing": "Tell the story of the Python pipelines.",
            }
        ],
        "ask_them": [{"text": "How is the Series A shaping the team?", "claim_indexes": [0, 1]}],
        "removed_by_reason": {"bad_length": 1, "duplicate": 2},
    }


def test_the_request_carries_aliases_verdicts_and_statement_texts_only():
    user = coach.build_messages(FACTS, REQUIREMENTS, VERDICTS, CLAIMS)[1]["content"]
    assert "[f1] achievement: Built reporting pipelines" in user and "[f2] skill: SQL" in user
    assert "[d1] In 2024 we raised" in user
    assert "[r2] (required, no evidence) Experience with Kubernetes" in user
    assert "[r3] (preferred, partly met) Communicate with stakeholders" in user
    for private in ("fact-1", "fact-2", "@", "http"):
        assert private not in user
