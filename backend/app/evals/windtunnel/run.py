"""The wind tunnel's run driver (INDEX "Harness API", "Grading" and "Run metrics").

run() builds one world per scenario and attempt, then drives every attempt on one thread through the real queue, graph,
Guardian, relay and HTTP API, with only the clock and the people simulated:

    each pass:  mark queued jobs of kinds the tunnel does not run as dead -> drain the queue -> relay the outbox into
                the capture sink (nothing is ever sent) -> let each attempt's scripted people react
    when idle:  an attempt that waits only for days has its clock advanced to its next due time; if every attempt waits
                on a real undo window or retry delay, sleep a little

Results are written once per attempt (sim_runs, sim_results) and the eval run is frozen at the end. A model call or a
job that fails ends the attempt it belongs to as `error` at once. Everything the run changes (the fake provider, the
page directory, the cached settings, the apply-link check, the sessions it opened) is put back when it ends.

Results are wiring checks of the agents' behaviour under recorded answers, not model quality.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from sqlalchemy import Connection, text

from app.evals import EVALS, runner
from app.ingest import api as ingest
from app.platform import config, db, queue, relay, sessions
from app.platform.llm import fake, registry

from . import graders, guard, scenario, world
from .actors import ActorError, Attempt
from .clock import VirtualClock, next_due, shift
from .graders import Mail

SCENARIOS = EVALS / "wind_tunnel" / "scenarios"
MAX_REAL_SECONDS = 1200  # assumption: the whole run
IDLE_SLEEP_SECONDS = 2  # assumption: while every attempt waits on a real undo window or retry delay
SWEEP_SECONDS = 10  # assumption: how often the Guardian's settle and expire sweeps are enqueued while idle
ATTEMPTS = 5  # EVL-04: a scenario passes only at five of five
# The jobs the scenarios need. Everything else (ingest, brief, nightly matching, trust) is marked dead, never run.
ALLOWED_KINDS = (
    "campaign.resume",
    "campaign.wake",
    "campaign.from_introduction",
    "campaign.introduction_answered",
    "campaign.application_changed",
    "graph.delete",
    "guardian.release",
    "guardian.expire",
    "guardian.settle",
    "tailor.generate",
    "apply.packet",
)
SWEEPS = ("guardian.settle", "guardian.expire")
SKIPPED = "windtunnel_skipped"
_started = False


class WatchRefused(Exception):
    """--watch prints a session cookie, which is never done on a production configuration."""


class RunFailed(Exception):
    """A job tied to no attempt failed, or the run's own deadline passed with the database in an unknown state."""


def _startup() -> None:
    """The start-up steps of app.worker.main() without its loops, once per process: importing the worker module loads
    every handler and registry, and the checkpoint schema is checked like a worker's start (registrations raise when
    made twice, so this happens once)."""
    global _started
    if _started:
        return
    import app.worker  # noqa: F401  (importing it registers every queue handler and mail template)
    from app.guardian import runtime

    runtime.assert_checkpoints_current()
    _started = True


def _reset_settings() -> None:
    clear = getattr(config.get, "cache_clear", None)
    if clear is not None:
        clear()


