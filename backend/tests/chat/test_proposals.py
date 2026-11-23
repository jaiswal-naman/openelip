"""CHT-03, CHT-04, threats T-05-16, T-05-19: record proposals from chat (plan 05-03).

record.propose reads the candidate's current message only; code then checks every item it returns (AI_SYSTEM 4.8) and
writes the ones that pass as `proposed` profile events. The record is unchanged until profile.api.confirm. The candidate
and every answer are invented; the real gateway runs on the fake provider, and the real record code on the test database
as the app_api role (what the API uses), so row-level security is in force.
"""

import hashlib
import inspect
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy import text

from app.chat import proposals
from app.chat.proposals import PROMPT, PROMPT_VERSION, build_messages, propose
from app.chat.schemas import ProposalList
from app.platform import db
from app.platform.llm import AIDisabled, SchemaInvalid, fake
from app.profile import api as profile
from app.profile import events, models, service
from tests.chat.fixtures import harness
from tests.match.factories import make_user

MESSAGE = "I would also take jobs in Pune. I know Rust now, and please keep it full time."
UUID_TEXT = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


@pytest.fixture
def tables(engine):
    harness.empty_tables(engine)
    yield
    harness.empty_tables(engine)
    fake.replay_from(None)


@pytest.fixture
def world(engine, tables) -> SimpleNamespace:
    """Ada, committed (the proposals run on their own connections): a location constraint (India, Mumbai), a full-time
    constraint, a target-title preference and the skill Python."""
    w = SimpleNamespace()
    with engine.begin() as conn:
        w.ada = make_user(conn)
        w.location = service.set_constraint(
            conn, w.ada, "location", {"country_codes": ["IN"], "cities": ["mumbai"]}, "India: Mumbai."
        ).row["id"]
        w.full_time = service.set_constraint(
            conn, w.ada, "employment_type", {"employment_types": ["full_time"]}, "Full time."
        ).row["id"]
        w.title = service.add_preference(
            conn, w.ada, "target_title", "Data engineer roles.", value="data engineer", weight=2
        ).row["id"]
        w.python = service.add_fact(conn, w.ada, "skill", "Python").row["id"]
    return w


@pytest.fixture
def answer(monkeypatch, tmp_path):
    """answer({...}, ...): the model's answers to record.propose, in order, through the real gateway."""

    def give(*answers):
        return harness.use_provider(monkeypatch, harness.write_answers(tmp_path, *answers))

    return give


def item(entity, field, quote, *, operation="set", statement="Stated by the candidate.", **value):
    return {
        "entity": entity,
        "operation": operation,
        "field": field,
        "value": value,
        "evidence_quote": quote,
        "statement": statement,
    }


def said(*items):
    return {"proposals": list(items)}


def stored(engine, user, where="true"):
    with engine.connect() as conn:
        return conn.execute(
            text(
                f"""SELECT id, event_type::text AS event_type, entity::text AS entity, operation::text AS operation,
                           entity_id, before, after, origin::text AS origin, reason
                      FROM profile_events WHERE user_id = :u AND {where} ORDER BY created_at, id"""
            ),
            {"u": user},
        ).all()


def waiting(engine, user):
    """The proposals waiting for an answer: what a call stored (one a test declined first has its answer)."""
    return stored(
        engine,
        user,
        """event_type = 'proposed'
           AND NOT EXISTS (SELECT 1 FROM profile_events a WHERE a.proposal_event_id = profile_events.id)""",
    )


def record_of(engine, user):
    with db.transaction(user) as conn:
        return profile.get_record(conn, user)


def record_state(engine, user):
    """Everything proposing must leave as it was."""
    record = record_of(engine, user)
    return (
        record["record_version"],
        [(c["id"], c["kind"], c["statement"], c["cities"], c["employment_types"]) for c in record["constraints"]],
        [(p["id"], p["value"], p["weight"]) for p in record["preferences"]],
        [(f["id"], f["statement"]) for f in record["facts"]],
    )


# ---- a kept proposal -----------------------------------------------------------------------------------------------


