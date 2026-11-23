"""The instance operator's command line (ARCHITECTURE 3.1).

Run:  python -m app.cli <group> <command>

Each command calls a function its owning module provides, imported when the command runs. A command whose function
does not exist yet says so and exits 2. A command may return an exit code; one that returns nothing exits 0.
"""

import argparse
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
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


def _sources_import(args: argparse.Namespace) -> int:
    """Register the live boards named in a file (one name per line). A file that cannot be read exits 2."""
    from app.ingest import discover
    from app.platform import db
    from app.trust.api import audit_log

    try:
        names = Path(args.path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        print(f"cannot read {args.path}: {type(error).__name__}", file=sys.stderr)
        return 2
    with db.transaction(role="worker") as conn:
        result = discover.import_boards(conn, names, dry_run=args.dry_run)
        if not args.dry_run:  # a dry run writes nothing, so it leaves no row either; the row holds counts, not names
            counts = {
                "probed": result.probed,
                "registered": result.registered,
                "already_known": result.already_known,
                "rejected": len(result.rejected),
            }
            audit_log(conn, "ingest.sources_imported", "operator", None, counts)
    prefix = "dry run, nothing written: " if args.dry_run else ""
    print(
        f"{prefix}probed {result.probed}, registered {result.registered}, already known {result.already_known}, "
        f"rejected {len(result.rejected)}"
    )
    for name in result.rejected:
        print(f"  not a board name: {name[:80]}")
    return 0


def _jobs_verify(args: argparse.Namespace) -> int:
    """alive and closed exit 0, a check that could not tell exits 3, a job that does not exist exits 2."""
    from sqlalchemy import text

    from app.ingest import liveness
    from app.platform import db

    with db.transaction(role="worker") as conn:
        if conn.execute(text("SELECT 1 FROM jobs WHERE id = :id"), {"id": args.job_id}).first() is None:
            print(f"job {args.job_id} does not exist", file=sys.stderr)
            return 2
        alive = liveness.verify_live(conn, args.job_id)
    print("unknown" if alive is None else "alive" if alive else "closed")
    return 3 if alive is None else 0


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


def _demo_seed_matches(args: argparse.Namespace) -> int:
    """Invented demo data for the matches and the morning brief, written into a scratch database (app/demo.py). A
    refusal says why and exits 2; nothing has been written then."""
    from app import demo

    try:
        seeded = demo.seed_matches(args.email)
    except demo.SeedRefused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    print(seeded.report())
    return 0


def _demo_seed_tailor(args: argparse.Namespace) -> int:
    """Invented facts for the chat, tailoring and packet demo, on an account `demo seed-matches` already seeded."""
    from app import demo

    try:
        seeded = demo.seed_tailor(args.email)
    except demo.SeedRefused as refusal:
        print(f"refused: {refusal}", file=sys.stderr)
        return 2
    print(seeded.report())
    return 0


COMMANDS: dict[tuple[str, str], Callable[[argparse.Namespace], int | None]] = {
    ("queue", "list"): _queue_list,
    ("queue", "retry"): _queue_retry,
    ("ingest", "run"): _ingest_run,
    ("sources", "discover"): _sources_discover,
    ("sources", "import"): _sources_import,
    ("jobs", "verify"): _jobs_verify,
    ("outbox", "resolve"): _outbox_resolve,
    ("models", "sync"): _models_sync,
    ("demo", "seed-matches"): _demo_seed_matches,
    ("demo", "seed-tailor"): _demo_seed_tailor,
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
    board_import = sources.add_parser(
        "import", help="register the live boards named in a file, one board name per line"
    )
    board_import.add_argument("path")
    board_import.add_argument("--dry-run", action="store_true", help="probe and report, write nothing")

    jobs = groups.add_parser("jobs", help="single jobs").add_subparsers(dest="command", required=True)
    jobs.add_parser("verify", help="check a job's apply link and close the job if it is gone").add_argument(
        "job_id", type=int
    )

    outbox = groups.add_parser("outbox", help="outgoing email").add_subparsers(dest="command", required=True)
    resolve = outbox.add_parser("resolve", help="settle a row left in `unknown`")
    resolve.add_argument("outbox_id", type=UUID)
    outcome = resolve.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--sent", dest="sent", action="store_true", help="the message was delivered")
    outcome.add_argument("--failed", dest="sent", action="store_false", help="the message was not delivered")

    models = groups.add_parser("models", help="the model registry").add_subparsers(dest="command", required=True)
    models.add_parser("sync", help="load config/models.toml into model_registry")

    demo = groups.add_parser("demo", help="invented data for the demo, scratch databases only").add_subparsers(
        dest="command", required=True
    )
    demo.add_parser("seed-matches", help="give an account invented matches and a morning brief").add_argument(
        "--email", required=True, help="the address of an account that has signed in"
    )
    demo.add_parser("seed-tailor", help="give a seeded account invented facts for tailoring and a packet").add_argument(
        "--email", required=True, help="the address of an account `demo seed-matches` was run for"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[(args.group, args.command)](args) or 0
    except NotImplementedError:
        print(f"{args.group} {args.command}: not implemented yet", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
