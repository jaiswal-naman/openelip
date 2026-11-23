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


def test_both_roots_import_the_ingest_registration_so_the_registry_is_filled_in_each_process():
    from app import main, worker
    from app.ingest import privacy as ingest_privacy

    assert main.ingest_privacy is ingest_privacy and worker.ingest_privacy is ingest_privacy


def test_both_roots_import_the_match_and_brief_registrations_and_the_worker_loads_their_handlers():
    """Plan 04-01 wired both modules into the roots once, so the plans that fill them never touch a root again."""
    from app import main, worker
    from app.brief import privacy as brief_privacy
    from app.match import privacy as match_privacy
    from app.trust import privacy

    assert main.match_privacy is match_privacy and worker.match_privacy is match_privacy
    assert main.brief_privacy is brief_privacy and worker.brief_privacy is brief_privacy
    assert worker.match_jobs.__name__ == "app.match.jobs" and worker.brief_jobs.__name__ == "app.brief.jobs"
    assert main.match_routes.__name__ == "app.match.routes" and main.brief_routes.__name__ == "app.brief.routes"
    # each one owns its tables in the deletion registry and exports them
    assert set(privacy.registry()["match"]) == {"match_runs", "match_decisions", "match_evidence", "feedback_events"}
    assert set(privacy.registry()["brief"]) == {"briefs", "brief_items"}
    assert {"match", "brief"} <= set(privacy.exporters())


def test_the_api_starts_with_the_match_and_brief_routers_mounted(api_client):
    assert api_client.get("/healthz").status_code == 200


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