def test_a_kept_proposal_is_a_proposed_event_with_origin_chat_and_the_record_is_unchanged_until_confirmed(
    engine, world, answer
):
    answer(
        said(
            item(
                "constraint",
                "location",
                "take jobs in Pune",
                statement="Open to jobs in Pune.",
                country_codes=["in"],
                cities=["Mumbai", "Pune"],
            )
        )
    )
    before = record_state(engine, world.ada)

    (event,) = propose(world.ada, MESSAGE, principal=world.ada)

    assert (event.entity, event.operation, event.statement) == ("constraint", "update", "Open to jobs in Pune.")
    assert event.before["id"] == world.location and event.before["cities"] == ["mumbai"]
    assert event.before["country_codes"] == ["IN"] and "created_at" not in event.before
    assert event.after == {
        "kind": "location",
        "statement": "Open to jobs in Pune.",
        "country_codes": ["IN"],  # the record wants capitals
        "cities": ["Mumbai", "Pune"],
    }
    (row,) = waiting(engine, world.ada)
    assert (row.id, row.event_type, row.entity, row.operation, row.origin) == (
        event.event_id,
        "proposed",
        "constraint",
        "update",
        "chat",
    )
    assert str(row.entity_id) == world.location and row.reason == "take jobs in Pune"  # the quote is kept with it
    assert record_state(engine, world.ada) == before  # nothing changed: not the version, the constraints, the facts

    with db.transaction(world.ada) as conn:
        confirmed = profile.confirm(conn, world.ada, event.event_id)
    assert confirmed.entity == "constraint"
    after = record_of(engine, world.ada)
    (location,) = [c for c in after["constraints"] if c["kind"] == "location"]
    assert location["cities"] == ["mumbai", "pune"] and after["record_version"] == before[0] + 1  # now it applies


def test_a_new_skill_a_new_preference_and_a_removal_come_back_as_events_the_stream_can_send(engine, world, answer):
    answer(
        said(
            item("fact", "skill", "I know Rust now", statement="Knows Rust.", text="Rust"),
            item(
                "preference", "industry", "also take jobs", statement="Interested in fintech.", text="fintech", weight=1
            ),
            item("preference", "target_title", "keep it full time", operation="remove", text="data engineer"),
            item("constraint", "employment_type", "keep it full time", operation="remove"),
        )
    )
    created, preference, removed_title, removed_constraint = propose(world.ada, MESSAGE, principal=world.ada)

    assert (created.entity, created.operation, created.before) == ("fact", "create", None)
    assert created.after == {"kind": "skill", "statement": "Rust"}
    assert (preference.entity, preference.operation, preference.before) == ("preference", "create", None)
    assert preference.after == {
        "kind": "industry",
        "statement": "Interested in fintech.",
        "value": "fintech",
        "weight": 1,
    }
    assert (removed_title.operation, removed_title.after, removed_title.before["id"]) == ("remove", None, world.title)
    assert (removed_constraint.operation, removed_constraint.before["id"]) == ("remove", world.full_time)
    assert {row.operation for row in waiting(engine, world.ada)} == {"create", "remove"} and len(
        waiting(engine, world.ada)
    ) == 4


def test_a_preference_weight_that_changes_is_an_update_and_a_dealbreaker_always_weighs_minus_three(
    engine, world, answer
):
    answer(
        said(
            item("preference", "target_title", "also take jobs", text="Data Engineer", weight=3),
            item("preference", "untyped_dealbreaker", "also take jobs", text="no crypto firms"),
        )
    )
    update, dealbreaker = propose(world.ada, MESSAGE, principal=world.ada)
    assert (update.operation, update.before["id"], update.before["weight"], update.after["weight"]) == (
        "update",
        world.title,
        2,
        3,
    )
    assert (dealbreaker.operation, dealbreaker.after["weight"]) == ("create", -3)


# ---- what is dropped, one rule at a time ----------------------------------------------------------------------------


