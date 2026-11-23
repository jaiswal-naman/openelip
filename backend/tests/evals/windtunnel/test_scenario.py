"""The scenario loader and the stored rows (INDEX "Scenario file"; T-08-25)."""

import pytest
from sqlalchemy import text

from app.evals.windtunnel import scenario
from tests.evals.windtunnel import scenario_text


def edited(tmp_path, old, new, **kwargs):
    path = scenario_text.write(tmp_path, **kwargs)
    path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")
    return path


def test_it_loads_a_valid_file(tmp_path):
    loaded = scenario.load(scenario_text.write(tmp_path))
    assert (loaded.key, loaded.version, loaded.persona, loaded.start) == ("unit-a", 1, "cooperative", "match")
    assert loaded.max_virtual_days == 20 and loaded.company_host == "careers.lumen.example"
    assert loaded.recorded == tmp_path / "unit-a" / "recorded"
    assert loaded.candidate_rules[0]["do"] == "start_campaign" and loaded.expect["goal"] == "stage:interview"


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('persona = "cooperative"', 'persona = "friendly"', "unknown persona"),
        ('start = "match"', 'start = "inbox"', "unknown start"),
        ('labels = "agent-drafted, maintainer review pending"', 'labels = "reviewed"', "labels must be"),
        ("invented = true", "invented = false", "invented must be true"),
        ("invented = true", "", "invented must be true"),
        ('on = "start"', 'on = "tuesday"', "unknown candidate trigger"),
        ('do = "start_campaign"', 'do = "send_money"', "unknown candidate action"),
        ('on = "mail:follow_up_v1:1"', 'on = "mail:follow_up_v1"', "unknown employer trigger"),
        ('do = "reply interview"', 'do = "reply maybe"', "unknown employer action"),
        ('goal = "stage:interview"', 'goal = "stage:interview"\nmood = "happy"', "unknown expectation"),
        ("[expect]\ngoal", "[expect]\nstage_in = []\nx", "unknown expectation"),
        (
            'apply_url = "https://careers.lumen.example/apply/1"',
            'apply_url = "https://careers.lumen.com/apply/1"',
            "company host",
        ),
        ('contact = "talent@lumen.example"', 'contact = "talent@lumen.com"', "contact host"),
        ("max_virtual_days = 20", "max_virtual_days = 0", "max_virtual_days"),
        ("version = 1", "", "missing or empty 'version'"),
    ],
)
def test_it_refuses_what_is_outside_the_format(tmp_path, old, new, message):
    with pytest.raises(scenario.ScenarioInvalid, match=message):
        scenario.load(edited(tmp_path, old, new))


def test_it_refuses_a_missing_recorded_folder(tmp_path):
    path = scenario_text.write(tmp_path)
    (tmp_path / "unit-a" / "recorded").rmdir()
    with pytest.raises(scenario.ScenarioInvalid, match="recorded/"):
        scenario.load(path)


def test_it_refuses_an_attacker_address_on_a_real_looking_host(tmp_path):
    path = scenario_text.write(tmp_path, extra="")
    path.write_text(
        path.read_text(encoding="utf-8").replace("[inject]", '[inject]\nattacker_address = "x@evil.com"'),
        encoding="utf-8",
    )
    with pytest.raises(scenario.ScenarioInvalid, match="attacker"):
        scenario.load(path)


def test_an_introduction_start_needs_its_organization(tmp_path):
    path = scenario_text.write(tmp_path, start="introduction")
    assert scenario.load(path).start == "introduction"
    path.write_text(path.read_text(encoding="utf-8").replace('organization = "Lumen Demo"', ""), encoding="utf-8")
    with pytest.raises(scenario.ScenarioInvalid, match="organization"):
        scenario.load(path)


def test_load_dir_sorts_by_key_and_refuses_a_shared_company_host(tmp_path):
    scenario_text.write(tmp_path, "unit-b", host="b.example")
    scenario_text.write(tmp_path, "unit-a", host="a.example")
    assert [s.key for s in scenario.load_dir(tmp_path)] == ["unit-a", "unit-b"]
    assert [s.key for s in scenario.load_dir(tmp_path, ["unit-b"])] == ["unit-b"]
    with pytest.raises(scenario.ScenarioInvalid, match="no scenario file"):
        scenario.load_dir(tmp_path, ["unit-z"])
    scenario_text.write(tmp_path, "unit-c", host="a.example")
    with pytest.raises(scenario.ScenarioInvalid, match="shares its company host"):
        scenario.load_dir(tmp_path)


def test_ensure_rows_inserts_once_and_refuses_a_changed_definition(engine, tmp_path):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE sim_results, sim_runs, sim_scenarios CASCADE"))
    first = scenario.load(scenario_text.write(tmp_path))
    with engine.begin() as conn:
        ids = scenario.ensure_rows(conn, [first])
    with engine.begin() as conn:
        assert scenario.ensure_rows(conn, [first]) == ids
        stored = conn.execute(text("SELECT persona::text, definition FROM sim_scenarios WHERE key = 'unit-a'")).all()
    assert len(stored) == 1 and stored[0].persona == "cooperative" and stored[0].definition["world"]["job"]["title"]
    changed = scenario.load(edited(tmp_path / "other", "Data engineer", "Data scientist"))
    with engine.begin() as conn, pytest.raises(scenario.ScenarioChanged):
        scenario.ensure_rows(conn, [changed])
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM sim_scenarios")).scalar_one() == 1
