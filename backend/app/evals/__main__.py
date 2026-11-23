"""python -m app.evals run <suite> [--recorded FILE]    replay a gold set, record the run, print its id
python -m app.evals run <suite> --live                call the real model on a gold set, record the run, print its id
python -m app.evals run wind_tunnel                   replay every wind-tunnel scenario five times, print the run id
python -m app.evals windtunnel <scenario> [--pace S] [--watch] [--live]   one scenario, five attempts, print the run id
python -m app.evals gate <suite> --run <id>          exit 0 if the run meets its floor, 1 if not
python -m app.evals export <suite> --run <id> [--out DIR]   write evals/results/<suite>.json from a finished run

All use the worker database role (DATABASE_URL_WORKER); the wind tunnel also needs the migrate and API logins, all three
on one database whose name ends in _test (or is `evals`, CI's), and refuses any other. The last line `run`, `windtunnel`
and `export` print on standard output is the run id (the file path for `export`); the gateway's own log lines come
before it.

--live is the first live run of a suite whose scorer has one (evidence_faithfulness), or of a wind-tunnel scenario: it
needs LLM_PROVIDER and that provider's key in the environment, it costs the model's price for every call, and the run it
records is frozen for good in the database DATABASE_URL_WORKER names. It is what opens a route gate
(docs/AI_SYSTEM.md 2.2).
Every other command forces the fake provider, whatever the environment says.
"""

import argparse
import os
import sys
from collections.abc import MutableMapping, Sequence
from pathlib import Path
from uuid import UUID

from app.platform import config
from app.platform.llm import AIDisabled

from . import gate, results, runner

WIND_TUNNEL = "wind_tunnel"


def main(argv: Sequence[str] | None = None) -> int:
    given = list(sys.argv[1:] if argv is None else argv)
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
    tunnel = commands.add_parser("windtunnel", help="run one wind-tunnel scenario five times and record the run")
    tunnel.add_argument("scenario")
    tunnel.add_argument("--pace", type=float, default=0.0, help="seconds to pause after each pass of the loop")
    tunnel.add_argument("--watch", action="store_true", help="print attempt 1's session cookie to standard error")
    tunnel.add_argument("--live", action="store_true", help="call the real model the environment configures")
    tunnel.add_argument(
        "--scenarios-dir", type=Path, help="read the scenarios from this folder (default: the repository's)"
    )
    export = commands.add_parser("export", help="write evals/results/<suite>.json from a finished recorded run")
    export.add_argument("suite")
    export.add_argument("--run", required=True, type=UUID, dest="run_id")
    export.add_argument("--out", type=Path, help="write the file into this folder (default: evals/results)")
    args = parser.parse_args(given)
    if args.command == "run" and args.suite == WIND_TUNNEL and (args.live or args.recorded is not None):
        parser.error("the wind tunnel replays its own recordings: use `windtunnel <scenario> --live` for a live run")
    if args.command == "run" and args.live and args.recorded is not None:
        parser.error("--live calls a real model, so there is no recording to replay: drop --recorded")
    previous = os.environ.get("LLM_PROVIDER")
    choose_provider(given, os.environ)  # every command but a live one runs on the fake provider
    _reset_settings()
    try:
        return _dispatch(args)
    finally:  # a process ends here; an in-process caller (a test) gets its environment back
        if previous is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = previous
        _reset_settings()


def _reset_settings() -> None:
    clear = getattr(config.get, "cache_clear", None)
    if clear is not None:
        clear()


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "gate":
        return gate.main(args.suite, args.run_id)
    if args.command == "export":
        return _export(args)
    if args.command == "windtunnel":
        return _wind_tunnel(None if args.scenario is None else [args.scenario], args)
    if args.suite == WIND_TUNNEL:
        return _wind_tunnel(None, args)
    try:
        done = runner.run_suite(args.suite, args.recorded, live=args.live)
    except runner.UnknownSuite:
        print(
            f"run: unknown suite {args.suite!r}; the suites are {', '.join([*runner.SUITES, WIND_TUNNEL])}",
            file=sys.stderr,
        )
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


def _wind_tunnel(keys: list[str] | None, args: argparse.Namespace) -> int:
    """`run wind_tunnel` (all scenarios) and `windtunnel <key>`: five attempts each, the run recorded, id printed."""
    # the application and the worker's registrations are imported here only: `run harness_smoke` and `gate` stay light
    from app.main import app

    from .windtunnel import guard, scenario
    from .windtunnel import run as tunnel

    folder = getattr(args, "scenarios_dir", None) or tunnel.SCENARIOS
    try:
        done = tunnel.run(
            keys,
            asgi_app=app,
            attempts=tunnel.ATTEMPTS,
            scenarios_dir=folder,
            pace=getattr(args, "pace", 0.0),
            watch=getattr(args, "watch", False),
            live=getattr(args, "live", False),
        )
    except scenario.ScenarioInvalid as refusal:
        known = ", ".join(path.stem for path in sorted(folder.glob("*.toml")))
        print(f"windtunnel: {refusal}; the scenarios are {known}", file=sys.stderr)
        return 2
    except runner.LiveNeedsRealProvider:
        print(
            "windtunnel: --live calls a real model: set LLM_PROVIDER to a provider that is not fake, and its key",
            file=sys.stderr,
        )
        return 2
    except runner.ReplayNeedsFake:
        print("windtunnel: a replay needs LLM_PROVIDER=fake; this command never calls a real model", file=sys.stderr)
        return 2
    except (guard.NotATestDatabase, guard.WorkerAttached, guard.DatabaseBusy, tunnel.WatchRefused) as refusal:
        print(f"windtunnel: {refusal}", file=sys.stderr)
        return 2
    except AIDisabled as disabled:
        print(f"windtunnel: no usable model route ({disabled.reason})", file=sys.stderr)
        return 2
    print(
        f"{WIND_TUNNEL}: {done.passed} of {done.n} scenarios passed all attempts, status {done.status}", file=sys.stderr
    )
    print(done.run_id)
    return 2 if done.status == "error" else 0


def _export(args: argparse.Namespace) -> int:
    try:
        path = results.export(args.run_id, args.suite, args.out or results.RESULTS)
    except results.ExportRefused as refusal:
        print(f"export: {refusal}", file=sys.stderr)
        return 2
    print(path)
    return 0


def choose_provider(argv: Sequence[str], environ: MutableMapping[str, str]) -> None:
    """A replay needs no model, so it runs on the fake provider whatever the environment says. --live is the one run
    that calls a real model: the provider the environment chose stands."""
    if "--live" not in argv:
        environ["LLM_PROVIDER"] = "fake"


if __name__ == "__main__":
    raise SystemExit(main())
