"""Only usable facts leave the profile module (REC-02, threat T-3-15; plan 03-04).

A fact is usable when it is confirmed, not rejected and not superseded. This file makes one fact in each state and
checks every function of app.profile.api, the only door other modules may use, and fails when a function is added to
the api without a check here. A second test fails when any other module imports anything from app.profile but the api.
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

import app
from app.profile import api, events, protected, service

TEXT = (
    "Senior Backend Engineer, Northwind Logistics (Mar 2021 - Present)\n"
    "Built order pipelines in Python.\nKubernetes, Helm, Terraform"
)


def one(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def make_user(conn, name: str) -> UUID:
    return one(conn, "INSERT INTO users (email) VALUES (:e) RETURNING id", e=f"{name}-{uuid4().hex[:8]}@example.test")


def resume_fact(conn, user: UUID, document: UUID, start: int, end: int) -> UUID:
    return one(
        conn,
        """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span, source_quote)
           VALUES (:u, 'role', :q, 'resume', :d, int4range(:a, :b), :q) RETURNING id""",
        u=user,
        d=document,
        a=start,
        b=end,
        q=TEXT[start:end],
    )


class Person:
    """One candidate with a fact in every state, and constraints and preferences active, replaced and removed."""

    def __init__(self, conn, name: str) -> None:
        self.id = make_user(conn, name)
        document = one(
            conn,
            """INSERT INTO documents (user_id, content, mime_type, byte_size, sha256, status, extracted_text, parsed_at)
               VALUES (:u, :c, 'application/pdf', 100, :h, 'parsed', :t, now()) RETURNING id""",
            u=self.id,
            c=b"sealed",
            h=b"h" * 32,
            t=TEXT,
        )
        self.unconfirmed = resume_fact(conn, self.id, document, 0, 10)
        rejected = resume_fact(conn, self.id, document, 11, 20)
        retired = resume_fact(conn, self.id, document, 21, 40)
        self.confirmed = resume_fact(conn, self.id, document, 41, 66)
        service.reject_fact(conn, self.id, rejected)
        self.rejected = rejected
        self.superseded = retired
        self.corrected = UUID(service.correct_fact(conn, self.id, retired, "Built order pipelines").row["id"])
        service.confirm_fact(conn, self.id, self.confirmed)

        self.written = UUID(service.add_fact(conn, self.id, "skill", "Kubernetes").row["id"])
        service.set_constraint(conn, self.id, "remote_policy", {"arrangements": ["remote"]}, "Remote only")
        service.set_constraint(conn, self.id, "location", {"country_codes": ["IN"]}, "India")
        service.set_constraint(conn, self.id, "location", {"country_codes": ["US"], "cities": ["austin"]}, "Austin")
        seniority = service.set_constraint(conn, self.id, "seniority", {"seniorities": ["senior"]}, "Senior")
        service.remove_constraint(conn, self.id, UUID(seniority.row["id"]))
        service.set_constraint(
            conn, self.id, "salary_floor", {"salary_floor": {"amount": 3000000, "currency": "INR"}}, "30 lakh"
        )
        kept = service.add_preference(conn, self.id, "industry", "Fintech", value="fintech", weight=1)
        replaced = service.add_preference(conn, self.id, "target_title", "Staff", value="staff engineer", weight=2)
        service.change_preference(conn, self.id, UUID(replaced.row["id"]), {"weight": 3})
        gone = service.add_preference(conn, self.id, "other", "Small teams", weight=1)
        service.remove_preference(conn, self.id, UUID(gone.row["id"]))
        self.kept_preference = kept.row["id"]
        self.proposal = events.propose_change(
            conn, self.id, origin="chat", entity="fact", operation="create", after={"kind": "skill", "statement": "Go"}
        )


@pytest.fixture
def world(conn) -> tuple[Person, Person]:
    return Person(conn, "ada"), Person(conn, "bob")


def ids(rows) -> set[str]:
    return {row["id"] for row in rows}


# ---- every function of the api -------------------------------------------------------------------


def check_usable_facts(conn, ada: Person, bob: Person) -> None:
    facts = api.usable_facts(conn, ada.id)
    assert ids(facts) == {str(ada.confirmed), str(ada.corrected), str(ada.written)}
    assert {f["status"] for f in facts} == {"confirmed"}
    assert not ids(facts) & {str(ada.unconfirmed), str(ada.rejected), str(ada.superseded)}
    assert ids(api.usable_facts(conn, bob.id)).isdisjoint(ids(facts))  # nothing of another person comes back
    by_id = {f["id"]: f for f in facts}
    assert by_id[str(ada.confirmed)]["provenance"]["quote"] == TEXT[41:66]  # a resume fact keeps its source
    assert by_id[str(ada.written)]["provenance"] is None


def check_get_record(conn, ada: Person, bob: Person) -> None:
    record = api.get_record(conn, ada.id)
    assert ids(record["facts"]) == ids(api.usable_facts(conn, ada.id))
    # facts waiting for review and proposals waiting for an answer are counted, never returned
    assert (record["unconfirmed_facts"], record["open_proposals"]) == (1, 1)
    assert record["record_version"] == one(conn, "SELECT record_version FROM users WHERE id = :u", u=ada.id)
    assert {c["kind"] for c in record["constraints"]} == {"remote_policy", "location", "salary_floor"}
    assert [c["cities"] for c in record["constraints"] if c["kind"] == "location"] == [
        ["austin"]
    ]  # not the old India row
    assert sorted(p["value"] for p in record["preferences"]) == ["fintech", "staff engineer"]
    assert [p["weight"] for p in record["preferences"] if p["value"] == "staff engineer"] == [3]
    assert not ids(record["preferences"]) & {str(bob.kept_preference)}
    assert TEXT[0:10] not in repr(record)  # the waiting fact's words are nowhere in what a consumer gets
    assert "Go" not in {f["statement"] for f in record["facts"]}  # nor the open proposal's


def check_compiled_constraints(conn, ada: Person, bob: Person) -> None:
    compiled = api.compiled_constraints(conn, ada.id)
    assert set(compiled) == {"remote_policy", "location", "salary_floor"}  # a kind never set is absent, never defaulted
    assert compiled["remote_policy"]["arrangements"] == ["remote"]
    assert (compiled["location"]["country_codes"], compiled["location"]["cities"]) == (["US"], ["austin"])
    assert compiled["salary_floor"]["salary_floor"] == {"amount": 3000000, "currency": "INR"}
    assert all("statement" not in value and isinstance(value["id"], str) for value in compiled.values())
    assert api.compiled_constraints(conn, make_user(conn, "new")) == {}


def check_facts_by_ids(conn, ada: Person, bob: Person) -> None:
    """The one read that may return a fact that is no longer usable: a verdict made while a fact was confirmed cites it
    by id, and must still be able to show it once the person has corrected or retired it (plan 04-04). It returns only
    facts the person confirmed at some time, so a fact waiting for review or rejected unconfirmed never leaves."""
    service.reject_fact(conn, ada.id, ada.written)  # a confirmed fact, retired since
    asked = [ada.confirmed, ada.corrected, ada.written, ada.unconfirmed, ada.rejected, ada.superseded]
    found = {f["id"]: f for f in api.facts_by_ids(conn, ada.id, asked)}
    assert set(found) == {str(ada.confirmed), str(ada.corrected), str(ada.written)}
    assert (found[str(ada.confirmed)]["status"], found[str(ada.written)]["status"]) == ("confirmed", "superseded")
    assert not set(found) & {str(ada.unconfirmed), str(ada.rejected), str(ada.superseded)}  # never confirmed
    assert (
        found[str(ada.confirmed)]["provenance"]["quote"] == TEXT[41:66]
        and found[str(ada.written)]["provenance"] is None
    )
    assert api.facts_by_ids(conn, bob.id, asked) == []  # nothing of another person comes back
    assert api.facts_by_ids(conn, ada.id, []) == []


def check_declined_since(conn, ada: Person, bob: Person) -> None:
    """Not a read of the record: the log of proposals the person said no to (plan 05-03), so a proposer does not offer
    them again. Declined ones only, from the time asked for on, the person's own, with entity, field and value."""
    employment = {"kind": "employment_type", "statement": "Full time only.", "employment_types": ["full_time"]}
    industry = {"kind": "industry", "statement": "Open to fintech.", "value": "fintech", "weight": 1}
    never = events.propose_change(
        conn, ada.id, origin="chat", entity="constraint", operation="create", after=employment
    )
    stale = events.propose_change(conn, ada.id, origin="chat", entity="preference", operation="create", after=industry)
    removal = events.propose_change(
        conn, ada.id, origin="feedback", entity="fact", operation="remove", entity_id=ada.written
    )
    for proposal in (never, stale, removal):
        events.decline(conn, ada.id, proposal)
    zig = {"kind": "skill", "statement": "Zig"}
    events.decline(
        conn, bob.id, events.propose_change(conn, bob.id, origin="chat", entity="fact", operation="create", after=zig)
    )
    # Ada's own `proposal` (made in Person) is still waiting for an answer: it is never in the result.
    conn.execute(text("SELECT set_config('app.maintenance', 'on', true)"))  # the log is append-only
    conn.execute(
        text("UPDATE profile_events SET created_at = now() - interval '40 days' WHERE proposal_event_id = :p"),
        {"p": stale},
    )
    conn.execute(text("SELECT set_config('app.maintenance', 'off', true)"))

    since = datetime.now(timezone.utc) - timedelta(days=30)
    found = api.declined_since(conn, ada.id, since)
    assert [(d.entity, d.operation, d.field) for d in found] == [
        ("fact", "remove", "skill"),  # newest first; a removal is described by the row it would have removed
        ("constraint", "create", "employment_type"),
    ]
    assert found[1].value["employment_types"] == ["full_time"] and found[0].value["statement"] == "Kubernetes"
    assert all(isinstance(d.declined_at, datetime) for d in found)
    older = {d.field for d in api.declined_since(conn, ada.id, since - timedelta(days=30))}
    assert older == {"skill", "employment_type", "industry"}  # the 40 day old one is there when asked for further back
    assert [d.value["statement"] for d in api.declined_since(conn, bob.id, since)] == ["Zig"]  # nothing of Ada's
    assert api.declined_since(conn, make_user(conn, "new"), since) == []


