"""What the campaigns module teaches the Guardian when it is imported (INDEX decisions 6 and 32).

The Guardian (layer 4) may not import campaigns (layer 6) or tailor (a sibling), so two readers are registered here:
  * the subject reader, which turns a campaign row into the SubjectGate the policy engine and the checks read; and
  * the citation check, built from the crew's needs_citation test and the tailoring verifier's two layers.
Both processes import app.campaigns, so the API and the worker both have them.
"""

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.guardian import api as guardian
from app.guardian.checks import CITATION_UNRESOLVED, UNSUPPORTED_CLAIM, CitedSentence
from app.hiring import api as hiring
from app.tailor import api as tailor

from .crew import common

PAUSE_REASONS = ("candidate", "budget_exhausted", "scam_suspected")


def read_gate(conn: Connection, campaign_id: UUID) -> guardian.SubjectGate | None:
    """The campaign as the Guardian reads it, or None when this connection cannot see it."""
    row = conn.execute(
        text(
            """SELECT c.id, c.user_id, c.job_id, c.stage::text AS stage, c.paused_at IS NOT NULL AS paused,
                      c.introduction_id, c.follow_up_email, c.budget_microusd, u.status::text = 'active' AS active
               FROM campaigns c JOIN users u ON u.id = c.user_id WHERE c.id = :id"""
        ),
        {"id": campaign_id},
    ).one_or_none()
    if row is None:
        return None
    organization_id = None
    if row.introduction_id is not None:
        ref = hiring.introduction_ref(conn, row.introduction_id)
        organization_id = None if ref is None else ref.organization_id
    paused_reason = None
    if row.paused:
        paused_reason = conn.execute(
            text(
                """SELECT outcome_code FROM agent_actions
                   WHERE campaign_id = :c AND kind = 'state_change' AND outcome_code = ANY(CAST(:reasons AS text[]))
                   ORDER BY id DESC LIMIT 1"""
            ),
            {"c": campaign_id, "reasons": list(PAUSE_REASONS)},
        ).scalar_one_or_none()
        paused_reason = paused_reason or "candidate"
    return guardian.SubjectGate(
        kind="campaign",
        id=row.id,
        user_id=row.user_id,
        user_active=row.active,
        paused_reason=paused_reason,
        closed=row.stage == "closed",
        job_id=row.job_id,
        organization_id=organization_id,
        follow_up_email=row.follow_up_email,
        budget_microusd=row.budget_microusd,
    )


def _stand_in(kind: str, statement: str) -> tailor.Fact:
    return tailor.Fact(id=kind, kind="note", statement=" ".join(statement.split()))


def check_citations(
    sentences: Sequence[CitedSentence],
    *,
    facts: Mapping[str, dict[str, Any]],
    claims: Sequence[dict[str, Any]],
    skills: Sequence[str],
    job_terms: Sequence[str],
    principal: UUID | None,
) -> list[str | None]:
    """One reason code or None per sentence. A sentence that cites nothing passes only when it says nothing checkable (a
    greeting, thanks, plain interest); one that cites must be supported by what it cites at layer 1 (numbers, skills,
    titles, employers, dates) and then by verify.entail, which fails closed (AI_SYSTEM 7.4)."""
    table = {fact_id: tailor.to_fact(row) for fact_id, row in facts.items()}
    courtesy = _stand_in("courtesy", common.COURTESY)
    verdicts: list[str | None] = [None] * len(sentences)
    pending: list[tuple[int, str, list[tailor.Fact]]] = []
    for position, sentence in enumerate(sentences):
        said = sentence.text.strip()
        cited = [table[fact_id] for fact_id in sentence.fact_ids if fact_id in table]
        cited += [_stand_in("dossier", str(claims[i]["text"])) for i in sentence.claim_indexes]
        cited += [_stand_in("event", event) for event in sentence.events]
        if not cited:
            if common.needs_citation(said, skills=skills, job_terms=job_terms):
                verdicts[position] = CITATION_UNRESOLVED
        elif tailor.unsupported_reason(said, [*cited, courtesy], skills=skills, job_terms=job_terms) is not None:
            verdicts[position] = UNSUPPORTED_CLAIM
        else:
            pending.append((position, said, cited))
    answers = tailor.entailed([(said, cited) for _, said, cited in pending], principal=principal)
    for (position, _, _), supported in zip(pending, answers, strict=True):
        if not supported:
            verdicts[position] = UNSUPPORTED_CLAIM
    return verdicts


guardian.register_subject("campaign", read_gate)
guardian.register_citation_check(check_citations)
