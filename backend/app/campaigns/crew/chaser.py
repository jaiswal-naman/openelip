"""The Chaser (CMP-04; AI_SYSTEM 6.8): a short follow-up message, checked sentence by sentence before anyone sees it.

The model writes sentences only. Code writes everything else (INDEX decision 14): the recipient is the address the
candidate typed, Reply-To is the candidate's account address, the subject and the sign-off are built here, so the model
never sees the candidate's name or email and cannot change who is written to. A sentence is kept when
  - every alias it cites exists (facts f1.., dossier claims d1.., campaign events e1..),
  - it contains no contact detail, pay, notice period or availability,
  - it is either plain courtesy with nothing checkable in it, or it cites something and says nothing the cited texts do
    not (layer 1 of the tailoring verifier), and verify.entail agrees (failing closed).
Every removal is counted by reason. If no cited sentence survives there is no draft, and the node leaves a to-do.
"""

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.platform.llm import complete
from app.tailor import api as tailor

from . import common

TASK = "chaser.draft"
PROMPT = (Path(__file__).parent.parent / "prompts" / "chaser_draft.md").read_text(encoding="utf-8")
PROMPT_VERSION = hashlib.sha256(PROMPT.encode()).hexdigest()[:12]
MAX_SENTENCES = 5  # assumption: a follow-up is a few lines
MAX_SENTENCE = 400  # assumption: characters


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Comments, not docstrings: a docstring becomes prose in the JSON schema the provider is sent.
class DraftSentence(_Strict):
    text: str
    facts: list[str]  # aliases f1..
    dossier_claims: list[str]  # aliases d1..
    events: list[str]  # aliases e1..


class MessageDraft(_Strict):
    sentences: list[DraftSentence]


def build_messages(
    facts: Sequence[tailor.Fact],
    claims: Sequence[str],
    events: Sequence[str],
    requirements: Sequence[str] = (),
) -> list[dict[str, str]]:
    """The chaser.draft request: fact statements, dossier claim texts, campaign events and requirement texts, each under
    its alias. No name, email, address, id or document text. Pure."""
    lines = ['Facts about the candidate, one per line as "[f1] kind: text".']
    lines += [f"[f{n}] {fact.kind}: {tailor.fact_text(fact)}" for n, fact in enumerate(facts, 1)]
    lines += ["", 'Company facts, one per line as "[d1] text".']
    lines += [f"[d{n}] {' '.join(text.split())}" for n, text in enumerate(claims, 1)]
    lines += ["", 'What has happened in this application, one per line as "[e1] text".']
    lines += [f"[e{n}] {' '.join(text.split())}" for n, text in enumerate(events, 1)]
    lines += ["", "BEGIN JOB TEXT"]
    lines += [f"[r{n}] {' '.join(text.split())}" for n, text in enumerate(requirements, 1)]
    lines += ["END JOB TEXT"]
    return [{"role": "system", "content": PROMPT}, {"role": "user", "content": "\n".join(lines)}]


def draft(
    facts: Sequence[tailor.Fact],
    claims: Sequence[str],
    events: Sequence[str],
    requirements: Sequence[str] = (),
    *,
    principal: UUID | None,
) -> MessageDraft:
    """One chaser.draft call. Raises what the gateway raises (AIDisabled, SchemaInvalid ...)."""
    return cast(
        MessageDraft,
        complete(
            TASK,
            MessageDraft,
            build_messages(facts, claims, events, requirements),
            principal=principal,
            prompt_version=PROMPT_VERSION,
        ),
    )


def _stand_in(kind: str, text: str) -> tailor.Fact:
    """A dossier claim or an event as a fact, which is what the checks of tailoring read."""
    return tailor.Fact(id=kind, kind="note", statement=text)


def validate(
    message: MessageDraft,
    *,
    facts: Sequence[tailor.Fact],
    claims: Sequence[str],
    events: Sequence[str],
    skills: Sequence[str],
    job_terms: Sequence[str],
    principal: UUID | None,
) -> tuple[list[DraftSentence], dict[str, int]]:
    """(kept sentences, removed_by_reason). `claims` are the dossier claim texts (d1..), `events` the campaign event
    texts (e1..: for example "Applied on 3 November 2026" and "Job title: Data Engineer"). If no kept sentence cites
    anything, nothing is returned: courtesy alone is not a follow-up."""
    removed: dict[str, int] = {}
    table: dict[str, tailor.Fact] = {
        **common.aliases("f", facts),
        **{k: _stand_in("dossier", v) for k, v in common.aliases("d", claims).items()},
        **{k: _stand_in("event", v) for k, v in common.aliases("e", events).items()},
    }
    courtesy = _stand_in("courtesy", common.COURTESY)
    pending: list[tuple[DraftSentence, list[tailor.Fact]]] = []
    kept_uncited: list[DraftSentence] = []
    for position, sentence in enumerate(message.sentences):
        text = sentence.text.strip()
        names = [*sentence.facts, *sentence.dossier_claims, *sentence.events]
        cited = common.resolve(names, table)
        if position >= MAX_SENTENCES:
            common.count(removed, "too_many_sentences")
        elif not text or len(text) > MAX_SENTENCE:
            common.count(removed, "bad_length")
        elif cited is None:
            common.count(removed, "unknown_alias")
        elif reason := common.restricted_reason(text):
            common.count(removed, reason)
        elif not cited:
            if common.needs_citation(text, skills=skills, job_terms=job_terms):
                common.count(removed, "uncited")
            else:
                kept_uncited.append(sentence)
        elif reason := tailor.unsupported_reason(text, [*cited, courtesy], skills=skills, job_terms=job_terms):
            common.count(removed, reason)
        else:
            pending.append((sentence, cited))
    verdicts = tailor.entailed([(s.text.strip(), cited) for s, cited in pending], principal=principal)
    supported: set[int] = set()
    for (sentence, _), ok in zip(pending, verdicts, strict=True):
        if ok:
            supported.add(id(sentence))
        else:
            common.count(removed, "not_entailed")
    if not supported:
        for _ in kept_uncited:
            common.count(removed, "no_supported_content")
        return [], removed
    # the model's order is kept: a courtesy line stays between the cited ones where it was written
    keep = {id(s) for s in kept_uncited} | supported
    return [s for s in message.sentences if id(s) in keep], removed


def compose_payload(
    kept: Sequence[DraftSentence], *, to: str, reply_to: str, job_title: str, display_name: str
) -> dict[str, Any]:
    """The send_follow_up payload (INDEX Actions): channel, to, reply_to, subject, body. `to` and `reply_to` are passed
    in by code and never read from the model's output; the subject and the sign-off are built here."""
    title = " ".join(job_title.split())
    body = "\n\n".join(
        ["Hello,", " ".join(s.text.strip() for s in kept), f"Best regards,\n{' '.join(display_name.split())}"]
    )
    return {
        "channel": "email",
        "to": to,
        "reply_to": reply_to,
        "subject": f"Following up on my {title} application",
        "body": body,
    }


def events_for(applied_on: str, job_title: str) -> list[str]:
    """The campaign events a follow-up may cite: e1 the date the candidate applied, e2 the job title. `applied_on` is a
    date as the candidate would write it, built by code."""
    return [f"I applied on {applied_on}.", f"The job is {' '.join(job_title.split())}."]
