"""Replay a gold set through the model gateway and record the run (AI_SYSTEM 12; requirement PLT-08).

A replay is the default: the gateway's fake provider answers every item from a recorded file, in item order, so a pull
request needs no model key and a run gives the same result every time. The run is one eval_runs row, one eval_items row
per gold item, and a frozen verdict once it finishes. The regression gate (gate.py) reads those rows afterwards.

A live run (live=True) is the one run that calls a real model: the provider the environment configures answers every
item, and the run records method "model" and the model's name, which is what a route gate asks for (a replay records
neither, so it never proves a route). Only a suite whose scorer declares LIVE has one, and only an OwnScorer can.
"""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, NamedTuple
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.platform import config, db, llm
from app.platform.llm import fake, registry

from . import EVALS, HARNESS_ONLY, gate

# The rule of app.evals.scratch, and of the demo seed's (a name ending in _test), plus the CI eval job's `evals`. Its
# own copy: a scorer's own check (scratch.refusal) stays a second line of defence that a test can switch off alone.
SCRATCH_NAME = re.compile(r"[a-z0-9_]+_test|evals")


class UnknownSuite(Exception):
    """The suite name has no entry in SUITES."""


class ReplayNeedsFake(Exception):
    """A replay with a real provider configured would call the model and ignore the recording (and cost money)."""


class NotAScratchDatabase(Exception):
    """A replay writes a frozen run row, and the suites' seeds write invented rows: only to a scratch database."""


class LiveNeedsRealProvider(Exception):
    """A live run with the fake provider configured would answer from no recording and prove nothing about a model."""


class LiveNotSupported(Exception):
    """The suite has no live mode: its scorer does not declare LIVE (or it is not a scorer of its own)."""


class SmokeLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["request", "thanks", "question", "statement", "greeting"]


@dataclass(frozen=True)
class Suite:
    task: str  # the gateway task name; under the fake provider no models.toml entry is needed
    instruction: str
    schema: type[BaseModel]

    @property
    def prompt_version(self) -> str:
        return hashlib.sha256(self.instruction.encode()).hexdigest()[:12]  # the template's content hash (AI_SYSTEM 2.2)


@dataclass(frozen=True)
class OwnScorer:
    """A suite that scores itself, because an item needs several gateway calls or a score that is not an exact match.
    Its script (a path under evals/) defines TASK (a task of its route), CONFIG (facts for the run record), RECORDED
    (its default recording) and run(recorded) -> Scored, and brings its own gold set and recording format. A script that
    sets LIVE = True also takes run(None), which calls the real model instead of replaying a recording."""

    script: str


SUITES: dict[str, Suite | OwnScorer] = {
    "harness_smoke": Suite(
        task="eval.harness_smoke",
        instruction=(
            'Classify the message as request, thanks, question, statement or greeting. Reply with JSON: {"kind": "..."}'
        ),
        schema=SmokeLabel,
    ),
    "resume_extraction": OwnScorer("resume_extraction/facts/score.py"),
    "evidence_faithfulness": OwnScorer("evidence_faithfulness/score.py"),
    "constraint_property": OwnScorer("constraint_property/score.py"),
    "role_intake": OwnScorer("role_intake/score.py"),
    "tailoring_integrity": OwnScorer("tailoring_integrity/score.py"),
    "chat": OwnScorer("chat/score.py"),
    "record_proposals": OwnScorer("record_proposals/score.py"),
    "injection": OwnScorer("injection/score.py"),
    "dossier": OwnScorer("dossier/score.py"),
    "agent_drafts": OwnScorer("agent_drafts/score.py"),
    "guardian": OwnScorer("guardian/score.py"),
}


class Finished(NamedTuple):
    run_id: UUID
    status: str
    passed: int
    n: int


class Result(NamedTuple):
    key: str
    slice: str | None
    expected: dict[str, Any]
    actual: dict[str, Any] | None
    is_passed: bool


class Scored(NamedTuple):
    """What an OwnScorer hands back: a Result per item, and the run's metrics. The gate reads metrics["score"]; the
    other keys are recorded beside the usual n and passed, and a key of the same name replaces it."""

    results: list[Result]
    metrics: dict[str, Any]


