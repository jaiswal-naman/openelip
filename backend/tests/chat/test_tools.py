"""CHT-01 to CHT-04, threats T-05-17 and T-05-23: the seven chat tools on real data (plan 05-04).

Each tool runs as the signed-in person on its own app_api transaction, so row-level security is in force, against the
committed rows of an invented world: Ada, whose hard constraints rule three postings out, and Bob, who has decisions on
the same jobs. What a test shows: constraints are applied without being asked, a tool reads only its caller's rows, the
model gets aliases and never an id (the stream and the stored tool row get the real ids), and the one tool that writes
only proposes.
"""

import json
import re
import typing
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.chat import proposals, tools
from app.chat.refs import AliasTable
from app.chat.schemas import (
    GetApplications,
    GetDecision,
    GetJob,
    GetRecord,
    ListMatches,
    ProposalEvent,
    ProposeRecordChange,
    SearchJobs,
    ToolCall,
)
from app.ingest import api as ingest
from app.platform import db
from app.platform.llm import AIDisabled
from app.profile import api as profile
from tests.chat.fixtures import harness
from tests.chat.fixtures.service import world as chat_world
from tests.match.factories import RESUME_FACTS, make_job
from tests.p5_factories import make_application

MESSAGE = "I would also take jobs in Pune."
SEVEN = {
    "get_record",
    "list_matches",
    "get_decision",
    "search_jobs",
    "get_job",
    "get_applications",
    "propose_record_change",
}
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def w(engine, monkeypatch):
    world = chat_world.start(engine, monkeypatch)
    world.engine = engine
    yield world
    harness.empty_tables(engine)


def call(user, args, aliases=None, message=""):
    """Run the tool `args` names as `user`, as the loop does: (user id, parsed input, the session's alias table)."""
    table = aliases if aliases is not None else AliasTable()
    return tools.build_tools(message)[args.tool](user, args, table), table


def tool_names() -> set[str]:
    names: set[str] = set()
    for variant in typing.get_args(ToolCall):
        names |= set(typing.get_args(variant.model_fields["tool"].annotation))
    return names


def found_ids(table: AliasTable, rows: list[dict], key: str) -> set[str]:
    return {table.get(row[key]).id for row in rows}


# ---- which tools there are -----------------------------------------------------------------------------------------


def test_the_tools_are_the_seven_the_model_may_call_and_none_of_them_can_send_fetch_or_approve():
    built = tools.build_tools("hello")
    assert set(built) == tool_names() == SEVEN
    reads = [name for name in built if name != "propose_record_change"]
    assert all(name.startswith(("get_", "list_", "search_")) for name in reads)


def test_no_tool_input_names_a_person_or_a_way_to_widen_the_search():
    for variant in typing.get_args(ToolCall):
        assert not set(variant.model_fields) & {"user", "user_id", "person", "email", "constrained_for", "constraints"}
    for attempt in ({"apply_constraints": False}, {"constrained_for": str(uuid4())}, {"user_id": str(uuid4())}):
        with pytest.raises(ValidationError):
            SearchJobs(tool="search_jobs", q="engineer", **attempt)


# ---- search_jobs: CHT-02 -------------------------------------------------------------------------------------------


def test_search_jobs_never_returns_a_job_that_breaks_the_callers_hard_constraints(w):
    output, table = call(w.ada, SearchJobs(tool="search_jobs", q="engineer"))

    assert found_ids(table, output.data["results"], "job") == {str(w.ok.id)}
    assert output.refs == {"job": [str(w.ok.id)]}
    assert output.data["constraints_applied"] is True


def test_an_unconstrained_search_does_find_the_other_three_so_the_filter_is_what_hides_them(w):
    with db.transaction(w.ada) as conn:
        page = ingest.search(
            conn, q="engineer", country=None, city=None, remote=None, limit=10, cursor=None, mode="hybrid"
        )
    assert {row["id"] for row in page.rows} == {w.ok.id, w.abroad.id, w.blocked.id, w.onsite.id}


