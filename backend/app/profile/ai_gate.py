"""Whether a resume may be sent to a model right now (AI_SYSTEM 2.2; TRU-05).

A feature runs on any model that is configured: it is not held back because nobody has measured that model. For the
three resume.facts tasks that means: a provider serves each of them (no route, no key, an unpriced model or the fake in
production are still "AI is off": the upload answers 503 ai_disabled and a queued parse fails as ai_disabled). The gold
set measures how accurate a model is; it does not decide whether the model may be used.

When the provider is a real model and eval_runs holds no passed resume_extraction run on the test split, by the model
method, whose config names the model the routes use and the prompt versions in use, the check writes one info line
(status "unproven") so an operator can see that nobody has measured this model. Nothing else changes. A live run
records method, split, model and prompt_versions in its config (evals/resume_extraction/facts/README.md).

The fake provider replays recordings and calls no model; the gateway refuses it in production. The same rule holds for
job.requirements.extract in app.ingest.extraction (the two modules cannot import each other).
"""

import json

from sqlalchemy import Connection, text

from app.platform import config, log
from app.platform.llm import registry

from .extract import workflow

# Whether a passed run measured the routes: the model method on the test split, naming the model and the prompts in use.
# It decides only whether the log line below is written.
ROUTE_IS_PROVEN = text(
    """SELECT EXISTS (
         SELECT 1 FROM eval_runs
          WHERE suite = 'resume_extraction' AND status = 'passed'
            AND config->>'method' = 'model' AND config->>'split' = 'test'
            AND config->>'model' = :model
            AND config->'prompt_versions' = CAST(:versions AS jsonb))"""
)


def require_available(conn: Connection) -> None:
    """Raise AIDisabled unless every resume task has a provider to call.

    AIDisabled.reason says which: no_route, no_key, unpriced or fake_in_production from the registry (nothing usable is
    configured). A real model that has no passed run does not raise: it is served, and the check logs it as unproven."""
    settings = config.get()
    routes = [registry.route(task.name, settings) for task in workflow.TASKS]
    if all(route.adapter == "fake" for route in routes):
        return
    models = {route.model for route in routes}
    measured = False
    if len(models) == 1:  # one run names one model, so three tasks on different models cannot be vouched for by it
        found = {"model": next(iter(models)), "versions": json.dumps(workflow.prompt_versions())}
        measured = bool(conn.execute(ROUTE_IS_PROVEN, found).scalar_one())
    if not measured:
        log.event("profile.ai_gate", status="unproven", model=", ".join(sorted(models)))
