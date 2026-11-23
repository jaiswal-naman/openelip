"""IDX-09, the operator's bulk board import: only boards that answer with live postings are registered.

The three board APIs are replaced by a table of invented answers, the way test_store.py does for discovery, so nothing
touches the network. The import_boards tests run in the `conn` fixture's transaction, which is rolled back. The
command-line tests commit through the worker's own login, so they empty the source tables before and after.
"""

import pytest
from sqlalchemy import text

from app import cli
from app.ingest import discover, jobs
from app.ingest.discover import ImportResult

# (ats, token) -> how many postings the board answers with. Every other pair answers 404.
ANSWERS = {
    ("greenhouse", "livegh"): 2,
    ("lever", "twin"): 1,
    ("ashby", "twin"): 3,
    ("greenhouse", "empty"): 0,
    ("lever", "empty"): 0,
    ("ashby", "empty"): 0,
}
FILE = ["livegh", "empty", "gone", "bad name!"]  # live, answers with nothing, answers 404, not a name


@pytest.fixture
def probes(monkeypatch):
    """Replace the board APIs. Returns the list of (ats, token) pairs the import asked about."""
    asked = []

    async def answer(client, ats, token):
        asked.append((ats, token))
        count = ANSWERS.get((ats, token))
        if count is None:
            return "http_error", 404, None, None
        return "ok", 200, [{"external_id": str(n)} for n in range(count)], None

    async def company(client, token):
        return "Live Greenhouse Inc"

    monkeypatch.setattr(discover, "fetch_postings", answer)
    monkeypatch.setattr(discover, "greenhouse_name", company)
    return asked


def sources(conn):
    rows = conn.execute(text("SELECT ats::text, token, company_name, is_active FROM job_sources ORDER BY ats, token"))
    return [tuple(row) for row in rows]


# --- what is registered -------------------------------------------------------------------------------------------


def test_only_the_board_with_postings_is_registered_and_the_counts_say_so(conn, probes):
    result = discover.import_boards(conn, FILE)

    assert result == ImportResult(probed=3, registered=1, already_known=0, rejected=["bad name!"])
    assert sources(conn) == [("greenhouse", "livegh", "Live Greenhouse Inc", True)]


def test_a_name_that_fails_the_pattern_is_never_sent_anywhere(conn, probes):
    discover.import_boards(conn, ["../../internal", "evil.test/path", "a b", "livegh"])

    assert sorted(set(token for _, token in probes)) == ["livegh"]  # and only to the three fixed APIs
    assert sorted(set(ats for ats, _ in probes)) == ["ashby", "greenhouse", "lever"]
    assert len(probes) == 3


def test_importing_the_same_file_again_registers_nothing_and_counts_the_board_as_known(conn, probes):
    discover.import_boards(conn, FILE)

    again = discover.import_boards(conn, FILE)

    assert again == ImportResult(probed=3, registered=0, already_known=1, rejected=["bad name!"])
    assert len(sources(conn)) == 1


def test_an_inactive_known_board_that_is_live_again_becomes_active(conn, probes):
    discover.import_boards(conn, FILE)
    conn.execute(text("UPDATE job_sources SET is_active = false"))

    result = discover.import_boards(conn, FILE)

    assert (result.registered, result.already_known) == (0, 1)
    assert sources(conn) == [("greenhouse", "livegh", "Live Greenhouse Inc", True)]


def test_a_name_that_is_live_on_two_boards_registers_both(conn, probes):
    result = discover.import_boards(conn, ["twin"])

    assert (result.probed, result.registered) == (1, 2)
    assert sources(conn) == [("ashby", "twin", "twin", True), ("lever", "twin", "twin", True)]  # no name is invented


def test_a_dead_board_that_was_registered_earlier_is_left_alone(conn, probes):
    conn.execute(text("INSERT INTO job_sources (ats, token, company_name) VALUES ('lever', 'gone', 'Gone Co')"))

    result = discover.import_boards(conn, ["gone"])

    assert (result.probed, result.registered, result.already_known) == (1, 0, 0)  # it answers 404, so it is not live
    assert sources(conn) == [("lever", "gone", "Gone Co", True)]


# --- dry run ------------------------------------------------------------------------------------------------------


def test_a_dry_run_reports_the_same_counts_and_writes_nothing(conn, probes):
    dry = discover.import_boards(conn, FILE, dry_run=True)
    assert sources(conn) == []

    real = discover.import_boards(conn, FILE)
    assert dry == real == ImportResult(3, 1, 0, ["bad name!"])


def test_a_dry_run_does_not_reactivate_a_known_board(conn, probes):
    discover.import_boards(conn, FILE)
    conn.execute(text("UPDATE job_sources SET is_active = false"))

    result = discover.import_boards(conn, FILE, dry_run=True)

    assert (result.registered, result.already_known) == (0, 1)
    assert sources(conn) == [("greenhouse", "livegh", "Live Greenhouse Inc", False)]


# --- reading the names --------------------------------------------------------------------------------------------


