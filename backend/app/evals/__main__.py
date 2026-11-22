"""python -m app.evals run <suite> [--recorded FILE]    replay a gold set, record the run, print its id
python -m app.evals gate <suite> --run <id>          exit 0 if the run meets its floor, 1 if not

Both use the worker database role (DATABASE_URL_WORKER). The last line `run` prints on standard output is the run id;
the gateway's own log lines come before it.
"""

import argparse
import os
import sys
from collections.abc import Sequence
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
    check = commands.add_parser("gate", help="compare a recorded run with its floor in evals/thresholds.toml")
    check.add_argument("suite")
    check.add_argument("--run", required=True, type=UUID, dest="run_id")
    args = parser.parse_args(argv)
    if args.command == "gate":
        return gate.main(args.suite, args.run_id)
    try:
        done = runner.run_suite(args.suite, args.recorded)
    except runner.UnknownSuite:
        print(f"run: unknown suite {args.suite!r}; the suites are {', '.join(runner.SUITES)}", file=sys.stderr)
        return 2
    except runner.ReplayNeedsFake:
        print("run: a replay needs LLM_PROVIDER=fake; this command never calls a real model", file=sys.stderr)
        return 2
    except AIDisabled:
        print("run: the fake provider is refused when APP_ENV=production", file=sys.stderr)
        return 2
    print(f"{args.suite}: {done.passed} of {done.n} items passed, status {done.status}", file=sys.stderr)
    print(done.run_id)
    return 2 if done.status == "error" else 0


if __name__ == "__main__":
    os.environ["LLM_PROVIDER"] = "fake"  # a replay needs no model: the answers come from a recorded file
    config.get.cache_clear()
    raise SystemExit(main())
