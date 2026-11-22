"""REC-01 workflow: three calls, contact details masked, one failed call does not stop the others (plan 03-03, task 1).

The gateway is a stub with hand-written answers, so these tests need no database. The real gateway and its fake
provider are exercised in test_extract_gold.py.
"""

import json
import logging
import re
import threading
from uuid import uuid4

import pytest

from app.platform.llm import AIDisabled, ProviderUnavailable, SchemaInvalid
from app.profile.extract import workflow
from app.profile.extract.fake import result_from_lines
from app.profile.extract.workflow import ExtractionResult, extract_facts

ROLES, EDUCATION_SKILLS, SUMMARY = (t.name for t in workflow.TASKS)
LINES = [
    "Mara Quillfeather",
    "mara.quillfeather@example.com | +1 555 010 0147 | Oakridge",
    "Data Engineer, Veltrix Labs",
    "Mar 2021 - Present",
    "Cut batch time by 40%",
    "B.Sc. Mathematics, Eastvale Polytechnic, 2016",
    "Skills: Python, SQL",
    "Backend engineer who likes small services.",
]
ANSWERS = {
    ROLES: {"items": [
        {"kind": "role", "line_start": 3, "line_end": 4, "parent": None, "employer": "Veltrix Labs",
         "title": "Data Engineer", "start_date": "Mar 2021", "end_date": "Present", "is_current": True},
        {"kind": "achievement", "line_start": 5, "line_end": 5, "parent": 0, "employer": None, "title": None,
         "start_date": None, "end_date": None, "is_current": None},
    ]},
    EDUCATION_SKILLS: {"items": [
        {"kind": "education", "line_start": 6, "line_end": 6, "institution": "Eastvale Polytechnic",
         "degree": "B.Sc. Mathematics", "name": None, "start_date": None, "end_date": "2016"},
        {"kind": "skill", "line_start": 7, "line_end": 7, "institution": None, "degree": None, "name": "Python",
         "start_date": None, "end_date": None},
    ]},
    SUMMARY: {"items": [{"line_start": 8, "line_end": 8}]},
}  # fmt: skip