def test_names_are_read_the_way_the_seed_file_is_read():
    lines = ["# candidate boards", "", "  LiveGH  ", "livegh", "   # indented note", "empty", "no good", "x" * 81]

    tokens, rejected = discover.parse_names(lines)

    assert tokens == ["empty", "livegh"]  # stripped, lower case, one each, sorted
    assert rejected == ["no good", "x" * 81]  # a blank line or a comment is not a rejected name


def test_the_seed_job_and_the_import_register_the_same_boards(conn, probes, monkeypatch, tmp_path):
    seed = tmp_path / "seed_candidates.txt"
    seed.write_text("# invented candidates\n" + "\n".join(FILE) + "\n", encoding="utf-8")
    monkeypatch.setattr(discover, "CANDIDATES", seed)

    jobs.discover_sources(conn, {})  # what the worker runs when the index has no source

    seeded = sources(conn)
    assert seeded == [("greenhouse", "livegh", "Live Greenhouse Inc", True)]
    again = discover.import_boards(conn, seed.read_text(encoding="utf-8").splitlines())
    assert (again.registered, again.already_known) == (0, 1)  # the very board the seed job registered
    assert sources(conn) == seeded


# --- the command line ---------------------------------------------------------------------------------------------


@pytest.fixture
def committed_index(engine):
    """The command opens its own worker connection and commits, so this empties the tables before and after."""
    empty = text("TRUNCATE fetch_runs, jobs, job_sources RESTART IDENTITY CASCADE")
    with engine.begin() as c:
        c.execute(empty)
    yield
    with engine.begin() as c:
        c.execute(empty)


def run_cli(capsys, *argv):
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out.strip().splitlines(), captured.err.strip()


def stored_sources(engine):
    with engine.connect() as c:
        return sources(c)


def test_the_command_is_parsed_and_has_a_handler():
    parse = cli.build_parser().parse_args
    args = parse(["sources", "import", "boards.txt"])
    assert (args.group, args.command, args.path, args.dry_run) == ("sources", "import", "boards.txt", False)
    assert parse(["sources", "import", "boards.txt", "--dry-run"]).dry_run is True
    assert ("sources", "import") in cli.COMMANDS
    with pytest.raises(SystemExit):
        parse(["sources", "import"])


def test_the_command_registers_the_live_boards_prints_the_counts_and_exits_0(
    engine, committed_index, probes, capsys, tmp_path
):
    boards = tmp_path / "boards.txt"
    boards.write_text("\n".join(FILE), encoding="utf-8")

    code, out, err = run_cli(capsys, "sources", "import", str(boards))

    assert (code, err) == (0, "")
    assert out == ["probed 3, registered 1, already known 0, rejected 1", "  not a board name: bad name!"]
    assert stored_sources(engine) == [("greenhouse", "livegh", "Live Greenhouse Inc", True)]


def test_the_command_run_again_reports_the_board_as_known(engine, committed_index, probes, capsys, tmp_path):
    boards = tmp_path / "boards.txt"
    boards.write_text("\n".join(FILE), encoding="utf-8")
    run_cli(capsys, "sources", "import", str(boards))

    code, out, _ = run_cli(capsys, "sources", "import", str(boards))

    assert code == 0 and out[0] == "probed 3, registered 0, already known 1, rejected 1"
    assert len(stored_sources(engine)) == 1


def test_a_dry_run_through_the_command_writes_nothing_and_says_so(engine, committed_index, probes, capsys, tmp_path):
    boards = tmp_path / "boards.txt"
    boards.write_text("\n".join(FILE), encoding="utf-8")

    code, out, _ = run_cli(capsys, "sources", "import", str(boards), "--dry-run")

    assert code == 0 and out[0] == "dry run, nothing written: probed 3, registered 1, already known 0, rejected 1"
    assert stored_sources(engine) == []


def test_a_rejected_line_is_shown_cut_to_80_characters(committed_index, probes, capsys, tmp_path):
    boards = tmp_path / "boards.txt"
    boards.write_text("y" * 300 + " z\n", encoding="utf-8")

    _, out, _ = run_cli(capsys, "sources", "import", str(boards))

    assert out == ["probed 0, registered 0, already known 0, rejected 1", "  not a board name: " + "y" * 80]
    assert probes == []  # nothing valid, so nothing is asked of any board API


def test_a_file_that_is_not_there_exits_2_and_probes_nothing(committed_index, probes, capsys, tmp_path):
    missing = tmp_path / "no-such-file.txt"

    code, out, err = run_cli(capsys, "sources", "import", str(missing))

    assert (code, out, probes) == (2, [], [])
    assert "no-such-file.txt" in err


def test_a_file_that_is_not_text_exits_2_too(committed_index, probes, capsys, tmp_path):
    binary = tmp_path / "boards.bin"
    binary.write_bytes(b"\xff\xfe\x00\x80 not utf-8")

    code, _, err = run_cli(capsys, "sources", "import", str(binary))

    assert (code, probes) == (2, [])
    assert err.startswith("cannot read")
