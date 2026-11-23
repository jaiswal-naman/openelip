"""The Chaser (plan 07-02, CMP-04): sentence validation and the payload code builds. The gateway runs on the fake
provider with a recording per test; everything is invented."""

import json

import pytest

from app.campaigns.crew import chaser
from app.platform.llm import fake
from app.tailor import api as tailor
from tests.tailor.fixtures.service.harness import use_provider, write_answers

FACTS = [
    tailor.Fact("fact-1", "achievement", "Built reporting pipelines in Python for a logistics team."),
    tailor.Fact("fact-2", "achievement", "Cut report time from 6 hours to 40 minutes."),
]
CLAIMS = ["Lumen Freight builds route planning software for regional trucking companies."]
EVENTS = chaser.events_for("3 November 2026", "Data Engineer")


@pytest.fixture(autouse=True)
def _reset_fake():
    yield
    fake.replay_from(None)


def sentence(text, facts=(), claims=(), events=()):
    return chaser.DraftSentence(text=text, facts=list(facts), dossier_claims=list(claims), events=list(events))


def run(message, tmp_path, monkeypatch, *entailed):
    items = [{"edit": f"e{n}", "entailed": ok} for n, ok in enumerate(entailed, 1)]
    calls = use_provider(monkeypatch, write_answers(tmp_path, {"items": items}) if entailed else None)
    kept, removed = chaser.validate(
        message, facts=FACTS, claims=CLAIMS, events=EVENTS, skills=["Python"], job_terms=[], principal=None
    )
    return kept, removed, calls


def test_validate_keeps_supported_sentences_and_a_courtesy_and_counts_every_removal(tmp_path, monkeypatch):
    monkeypatch.setattr(chaser, "MAX_SENTENCES", 20)  # this test is about the checks, not the cap
    message = chaser.MessageDraft(
        sentences=[
            sentence("I applied for the Data Engineer role on 3 November 2026.", events=["e1", "e2"]),
            sentence("I built reporting pipelines in Python for a logistics team.", facts=["f1"], claims=["d1"]),
            sentence("I cut report time from 12 hours to 40 minutes.", facts=["f2"]),  # a number f2 lacks
            sentence("Thank you for your time."),  # courtesy: nothing checkable, nothing cited
            sentence("Please reply to ada@example.test.", facts=["f1"]),
            sentence("My salary expectation is flexible."),
            sentence("I led the Kubernetes migration."),  # a checkable name, nothing cited
            sentence("I also know Rust.", facts=["f9"]),  # an alias that does not exist
            sentence("I improved reporting for a logistics team.", facts=["f1"]),  # entailed rejects it
            sentence("See https://example.test/me for more.", facts=["f1"]),
            sentence("Call me on +44 20 7946 0958.", facts=["f1"]),
        ]
    )
    kept, removed, calls = run(message, tmp_path, monkeypatch, True, True, False)
    assert [s.text for s in kept] == [
        "I applied for the Data Engineer role on 3 November 2026.",
        "I built reporting pipelines in Python for a logistics team.",
        "Thank you for your time.",
    ]
    assert removed == {
        "changed_number": 1,
        "contact_email": 1,
        "restricted_content": 1,
        "uncited": 1,
        "unknown_alias": 1,
        "not_entailed": 1,
        "contact_link": 1,
        "contact_phone": 1,
    }
    assert [c[0] for c in calls] == ["verify.entail"]  # one call for every survivor of layer 1


def test_a_draft_whose_cited_sentences_are_all_removed_has_no_draft(tmp_path, monkeypatch):
    message = chaser.MessageDraft(
        sentences=[
            sentence("Thank you for your time."),
            sentence("I cut report time from 12 hours to 40 minutes.", facts=["f2"]),
        ]
    )
    kept, removed, calls = run(message, tmp_path, monkeypatch)
    assert kept == []
    assert removed == {"changed_number": 1, "no_supported_content": 1}
    assert calls == []  # nothing reached layer 2


def test_a_missing_entailment_answer_fails_closed(tmp_path, monkeypatch):
    message = chaser.MessageDraft(sentences=[sentence("I built reporting pipelines in Python.", facts=["f1"])])
    use_provider(monkeypatch, write_answers(tmp_path, {"items": []}))
    kept, removed = chaser.validate(
        message, facts=FACTS, claims=CLAIMS, events=EVENTS, skills=["Python"], job_terms=[], principal=None
    )
    assert kept == [] and removed["not_entailed"] == 1


def test_too_many_and_overlong_sentences_are_removed(tmp_path, monkeypatch):
    many = [sentence(f"Thank you for your time {n}.") for n in range(chaser.MAX_SENTENCES + 1)]
    kept, removed, _ = run(
        chaser.MessageDraft(sentences=[sentence("x" * 500, facts=["f1"]), *many]), tmp_path, monkeypatch
    )
    assert kept == [] and removed["bad_length"] == 1 and removed["too_many_sentences"] >= 1


def test_compose_payload_is_built_by_code_whatever_the_model_wrote():
    kept = [sentence("I applied on 3 November 2026."), sentence("Thank you for your time.")]
    payload = chaser.compose_payload(
        kept, to="hr@lumen.example", reply_to="ada@mail.example", job_title="Data  Engineer", display_name="Ada Example"
    )
    assert payload == {
        "channel": "email",
        "to": "hr@lumen.example",
        "reply_to": "ada@mail.example",
        "subject": "Following up on my Data Engineer application",
        "body": "Hello,\n\nI applied on 3 November 2026. Thank you for your time.\n\nBest regards,\nAda Example",
    }
    # the draft's own fields cannot name a recipient or a subject: the schema has none, and extra keys are refused
    with pytest.raises(ValueError):
        chaser.MessageDraft.model_validate({"sentences": [], "to": "x@example.test"})
    with pytest.raises(ValueError):
        chaser.DraftSentence.model_validate(
            {"text": "x", "facts": [], "dossier_claims": [], "events": [], "subject": "s"}
        )


def test_the_request_carries_aliases_and_statement_texts_only():
    messages = chaser.build_messages(FACTS, CLAIMS, EVENTS, ["Build pipelines in Python"])
    user = messages[1]["content"]
    assert messages[0]["content"] == chaser.PROMPT
    assert "[f1] achievement: Built reporting pipelines in Python for a logistics team." in user
    assert "[d1] Lumen Freight builds" in user and "[e1] I applied on 3 November 2026." in user
    assert "[r1] Build pipelines in Python" in user
    for private in ("fact-1", "fact-2", "@", "http", "Ada"):
        assert private not in user
    assert json.dumps(messages).count("fact-") == 0
