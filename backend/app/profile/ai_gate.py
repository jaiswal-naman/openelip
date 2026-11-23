"""Whether a resume may be sent to a model right now (AI_SYSTEM 2.2; TRU-05).

A route may serve a task only when a passed gold-set run for that task and model exists. For the three resume.facts
tasks that means: a provider serves each of them, and, when the provider is a real model, eval_runs holds a passed
resume_extraction run on the test split, by the model method, whose config names the model the routes use and the prompt
versions in use. Until a live run has been recorded no real model reads a resume, key or no key: the upload answers
503 ai_disabled and a queued parse fails as ai_disabled, which is the same "AI is off" the person sees with no key.

The fake provider replays recordings and calls no model, so it needs no proof; the gateway refuses it in production. The
replay CI records names the model "recorded" and no method or split, so it never proves a real route. A live run must
record method, split, model and prompt_versions in its config (evals/resume_extraction/facts/README.md). The same rule
guards job.requirements.extract in app.ingest.extraction (the two modules cannot import each other).
"""

import json
import logging

from sqlalchemy import Connection, text

from app.platform import config, log
from app.platform.llm import AIDisabled, registry

from .extract import workflow

# Whether a passed run proves the routes: the model method on the test split, naming the model and the prompts in use.
ROUTE_IS_PROVEN = text(
    """SELECT EXISTS (
         SELECT 1 FROM eval_runs
          WHERE suite = 'resume_extraction' AND status = 'passed'
            AND config->>'method' = 'model' AND config->>'split' = 'test'
            AND config->>'model' = :model
            AND config->'prompt_versions' = CAST(:versions AS jsonb))"""
)


def require_available(conn: Connection) -> None:
    """Raise AIDisabled unless every resume task has a provider and a real model is proven by a passed run.

    AIDisabled.reason says which: no_route, no_key or unpriced from the registry (nothing is configured), or unproven (a
    real model that nobody has measured on the resume gold set). The API answers any of them as 503 ai_disabled."""
    settings = config.get()
    routes = [registry.route(task.name, settings) for task in workflow.TASKS]
    if all(route.adapter == "fake" for route in routes):
        return
    models = {route.model for route in routes}
    proven = False
    if len(models) == 1:  # one run names one model, so three tasks on different models cannot be vouched for by it
        found = {"model": next(iter(models)), "versions": json.dumps(workflow.prompt_versions())}
        proven = bool(conn.execute(ROUTE_IS_PROVEN, found).scalar_one())
    if not proven:
        # Without this line a person sees "AI is off" while the operator has set a key, and nothing says why.
        log.event("profile.ai_gate", level=logging.WARNING, error_code="unproven", model=", ".join(sorted(models)))
        raise AIDisabled("unproven")