def check_screen(conn, ada: Person, bob: Person) -> None:
    """The protected-characteristic screen is re-exported, not copied: the same function, so the same answers."""
    assert api.screen is protected.screen
    assert api.screen("Marital status: married") == "marital_status"
    assert api.screen("Built order pipelines in Python.") is None


# Reads are checked for the usable rule. The two writers return no rows of the record: an id, or ids. declined_since
# reads the change log of declined proposals and screen reads nothing: both are checked for what they return.
CHECKS = {
    "usable_facts": check_usable_facts,
    "get_record": check_get_record,
    "compiled_constraints": check_compiled_constraints,
    "facts_by_ids": check_facts_by_ids,
    "declined_since": check_declined_since,
    "screen": check_screen,
}
WRITERS = {"propose_change", "confirm"}


def test_every_api_function_has_a_usable_only_check_so_a_new_one_cannot_slip_in(conn, world):
    assert set(api.__all__) == set(CHECKS) | WRITERS
    assert all(callable(getattr(api, name)) for name in api.__all__)


@pytest.mark.parametrize("name", sorted(CHECKS))
def test_a_read_through_the_api_returns_only_usable_rows(conn, world, name):
    CHECKS[name](conn, *world)


def test_the_writers_hand_back_ids_not_rows(conn, world):
    ada, _ = world
    proposal = api.propose_change(
        conn, ada.id, origin="agent", entity="fact", operation="remove", entity_id=ada.written, after=None
    )
    assert isinstance(proposal, UUID)
    confirmed = api.confirm(conn, ada.id, proposal)
    assert isinstance(confirmed, api.Confirmed) and confirmed.entity_id == ada.written
    assert ada.written not in {UUID(f["id"]) for f in api.usable_facts(conn, ada.id)}  # retired, and not returned


