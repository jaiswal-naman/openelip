"""The composition roots already import every hook the later plans fill in, and the CLI knows every command."""

import importlib
from uuid import uuid4

import pytest

from app import cli


@pytest.mark.parametrize("module", ["app.main", "app.worker", "app.cli"])
def test_the_roots_import_without_a_database(module):
    importlib.import_module(module)


def test_the_worker_loop_holds_every_hook_it_runs():
    from app import worker

    assert callable(worker.scheduler.tick) and callable(worker.queue.run_once) and callable(worker.relay.relay_once)
    assert worker.jobs.__name__ == "app.ingest.jobs"  # importing it is how ingest registers its handlers


@pytest.mark.parametrize(
    "argv",
    [
        ["queue", "list"],
        ["queue", "list", "--status", "dead"],
        ["queue", "retry", "7"],
        ["ingest", "run"],
        ["sources", "discover"],
        ["models", "sync"],
        ["outbox", "resolve", str(uuid4()), "--sent"],
    ],
)
def test_every_command_parses_and_has_a_handler(argv):
    args = cli.build_parser().parse_args(argv)
    assert (args.group, args.command) in cli.COMMANDS


def test_an_outbox_row_is_resolved_as_sent_or_as_failed_never_both_or_neither():
    parse = cli.build_parser().parse_args
    row = str(uuid4())
    assert parse(["outbox", "resolve", row, "--sent"]).sent is True
    assert parse(["outbox", "resolve", row, "--failed"]).sent is False
    for argv in (["outbox", "resolve", row], ["outbox", "resolve", row, "--sent", "--failed"]):
        with pytest.raises(SystemExit):
            parse(argv)


def test_an_unknown_command_is_refused():
    with pytest.raises(SystemExit):
        cli.main(["nonsense"])