class _Driver:
    def __init__(
        self,
        scenarios: Sequence[scenario.Scenario],
        asgi_app: FastAPI,
        attempts: int,
        pace: float,
        watch: bool,
        live: bool,
    ) -> None:
        self.scenarios, self.asgi_app, self.attempts = scenarios, asgi_app, attempts
        self.pace, self.watch, self.live = pace, watch, live
        self.worker_id = f"windtunnel:{socket.gethostname()}:{os.getpid()}"
        self.run_id: UUID | None = None
        self.scenario_ids: dict[str, UUID] = {}
        self.all: list[Attempt] = []
        self.sim_runs: dict[int, UUID] = {}  # id(attempt) -> sim_runs.id
        self.tokens: list[str] = []
        self.by_user: dict[UUID, Attempt] = {}
        self.skipped: Counter[str] = Counter()
        self.seen_errors: set[tuple[int, int]] = set()
        self.last_sweep = 0.0
        self.sweep_number = 0
        self.metrics: dict[int, dict[str, Any]] = {}
        self.violations: dict[int, dict[str, int]] = {}
        self.results: dict[int, str] = {}
        self.watched = False

    # ---- set-up -----------------------------------------------------------------------------------------------

    def open_attempts(self, tag: str) -> None:
        with db.transaction(role="migrate") as conn:
            for one in self.scenarios:
                for number in range(1, self.attempts + 1):
                    made = world.build(conn, one, number, tag, replay=not self.live)
                    clock = VirtualClock(made.started)
                    candidate_token = sessions.start(conn, made.user_id)
                    employer_token = sessions.start(conn, made.employer_id) if made.employer_id else None
                    self.tokens += [candidate_token] + ([employer_token] if employer_token else [])
                    attempt = Attempt(one, made, number, clock, self.asgi_app, candidate_token, employer_token)
                    self.all.append(attempt)
                    self.by_user[made.user_id] = attempt
                    if made.employer_id:
                        self.by_user[made.employer_id] = attempt
                    if self.watch and not self.watched:
                        self.watched = True  # attempt 1 of the first scenario: the one a person follows
                        print(f"wind tunnel: session cookie {sessions.COOKIE_NAME}={candidate_token}", file=sys.stderr)
                        print("wind tunnel: open /control/runs with this cookie to watch it", file=sys.stderr)

    def record_start(self) -> None:
        with db.transaction(role="worker") as conn:
            for attempt in self.all:
                self.sim_runs[id(attempt)] = conn.execute(
                    text(
                        """INSERT INTO sim_runs (eval_run_id, scenario_id, attempt, seed, candidate_user_id)
                           VALUES (:run, :scenario, :attempt, :seed, :user) RETURNING id"""
                    ),
                    {
                        "run": self.run_id,
                        "scenario": self.scenario_ids[attempt.scenario.key],
                        "attempt": attempt.number,
                        "seed": attempt.number,
                        "user": attempt.world.user_id,
                    },
                ).scalar_one()

    # ---- the passes -------------------------------------------------------------------------------------------

    def live_attempts(self) -> list[Attempt]:
        return [a for a in self.all if a.status == "running"]

    def skip_other_kinds(self) -> None:
        with db.transaction(role="migrate") as conn:
            rows = conn.execute(
                text(
                    """UPDATE queue_jobs SET status = 'dead', last_error_code = :code, finished_at = now(),
                              locked_by = NULL, locked_at = NULL
                        WHERE status = 'queued' AND NOT kind = ANY(:allowed) RETURNING kind"""
                ),
                {"code": SKIPPED, "allowed": list(ALLOWED_KINDS)},
            ).all()
        self.skipped.update(row.kind for row in rows)

    def drain(self) -> int:
        ran = 0
        while queue.run_once(lambda: db.transaction(role="worker"), self.worker_id):
            ran += 1
        return ran

    def check_errors(self) -> None:
        """A job that recorded an error ends the attempt it belongs to, now, not after a retry delay."""
        with db.transaction(role="migrate") as conn:
            rows = conn.execute(
                text(
                    """SELECT id, kind, attempts, last_error_code, user_id, payload ->> 'campaign_id' AS campaign
                         FROM queue_jobs WHERE last_error_code IS NOT NULL AND last_error_code <> :skipped"""
                ),
                {"skipped": SKIPPED},
            ).all()
        for row in rows:
            if (row.id, row.attempts) in self.seen_errors:
                continue
            self.seen_errors.add((row.id, row.attempts))
            owner = self.by_user.get(row.user_id) if row.user_id else None
            if owner is None and row.campaign:
                owner = next((a for a in self.all if str(a.campaign_id) == row.campaign), None)
            if owner is None:
                raise RunFailed(f"a {row.kind} job of no attempt failed with {row.last_error_code}")
            if owner.status == "running":
                self.finish(owner, "error", f"job {row.kind} failed: {row.last_error_code}")

    def relay(self) -> int:
        sent = 0

        def sink(captured: relay.Captured) -> None:
            with db.transaction(role="migrate") as conn:
                owner_id, payload = conn.execute(
                    text("SELECT user_id, payload FROM outbox WHERE id = :id"), {"id": captured.outbox_id}
                ).one()
            attempt = self.by_user.get(owner_id) if owner_id else None
            if attempt is None:
                raise RunFailed("a message of no attempt reached the capture sink")
            attempt.note_mail(
                Mail(
                    outbox_id=str(captured.outbox_id),
                    template=captured.template,
                    to=captured.to,
                    subject=captured.subject,
                    body=captured.body,
                    headers=dict(captured.headers),
                    agent_action_id=captured.agent_action_id,
                    dedupe_key=captured.dedupe_key,
                    payload=dict(payload or {}),
                    day=attempt.clock.day_now(),
                )
            )

        while True:
            with db.transaction(role="worker") as conn:
                count = relay.relay_once(conn, capture=sink)  # always with capture: nothing is ever sent
            if not count:
                return sent
            sent += count

    def sweeps(self) -> None:
        """The Guardian's settle and expire sweeps, enqueued with a unique key per call (the scheduler is never ticked,
        so no ingest, brief or nightly job starts)."""
        self.sweep_number += 1
        self.last_sweep = time.monotonic()
        with db.transaction(role="worker") as conn:
            for kind in SWEEPS:
                queue.enqueue(conn, kind, {}, unique_key=f"{kind}:{self.worker_id}:{self.sweep_number}")

    def real_wait(self, attempt: Attempt) -> bool:
        """The attempt waits on something only real time ends: an outbox row inside its undo window, a job on its
        retry or release time."""
        if attempt.campaign_id is None:
            return False
        with db.transaction(role="migrate") as conn:
            found = conn.execute(
                text(
                    """SELECT EXISTS (SELECT 1 FROM outbox WHERE user_id = :u AND status IN ('pending', 'sending')
                                      AND not_before < 'infinity')
                           OR EXISTS (SELECT 1 FROM queue_jobs
                                       WHERE status IN ('queued', 'running') AND kind <> 'campaign.wake'
                                         AND (user_id = :u OR payload ->> 'campaign_id' = :c))"""
                ),
                {"u": attempt.world.user_id, "c": str(attempt.campaign_id)},
            ).scalar_one()
        return bool(found)

    def idle(self) -> bool:
        """Nothing ran: advance each waiting attempt's clock, end the finished ones; True when something moved."""
        moved = False
        for attempt in self.live_attempts():
            now = attempt.clock.now()
            if attempt.clock.day_now() > attempt.scenario.max_virtual_days:
                self.finish(attempt, "done")
                continue
            due = attempt.deadlines()
            if attempt.campaign_id is not None:
                with db.transaction(role="migrate") as conn:
                    campaign_due = next_due(conn, attempt.campaign_id, after=now)
                if campaign_due is not None:
                    due.append(campaign_due)
            due = [moment for moment in due if moment > now]
            if self.real_wait(attempt):
                continue
            if not due:
                self.finish(attempt, "done")  # nothing left that could happen
                continue
            delta = min(due) - now + timedelta(seconds=1)
            if attempt.campaign_id is not None:
                with db.transaction(role="migrate") as conn:
                    shift(conn, attempt.campaign_id, delta)
            attempt.clock.advance(delta)
            moved = True
        return moved

    def loop(self) -> None:
        deadline = time.monotonic() + MAX_REAL_SECONDS
        while self.live_attempts():
            if time.monotonic() > deadline:
                for attempt in self.live_attempts():
                    self.finish(attempt, "error", "the run's real-time limit passed")
                break
            self.skip_other_kinds()
            ran = self.drain()
            self.check_errors()
            sent = self.relay()
            if sent:
                self.sweeps()
            acted = False
            for attempt in self.live_attempts():
                try:
                    acted = attempt.react() or acted
                except ActorError as failure:
                    self.finish(attempt, "error", str(failure))
                except Exception as failure:  # an attempt that breaks must not stop the others
                    self.finish(attempt, "error", f"{type(failure).__name__} in the scripted people")
            if ran or sent or acted:
                if self.pace:
                    time.sleep(self.pace)
                continue
            if self.idle():
                continue
            if time.monotonic() - self.last_sweep > SWEEP_SECONDS:
                self.sweeps()
                continue
            time.sleep(max(IDLE_SLEEP_SECONDS, self.pace))

    # ---- grading and recording -------------------------------------------------------------------------------

    def finish(self, attempt: Attempt, status: str, reason: str | None = None) -> None:
        """End one attempt: grade it (or record why it errored) and write its rows."""
        if attempt.status != "running":
            return
        attempt.status = status
        rows: list[graders.SimResult]
        final = status
        if status == "error" or attempt.campaign_id is None:
            final = "error"
            code = reason if status == "error" else "no campaign was started"
            rows = [graders.SimResult("outcome", "harness_error", False, {"reason": code})]
            self.metrics[id(attempt)] = {}
        else:
            with db.transaction(role="migrate") as conn:
                trajectory = graders.collect(
                    conn, attempt.world, attempt.campaign_id, attempt.captured, attempt.log, attempt.clock
                )
            rows = graders.grade(trajectory, attempt.scenario.expect)
            self.metrics[id(attempt)] = graders.metrics(trajectory)
            final = "passed" if all(row.passed for row in rows) else "failed"
        self.results[id(attempt)] = final
        self.violations[id(attempt)] = {r.code: int(r.details.get("count", 0)) for r in rows if r.kind == "violation"}
        with db.transaction(role="worker") as conn:
            for row in rows:
                conn.execute(
                    text(
                        """INSERT INTO sim_results (sim_run_id, kind, code, passed, details)
                           VALUES (:run, CAST(:kind AS sim_result_kind), :code, :passed, CAST(:details AS jsonb))"""
                    ),
                    {
                        "run": self.sim_runs[id(attempt)],
                        "kind": row.kind,
                        "code": row.code,
                        "passed": row.passed,
                        "details": json.dumps(row.details),
                    },
                )
            conn.execute(
                text(
                    """UPDATE sim_runs SET status = CAST(:status AS sim_run_status), finished_at = now(),
                              campaign_id = :campaign WHERE id = :id"""
                ),
                {"status": final, "campaign": attempt.campaign_id, "id": self.sim_runs[id(attempt)]},
            )
        world.release(attempt.world)
        attempt.close()

    def summary(self) -> dict[str, Any]:
        """eval_runs.metrics (INDEX "Run metrics")."""
        per_scenario: dict[str, Any] = {}
        totals: Counter[str] = Counter()
        minutes, interviews = 0, 0
        for one in self.scenarios:
            mine = [a for a in self.all if a.scenario.key == one.key]
            won = sum(self.results.get(id(a)) == "passed" for a in mine)
            figures = [self.metrics.get(id(a), {}) for a in mine]
            per_scenario[one.key] = {
                "persona": one.persona,
                "version": one.version,
                "attempts_passed": won,
                "passed": len(mine) == ATTEMPTS and won == ATTEMPTS,
                "medians": graders.medians(figures),
            }
            minutes += sum(f.get("human_minutes", 0) for f in figures)
            interviews += sum(f.get("interviews", 0) for f in figures)
            for attempt in mine:
                totals.update(self.violations.get(id(attempt), {}))
        names = {code: f"{code}s" for code in graders.VIOLATIONS}
        passed = sum(1 for s in per_scenario.values() if s["passed"])
        return {
            "n": len(self.scenarios),
            "passed": passed,
            "score": 1.0 if passed == len(self.scenarios) and self.scenarios else 0.0,
            "attempts": len(self.all),
            "attempts_passed": sum(1 for a in self.all if self.results.get(id(a)) == "passed"),
            "totals": {names[code]: totals.get(code, 0) for code in graders.VIOLATIONS},
            "scenarios": per_scenario,
            "human_minutes_per_interview": minutes / interviews if interviews else None,
        }