class Drops(logging.Handler):
    """The codes of the `chat.proposal_dropped` events logged while it is attached: why each item was dropped."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.codes: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        line = json.loads(record.getMessage())
        if line["event"] == "chat.proposal_dropped":
            self.codes.append(line["error_code"])


def dropped(engine, world, answer, *items, why, message=MESSAGE):
    """Propose with these items and assert that none came out, that each was dropped for `why` (and not for some other
    rule that would also have caught it), and that nothing was stored."""
    answer(said(*items))
    before = record_state(engine, world.ada)
    drops, events_log = Drops(), logging.getLogger("app.event")
    events_log.addHandler(drops)
    try:
        assert propose(world.ada, message, principal=world.ada) == []
    finally:
        events_log.removeHandler(drops)
    assert drops.codes == [why] * len(items)
    assert waiting(engine, world.ada) == [] and record_state(engine, world.ada) == before


QUOTE = "quote_not_in_message"


def test_a_quote_that_is_not_in_the_candidates_message_is_dropped(engine, world, answer):
    dropped(engine, world, answer, item("fact", "skill", "I know Rust and Go", text="Rust"), why=QUOTE)


def test_a_quote_may_not_come_from_anywhere_else(engine, world, answer):
    """Job text, a tool result or an earlier message can only reach a quote by being in the current message."""
    posting = item("fact", "skill", "Required: Kubernetes and Terraform", text="Kubernetes")
    dropped(engine, world, answer, posting, why=QUOTE)


def test_an_empty_or_blank_quote_is_dropped_though_it_is_a_substring_of_everything(engine, world, answer):
    blank = (item("fact", "skill", "", text="Rust"), item("fact", "skill", "   ", text="Go"))
    dropped(
        engine, world, answer, *blank, why=QUOTE
    )  # by the quote rule itself, not later by the record's reason check


def test_the_quote_must_match_exactly_not_nearly(engine, world, answer):
    dropped(engine, world, answer, item("fact", "skill", "i know rust now", text="Rust"), why=QUOTE)  # other case
    dropped(engine, world, answer, item("fact", "skill", "I know  Rust", text="Rust"), why=QUOTE)  # other spacing


def test_a_fact_of_kind_role_is_dropped_because_experience_goes_through_the_resume(engine, world, answer):
    role = item("fact", "role", "I know Rust now", text="Staff engineer at Initech Demo")
    dropped(engine, world, answer, role, why="not_allowed")


@pytest.mark.parametrize("kind", ["achievement", "education", "project", "other", "made_up"])
def test_a_field_that_is_not_on_the_allow_list_is_dropped(engine, world, answer, kind):
    dropped(engine, world, answer, item("fact", kind, "I know Rust now", text="Rust"), why="not_allowed")


def test_a_field_of_another_entity_is_not_allowed_either_and_a_fact_is_never_removed(engine, world, answer):
    dropped(
        engine,
        world,
        answer,
        item("constraint", "target_title", "also take jobs", country_codes=["IN"]),  # a preference kind
        item("preference", "excluded_company", "also take jobs", text="Initech Demo", weight=-2),  # a constraint kind
        item("fact", "skill", "I know Rust now", operation="remove", text="Python"),
        why="not_allowed",
    )


REFUSED = "validation_failed"  # the code of the record's own refusal of a value
FLOOR = "salary_floor"
NOT_RIGHT = [  # (item, the code it is dropped with)
    (item("constraint", "location", "take jobs in Pune", country_codes=["India"]), REFUSED),
    (item("constraint", "location", "take jobs in Pune"), REFUSED),  # no country at all
    (item("constraint", FLOOR, "keep it full time", salary_floor={"amount": -5, "currency": "INR"}), REFUSED),
    (item("constraint", FLOOR, "keep it full time", salary_floor={"amount": 30, "currency": "rupees"}), REFUSED),
    (item("constraint", "remote_policy", "take jobs in Pune", arrangements=["office"]), REFUSED),
    (item("constraint", "seniority", "keep it full time", seniorities=["boss"]), REFUSED),
    (item("preference", "industry", "also take jobs", text="fintech"), REFUSED),  # no weight
    (item("preference", "industry", "also take jobs", text="fintech", weight=9), REFUSED),
    (item("preference", "industry", "also take jobs", weight=2), REFUSED),  # nothing wanted
    (item("fact", "skill", "I know Rust now"), "no_value"),  # no skill named
]


def label(case):
    return f"{case['field']}-{json.dumps(case['value'])[:40]}" if isinstance(case, dict) else case


@pytest.mark.parametrize(("bad", "why"), NOT_RIGHT, ids=label)
def test_a_value_that_does_not_type_check_is_dropped_and_nothing_is_stored(engine, world, answer, bad, why):
    """The record's own checks refuse it when it is written (a savepoint: nothing is left), or code finds no value."""
    dropped(engine, world, answer, bad, why=why)


