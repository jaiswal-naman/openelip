"""Whether the evidence task may call a real model right now (AI_SYSTEM 2.2 and 12.8).

A feature runs on any model that is configured: it is not held back because nobody has measured that model. For
match.evidence.score that means a provider serves the task (no route, no key, an unpriced model or the fake in
production are still "AI is off": the run completes without scoring and every retrieved job is withheld not_scored).
The gold set measures how accurate a model is; it does not decide whether the model may be used.

When the provider is a real model and eval_runs holds no passed evidence_faithfulness run by the model method whose
config names the route's model and the current prompt version, the check writes one info line (status "unproven") so an
operator can see that nobody has measured this model. Nothing else changes. A live run records `method` ("model"),
`model` and `prompt_version` in its config. The same rule holds for resume.facts in app.profile.ai_gate and
job.requirements.extract in app.ingest.extraction (the modules cannot import each other).

The evaluation suite and the benchmark's model methods call the gateway directly: they are how a model gets measured, so
this check is not theirs.
"""

from sqlalchemy import Connection, text

from app.platform import config, log
from app.platform.llm import registry

from .evidence import PROMPT_VERSION, TASK

# Whether a passed gold-set run measured this route: the model method, naming the model and the prompt in use.
# It decides only whether the log line below is written.
ROUTE_IS_PROVEN = text(
    """SELECT EXISTS (
         SELECT 1 FROM eval_runs
          WHERE suite = 'evidence_faithfulness' AND status = 'passed'
            AND config->>'method' = 'model'
            AND config->>'model' = :model AND config->>'prompt_version' = :prompt_version)"""
)


def require_available(conn: Connection) -> None:
    """Raise AIDisabled unless the evidence task has a provider to call.

    AIDisabled.reason says which: no_route, no_key, unpriced or fake_in_production from the registry (nothing usable is
    configured). A real model that has no passed run does not raise: it is served, and the check logs it as unproven."""
    route = registry.route(TASK, config.get())
    if route.adapter == "fake":
        return
    measured = conn.execute(ROUTE_IS_PROVEN, {"model": route.model, "prompt_version": PROMPT_VERSION}).scalar_one()
    if not measured:
        log.event("match.ai_gate", status="unproven", model=route.model)
