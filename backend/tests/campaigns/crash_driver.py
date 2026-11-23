"""A test helper, not product code (plan 07-04): runs queue jobs and the relay in a process of its own against the test
database, and kills itself with `os._exit(137)` (what `kill -9` leaves: no `finally`, no commit, no checkpoint) at a
named
point. tests/campaigns/test_kill_resume.py starts it as a subprocess.

    python tests/campaigns/crash_driver.py --crash-at <point> --count N

The environment carries the test database (DATABASE_URL_*), LLM_PROVIDER=fake, LLM_FAKE_DIR, RESEARCH_PAGES_DIR and the
fake mail server's SMTP_URL. Points (N counts occurrences within this process):

  after-node:<node>     after the node returned: its step and artifacts are committed, its checkpoint is not
  before-draft-packet   after the packet_draft ids were committed and before the Writer is called
  in-propose            inside the Guardian proposal, after it ran and before its transaction commits
  in-model:<task>       inside the Nth model call of the task, before an answer comes back
  after-release         after guardian.release committed the schedule and the outbox row
  after-route           after `route` decided, before the checkpoint of its answer is written
With no --crash-at it runs to idle.
"""

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # the backend directory: `app` is importable


def die() -> None:
    os._exit(137)


def counted(count: int) -> Callable[[], bool]:
    seen = {"n": 0}

    def hit() -> bool:
        seen["n"] += 1
        return seen["n"] == count

    return hit


def _get(owner: Any, name: str) -> Any:
    return owner[name] if isinstance(owner, dict) else getattr(owner, name)


def _set(owner: Any, name: str, value: Any) -> None:
    if isinstance(owner, dict):
        owner[name] = value
    else:
        setattr(owner, name, value)


def after(owner: Any, name: str, count: int) -> None:
    original, hit = _get(owner, name), counted(count)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if hit():
            die()
        return result

    _set(owner, name, wrapped)


def before(owner: Any, name: str, count: int) -> None:
    original, hit = _get(owner, name), counted(count)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if hit():
            die()
        return original(*args, **kwargs)

    _set(owner, name, wrapped)


def install(point: str, count: int) -> None:
    from app.apply import api as apply
    from app.campaigns import board, graph, nodes
    from app.guardian import approvals
    from app.platform.llm import fake

    kind, _, argument = point.partition(":")
    if kind == "after-node":
        after(graph.WORK, argument, count)  # graph.build reads the dict when the graph is compiled, after this
    elif kind == "before-draft-packet":
        before(apply, "draft_packet", count)
    elif kind == "in-propose":
        after(board, "propose", count)
    elif kind == "in-model":
        original, hit = fake.call, counted(count)

        def call(task: str) -> Any:
            if task == argument and hit():
                die()
            return original(task)

        fake.call = call
    elif kind == "after-release":
        after(approvals, "release", count)
    elif kind == "after-route":
        after(nodes, "route", count)
    else:
        raise SystemExit(f"unknown crash point {point!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crash-at")
    parser.add_argument("--count", type=int, default=1)
    args = parser.parse_args()

    from app.campaigns import jobs as campaign_jobs  # noqa: F401  (registers the campaign handlers)
    from app.guardian import jobs as guardian_jobs  # noqa: F401  (registers guardian.release and follow_up_v1)
    from app.ingest import api as ingest
    from app.platform import db, queue, relay

    ingest.verify_live = lambda conn, job_id: True  # type: ignore[assignment]  # no network: the apply link is alive
    if args.crash_at:
        install(args.crash_at, args.count)
    while True:
        ran = queue.run_once(lambda: db.transaction(role="worker"), f"crash-driver:{os.getpid()}")
        with db.transaction(role="worker") as conn:
            sent = relay.relay_once(conn)
        if not ran and not sent:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
