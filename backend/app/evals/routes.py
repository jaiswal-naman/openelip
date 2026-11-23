"""The scoreboard and one run's detail (API 2.19, EVL-04, EVL-05).

Every account may read it, so it holds nothing personal: the rows come from the reference tables of the wind tunnel and
from `eval_runs`, whose `config` is cut down to a list of keys and whose `metrics` are aggregates. No `eval_items` is
read (the API login has no grant on them; decision 26). Nothing here computes a quality number: a value is a stored one
with the run that recorded it, and where no run was recorded the value is null, never zero. A replay run is a wiring
check, so every value carries its `mode`.

The handlers are plain `def` on the API login under the caller's id.
"""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from app.platform import db
from app.platform.api import Envelope, envelope
from app.platform.errors import ApiError
from app.platform.sessions import Principal, current_user

from . import HARNESS_ONLY

router = APIRouter(prefix="/v1/control/evals")
Signed = Annotated[Principal, Depends(current_user)]

# What a run's config may show: ids and names of what was under test, never anything a person wrote.
CONFIG_KEYS = (
    "mode",
    "provider",
    "model",
    "routes",
    "prompt_version",
    "prompt_versions",
    "scenario_versions",
    "attempts",
    "labels",
    "recorded",
)
FINISHED = "('passed', 'failed')"  # a run still running, or one that ended in error, has no result to show
PROXY = "a proxy"


class SuiteResult(BaseModel):
    run_id: UUID
    status: str
    mode: str  # "replay" is a wiring check, not a quality measure
    n: int | None
    passed: int | None
    score: float | None
    git_sha: str
    finished_at: datetime
    model: str | None
    labels: str | None


class Suite(BaseModel):
    suite: str
    result: SuiteResult | None


class ScenarioResult(BaseModel):
    run_id: UUID
    version: int  # the version of the scenario that run used
    attempts: int
    attempts_passed: int
    passed: bool
    attempt_statuses: list[str]
    medians: dict[str, Any] | None
    mode: str
    git_sha: str
    finished_at: datetime


class Scenario(BaseModel):
    key: str
    title: str
    version: int
    result: ScenarioResult | None


class Persona(BaseModel):
    persona: str
    scenarios: list[Scenario]


class LiveRun(BaseModel):
    run_id: UUID
    finished_at: datetime


class HumanMinutes(BaseModel):
    value: float
    run_id: UUID
    label: str


class WindTunnel(BaseModel):
    last_live_run: LiveRun | None
    personas: list[Persona]
    human_minutes_per_interview: HumanMinutes | None


class Scoreboard(BaseModel):
    suites: list[Suite]
    wind_tunnel: WindTunnel


class AttemptResult(BaseModel):
    kind: str
    code: str
    passed: bool
    details: dict[str, Any]


class Attempt(BaseModel):
    scenario_key: str
    attempt: int
    seed: int
    status: str
    started_at: datetime
    finished_at: datetime | None
    results: list[AttemptResult]


class EvalRun(BaseModel):
    run_id: UUID
    suite: str
    status: str
    mode: str
    git_sha: str
    started_at: datetime
    finished_at: datetime | None
    config: dict[str, Any]
    metrics: dict[str, Any] | None
    attempts: list[Attempt] | None  # wind tunnel only


def _mode(config: dict[str, Any]) -> str:
    return str(config.get("mode") or "replay")


def _suite_results(conn: Connection) -> list[Suite]:
    names = [
        n
        for n in conn.execute(text("SELECT unnest(enum_range(NULL::eval_suite))::text")).scalars()
        if n != "wind_tunnel" and n not in HARNESS_ONLY
    ]
    latest = {
        r.suite: r
        for r in conn.execute(
            text(
                f"""SELECT DISTINCT ON (suite) suite::text AS suite, id, status::text AS status, config, metrics,
                           git_sha, finished_at
                    FROM eval_runs WHERE suite = ANY(CAST(:names AS eval_suite[])) AND status IN {FINISHED}
                    ORDER BY suite, finished_at DESC, id"""
            ),
            {"names": names},
        )
    }
    suites = []
    for name in names:
        row = latest.get(name)
        result = None
        if row is not None:
            metrics = row.metrics or {}
            result = SuiteResult(
                run_id=row.id,
                status=row.status,
                mode=_mode(row.config),
                n=metrics.get("n"),
                passed=metrics.get("passed"),
                score=metrics.get("score"),
                git_sha=row.git_sha,
                finished_at=row.finished_at,
                model=row.config.get("model"),
                labels=row.config.get("labels"),
            )
        suites.append(Suite(suite=name, result=result))
    return suites


