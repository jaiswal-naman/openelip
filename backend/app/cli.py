"""The instance operator's command line (ARCHITECTURE 3.1).

Run:  python -m app.cli <group> <command>

Each command calls a function its owning module provides, imported when the command runs. A command whose function
does not exist yet says so and exits 2.
"""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from uuid import UUID

QUEUE_STATUSES = ["queued", "running", "succeeded", "dead"]
LIST_LIMIT = 100  # `queue list` shows the newest jobs; the scheduler alone adds a job a minute


def _queue_list(args: argparse.Namespace) -> None:
    from sqlalchemy import text

    from app.platform import db

    with db.transaction(role="worker") as conn:
        rows = conn.execute(
            text(
                """SELECT id, kind, status, attempts, last_error_code FROM queue_jobs
                    WHERE CAST(:status AS queue_status) IS NULL OR status = CAST(:status AS queue_status)
                    ORDER BY id DESC LIMIT :limit"""
            ),
            {"status": args.status, "limit": LIST_LIMIT},
        ).all()
    print("id      kind                    status     attempts  error_code")
    for job in rows:
        print(f"{job.id:<7} {job.kind:<23} {job.status:<10} {job.attempts:<9} {job.last_error_code or ''}")


def _queue_retry(args: argparse.Namespace) -> None:
    from sqlalchemy import text

    from app.platform import db

    with db.transaction(role="worker") as conn:
        found = conn.execute(
            text(
                """UPDATE queue_jobs SET status = 'queued', attempts = 0, run_at = now(), last_error_code = NULL,
                          finished_at = NULL, locked_by = NULL, locked_at = NULL
                    WHERE id = :id AND status = 'dead' RETURNING id"""
            ),
            {"id": args.job_id},
        ).first()
    if found is None:
        raise SystemExit(f"job {args.job_id} is not a dead job")
    print(f"job {args.job_id} queued again")


def _ingest_run(args: argparse.Namespace) -> None:
    from sqlalchemy import text

    from app.platform import db, queue

    slot = f"manual-{int(time.time())}"  # never equal to a scheduled slot, so a manual run is not swallowed by one
    with db.transaction(role="worker") as conn:
        source_ids = conn.execute(text("SELECT id FROM job_sources WHERE is_active ORDER BY id")).scalars().all()
        queued = [
            queue.enqueue(
                conn,
                "ingest.fetch",
                {"source_id": source_id, "slot": slot},
                unique_key=f"ingest.fetch:{source_id}:{slot}",
                queue="ingest",
            )
            for source_id in source_ids
        ]
    if not source_ids:
        print("No active sources. Run: python -m app.cli sources discover")
        return
    print(f"{sum(job is not None for job in queued)} fetch jobs queued; the worker runs them")


def _sources_discover(args: argparse.Namespace) -> None:
    from app.platform import db, queue

    slot = f"manual-{int(time.time())}"
    with db.transaction(role="worker") as conn:
        queue.enqueue(conn, "ingest.discover", {}, unique_key=f"ingest.discover:{slot}", queue="ingest")
    print("discovery queued; the worker runs it")


def _outbox_resolve(args: argparse.Namespace) -> None:
    from app.platform import relay

    relay.resolve(args.outbox_id, sent=args.sent)
    print("resolved")


def _models_sync(args: argparse.Namespace) -> None:
    from app.platform import db
    from app.platform.llm import registry

    with db.transaction(role="migrate") as conn:
        count = registry.sync(conn)
    print(f"{count} model routes synced")


COMMANDS: dict[tuple[str, str], Callable[[argparse.Namespace], None]] = {
    ("queue", "list"): _queue_list,
    ("queue", "retry"): _queue_retry,
    ("ingest", "run"): _ingest_run,
    ("sources", "discover"): _sources_discover,
    ("outbox", "resolve"): _outbox_resolve,
    ("models", "sync"): _models_sync,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    groups = parser.add_subparsers(dest="group", required=True)

    queue = groups.add_parser("queue", help="inspect and retry background jobs").add_subparsers(
        dest="command", required=True
    )
    queue.add_parser("list", help="list jobs").add_argument("--status", choices=QUEUE_STATUSES)
    queue.add_parser("retry", help="put a dead job back in the queue").add_argument("job_id", type=int)

    ingest = groups.add_parser("ingest", help="the job index").add_subparsers(dest="command", required=True)
    ingest.add_parser("run", help="fetch every active source")

    sources = groups.add_parser("sources", help="job-board sources").add_subparsers(dest="command", required=True)
    sources.add_parser("discover", help="find which candidate boards exist")

    outbox = groups.add_parser("outbox", help="outgoing email").add_subparsers(dest="command", required=True)
    resolve = outbox.add_parser("resolve", help="settle a row left in `unknown`")
    resolve.add_argument("outbox_id", type=UUID)
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--sent", dest="sent", action="store_true", help="the message was delivered")
    outcome.add_argument("--failed", dest="sent", action="store_false", help="the message was not delivered")

    models = groups.add_parser("models", help="the model registry").add_subparsers(dest="command", required=True)
    models.add_parser("sync", help="load config/models.toml into model_registry")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        COMMANDS[(args.group, args.command)](args)
    except NotImplementedError:
        print(f"{args.group} {args.command}: not implemented yet", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
