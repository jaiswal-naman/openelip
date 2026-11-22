"""The instance operator's command line (ARCHITECTURE 3.1).

Run:  python -m app.cli <group> <command>

Each command calls a function its owning module provides, imported when the command runs. A command whose function
does not exist yet says so and exits 2.
"""

import argparse
import sys
from collections.abc import Callable, Sequence
from uuid import UUID

QUEUE_STATUSES = ["queued", "running", "succeeded", "dead"]


def _queue_list(args: argparse.Namespace) -> None:
    raise NotImplementedError("plan 01-02")


def _queue_retry(args: argparse.Namespace) -> None:
    raise NotImplementedError("plan 01-02")


def _ingest_run(args: argparse.Namespace) -> None:
    raise NotImplementedError("plan 01-02")


def _sources_discover(args: argparse.Namespace) -> None:
    raise NotImplementedError("plan 01-02")


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