def load_gold(name: str) -> list[dict[str, Any]]:
    lines = (EVALS / "gold" / f"{name}.jsonl").read_text(encoding="utf-8").splitlines()
    items = [json.loads(line) for line in lines if line.strip()]
    keys = [item["key"] for item in items]
    if len(set(keys)) != len(keys):
        raise ValueError(f"gold/{name}.jsonl has a repeated key")  # eval_items is unique on (run, key)
    return items


def git_sha() -> str:
    """The commit under test: GIT_SHA (a build or CI sets it), else git, else "unknown" (never a guess)."""
    if given := os.environ.get("GIT_SHA"):
        return given
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, cwd=EVALS.parent
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return done.stdout.strip()


def _score(suite: Suite, item: dict[str, Any]) -> Result:
    messages = [{"role": "user", "content": f"{suite.instruction}\n\n{item['text']}"}]
    try:
        answer = llm.complete(suite.task, suite.schema, messages, principal=None, prompt_version=suite.prompt_version)
        actual: dict[str, Any] | None = answer.model_dump(mode="json")
    except llm.SchemaInvalid:
        actual = None  # invalid twice: this item fails, the run goes on
    return Result(item["key"], item.get("slice"), item["expected"], actual, actual == item["expected"])


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value)


def _finish(
    run_id: UUID, name: str, results: list[Result], error: str | None, extra: dict[str, Any] | None = None
) -> Finished:
    passed = sum(r.is_passed for r in results)
    metrics: dict[str, Any] = {"n": len(results), "passed": passed, "score": passed / len(results) if results else 0.0}
    metrics.update(extra or {})  # an OwnScorer's metrics, its own "score" included
    if name in HARNESS_ONLY:
        metrics["harness_check_only"] = True
    if error:
        status, gate_passed = "error", None
        metrics["error_code"] = error
    else:
        bar = gate.floor(name)
        gate_passed = bar is not None and metrics["score"] >= bar
        status = "passed" if gate_passed else "failed"
    with db.transaction(role="worker") as conn:
        for r in results:
            conn.execute(
                text(
                    "INSERT INTO eval_items (eval_run_id, item_key, slice, expected, actual, is_passed, score) "
                    "VALUES (:run, :key, :slice, CAST(:expected AS jsonb), CAST(:actual AS jsonb), :passed, :score)"
                ),
                {
                    "run": run_id,
                    "key": r.key,
                    "slice": r.slice,
                    "expected": _json(r.expected),
                    "actual": _json(r.actual),
                    "passed": r.is_passed,
                    "score": 1.0 if r.is_passed else 0.0,
                },
            )
        conn.execute(
            text(
                "UPDATE eval_runs SET status = CAST(:status AS eval_status), metrics = CAST(:metrics AS jsonb), "
                "is_gate_passed = :gate_passed, finished_at = now() WHERE id = :id"
            ),
            {"status": status, "metrics": json.dumps(metrics), "gate_passed": gate_passed, "id": run_id},
        )
    return Finished(run_id, status, passed, len(results))


def _start(name: str, run_config: dict[str, Any]) -> UUID:
    with db.transaction(role="worker") as conn:
        if run_config["mode"] == "replay":
            # A `dbname` query parameter overrides the URL's path, so refuse_unless_scratch's name check is not the
            # whole story: the server's own answer decides before the run row, which cannot be deleted, is written.
            where = conn.execute(text("SELECT current_database()")).scalar_one() or ""
            if not SCRATCH_NAME.fullmatch(where):
                raise NotAScratchDatabase(
                    f"the worker login is connected to database {where!r}, which does not end in _test and is not "
                    "'evals'. A replay writes a run that cannot be deleted: point it at a scratch database."
                )
        run_id: UUID = conn.execute(
            text(
                "INSERT INTO eval_runs (suite, git_sha, config) "
                "VALUES (CAST(:suite AS eval_suite), :sha, CAST(:config AS jsonb)) RETURNING id"
            ),
            {"suite": name, "sha": git_sha(), "config": json.dumps(run_config)},
        ).scalar_one()
    return run_id


