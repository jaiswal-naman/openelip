"""CMP-06 (plan 07-04): a worker killed in any node, mid model call, mid proposal, after a release or before a
checkpoint,
and restarted days later, ends in the same tables as an uninterrupted run, with no effect repeated.

The story is one campaign from start to closed: research, fit, packet, the candidate's approval, applied, eight quiet
days,
the follow-up, its approval and send, interview, offer. Its phases run in this process (the uninterrupted baseline runs
them all so). For a crash case, the phase that holds the crash point runs in tests/campaigns/crash_driver.py, a
subprocess that kills itself with `os._exit(137)` at the named point; the killed job's lease is expired and reaped the
way
production recovers it, the clock moves eight days on, and the driver runs again with no crash. The queue's unique keys,
`campaign_steps_once`, `approvals_one_per_action`, `outbox_one_per_action` and the proposal step ids make each re-run
harmless; the final tables are compared to the baseline's."""

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, NamedTuple

import pytest
from sqlalchemy import Engine, text

from app.campaigns import stages
from app.guardian import api as guardian
from app.guardian import approvals, runtime
from app.platform import db, queue
from app.platform.llm import fake
from tests.campaigns.fixtures.graph import world as w
from tests.guardian.conftest import clean, smtp  # noqa: F401
from tests.guardian.support import make_due

DRIVER = Path(__file__).with_name("crash_driver.py")
BACKEND = Path(__file__).resolve().parents[2]


class Crash(NamedTuple):
    point: str
    phase: str
    count: int = 1


CASES = [
    # the work nodes, each killed after its step committed (the checkpoint of the node is lost)
    Crash("after-node:research", "start"),
    Crash("after-node:fit", "start"),
    Crash("after-node:packet_draft", "start"),
    Crash("after-node:packet", "start"),  # after the proposal and the step were committed
    Crash("after-node:follow_up", "aged"),
    Crash("after-node:interview_prep", "interview"),
    # inside a node
    Crash("before-draft-packet", "start"),  # the packet's id is stored, the Writer has not been called
    Crash("in-propose", "start"),  # the packet proposal ran and its transaction never committed
    Crash("in-propose", "aged"),
    # inside a model call, every task of the graph
    Crash("in-model:research.plan", "start", 3),
    Crash("in-model:research.read_page", "start", 2),
    Crash("in-model:writer.packet", "start"),
    Crash("in-model:verify.entail", "start", 1),  # the Writer's own sentence check
    Crash("in-model:verify.entail", "start", 2),  # the Guardian's citation check on the packet
    Crash("in-model:chaser.draft", "aged"),
    Crash("in-model:verify.entail", "aged", 1),  # the Chaser's sentence check
    Crash("in-model:verify.entail", "aged", 2),  # the Guardian's citation check on the follow-up
    Crash("in-model:coach.prep", "interview"),
    Crash("in-model:verify.entail", "interview"),
    # the Guardian's release and the route's own checkpoint
    Crash("after-release", "approve_packet"),
    Crash("after-release", "approve_follow_up"),
    Crash("after-route", "start", 3),
    Crash("after-route", "aged"),
]


@pytest.fixture(autouse=True)
def graph_world(smtp, clean, monkeypatch):  # noqa: F811
    runtime.close()
    w.use_fakes(monkeypatch)
    yield
    runtime.close()
    fake.replay_from(None)