def test_a_value_equal_to_what_the_record_holds_is_dropped_whatever_its_order_or_case(engine, world, answer):
    dropped(
        engine,
        world,
        answer,
        item("constraint", "employment_type", "keep it full time", employment_types=["Full_Time"]),
        item("constraint", "location", "take jobs in Pune", country_codes=["in"], cities=["MUMBAI"]),
        item("preference", "target_title", "also take jobs", text="  DATA   engineer ", weight=2),
        item("fact", "skill", "I know Rust now", text="python"),
        why="unchanged",
    )


def test_a_removal_of_nothing_is_dropped(engine, world, answer):
    dropped(
        engine,
        world,
        answer,
        item("constraint", "excluded_company", "also take jobs", operation="remove"),  # none is set
        item("preference", "target_title", "also take jobs", operation="remove", text="astronaut"),
        why="nothing_to_remove",
    )


@pytest.mark.parametrize(
    "statement",
    ["Marital status: married.", "Date of birth: 4 March 1979.", "Disability: uses a wheelchair."],
)
def test_a_statement_that_the_screen_flags_is_dropped(engine, world, answer, statement):
    assert profile.screen(statement) is not None  # the narrow screen the plan names
    flagged = item("fact", "skill", "I know Rust now", statement=statement, text="Rust")
    dropped(engine, world, answer, flagged, why="protected_characteristic")


def test_a_value_the_screen_flags_is_dropped_too(engine, world, answer):
    flagged = item("preference", "other", "also take jobs", text="Religion: Jain", weight=1)
    dropped(engine, world, answer, flagged, why="protected_characteristic")


def test_a_protected_characteristic_in_the_quote_means_nothing_is_stored(engine, world, answer):
    """The quote is kept with the proposal, so a quote that states one cannot be kept: the record's own wider screen
    refuses it when the proposal is written, and nothing is stored."""
    message = "I am 45 years old and I know Rust now"
    dropped(
        engine, world, answer, item("fact", "skill", message, text="Rust"), why="validation_failed", message=message
    )


def test_the_same_message_may_state_a_protected_characteristic_when_the_quote_leaves_it_out(engine, world, answer):
    message = "I am 45 years old and I know Rust now"
    answer(said(item("fact", "skill", "I know Rust now", text="Rust", statement="Knows Rust.")))
    (event,) = propose(world.ada, message, principal=world.ada)
    assert event.after == {"kind": "skill", "statement": "Rust"}
    with engine.connect() as conn:  # the age is in no row: only the quote, which does not state it, was stored
        assert conn.execute(text("SELECT count(*) FROM profile_events WHERE reason ILIKE '%45%'")).scalar_one() == 0


# ---- what was declined ----------------------------------------------------------------------------------------------


def decline(engine, user, days_ago, **proposal):
    """A proposal of this person, declined this many days ago (the log is append-only: maintenance mode lets the test
    set the time the answer was written)."""
    with engine.begin() as conn:
        proposal_id = profile.propose_change(conn, user, origin="chat", **proposal)
        declined_id = events.decline(conn, user, proposal_id)
        conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        conn.execute(
            text("UPDATE profile_events SET created_at = now() - make_interval(days => :d) WHERE id = :id"),
            {"d": days_ago, "id": declined_id},
        )


RUST = dict(entity="fact", operation="create", after={"kind": "skill", "statement": "Rust"})


def test_the_same_change_declined_ten_days_ago_is_not_offered_again(engine, world, answer):
    decline(engine, world.ada, 10, **RUST)
    dropped(engine, world, answer, item("fact", "skill", "I know Rust now", text="rust"), why="declined_before")


def test_the_same_change_declined_thirty_one_days_ago_is_offered_again(engine, world, answer):
    decline(engine, world.ada, 31, **RUST)
    answer(said(item("fact", "skill", "I know Rust now", text="Rust")))
    (event,) = propose(world.ada, MESSAGE, principal=world.ada)
    assert event.after == {"kind": "skill", "statement": "Rust"}