# ---- the predicate and the doors -----------------------------------------------------------------


def explain(conn, predicate: str, user: UUID) -> str:
    conn.execute(text("SET LOCAL enable_seqscan = off"))
    sql = f"EXPLAIN SELECT {service.FACT_COLUMNS} FROM profile_facts f WHERE f.user_id = '{user}' AND {predicate}"
    return "\n".join(conn.execute(text(sql)).scalars())


def test_the_usable_predicate_is_the_one_of_the_partial_index(conn, world):
    """The planner uses a partial index only for a query that provably implies its WHERE clause. So a read of usable
    facts that uses profile_facts_usable_idx has the index's predicate, and one with a looser predicate does not."""
    ada, _ = world
    assert "profile_facts_usable_idx" in explain(conn, service.USABLE_FACT, ada.id)
    looser = "f.confirmed_at IS NOT NULL AND f.superseded_at IS NULL"  # would let a rejected fact through
    assert "profile_facts_usable_idx" not in explain(conn, looser, ada.id)


def other_modules_importing_profile(source: str) -> list[str]:
    """The names a module imports from app.profile, other than app.profile.api itself."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.startswith("app.profile") and a.name != "app.profile.api"]
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.profile"):
            if node.module == "app.profile":
                found += [f"app.profile.{a.name}" for a in node.names if a.name != "api"]
            elif node.module != "app.profile.api":
                found.append(node.module)
    return found


ROOTS = {"main.py", "worker.py", "cli.py"}  # the composition roots sit above every layer (ARCHITECTURE 3.3, rule 4)


def test_no_module_reaches_into_the_profile_package_except_through_its_api():
    package = Path(app.__file__).parent
    offenders = {}
    for path in package.rglob("*.py"):
        relative = path.relative_to(package)
        if relative.parts[0] == "profile" or relative.name in ROOTS and len(relative.parts) == 1:
            continue
        if found := other_modules_importing_profile(path.read_text(encoding="utf-8")):
            offenders[relative.as_posix()] = found
    assert offenders == {}, "only app.profile.api may be imported by another module"


@pytest.mark.parametrize(
    "source",
    [
        "from app.profile.service import usable",
        "import app.profile.models",
        "from app.profile import service",
        "from app.profile.events import confirm",
    ],
)
def test_the_import_check_itself_catches_a_module_that_reaches_in(source):
    assert other_modules_importing_profile(source) != []
    assert other_modules_importing_profile("from app.profile.api import get_record") == []
    assert other_modules_importing_profile("from app.profile import api") == []