def test_each_person_is_searched_under_their_own_constraints(w):
    """Bob has no company exclusion: the posting that is Ada's excluded company is his to see."""
    output, table = call(w.bob, SearchJobs(tool="search_jobs", q="engineer"))
    assert found_ids(table, output.data["results"], "job") == {str(w.ok.id), str(w.blocked.id)}


def test_a_step_that_tries_to_widen_the_search_is_ignored_because_the_tool_never_reads_such_a_field(w):
    widened = SearchJobs.model_construct(
        tool="search_jobs", q="engineer", country=None, city=None, remote=None, limit=5, apply_constraints=False
    )
    output, table = call(w.ada, widened)
    assert found_ids(table, output.data["results"], "job") == {str(w.ok.id)}


def test_search_jobs_describes_each_result_by_alias_and_holds_the_list_to_the_cap(w):
    with w.engine.begin() as c:
        for n in range(12):
            make_job(
                c,
                title=f"Search engineer {n}",
                description="Search ranking work.",
                requirements=[("Search ranking", "required")],
                country="IN",
                arrangements=("remote",),
            )
    many = SearchJobs.model_construct(
        tool="search_jobs", q="search engineer", country=None, city=None, remote=None, limit=50
    )
    output, table = call(w.ada, many)

    assert tools.SEARCH_LIMIT_MAX == 10 and len(output.data["results"]) == 10
    first = output.data["results"][0]
    assert re.fullmatch(r"j[0-9]+", first["job"]) and first["title"].startswith("Search engineer")
    assert {"company", "country", "city", "work_arrangements", "employment_type", "pay"} <= set(first)
    assert len(output.refs["job"]) == 10 and "apply_url" not in first


def test_search_jobs_filters_by_country_city_and_remote_when_asked(w):
    output, table = call(w.ada, SearchJobs(tool="search_jobs", q="analyst", country="in", remote=False))
    assert found_ids(table, output.data["results"], "job") == {str(w.fine.id)}  # hybrid is stated, not remote
    output, _ = call(w.ada, SearchJobs(tool="search_jobs", q="analyst", country="us"))
    assert output.data["results"] == []


# ---- list_matches and get_decision: CHT-01 -------------------------------------------------------------------------


def test_list_matches_returns_only_the_callers_decisions_though_another_person_has_one_on_the_same_job(w):
    output, table = call(w.ada, ListMatches(tool="list_matches"))
    seen = json.dumps([output.data, output.refs])

    assert {d["outcome"] for d in output.data["decisions"]} == {"shown", "filtered"}
    assert set(output.refs["decision"]) == {str(w.shown), str(w.filtered)}
    assert str(w.bobs_shown) not in seen and str(w.bobs_withheld) not in seen
    assert "Bob's own line." not in seen

    bobs, _ = call(w.bob, ListMatches(tool="list_matches"))
    assert set(bobs.refs["decision"]) == {str(w.bobs_shown), str(w.bobs_withheld)}
    assert str(w.shown) not in json.dumps([bobs.data, bobs.refs])