def test_a_different_value_for_the_same_field_is_not_held_back_by_a_decline(engine, world, answer):
    decline(engine, world.ada, 5, **RUST)
    answer(said(item("fact", "skill", "I know Rust now", text="Go")))
    assert len(propose(world.ada, MESSAGE, principal=world.ada)) == 1


def test_a_declined_constraint_is_matched_by_its_typed_value_not_by_the_words_that_described_it(engine, world, answer):
    decline(
        engine,
        world.ada,
        3,
        entity="constraint",
        operation="update",
        entity_id=UUID(world.location),
        after={"kind": "location", "statement": "Pune too.", "country_codes": ["IN"], "cities": ["mumbai", "pune"]},
    )
    dropped(
        engine,
        world,
        answer,
        item(
            "constraint",
            "location",
            "take jobs in Pune",
            statement="A different sentence.",
            country_codes=["IN"],
            cities=["Pune", "Mumbai"],
        ),
        why="declined_before",
    )


def test_a_declined_removal_is_not_proposed_again(engine, world, answer):
    decline(engine, world.ada, 4, entity="constraint", operation="remove", entity_id=UUID(world.full_time))
    removal = item("constraint", "employment_type", "keep it full time", operation="remove")
    dropped(engine, world, answer, removal, why="declined_before")


def test_someone_elses_decline_is_not_this_persons(engine, world, answer):
    with engine.begin() as conn:
        bob = make_user(conn)
    decline(engine, bob, 1, **RUST)
    answer(said(item("fact", "skill", "I know Rust now", text="Rust")))
    assert len(propose(world.ada, MESSAGE, principal=world.ada)) == 1


# ---- bounds ---------------------------------------------------------------------------------------------------------


def test_the_same_change_twice_in_one_answer_is_proposed_once(engine, world, answer):
    same = item("fact", "skill", "I know Rust now", text="Rust")
    answer(said(same, {**same, "statement": "Another wording."}, item("fact", "skill", "I know Rust now", text="RUST")))
    assert len(propose(world.ada, MESSAGE, principal=world.ada)) == 1


def test_only_the_first_five_items_of_an_answer_are_considered(engine, world, answer):
    skills = [item("fact", "skill", "I know Rust now", text=f"Skill {n}") for n in range(8)]
    answer(said(*skills))
    assert len(propose(world.ada, MESSAGE, principal=world.ada)) == proposals.MAX_PROPOSALS == 5


def test_an_answer_with_no_proposals_stores_and_returns_nothing(engine, world, answer):
    answer(said())
    assert propose(world.ada, "Why was the Initech job ruled out?", principal=world.ada) == []
    assert waiting(engine, world.ada) == []


# ---- the gateway's failures are the caller's -------------------------------------------------------------------------


def test_with_no_model_configured_the_call_raises_ai_disabled_and_stores_nothing(engine, world, monkeypatch, tmp_path):
    harness.use_provider(monkeypatch, harness.write_answers(tmp_path, said()), LLM_PROVIDER="anthropic")
    with pytest.raises(AIDisabled):
        propose(world.ada, MESSAGE, principal=world.ada)
    assert waiting(engine, world.ada) == []


def test_an_answer_that_does_not_fit_the_schema_twice_raises_schema_invalid(engine, world, answer):
    answer("not json at all", '{"proposals": "none"}')
    with pytest.raises(SchemaInvalid):
        propose(world.ada, MESSAGE, principal=world.ada)
    assert waiting(engine, world.ada) == []


def test_one_retry_is_enough_after_an_invalid_answer(engine, world, answer):
    calls = answer("{}", said(item("fact", "skill", "I know Rust now", text="Rust")))
    assert len(propose(world.ada, MESSAGE, principal=world.ada)) == 1
    assert [call[0] for call in calls] == ["record.propose", "record.propose"]
    assert {call[1] for call in calls} == {PROMPT_VERSION}  # the version of this prompt is what the ledger records


# ---- what the model is shown ----------------------------------------------------------------------------------------


