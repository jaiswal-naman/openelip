"""TWN-02 and the durability rule (plan 09-03): a worker killed while a Twin thread is parked, or in any node, mid model
call, mid proposal or after a release, and restarted, ends in the same tables as an uninterrupted run, with no duplicate
action, approval or message.

Two stories run to their end: an answer the candidate approves (dial ask first) and a question the record does not
answer that the candidate replies to. For a crash case, the phase that holds the crash point runs in
tests/twin/fixtures/graph/crash_driver.py, a subprocess that kills itself with `os._exit(137)` at the named point; the
killed job's lease is expired and reaped the way production recovers it, and the driver runs again with no crash. The
queue's unique keys, the nodes' check-first reads, `approvals_one_per_action` and `twin_messages_one_final` make each
re-run harmless; the final tables are compared to the uninterrupted run's."""

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Engine, text

from app.guardian import runtime
from app.platform import db, queue
from app.platform.llm import fake
from tests.guardian.conftest import clean  # noqa: F401
from tests.twin.fixtures.graph import world as w

DRIVER = Path(__file__).parent / "fixtures" / "graph" / "crash_driver.py"
BACKEND = Path(__file__).resolve().parents[2]
REPLY = "I can start in 30 days."


class Crash(NamedTuple):
    point: str
    phase: str  # ask: from the question to the parked thread; approve: from the approval to the final message
    story: str = "answer"
    count: int = 1


CASES = [
    Crash("after-drive", "ask"),  # killed after the thread parked: one action and one approval wait
    Crash("after-node:triage", "ask"),
    Crash("after-node:draft", "ask"),  # the proposal, approval and holding message are committed, the checkpoint is not
    Crash("in-model:twin.classify", "ask"),
    Crash("in-model:twin.answer", "ask"),
    Crash("in-propose", "ask"),  # the proposal ran and its transaction never committed
    Crash("after-release", "approve"),
    Crash("after-node:finish", "approve"),  # the message is committed, the checkpoint is not
    Crash("after-drive", "approve"),
    Crash("after-node:ask_candidate", "ask", "relay"),
    Crash("after-node:propose", "ask", "relay"),
    Crash("after-drive", "ask", "relay"),
    Crash("after-release", "approve", "relay"),  # the edited replacement was released, the resume was not run
    Crash("after-node:finish", "approve", "relay"),
]


@pytest.fixture(autouse=True)
def graph_world(clean, monkeypatch):  # noqa: F811
    runtime.close()
    yield
    runtime.close()
    fake.replay_from(None)