def test_list_matches_narrows_by_outcome_and_by_date_and_names_each_decision_and_job_by_alias(w):
    output, table = call(w.ada, ListMatches(tool="list_matches", outcome="filtered"))
    (decision,) = output.data["decisions"]
    assert table.get(decision["decision"]).id == str(w.filtered) and table.get(decision["job"]).id == str(w.blocked.id)
    assert (decision["title"], decision["company"], decision["outcome"]) == ("Data engineer", "Globex Demo", "filtered")
    assert decision["reasons"] == ["hard_constraint"] and decision["violated_kinds"] == ["excluded_company"]
    assert decision["date"] == chat_world.YESTERDAY.isoformat()
    # the constraint it broke is one the person can be shown, by alias
    (constraint,) = decision["constraints"]
    assert constraint["kind"] == "excluded_company" and table.get(constraint["constraint"]).id == str(w.excluded)
    assert constraint["statement"] == "Never Globex Demo."
    assert output.refs == {
        "decision": [str(w.filtered)],
        "job": [str(w.blocked.id)],
        "constraint": [str(w.excluded)],
    }

    today_only, _ = call(
        w.ada, ListMatches(tool="list_matches", since=chat_world.LOCAL_DATE, until=chat_world.LOCAL_DATE)
    )
    assert [d["outcome"] for d in today_only.data["decisions"]] == ["shown"]
    nothing, _ = call(w.ada, ListMatches(tool="list_matches", outcome="withheld"))
    assert nothing.data["decisions"] == [] and nothing.refs == {}


def test_get_decision_returns_the_evidence_table_with_fact_aliases(w):
    table = AliasTable()
    alias = table.add("decision", w.shown)
    output, _ = call(w.ada, GetDecision(tool="get_decision", decision=alias), table)
    data = output.data

    assert (data["decision"], data["outcome"], data["title"], data["is_on_request"]) == (
        alias,
        "shown",
        "Data engineer",
        False,
    )
    assert data["summary"] == "A fit, with the evidence below."
    first, second = data["evidence"]
    assert (first["requirement"], first["importance"], first["verdict"]) == ("Python", "required", "met")
    assert (second["requirement"], second["verdict"], second["facts"]) == ("SQL", "no_evidence", [])
    assert table.get(first["facts"][0]).kind == "fact" and table.get(first["facts"][0]).id == str(w.record.fact_ids[1])
    assert output.refs["fact"] == [str(w.record.fact_ids[1])] and output.refs["decision"] == [str(w.shown)]


def test_a_filtered_decision_comes_back_with_the_constraint_it_broke(w):
    table = AliasTable()
    output, _ = call(w.ada, GetDecision(tool="get_decision", decision=table.add("decision", w.filtered)), table)
    (constraint,) = output.data["constraints"]
    assert output.data["outcome"] == "filtered" and output.data["evidence"] == []
    assert table.get(constraint["constraint"]).id == str(w.excluded) and constraint["statement"] == "Never Globex Demo."


def test_get_decision_with_an_alias_no_tool_returned_is_an_unknown_reference_and_opens_no_connection(w, monkeypatch):
    def no_database(*args, **kwargs):
        raise AssertionError("the database was opened")

    monkeypatch.setattr(db, "transaction", no_database)
    for args in (
        GetDecision(tool="get_decision", decision="d7"),
        GetDecision(tool="get_decision", job="j3"),
        GetJob(tool="get_job", job="j9"),
    ):
        output, _ = call(w.ada, args)
        assert output.data["found"] is False and output.data["reason"] == "unknown_reference"
        assert output.refs == {} and output.untrusted is True


def test_an_alias_of_the_wrong_kind_is_an_unknown_reference_too(w, monkeypatch):
    table = AliasTable()
    job_alias, decision_alias = table.add("job", w.ok.id), table.add("decision", w.shown)
    monkeypatch.setattr(db, "transaction", lambda *a, **k: pytest.fail("the database was opened"))
    for args in (
        GetDecision(tool="get_decision", decision=job_alias),
        GetDecision(tool="get_decision", job=decision_alias),
        GetJob(tool="get_job", job=decision_alias),
    ):
        output, _ = call(w.ada, args, table)
        assert output.data["reason"] == "unknown_reference"


def test_a_decision_of_another_person_is_not_found_even_when_the_alias_table_holds_its_id(w):
    """The table is the session's, the person is the session's: acting as Ada, Bob's decision does not exist."""
    table = AliasTable()
    alias = table.add("decision", w.bobs_shown)
    output, _ = call(w.ada, GetDecision(tool="get_decision", decision=alias), table)
    assert (output.data["found"], output.data["reason"]) == (False, "not_found")
    assert "Bob's own line." not in json.dumps(output.data) and output.refs == {}