CURRENT = {
    "constraints": [
        {"id": "c-1", "kind": "location", "statement": "India: Mumbai.", "country_codes": ["IN"], "cities": ["mumbai"]},
        {
            "id": "c-2",
            "kind": "salary_floor",
            "statement": "30 lakh.",
            "salary_floor": {"amount": 3000000, "currency": "INR"},
        },
    ],
    "preferences": [{"id": "p-1", "kind": "target_title", "value": "data engineer", "weight": 2, "statement": "Data."}],
    "facts": [
        {"id": "f-1", "kind": "skill", "statement": "Python", "source": "resume"},
        {"id": "f-2", "kind": "certification", "statement": "AWS Solutions\nArchitect   Associate"},
    ],
}


def test_the_request_holds_only_the_prompt_the_current_values_and_the_current_message():
    assert list(inspect.signature(build_messages).parameters) == ["user_text", "current"]  # nothing else can be given
    system, user = build_messages("I know Rust now", CURRENT)
    assert system == {"role": "system", "content": PROMPT}
    assert user["role"] == "user"
    body = user["content"]
    assert body.endswith("BEGIN MESSAGE\nI know Rust now\nEND MESSAGE")
    assert '- location: {"country_codes": ["IN"], "cities": ["mumbai"]}' in body
    assert '- salary_floor: {"salary_floor": {"amount": 3000000, "currency": "INR"}}' in body
    assert "- remote_policy: (not set)" in body and "- excluded_company: (not set)" in body
    assert "- target_title: data engineer (weight 2)" in body
    assert "Skills: Python" in body and "Certifications: AWS Solutions Architect Associate" in body  # one line each
    assert not UUID_TEXT.search(body) and "c-1" not in body and "India: Mumbai." not in body  # no id, no statement


def test_the_model_is_called_with_exactly_that_request_and_nothing_from_earlier(engine, world, monkeypatch):
    sent = []

    def capture(task, schema, messages, *, principal, prompt_version, ledger):
        sent.append((task, schema, messages, principal, prompt_version, ledger))
        return ProposalList(proposals=[])

    monkeypatch.setattr(proposals, "complete", capture)
    propose(world.ada, "just this message", principal=world.ada)

    ((task, schema, messages, principal, version, ledger),) = sent
    assert (task, schema, principal, version) == ("record.propose", ProposalList, world.ada, PROMPT_VERSION)
    assert ledger == "api"  # chat proposes inside the API process, which holds no worker login
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"].endswith("BEGIN MESSAGE\njust this message\nEND MESSAGE")
    assert '- employment_type: {"employment_types": ["full_time"]}' in messages[1]["content"]
    assert "Python" in messages[1]["content"] and not UUID_TEXT.search(messages[1]["content"])


# ---- the prompt and the lists --------------------------------------------------------------------------------------


def test_the_prompt_version_is_the_twelve_character_hash_of_the_file():
    file = Path(proposals.__file__).parent / "prompts" / "record_propose.md"
    assert PROMPT_VERSION == hashlib.sha256(file.read_text(encoding="utf-8").encode()).hexdigest()[:12]
    assert len(PROMPT_VERSION) == 12


def test_the_allow_lists_are_the_records_own_lists():
    assert proposals.CONSTRAINT_KINDS == models.CONSTRAINT_KINDS
    assert proposals.PREFERENCE_KINDS == models.PREFERENCE_KINDS
    assert set(proposals.FACT_KINDS) <= set(models.FACT_KINDS)
    assert proposals.KIND_FIELDS == service.KIND_FIELDS
    assert proposals.DEALBREAKER_WEIGHT == service.DEALBREAKER_WEIGHT


def test_the_prompt_names_every_field_and_value_a_proposal_may_use():
    names = {
        *proposals.CONSTRAINT_KINDS,
        *proposals.PREFERENCE_KINDS,
        *proposals.FACT_KINDS,
        *models.WORK_ARRANGEMENTS,
        *models.EMPLOYMENT_TYPES,
        *models.SENIORITIES,
        *(name for names in proposals.KIND_FIELDS.values() for name in names),
        "evidence_quote",
        "statement",
        "text",
        "weight",
    }
    assert {name for name in names if name not in PROMPT} == set()