class Story:
    def __init__(self, engine: Engine, mail: Any, crash: Crash | None) -> None:
        self.engine, self.mail, self.crash, self.crashed = engine, mail, crash, False
        self.world = w.build_world(engine, follow_up_email=w.TYPED_ADDRESS)

    # ---- running the queue, in this process or in the crash driver ----

    def subprocess(self, crash: Crash | None) -> int:
        args = [sys.executable, str(DRIVER)] + (
            [] if crash is None else ["--crash-at", crash.point, "--count", str(crash.count)]
        )
        env = {**w.env_for_subprocess(), "SMTP_URL": self.mail.url(), "MAIL_FROM": "no-reply@careers.test"}
        done = subprocess.run(args, env=env, cwd=BACKEND, capture_output=True, text=True, timeout=300)
        if done.returncode not in (0, 137):
            raise AssertionError(done.stderr[-3000:] + done.stdout[-1500:])
        return done.returncode

    def recover(self) -> None:
        """What production does after a worker dies: the lease runs out, the reaper hands the job back, the lock the
        dead
        session held is gone, and days pass."""
        with self.engine.begin() as c:
            c.execute(
                text(
                    """UPDATE queue_jobs SET locked_at = now() - lease_seconds * interval '1 second' - interval '1
                    second'
                       WHERE status = 'running'"""
                )
            )
        with db.transaction(role="worker") as conn:
            assert queue.reap(conn) >= 1  # the killed job came back
        deadline = time.monotonic() + 10
        while (
            w.one(self.engine, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        w.age(self.engine, self.world, 8)

    def run(self, phase: str) -> None:
        if self.crash is not None and self.crash.phase == phase and not self.crashed:
            assert self.subprocess(self.crash) == 137, f"the driver never reached {self.crash}"
            self.crashed = True
            self.recover()
            assert self.subprocess(None) == 0
        else:
            fake.replay_from(None)  # a new process starts its recordings again
            w.run_jobs()

    # ---- the story ----

    def action(self, action_type: str) -> Any:
        (row,) = w.rows(
            self.engine,
            """SELECT a.id, a.payload, p.id AS approval FROM agent_actions a JOIN approvals p ON p.agent_action_id =
            a.id
               WHERE a.campaign_id = :c AND a.action_type = CAST(:t AS action_type) AND a.inputs ->> 'replaces' IS
               NULL""",
            c=self.world.campaign,
            t=action_type,
        )
        return row

    def approve(self, action_type: str) -> int:
        row = self.action(action_type)
        with db.transaction(self.world.user) as conn:
            guardian.decide(
                conn, self.world.user, row.approval, approve=True, payload_hash=guardian.payload_hash(row.payload)
            )
        w.resume(self.world, f"a{row.id}.scheduled")  # the event subscriber's job (plan 07-05) is done by hand
        return int(row.id)

    def tell(self) -> None:
        world, engine = self.world, self.engine
        w.resume(world)
        self.run("start")
        self.approve("submit_application")
        self.run("approve_packet")
        w.record_application(engine, world, "applied")
        w.resume(world, "app1")
        self.run("applied")
        w.age(engine, world, 8)
        self.run("aged")
        action = self.approve("send_follow_up")
        self.run("approve_follow_up")
        make_due(engine, action)  # the undo window is over: the relay may send
        self.run("sent")
        with db.transaction(role="worker") as conn:  # the sweep the worker's scheduler runs every minute
            approvals.settle(conn)
        w.record_application(engine, world, "interview")
        w.resume(world, "app2")
        self.run("interview")
        w.record_application(engine, world, "offer")
        w.resume(world, "app3")
        self.run("offer")
        with db.transaction(world.user, role="worker") as conn:
            stages.move(conn, world.campaign, "closed", cause="cancel", closed_reason="offer_accepted")
        self.run("closed")

    def snapshot(self) -> dict[str, Any]:
        e, c = self.engine, {"c": self.world.campaign}
        return {
            "stage": tuple(w.rows(e, "SELECT stage::text, closed_reason::text FROM campaigns WHERE id = :c", **c)[0]),
            "steps": sorted(w.steps(e, self.world).items()),
            "dossiers": [
                r.version for r in w.rows(e, "SELECT version FROM company_dossiers WHERE campaign_id = :c", **c)
            ],
            "tailored": w.one(e, "SELECT count(*) FROM tailored_resumes"),
            "packets": sorted(
                r.status
                for r in w.rows(e, "SELECT status::text AS status FROM application_packets WHERE campaign_id = :c", **c)
            ),
            "outside_actions": sorted(
                (r.action_type, r.status, r.step)
                for r in w.rows(
                    e,
                    """SELECT action_type::text AS action_type, status::text AS status, inputs ->> 'step_id' AS step
                       FROM agent_actions WHERE campaign_id = :c AND kind = 'outside_action'""",
                    **c,
                )
            ),
            "approvals": sorted(r.status for r in w.rows(e, "SELECT status::text AS status FROM approvals")),
            "outbox": sorted(
                (r.template, r.status) for r in w.rows(e, "SELECT template, status::text AS status FROM outbox")
            ),
            "sent": len(self.mail.messages),
            "threads": w.one(
                e,
                "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = :t",
                t=f"campaign:{self.world.campaign}",
            ),
            "prep": sorted(
                w.one(e, "SELECT output -> 'ask_them' FROM campaign_steps WHERE step_key = 'interview_prep'")[0]["text"]
                for _ in (0,)
            ),
            "state_changes": [
                r.outcome_code
                for r in w.rows(e, "SELECT outcome_code FROM agent_actions WHERE kind = 'state_change' ORDER BY id")
            ],
        }


_baseline: dict[str, Any] = {}


def baseline(engine: Engine, mail: Any) -> dict[str, Any]:
    if not _baseline:
        story = Story(engine, mail, None)
        story.tell()
        _baseline.update(story.snapshot())
    return _baseline


def test_the_uninterrupted_story_ends_closed_with_one_of_everything(engine, smtp) -> None:  # noqa: F811
    done = baseline(engine, smtp)
    assert done["stage"] == ("closed", "offer_accepted") and done["dossiers"] == [1] and done["threads"] == 0
    assert [a[:2] for a in done["outside_actions"]] == [
        ("send_follow_up", "executed"),
        ("submit_application", "executed"),
    ]
    assert done["outbox"] == [("follow_up_v1", "sent")] and done["sent"] == 1 and done["packets"] == ["approved"]
    assert dict(done["steps"])["follow_up:2"] == "waiting" and dict(done["steps"])["interview_prep"] == "done"


@pytest.mark.parametrize("crash", CASES, ids=lambda c: f"{c.point}#{c.count}@{c.phase}")
def test_a_worker_killed_there_and_restarted_days_later_reaches_the_same_tables(engine, smtp, crash) -> None:  # noqa: F811
    expected = baseline(engine, smtp)
    with engine.begin() as c:  # the baseline's rows are gone: start from empty tables again
        c.execute(text("TRUNCATE campaigns, users, jobs, job_sources, queue_jobs, outbox, llm_calls CASCADE"))
        c.execute(text("TRUNCATE langgraph.checkpoints, langgraph.checkpoint_blobs, langgraph.checkpoint_writes"))
    smtp.messages.clear()
    story = Story(engine, smtp, crash)
    story.tell()
    assert story.crashed
    assert story.snapshot() == expected
    steps = story.snapshot()["outside_actions"]
    assert len({(t, s) for t, _, s in steps}) == len(steps)  # no outside action twice for one step