def test_get_decision_by_job_gives_the_newest_decision_else_the_check_the_ledger_runs_on_request(w):
    table = AliasTable()

    def by_job(job):
        return call(w.ada, GetDecision(tool="get_decision", job=table.add("job", job)), table)[0].data

    stored = by_job(w.blocked.id)
    assert (stored["outcome"], stored["is_on_request"], stored["violated_kinds"]) == (
        "filtered",
        False,
        ["excluded_company"],
    )

    abroad = by_job(w.abroad.id)  # no decision: the constraints are checked on the spot
    assert (abroad["outcome"], abroad["is_on_request"], abroad["decision"]) == ("filtered", True, None)
    assert set(abroad["violated_kinds"]) == {"location", "remote_policy"}
    assert {c["kind"] for c in abroad["constraints"]} == {"location", "remote_policy"}

    fine = by_job(w.fine.id)
    assert (fine["outcome"], fine["is_on_request"], fine["violated_kinds"]) == ("not_evaluated", True, [])

    gone = by_job(10_000_000)
    assert (gone["found"], gone["reason"]) == (False, "not_found")


# ---- get_job -------------------------------------------------------------------------------------------------------


def test_get_job_returns_typed_fields_requirements_and_the_posting_as_an_untrusted_result(w):
    table = AliasTable()
    alias = table.add("job", w.ok.id)
    output, _ = call(w.ada, GetJob(tool="get_job", job=alias), table)
    data = output.data

    assert output.untrusted is True and output.source_kind == "job_posting"
    assert (data["job"], data["title"], data["company"], data["country"], data["status"]) == (
        alias,
        "Data engineer",
        "Initech Demo",
        "IN",
        "open",
    )
    assert data["work_arrangements"] == ["remote"] and data["pay"] is None
    assert [(r["text"], r["importance"]) for r in data["requirements"]] == [
        ("Python", "required"),
        ("SQL", "preferred"),
    ]
    assert data["requirements_known"] is True
    assert data["posting_text"] == "Build nightly pipelines with Python and SQL." and data["posting_truncated"] is False
    assert "apply_url" not in data and "source" not in data  # nothing for the model to turn into a link
    assert output.refs == {"job": [str(w.ok.id)]}


def test_a_long_posting_is_cut_to_the_cap_and_says_so(w):
    with w.engine.begin() as c:
        long = make_job(c, title="Wordy role", description="word " * 5000, requirements=[])
    table = AliasTable()
    output, _ = call(w.ada, GetJob(tool="get_job", job=table.add("job", long.id)), table)
    assert tools.POSTING_CHARS == 6000
    assert len(output.data["posting_text"]) == tools.POSTING_CHARS and output.data["posting_truncated"] is True


def test_get_job_for_a_job_the_index_does_not_list_is_not_found(w):
    table = AliasTable()
    output, _ = call(w.ada, GetJob(tool="get_job", job=table.add("job", 10_000_000)), table)
    assert (output.data["found"], output.data["reason"]) == (False, "not_found")


# ---- get_record and get_applications --------------------------------------------------------------------------------


def test_get_record_returns_facts_constraints_and_preferences_by_alias_with_the_date_each_was_added(w):
    output, table = call(w.ada, GetRecord(tool="get_record"))
    data = output.data

    with w.engine.connect() as conn:
        version = conn.execute(text("SELECT record_version FROM users WHERE id = :u"), {"u": w.ada}).scalar_one()
    assert data["record_version"] == version
    assert [f["statement"] for f in data["facts"]] == [statement for _, statement in RESUME_FACTS]
    assert found_ids(table, data["facts"], "fact") == {str(i) for i in w.record.fact_ids}
    assert {c["kind"] for c in data["constraints"]} == {"location", "remote_policy", "excluded_company"}
    assert found_ids(table, data["constraints"], "constraint") == set(output.refs["constraint"])
    assert [(p["kind"], p["value"], p["weight"]) for p in data["preferences"]] == [("target_title", "data engineer", 2)]
    assert data["waiting"] == {"unconfirmed_facts": 0, "open_proposals": 0}
    for row in (*data["facts"], *data["constraints"], *data["preferences"]):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["added"])
    assert output.source_kind == "career_record" and output.untrusted is True


