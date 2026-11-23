"""python -m app.evals run <suite> [--recorded FILE]    replay a gold set, record the run, print its id
python -m app.evals run <suite> --live                call the real model on a gold set, record the run, print its id
python -m app.evals gate <suite> --run <id>          exit 0 if the run meets its floor, 1 if not

Both use the worker database role (DATABASE_URL_WORKER). The last line `run` prints on standard output is the run id;
the gateway's own log lines come before it.

--live is the first live run of a suite whose scorer has one (evidence_faithfulness): it needs LLM_PROVIDER and that
provider's key in the environment, it costs the model's price for every pair, and the run it records is frozen for good
in the database DATABASE_URL_WORKER names. It is what opens a route gate (docs/AI_SYSTEM.md 2.2).
"""

import argparse
import os
import sys
from collections.abc import MutableMapping, Sequence
from pathlib import Path
from uuid import UUID

from app.platform import config
from app.platform.llm import AIDisabled

from . import gate, runner


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.evals")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="replay a gold set through the gateway and record the run")
    run.add_argument("suite")
    run.add_argument("--recorded", type=Path, help="replay this file instead of evals/recorded/<suite>.json")
    run.add_argument(
        "--live",
        action="store_true",
        help="call the real model the environment configures instead of replaying, and record a model-method run",
    )
    check = commands.add_parser("gate", help="compare a recorded run with its floor in evals/thresholds.toml")
    check.add_argument("suite")
    check.add_argument("--run", required=True, type=UUID, dest="run_id")
    args = parser.parse_args(argv)
    if args.command == "gate":
        return gate.main(args.suite, args.run_id)
    if args.live and args.recorded is not None:
        parser.error("--live calls a real model, so there is no recording to replay: drop --recorded")
    try:
        done = runner.run_suite(args.suite, args.recorded, live=args.live)
    except runner.UnknownSuite:
        print(f"run: unknown suite {args.suite!r}; the suites are {', '.join(runner.SUITES)}", file=sys.stderr)
        return 2
    except runner.ReplayNeedsFake:
        print("run: a replay needs LLM_PROVIDER=fake; this command never calls a real model", file=sys.stderr)
        return 2
    except runner.NotAScratchDatabase as refusal:
        print(f"run: {refusal}", file=sys.stderr)
        return 2
    except runner.LiveNotSupported:
        print(f"run: {args.suite} has no live mode", file=sys.stderr)
        return 2
    except runner.LiveNeedsRealProvider:
        print(
            "run: --live calls a real model: set LLM_PROVIDER to a provider that is not fake, and its key",
            file=sys.stderr,
        )
        return 2
    except AIDisabled as disabled:
        if args.live:
            print(
                f"run: no usable model route ({disabled.reason}): check LLM_PROVIDER and the provider's key",
                file=sys.stderr,
            )
        else:
            print("run: the fake provider is refused when APP_ENV=production", file=sys.stderr)
        return 2
    print(f"{args.suite}: {done.passed} of {done.n} items passed, status {done.status}", file=sys.stderr)
    print(done.run_id)
    return 2 if done.status == "error" else 0


def choose_provider(argv: Sequence[str], environ: MutableMapping[str, str]) -> None:
    """A replay needs no model, so it runs on the fake provider whatever the environment says. --live is the one run
    that calls a real model: the provider the environment chose stands."""
    if "--live" not in argv:
        environ["LLM_PROVIDER"] = "fake"


if __name__ == "__main__":
    choose_provider(sys.argv[1:], os.environ)
    config.get.cache_clear()
    raise SystemExit(main())