def _run_own(name: str, own: OwnScorer, recorded: Path | None, live: bool) -> Finished:
    """The same refusals, run record and floor as any suite, with the items and metrics of the suite's own script."""
    spec = importlib.util.spec_from_file_location(f"evals_{name}", EVALS / own.script)
    assert spec is not None and spec.loader is not None
    scorer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = scorer
    spec.loader.exec_module(scorer)
    settings = config.get()
    if live:
        if not getattr(scorer, "LIVE", False):
            raise LiveNotSupported(name)
        if settings.llm_provider == "fake":
            raise LiveNeedsRealProvider()
        route = registry.route(scorer.TASK, settings)  # AIDisabled when there is no key: nothing is written
        # method "model" and the model name are what a route gate reads (app.match.ai_gate): only this run has them
        how: dict[str, Any] = {"mode": "live", "method": "model"}
        recorded = None
    else:
        if settings.llm_provider != "fake":
            raise ReplayNeedsFake()
        route = registry.route(scorer.TASK, settings)
        recorded = recorded or scorer.RECORDED
        how = {"mode": "replay", "recorded": recorded.name}
    run_id = _start(
        name, {**how, "task": scorer.TASK, "provider": route.provider, "model": route.model, **scorer.CONFIG}
    )
    results: list[Result] = []
    metrics: dict[str, Any] = {}
    error: str | None = None
    try:
        scored: Scored = scorer.run(recorded)
        results, metrics = scored.results, scored.metrics
    except Exception as failure:  # a stale recording, a quote that does not match: the run ends in error
        error = type(failure).__name__
    return _finish(run_id, name, results, error, metrics)


def refuse_unless_scratch() -> None:
    """The refusal of a replay (05-FIXES B6), the demo seed's rule over all four logins: each of DATABASE_URL_MIGRATE,
    _API, _WORKER and _MATCH must name a database that ends in _test, or `evals` (the CI eval job's database). Raised
    before any run row or seed row is written; a suite's seed looks again at current_database() once connected."""
    settings = config.get()
    for role in ("migrate", "api", "worker", "match"):
        name = make_url(settings.database_url(role)).database or ""
        if not SCRATCH_NAME.fullmatch(name):
            raise NotAScratchDatabase(
                f"the {role} login names database {name!r}, which does not end in _test and is not 'evals'. A replay "
                "writes invented rows and a run that cannot be deleted: point all four DATABASE_URL_* at a scratch "
                "database."
            )


def run_suite(name: str, recorded: Path | None = None, *, live: bool = False) -> Finished:
    """Replay the suite's recording through the gateway and record the run; with live=True call the real model the
    environment configures instead, and record a model-method run. Raises UnknownSuite, ReplayNeedsFake,
    LiveNeedsRealProvider, LiveNotSupported or AIDisabled before anything is written (ValueError for a live run given a
    recording); a failure while scoring, such as a provider that stops answering, ends the run with status "error"."""
    if name not in SUITES:
        raise UnknownSuite(name)
    if live and recorded is not None:
        raise ValueError("a live run has no recording to replay")
    if not live:
        refuse_unless_scratch()  # first of all: nothing below has written a row yet
    suite = SUITES[name]
    if isinstance(suite, OwnScorer):
        return _run_own(name, suite, recorded, live)
    if live:
        raise LiveNotSupported(name)
    gold = load_gold(name)
    recorded = recorded or EVALS / "recorded" / f"{name}.json"
    settings = config.get()
    if settings.llm_provider != "fake":
        raise ReplayNeedsFake()
    route = registry.route(suite.task, settings)
    run_config = {
        "mode": "replay",
        "recorded": recorded.name,
        "task": suite.task,
        "provider": route.provider,
        "model": route.model,
        "prompt_version": suite.prompt_version,
    }
    run_id = _start(name, run_config)
    fake.replay_from(recorded)
    results: list[Result] = []
    error: str | None = None
    try:
        for item in gold:
            results.append(_score(suite, item))
    except Exception as failure:  # the recording ran out, or the provider is not usable: the run ends in error
        error = type(failure).__name__
    finally:
        fake.replay_from(None)
    return _finish(run_id, name, results, error)