def test_get_record_shows_nothing_of_another_person(w):
    output, table = call(w.bob, GetRecord(tool="get_record"))
    assert found_ids(table, output.data["facts"], "fact") == {str(i) for i in w.bobs_record.fact_ids}
    assert {c["kind"] for c in output.data["constraints"]} == {"location", "remote_policy"}
    assert output.data["preferences"] == []
    assert not set(output.refs["fact"]) & {str(i) for i in w.record.fact_ids}


def test_get_applications_returns_statuses_with_their_dates_and_the_job_by_alias(w):
    output, table = call(w.ada, GetApplications(tool="get_applications"))
    (row,) = output.data["applications"]

    assert (row["status"], row["title"], row["company"]) == ("applied", "Data engineer", "Initech Demo")
    assert table.get(row["job"]).id == str(w.ok.id)
    assert re.match(r"\d{4}-\d{2}-\d{2}T", row["status_changed_at"])
    assert [h["status"] for h in row["history"]] == ["applied"] and "occurred_at" in row["history"][0]
    assert output.refs == {"job": [str(w.ok.id)]}
    assert call(w.bob, GetApplications(tool="get_applications"))[0].data["applications"] == []


def test_a_long_record_and_long_lists_are_cut_to_their_caps_and_say_how_much_was_left_out(w, monkeypatch):
    monkeypatch.setattr(tools, "FACTS_MAX", 2)
    monkeypatch.setattr(tools, "LIST_LIMIT", 1)
    with w.engine.begin() as c:
        make_application(c, w.ada, w.fine)  # a second application: Ada now has two

    record, _ = call(w.ada, GetRecord(tool="get_record"))
    assert len(record.data["facts"]) == 2 and record.data["facts_omitted"] == len(w.record.fact_ids) - 2

    applications, _ = call(w.ada, GetApplications(tool="get_applications"))
    assert len(applications.data["applications"]) == 1 and applications.data["omitted"] == 1

    decisions, _ = call(w.ada, ListMatches(tool="list_matches"))
    assert [d["outcome"] for d in decisions.data["decisions"]] == ["shown"]  # the newest date first, one of two


# ---- what the model is shown ---------------------------------------------------------------------------------------


def test_every_tools_data_holds_aliases_only_and_every_result_is_marked_untrusted(w):
    table = AliasTable()
    asks = [
        GetRecord(tool="get_record"),
        ListMatches(tool="list_matches"),
        GetDecision(tool="get_decision", decision=table.add("decision", w.shown)),
        GetDecision(tool="get_decision", decision=table.add("decision", w.filtered)),
        SearchJobs(tool="search_jobs", q="engineer"),
        GetJob(tool="get_job", job=table.add("job", w.ok.id)),
        GetApplications(tool="get_applications"),
    ]
    for args in asks:
        output, _ = call(w.ada, args, table)
        serialised = json.dumps(output.data)  # strict: nothing here needs default=str
        assert UUID_TEXT.search(serialised) is None, args.tool
        assert output.untrusted is True, args.tool
        for kind, ids in output.refs.items():
            assert kind in ("decision", "job", "fact", "constraint") and ids, args.tool
            if kind == "job":
                assert all(i.isdigit() for i in ids)
            else:
                assert all(UUID(i) for i in ids)
        for ref in (str(w.bob), str(w.bobs_shown), str(w.bobs_withheld)):
            assert ref not in serialised and ref not in json.dumps(output.refs)


