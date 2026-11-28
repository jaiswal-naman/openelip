"""job.requirements.extract through the real gateway, answered by the fake provider from recorded files.

No model is called. The posting is invented for the tests, and so are the recorded answers (see recorded/README.md).
"""

import hashlib

import pytest

from app.ingest.requirements.segment import segments
from app.ingest.requirements.task import PROMPT, PROMPT_VERSION, TASK, render, run
from app.platform import config
from app.platform.llm import AIDisabled, SchemaInvalid, registry

# Invented. Segments: 1 intro, 2 heading, 3 "5+ years of Go", 4 "Bachelor's degree in CS", 5 "We offer free lunch".
POSTING = (
    "Invented posting for a unit test; Acme Robotics is not a real company.\n"
    "What you'll need\n"
    "5+ years of Go\n"
    "Bachelor's degree in CS\n"
    "We offer free lunch"
)


def test_a_recorded_answer_goes_through_the_gateway_and_becomes_rows(gateway_calls, replay):
    replay("invented_valid.json")
    result = run(POSTING)
    assert [(r.ordinal, r.text, r.kind, r.min_years) for r in result.rows] == [
        (1, "5+ years of Go", "experience", 5),  # cited with no quote: the whole segment
        (2, "Bachelor's degree in CS", "education", None),  # cited with an exact quote
    ]
    assert all(POSTING[r.start : r.end] == r.text for r in result.rows)
    assert result.rejected == {}
    assert gateway_calls == [(TASK, PROMPT_VERSION, None, 1, "ok")]  # no person is charged for the call


def test_an_answer_whose_every_item_cites_a_missing_segment_is_retried_once_then_invalid(gateway_calls, replay):
    replay("invented_every_item_cites_a_missing_segment.json")  # two answers: a third call would find none
    with pytest.raises(SchemaInvalid) as invalid:
        run(POSTING)
    assert str(invalid.value) == "schema_invalid"
    assert len(gateway_calls) == 2


def test_a_fresh_call_after_an_unusable_answer_can_succeed(gateway_calls, replay):
    replay("invented_missing_then_valid.json")
    result = run(POSTING)
    assert [r.text for r in result.rows] == ["5+ years of Go"]
    assert len(gateway_calls) == 2


def test_a_partly_valid_answer_keeps_its_valid_item_and_is_not_retried(gateway_calls, replay):
    replay("invented_partly_valid.json")  # one answer only: a second call would find none
    result = run(POSTING)
    assert [r.text for r in result.rows] == ["5+ years of Go"]
    assert result.rejected == {"segment_missing": 1}
    assert len(gateway_calls) == 1


@pytest.mark.parametrize("description", ["", "  \n\t\n "])
def test_a_posting_with_no_text_makes_no_call(gateway_calls, description):
    result = run(description)
    assert (result.rows, result.rejected) == ([], {})
    assert gateway_calls == []


def test_the_request_is_the_prompt_then_the_segments_as_numbered_lines():
    assert render(segments("a\n\nb")) == [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content": "[1] a\n[2] b"},
    ]


def test_the_prompt_version_is_the_content_hash_of_the_prompt_file():
    assert PROMPT_VERSION == hashlib.sha256(PROMPT.encode()).hexdigest()[:12]


def test_the_prompt_treats_the_posting_as_untrusted_data():
    low = PROMPT.lower()
    assert "untrusted" in low
    assert "ignore" in low  # T-02-11: an instruction inside a posting is not followed


def test_the_route_is_the_documented_one():
    keyed = config.load({"APP_ENV": "test", "LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"})
    route = registry.route(TASK, keyed)
    assert (route.provider, route.model, route.data_class) == ("anthropic", "claude-haiku-5-5", "public")
    assert (route.max_tokens, route.timeout_seconds, route.priced) == (4000, 60.0, True)  # AI_SYSTEM 2.2


def test_with_no_key_the_task_is_ai_disabled_and_nothing_is_called(gateway_calls, use_settings):
    use_settings(LLM_PROVIDER="anthropic")
    with pytest.raises(AIDisabled) as disabled:
        run(POSTING)
    assert disabled.value.reason == "no_key"
    assert gateway_calls == []


def test_a_local_server_has_no_route_for_the_task_and_nothing_is_called(gateway_calls, use_settings):
    use_settings(LLM_PROVIDER="openai_compatible", LLM_BASE_URL="http://llm:11434/v1")  # models.toml names none for it
    with pytest.raises(AIDisabled) as disabled:
        run(POSTING)
    assert disabled.value.reason == "no_route"
    assert gateway_calls == []