class Story:
    def __init__(self, engine: Engine, monkeypatch: pytest.MonkeyPatch, kind: str, crash: Crash | None) -> None:
        self.engine, self.crash, self.crashed, self.kind = engine, crash, False, kind
        self.case = "answerable" if kind == "answer" else "unknown"
        w.use_fakes(monkeypatch, self.case)
        self.world = w.build(engine, dial="ask_first", topics={"experience": "answer"})
        self.question = w.ask(self.world)

    def subprocess(self, crash: Crash | None) -> int:
        import os

        args = [sys.executable, str(DRIVER)] + (
            [] if crash is None else ["--crash-at", crash.point, "--count", str(crash.count)]
        )
        env = {**os.environ, "LLM_PROVIDER": "fake", "LLM_FAKE_DIR": str(w.RECORDED / self.case)}
        done = subprocess.run(args, env=env, cwd=BACKEND, capture_output=True, text=True, timeout=300)
        if done.returncode not in (0, 137):
            raise AssertionError(done.stderr[-3000:] + done.stdout[-1500:])
        return done.returncode

    def recover(self) -> None:
        """What production does after a worker dies: the lease runs out, the reaper hands the job back and the lock the
        dead session held is gone."""
        with self.engine.begin() as c:
            c.execute(
                text(
                    """UPDATE queue_jobs
                          SET locked_at = now() - lease_seconds * interval '1 second' - interval '1 second'
                        WHERE status = 'running'"""
                )
            )
        with db.transaction(role="worker") as conn:
            queue.reap(conn)
        deadline = time.monotonic() + 10
        while (
            w.one(self.engine, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)

    def run(self, phase: str) -> None:
        if self.crash is not None and self.crash.phase == phase and not self.crashed:
            assert self.subprocess(self.crash) == 137, f"the driver never reached {self.crash}"
            self.crashed = True
            self.recover()
            assert self.subprocess(None) == 0
        else:
            fake.replay_from(None)  # a new process starts its recordings again
            w.run_jobs()

    def tell(self) -> None:
        self.run("ask")
        waiting = w.pending(self.engine, self.question)
        assert w.finals(self.engine, self.question) == []
        w.approve(self.world, waiting.approval_id, waiting.payload, reply=REPLY if self.kind == "relay" else None)
        self.run("approve")

    def snapshot(self) -> dict[str, Any]:
        e, q = self.engine, self.question
        return {
            "messages": w.twin_messages(e, q),
            "finals": [(r.kind, r.body, r.topic) for r in w.finals(e, q)],
            "actions": sorted(
                (r.kind, r.status, r.replaces is not None)
                for r in w.rows(
                    e,
                    """SELECT inputs ->> 'kind' AS kind, status::text AS status, inputs ->> 'replaces' AS replaces
                         FROM agent_actions WHERE action_type = 'twin_answer'""",
                )
            ),
            "approvals": sorted(r.status for r in w.rows(e, "SELECT status::text AS status FROM approvals")),
            "checks": w.one(e, "SELECT count(*) FROM guardian_checks WHERE NOT passed"),
            "threads": w.checkpoints(e, q),
            "queued": w.one(e, "SELECT count(*) FROM queue_jobs WHERE status NOT IN ('succeeded', 'dead')"),
            "runs": w.rows(e, "SELECT status::text AS status FROM agent_runs WHERE agent_role = 'twin'")[0].status,
        }


_baseline: dict[str, dict[str, Any]] = {}


def baseline(engine: Engine, monkeypatch: pytest.MonkeyPatch, kind: str) -> dict[str, Any]:
    if kind not in _baseline:
        story = Story(engine, monkeypatch, kind, None)
        story.tell()
        _baseline[kind] = story.snapshot()
    return _baseline[kind]


def test_the_uninterrupted_answer_story_ends_with_one_message_and_nothing_left(engine, monkeypatch):
    done = baseline(engine, monkeypatch, "answer")
    assert [f[0] for f in done["finals"]] == ["answered"] and done["messages"][-1][1] == "answered"
    assert done["actions"] == [("answer", "executed", False)] and done["approvals"] == ["approved"]
    assert done["threads"] == 0 and done["queued"] == 0 and done["runs"] == "succeeded"


def test_the_uninterrupted_relay_story_ends_with_the_reply_relayed(engine, monkeypatch):
    done = baseline(engine, monkeypatch, "relay")
    assert done["finals"] == [("relayed", REPLY, "experience")]
    assert done["actions"] == [("relay", "executed", True), ("relay", "rejected", False)]
    assert done["threads"] == 0 and done["queued"] == 0


@pytest.mark.parametrize("crash", CASES, ids=lambda c: f"{c.story}:{c.point}#{c.count}@{c.phase}")
def test_a_worker_killed_there_and_restarted_reaches_the_same_tables(engine, monkeypatch, crash):
    expected = baseline(engine, monkeypatch, crash.story)
    with engine.begin() as c:  # the baseline's rows are gone: start from empty tables again
        c.execute(text("TRUNCATE users, organizations, queue_jobs, llm_calls CASCADE"))
        c.execute(text("TRUNCATE langgraph.checkpoints, langgraph.checkpoint_blobs, langgraph.checkpoint_writes"))
    story = Story(engine, monkeypatch, crash.story, crash)
    story.tell()
    assert story.crashed
    assert story.snapshot() == expected
    assert len(w.finals(engine, story.question)) == 1