def _used(conn: Connection, users: Sequence[UUID]) -> list[Any]:
    return list(
        conn.execute(
            text("SELECT DISTINCT task, prompt_version FROM llm_calls WHERE user_id = ANY(:u) ORDER BY 1, 2"),
            {"u": list(users)},
        )
    )


def run(
    keys: Sequence[str] | None,
    *,
    asgi_app: FastAPI,
    attempts: int = ATTEMPTS,
    scenarios_dir: Path = SCENARIOS,
    pace: float = 0.0,
    watch: bool = False,
    live: bool = False,
) -> runner.Finished:
    """Run the named scenarios (all of them when `keys` is None), `attempts` times each, and record the run."""
    settings = config.get()
    if live and settings.llm_provider == "fake":
        raise runner.LiveNeedsRealProvider()
    if not live and settings.llm_provider != "fake":
        raise runner.ReplayNeedsFake()
    if watch and settings.app_env == "production":
        raise WatchRefused("--watch prints a session cookie and is refused when APP_ENV is production")
    if not 1 <= attempts <= ATTEMPTS:
        raise ValueError(f"attempts must be from 1 to {ATTEMPTS}")
    guard.require_test_database()
    scenarios = scenario.load_dir(scenarios_dir, keys)
    if not scenarios:
        raise scenario.ScenarioInvalid("no scenario to run")
    driver = _Driver(scenarios, asgi_app, attempts, pace, watch, live)
    with db.transaction(role="migrate") as conn:
        guard.require_no_other_worker(conn, driver.worker_id)
        guard.require_quiet_database(conn)
    _startup()
    previous_pages = os.environ.get("RESEARCH_PAGES_DIR")
    real_verify = ingest.verify_live
    pages = Path(tempfile.mkdtemp(prefix="windtunnel-pages-"))
    error: str | None = None
    try:
        for one in scenarios:
            if one.pages.is_dir():
                shutil.copytree(one.pages, pages, dirs_exist_ok=True)
        os.environ["RESEARCH_PAGES_DIR"] = str(pages)
        _reset_settings()
        ingest.verify_live = lambda conn, job_id: True  # type: ignore[assignment]  # no apply link is ever fetched
        fake.replay_from(Path(tempfile.gettempdir()) / "windtunnel-no-such-recording.json")  # unregistered: fail loudly
        with db.transaction(role="worker") as conn:
            driver.scenario_ids = scenario.ensure_rows(conn, scenarios)
        driver.run_id = runner._start(
            "wind_tunnel",
            {
                "mode": "live" if live else "replay",
                "provider": settings.llm_provider,
                "scenario_versions": {s.key: s.version for s in scenarios},
                "attempts": attempts,
                "labels": scenario.LABEL,
            },
        )
        driver.open_attempts(f"{datetime.now(UTC):%m%d%H%M%S}")
        driver.record_start()
        driver.loop()
    except Exception as failure:
        error = type(failure).__name__
        for attempt in driver.live_attempts():
            try:
                driver.finish(attempt, "error", error)
            except Exception:  # the run is already failing; its error is the one reported
                attempt.status = "error"
        if driver.run_id is None:
            raise
    finally:
        for attempt in driver.all:
            world.release(attempt.world)
            attempt.close()
        fake.replay_from(None)
        ingest.verify_live = real_verify  # type: ignore[assignment]
        if previous_pages is None:
            os.environ.pop("RESEARCH_PAGES_DIR", None)
        else:
            os.environ["RESEARCH_PAGES_DIR"] = previous_pages
        _reset_settings()
        shutil.rmtree(pages, ignore_errors=True)
        if driver.tokens:
            with db.transaction(role="migrate") as conn:
                for token in driver.tokens:
                    sessions.revoke(conn, token)
    return _finish(driver, error)