def _wind_tunnel(conn: Connection) -> WindTunnel:
    personas = conn.execute(text("SELECT unnest(enum_range(NULL::sim_persona))::text")).scalars().all()
    scenarios = conn.execute(
        text(
            """SELECT DISTINCT ON (key) key, title, version, persona::text AS persona
               FROM sim_scenarios ORDER BY key, version DESC"""
        )
    ).all()
    # the newest finished run that included each scenario key (any version of it), with its verdict
    verdicts = {
        r.key: r
        for r in conn.execute(
            text(
                f"""SELECT DISTINCT ON (s.key) s.key, s.version, s.id AS scenario_id, e.id AS run_id, e.config,
                           e.metrics, e.git_sha, e.finished_at, v.attempts, v.attempts_passed, v.passed
                    FROM sim_scenario_verdicts v
                    JOIN sim_scenarios s ON s.id = v.scenario_id
                    JOIN eval_runs e ON e.id = v.eval_run_id
                    WHERE e.suite = 'wind_tunnel' AND e.status IN {FINISHED}
                    ORDER BY s.key, e.finished_at DESC, e.id"""
            )
        )
    }
    statuses: dict[tuple[UUID, UUID], list[str]] = {}
    if verdicts:
        for r in conn.execute(
            text(
                """SELECT eval_run_id, scenario_id, status::text AS status FROM sim_runs
                   WHERE eval_run_id = ANY(CAST(:runs AS uuid[])) ORDER BY attempt"""
            ),
            {"runs": [v.run_id for v in verdicts.values()]},
        ):
            statuses.setdefault((r.eval_run_id, r.scenario_id), []).append(r.status)
    by_persona: dict[str, list[Scenario]] = {p: [] for p in personas}
    for s in scenarios:
        v = verdicts.get(s.key)
        result = None
        if v is not None:
            medians = ((v.metrics or {}).get("scenarios") or {}).get(s.key, {}).get("medians")
            result = ScenarioResult(
                run_id=v.run_id,
                version=v.version,
                attempts=v.attempts,
                attempts_passed=v.attempts_passed,
                passed=v.passed,
                attempt_statuses=statuses.get((v.run_id, v.scenario_id), []),
                medians=medians,
                mode=_mode(v.config),
                git_sha=v.git_sha,
                finished_at=v.finished_at,
            )
        by_persona[s.persona].append(Scenario(key=s.key, title=s.title, version=s.version, result=result))
    for found in by_persona.values():
        found.sort(key=lambda sc: sc.key)
    live = conn.execute(
        text(
            f"""SELECT id, finished_at FROM eval_runs
                WHERE suite = 'wind_tunnel' AND status IN {FINISHED} AND config ->> 'mode' = 'live'
                ORDER BY finished_at DESC, id LIMIT 1"""
        )
    ).one_or_none()
    newest = conn.execute(
        text(
            f"""SELECT id, metrics -> 'human_minutes_per_interview' AS minutes FROM eval_runs
                WHERE suite = 'wind_tunnel' AND status IN {FINISHED} ORDER BY finished_at DESC, id LIMIT 1"""
        )
    ).one_or_none()
    minutes = None
    if newest is not None and isinstance(newest.minutes, int | float) and not isinstance(newest.minutes, bool):
        minutes = HumanMinutes(value=newest.minutes, run_id=newest.id, label=PROXY)
    return WindTunnel(
        last_live_run=None if live is None else LiveRun(run_id=live.id, finished_at=live.finished_at),
        personas=[Persona(persona=p, scenarios=by_persona[p]) for p in personas],
        human_minutes_per_interview=minutes,
    )


@router.get("", response_model=Envelope[Scoreboard])
def scoreboard(principal: Signed) -> dict[str, Any]:
    with db.transaction(user_id=principal.user_id) as conn:
        return envelope(Scoreboard(suites=_suite_results(conn), wind_tunnel=_wind_tunnel(conn)))


@router.get("/runs/{eval_run_id}", response_model=Envelope[EvalRun])
def run_detail(eval_run_id: UUID, principal: Signed) -> dict[str, Any]:
    with db.transaction(user_id=principal.user_id) as conn:
        row = conn.execute(
            text(
                """SELECT id, suite::text AS suite, status::text AS status, config, metrics, git_sha, started_at,
                          finished_at FROM eval_runs WHERE id = :id"""
            ),
            {"id": eval_run_id},
        ).one_or_none()
        if row is None or row.suite in HARNESS_ONLY:
            raise ApiError(404, "not_found", "No such resource.")
        attempts = _attempts(conn, eval_run_id) if row.suite == "wind_tunnel" else None
    data = EvalRun(
        run_id=row.id,
        suite=row.suite,
        status=row.status,
        mode=_mode(row.config),
        git_sha=row.git_sha,
        started_at=row.started_at,
        finished_at=row.finished_at,
        config={k: row.config[k] for k in CONFIG_KEYS if k in row.config},
        metrics=row.metrics,
        attempts=attempts,
    )
    return envelope(data)


def _attempts(conn: Connection, run_id: UUID) -> list[Attempt]:
    attempts = conn.execute(
        text(
            """SELECT r.id, s.key, r.attempt, r.seed, r.status::text AS status, r.started_at, r.finished_at
               FROM sim_runs r JOIN sim_scenarios s ON s.id = r.scenario_id
               WHERE r.eval_run_id = :run ORDER BY s.key, r.attempt"""
        ),
        {"run": run_id},
    ).all()
    results: dict[UUID, list[AttemptResult]] = {}
    for r in conn.execute(
        text(
            """SELECT x.sim_run_id, x.kind::text AS kind, x.code, x.passed, x.details FROM sim_results x
               JOIN sim_runs r ON r.id = x.sim_run_id WHERE r.eval_run_id = :run ORDER BY x.id"""
        ),
        {"run": run_id},
    ):
        results.setdefault(r.sim_run_id, []).append(
            AttemptResult(kind=r.kind, code=r.code, passed=r.passed, details=r.details)
        )
    return [
        Attempt(
            scenario_key=a.key,
            attempt=a.attempt,
            seed=a.seed,
            status=a.status,
            started_at=a.started_at,
            finished_at=a.finished_at,
            results=results.get(a.id, []),
        )
        for a in attempts
    ]