def test_an_alias_a_tool_gave_is_the_one_every_later_tool_uses(w):
    table = AliasTable()
    listed, _ = call(w.ada, ListMatches(tool="list_matches", outcome="shown"), table)
    (entry,) = listed.data["decisions"]
    got, _ = call(w.ada, GetDecision(tool="get_decision", decision=entry["decision"]), table)
    assert got.data["decision"] == entry["decision"] and got.data["job"] == entry["job"]
    job, _ = call(w.ada, GetJob(tool="get_job", job=entry["job"]), table)
    assert job.data["job"] == entry["job"]


# ---- propose_record_change: CHT-03 ----------------------------------------------------------------------------------


def stated_pune() -> dict:
    return {
        "proposals": [
            {
                "entity": "preference",
                "operation": "set",
                "field": "location",
                "value": {"text": "Pune", "weight": 2},
                "evidence_quote": MESSAGE,
                "statement": "Open to jobs in Pune.",
            }
        ]
    }


def record_of(user):
    """What a proposal must leave as it was: the record's version, its preferences and its constraints."""
    with db.transaction(user) as conn:
        record = profile.get_record(conn, user)
    return record["record_version"], [p["id"] for p in record["preferences"]], [c["id"] for c in record["constraints"]]


def test_propose_record_change_stores_a_proposal_from_the_current_message_and_changes_nothing(
    engine, monkeypatch, tmp_path
):
    w = chat_world.start(engine, monkeypatch, harness.write_answers(tmp_path, stated_pune()))
    try:
        before = record_of(w.ada)
        output, _ = call(w.ada, ProposeRecordChange(tool="propose_record_change"), message=MESSAGE)

        (event,) = output.proposals
        assert isinstance(event, ProposalEvent) and (event.entity, event.operation) == ("preference", "create")
        assert output.data["proposed"] == 1 and "confirms" in output.data["note"]
        assert UUID_TEXT.search(json.dumps(output.data)) is None  # the model learns no event id
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT event_type::text, origin::text, entity::text FROM profile_events WHERE id = :i"),
                {"i": event.event_id},
            ).one()
        assert tuple(row) == ("proposed", "chat", "preference")
        assert record_of(w.ada) == before  # not the version, the preferences nor the constraints
    finally:
        harness.empty_tables(engine)


def test_propose_record_change_reads_the_message_it_was_built_with_and_nothing_else(w, monkeypatch):
    seen = []
    event = ProposalEvent(uuid4(), "fact", "create", None, {"kind": "skill", "statement": "Rust"}, "Knows Rust.")

    def spy(user_id, user_text, *, principal):
        seen.append((user_id, user_text, principal))
        return [event]

    monkeypatch.setattr(proposals, "propose", spy)
    output, _ = call(w.ada, ProposeRecordChange(tool="propose_record_change"), message="I know Rust now.")

    assert seen == [(w.ada, "I know Rust now.", w.ada)]
    assert output.proposals == (event,) and output.data["proposed"] == 1


def test_with_nothing_to_propose_the_tool_says_so(engine, monkeypatch, tmp_path):
    w = chat_world.start(engine, monkeypatch, harness.write_answers(tmp_path, {"proposals": []}))
    try:
        output, _ = call(w.ada, ProposeRecordChange(tool="propose_record_change"), message="Hello there.")
        assert output.proposals == () and output.data["proposed"] == 0
    finally:
        harness.empty_tables(engine)


def test_without_a_model_the_tool_raises_so_the_turn_ends_instead_of_pretending(engine, monkeypatch):
    w = chat_world.start(engine, monkeypatch)
    try:
        chat_world.use_provider(monkeypatch, None, LLM_PROVIDER="anthropic")  # a real model with no key
        with pytest.raises(AIDisabled):
            call(w.ada, ProposeRecordChange(tool="propose_record_change"), message=MESSAGE)
    finally:
        harness.empty_tables(engine)