def _finish(driver: _Driver, error: str | None) -> runner.Finished:
    summary = driver.summary()
    if error:
        summary["error_code"] = error
    status = "error" if error else ("passed" if summary["passed"] == summary["n"] and summary["n"] else "failed")
    users = [a.world.user_id for a in driver.all]
    settings = config.get()
    with db.transaction(role="migrate") as conn:
        used = _used(conn, users)
    routes: dict[str, Any] = {}
    for row in used:
        try:
            route = registry.route(row.task, settings)
            routes[row.task] = {"provider": route.provider, "model": route.model}
        except Exception:  # a task with no route is reported without one
            routes[row.task] = None
    extra = {
        "routes": routes,
        "prompt_versions": {row.task: row.prompt_version for row in used},
        "skipped_jobs": dict(driver.skipped),
    }
    with db.transaction(role="worker") as conn:
        conn.execute(
            text(
                """UPDATE eval_runs SET status = CAST(:status AS eval_status), metrics = CAST(:metrics AS jsonb),
                          config = config || CAST(:extra AS jsonb), is_gate_passed = :gate, finished_at = now()
                    WHERE id = :id"""
            ),
            {
                "status": status,
                "metrics": json.dumps(summary),
                "extra": json.dumps(extra),
                "gate": None if status == "error" else status == "passed",
                "id": driver.run_id,
            },
        )
    assert driver.run_id is not None
    return runner.Finished(driver.run_id, status, summary["passed"], summary["n"])
