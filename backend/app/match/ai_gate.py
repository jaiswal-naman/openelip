"""Whether the evidence task may call a real model right now (AI_SYSTEM 2.2 and 12.8).

A route may serve a task only when a passed gold-set run for that task and model exists. For match.evidence.score that
is a passed evidence_faithfulness run by the model method, whose config names the model the route uses and the prompt
version in use. Until a live run has been recorded no real model scores a candidate's facts, key or no key: the run
completes without scoring, as it does with no key, and every retrieved job is withheld not_scored (nothing is shown from
the verdicts of a model nobody has measured).

The fake provider replays recordings and calls no model, so it needs no proof; the gateway refuses it in production. The
replay the CI records names the model "recorded" and no method, so it never proves a real route. A live run must record
`method` ("model"), `model` and `prompt_version` in its config. The suite has no split yet (its README chooses one when
the first live run is planned), so none is asked for: add it here with the split. The same rule guards resume.facts in
app.profile.ai_gate and job.requirements.extract in app.ingest.extraction (the modules cannot import each other).

The evaluation suite and the benchmark's model methods call the gateway directly: they are how a route gets proven, so
this gate is not theirs.
"""

import logging

from sqlalchemy import Connection, text

from app.platform import config, log
from app.platform.llm import AIDisabled, registry

from .evidence import PROMPT_VERSION, TASK

# Whether a passed gold-set run proves this route: the model method, naming the model and the prompt in use.
ROUTE_IS_PROVEN = text(
    """SELECT EXISTS (
         SELECT 1 FROM eval_runs
          WHERE suite = 'evidence_faithfulness' AND status = 'passed'
            AND config->>'method' = 'model'
            AND config->>'model' = :model AND config->>'prompt_version' = :prompt_version)"""
)


def require_available(conn: Connection) -> None:
    """Raise AIDisabled unless the evidence task has a provider and, when that is a real model, a passed run proves it.

    AIDisabled.reason says which: no_route, no_key or fake_in_production from the registry (nothing usable is
    configured), or unproven (a real model that nobody has measured on the evidence gold set)."""
    route = registry.route(TASK, config.get())
    if route.adapter == "fake":
        return
    proven = conn.execute(ROUTE_IS_PROVEN, {"model": route.model, "prompt_version": PROMPT_VERSION}).scalar_one()
    if not proven:
        # Without this line a person sees no matches while the operator has set a key, and nothing says why.
        log.event("match.ai_gate", level=logging.WARNING, error_code="unproven", model=route.model)
        raise AIDisabled("unproven")