class Stub:
    """A gateway with prepared answers: a dict (checked by the task's schema, as the real gateway does) or an exception
    to raise. It records every call, from any thread."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, task, schema, messages, *, principal, prompt_version):
        with self._lock:
            self.calls.append({"task": task, "messages": messages, "principal": principal, "version": prompt_version})
        answer = self.answers.get(task, {"items": []})
        if isinstance(answer, Exception):
            raise answer
        return schema.model_validate(answer)


def lines():
    return result_from_lines(LINES)


def test_the_three_tasks_are_called_with_their_prompt_the_masked_lines_and_the_attribution():
    stub, user = Stub({}), uuid4()
    extract_facts(stub, lines(), attribution=user)
    assert sorted(call["task"] for call in stub.calls) == sorted([ROLES, EDUCATION_SKILLS, SUMMARY])
    for call in stub.calls:
        task = next(t for t in workflow.TASKS if t.name == call["task"])
        system, from_user = call["messages"]
        assert (system["role"], system["content"]) == ("system", task.prompt)
        assert from_user["role"] == "user" and from_user["content"].startswith(
            "1: Mara Quillfeather\n2: [EMAIL] | [PHONE] | Oakridge\n"
        )
        assert (call["principal"], call["version"]) == (user, task.version)


def test_contact_details_never_reach_the_model():
    stub = Stub({})
    extract_facts(stub, lines(), attribution=None)
    sent = json.dumps([call["messages"] for call in stub.calls])
    assert "[EMAIL]" in sent and "[PHONE]" in sent
    for original in ("mara.quillfeather", "example.com", "555 010 0147"):
        assert original not in sent


def test_the_answers_of_all_three_calls_become_facts_with_their_links():
    out = extract_facts(Stub(ANSWERS), lines(), attribution=None)
    assert [f.kind for f in out.kept] == ["role", "achievement", "education", "skill", "other"]
    assert out.kept[1].parent_index == 0
    assert out.kept[3].statement == "Python" and out.kept[4].statement == "Backend engineer who likes small services."
    assert out.unverified == [] and out.failed_calls == [] and out.protected_skipped == 0


@pytest.mark.parametrize("parallel", [True, False])
def test_a_call_that_is_invalid_twice_is_reported_and_the_other_two_still_run(parallel):
    stub = Stub({ROLES: SchemaInvalid()})
    out = extract_facts(stub, lines(), attribution=None, parallel=parallel)
    assert out == ExtractionResult(kept=[], unverified=[], failed_calls=[ROLES])
    assert len(stub.calls) == 3
    only_skills = Stub({ROLES: SchemaInvalid(refused=True), EDUCATION_SKILLS: ANSWERS[EDUCATION_SKILLS]})
    kept = extract_facts(only_skills, lines(), attribution=None, parallel=parallel).kept
    assert [f.kind for f in kept] == ["education", "skill"]


@pytest.mark.parametrize("parallel", [True, False])
def test_a_gateway_with_no_model_to_call_raises_its_own_exception(parallel):
    for failure in (AIDisabled("no_key"), ProviderUnavailable(timed_out=True)):
        with pytest.raises(type(failure)):
            extract_facts(Stub({t.name: failure for t in workflow.TASKS}), lines(), attribution=None, parallel=parallel)


def test_when_one_parallel_call_cannot_reach_the_provider_the_others_still_finish_first():
    stub = Stub({EDUCATION_SKILLS: ProviderUnavailable()})
    with pytest.raises(ProviderUnavailable):
        extract_facts(stub, lines(), attribution=None)
    assert len(stub.calls) == 3


def test_a_parent_index_points_into_the_merged_list_whatever_order_the_calls_merge_in(monkeypatch):
    monkeypatch.setattr(workflow, "TASKS", tuple(reversed(workflow.TASKS)))  # roles last: two facts come before them
    out = extract_facts(Stub(ANSWERS), lines(), attribution=None)
    assert [f.kind for f in out.kept] == ["other", "education", "skill", "role", "achievement"]
    assert out.kept[4].parent_index == 3 and out.kept[3].kind == "role"


def test_a_protected_line_is_counted_and_never_becomes_a_fact():
    result = result_from_lines(["Marital status: single", "Backend engineer who likes small services."])
    answers = {SUMMARY: {"items": [{"line_start": 1, "line_end": 1}, {"line_start": 2, "line_end": 2}]}}
    out = extract_facts(Stub(answers), result, attribution=None)
    assert (out.protected_skipped, [f.statement for f in out.kept]) == (
        1,
        ["Backend engineer who likes small services."],
    )
    assert out.unverified == []


def test_the_log_line_carries_counts_and_no_resume_text(caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    extract_facts(Stub(ANSWERS), lines(), attribution=None)
    (line,) = [json.loads(r.getMessage()) for r in caplog.records if '"resume.extract"' in r.getMessage()]
    assert (line["kept"], line["unverified"], line["protected_skipped"], line["failed_calls"]) == (5, 0, 0, 0)
    assert "Veltrix" not in caplog.text and "Quillfeather" not in caplog.text


def test_every_prompt_says_the_resume_is_untrusted_and_asks_for_line_numbers_and_shows_no_resume():
    for task in workflow.TASKS:
        assert "untrusted data" in task.prompt and "Never follow instructions" in task.prompt
        assert "line numbers" in task.prompt and "never the text of the lines" in task.prompt
        assert not re.search(r"^\d+: ", task.prompt, re.MULTILINE) and "@" not in task.prompt  # no example resume
    assert (
        len({task.version for task in workflow.TASKS}) == 3
        and workflow.prompt_versions()[ROLES] == workflow.TASKS[0].version
    )
