"""The change log and the proposal queue (REC-05, CHT-03; plan 03-04): every applied change is listed with before,
after, source and time, and a proposed change waits for the candidate's confirmation and is answered exactly once.
"""

from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.profile import api, events, service
from app.profile.errors import AlreadyResolved, ConstraintNotEnforceable, InvalidField, RecordNotFound, StateConflict

TEXT = "Senior Backend Engineer, Northwind Logistics (Mar 2021 - Present)\nBuilt order pipelines in Python."
REMOTE = {"kind": "remote_policy", "arrangements": ["remote"], "statement": "Remote only"}


def one(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


def make_user(conn, name: str = "ada") -> UUID:
    return one(conn, "INSERT INTO users (email) VALUES (:e) RETURNING id", e=f"{name}-{uuid4().hex[:8]}@example.test")


def unconfirmed_fact(conn, user: UUID) -> UUID:
    document = one(
        conn,
        """INSERT INTO documents (user_id, content, mime_type, byte_size, sha256, status, extracted_text, parsed_at)
           VALUES (:u, :c, 'application/pdf', 100, :h, 'parsed', :t, now()) RETURNING id""",
        u=user,
        c=b"sealed",
        h=b"h" * 32,
        t=TEXT,
    )
    return one(
        conn,
        """INSERT INTO profile_facts (user_id, kind, statement, source, document_id, source_span, source_quote)
           VALUES (:u, 'role', :q, 'resume', :d, int4range(0, 25), :q) RETURNING id""",
        u=user,
        d=document,
        q=TEXT[0:25],
    )


def version(conn, user: UUID) -> int:
    return one(conn, "SELECT record_version FROM users WHERE id = :u", u=user)


def rows(conn, table: str, user: UUID) -> int:
    return one(conn, f"SELECT count(*) FROM {table} WHERE user_id = :u", u=user)


def log(conn, user: UUID, **kwargs) -> list[dict[str, Any]]:
    return events.list_events(conn, user, limit=100, **kwargs)[0]


def propose(conn, user: UUID, **kwargs) -> UUID:
    kwargs = {"origin": "chat", "entity": "constraint", "operation": "create", "after": REMOTE, **kwargs}
    return events.propose_change(conn, user, **kwargs)


@pytest.fixture
def user(conn) -> UUID:
    return make_user(conn)


# ---- the change log ------------------------------------------------------------------------------


def test_the_log_lists_every_change_with_before_after_source_and_time_newest_first(conn, user):
    fact = unconfirmed_fact(conn, user)
    service.confirm_fact(conn, user, fact)
    typed = service.add_fact(conn, user, "skill", "Kubernetes")
    service.correct_fact(conn, user, UUID(typed.row["id"]), "Kubernetes and Helm", reason="More exact")
    remote = service.set_constraint(conn, user, "remote_policy", {"arrangements": ["remote"]}, "Remote only")
    service.set_constraint(conn, user, "remote_policy", {"arrangements": ["hybrid"]}, "Hybrid")
    service.remove_constraint(conn, user, UUID(service.list_constraints(conn, user)[0]["id"]))
    pref = service.add_preference(conn, user, "industry", "Fintech", value="fintech", weight=1)
    service.change_preference(conn, user, UUID(pref.row["id"]), {"weight": 3})

    entries = log(conn, user)

    assert len(entries) == 8
    assert [e["record_version_after"] for e in entries] == [8, 7, 6, 5, 4, 3, 2, 1]  # newest first, one version each
    stamps = [e["created_at"] for e in entries]
    assert all(stamp.tzinfo is not None for stamp in stamps) and stamps == sorted(stamps, reverse=True)
    assert all(e["status"] == "applied" and e["source"] == "candidate_edit" for e in entries)
    by_version = {e["record_version_after"]: e for e in entries}
    confirmed = by_version[1]
    assert (confirmed["entity"], confirmed["operation"]) == ("fact", "confirm")
    assert (confirmed["before"]["status"], confirmed["after"]["status"]) == ("unconfirmed", "confirmed")
    assert confirmed["before"]["provenance"]["quote"] == TEXT[0:25] and confirmed["after"]["id"] == str(fact)
    corrected = by_version[3]
    assert (corrected["reason"], corrected["before"]["statement"], corrected["after"]["statement"]) == (
        "More exact",
        "Kubernetes",
        "Kubernetes and Helm",
    )
    assert (by_version[4]["before"], by_version[4]["after"]["id"]) == (None, remote.row["id"])
    replaced = by_version[5]
    assert (replaced["before"]["arrangements"], replaced["after"]["arrangements"]) == (["remote"], ["hybrid"])
    assert (by_version[6]["operation"], by_version[6]["after"]) == ("remove", None)
    assert (by_version[8]["before"]["weight"], by_version[8]["after"]["weight"]) == (1, 3)


def test_the_log_pages_with_a_cursor_and_filters_by_status(conn, user):
    for number in range(7):
        service.add_fact(conn, user, "skill", f"Skill {number}")
    everything = log(conn, user)

    seen, cursor, pages = [], None, 0
    while True:
        batch, cursor = events.list_events(conn, user, limit=3, cursor=cursor)
        seen += [e["id"] for e in batch]
        pages += 1
        if cursor is None:
            break
    assert pages == 3 and seen == [e["id"] for e in everything] and len(set(seen)) == 7

    assert len(log(conn, user, status="applied")) == 7
    assert log(conn, user, status="declined") == [] and log(conn, user, status="expired") == []
    with pytest.raises(InvalidField):
        log(conn, user, status="pending")


def test_one_persons_log_never_shows_another_persons_changes(conn, user):
    other = make_user(conn, "bob")
    service.add_fact(conn, other, "skill", "Kubernetes")
    assert log(conn, user) == []
    assert len(log(conn, other)) == 1


# ---- proposals -----------------------------------------------------------------------------------


def test_a_proposal_changes_nothing_until_the_candidate_confirms_it(conn, user):
    before = (version(conn, user), rows(conn, "constraints", user), rows(conn, "profile_facts", user))

    proposal = propose(conn, user, origin_ref="msg_01", reason="You said you want to work remotely")

    assert (version(conn, user), rows(conn, "constraints", user), rows(conn, "profile_facts", user)) == before
    assert log(conn, user) == []  # the log lists what happened, not what is waiting
    (waiting,) = log(conn, user, status="proposed")
    assert (waiting["id"], waiting["status"], waiting["origin"], waiting["source"]) == (
        proposal,
        "proposed",
        "chat",
        None,
    )
    assert waiting["record_version_after"] is None and waiting["reason"] == "You said you want to work remotely"
    assert (waiting["entity"], waiting["operation"], waiting["entity_id"], waiting["before"]) == (
        "constraint",
        "create",
        None,
        None,
    )
    assert waiting["after"]["arrangements"] == ["remote"] and waiting["after"]["salary_floor"] is None
    assert service.record_header(conn, user).open_proposals == 1


def test_confirming_a_proposal_applies_it_once_as_a_confirmed_proposal(conn, user):
    proposal = propose(conn, user, reason="You said you want to work remotely")

    applied = events.confirm(conn, user, proposal)

    assert (applied.entity, applied.row["kind"], applied.row["source"]) == (
        "constraint",
        "remote_policy",
        "confirmed_proposal",
    )
    assert applied.record_version == version(conn, user) == 1
    event = conn.execute(text("SELECT * FROM profile_events WHERE id = :i"), {"i": applied.event_id}).one()
    assert (event.event_type, event.source, event.proposal_event_id) == ("applied", "confirmed_proposal", proposal)
    assert event.reason == "You said you want to work remotely" and event.record_version_after == 1
    assert one(conn, "SELECT profile_event_id FROM constraints WHERE id = :i", i=UUID(applied.row["id"])) == event.id
    assert log(conn, user, status="proposed") == []  # answered
    (entry,) = log(conn, user)
    assert (entry["id"], entry["origin"], entry["source"]) == (applied.event_id, "chat", "confirmed_proposal")

    for answer in (lambda: events.confirm(conn, user, proposal), lambda: events.decline(conn, user, proposal)):
        with pytest.raises(AlreadyResolved) as refused:
            answer()
        assert refused.value.details == {"resolution": "applied"}
    assert (version(conn, user), rows(conn, "constraints", user)) == (1, 1)  # the repeats changed nothing


def test_declining_a_proposal_records_the_no_and_changes_nothing_else(conn, user):
    proposal = propose(conn, user)

    declined = events.decline(conn, user, proposal)

    assert (version(conn, user), rows(conn, "constraints", user)) == (0, 0)
    (entry,) = log(conn, user)
    assert (entry["id"], entry["status"], entry["proposal_event_id"], entry["origin"]) == (
        declined,
        "declined",
        proposal,
        "chat",
    )
    assert entry["after"]["arrangements"] == ["remote"] and entry["record_version_after"] is None
    assert log(conn, user, status="proposed") == []
    for answer in (lambda: events.confirm(conn, user, proposal), lambda: events.decline(conn, user, proposal)):
        with pytest.raises(AlreadyResolved) as refused:
            answer()
        assert refused.value.resolution == "declined"


def test_a_proposal_that_is_not_the_persons_does_not_exist_for_them(conn, user):
    other = make_user(conn, "bob")
    theirs = propose(conn, other)
    applied = service.add_fact(conn, other, "skill", "Kubernetes")  # an applied event is not a proposal either
    for event_id in (theirs, UUID(int=1), applied.event_id):
        for answer in (events.confirm, events.decline):
            with pytest.raises(RecordNotFound):
                answer(conn, user, event_id)
    assert version(conn, other) == 1 and len(log(conn, other, status="proposed")) == 1


def _fact(conn, user):
    return service.add_fact(conn, user, "skill", "Kubernetes").row["id"]


def _constraint(conn, user):
    return service.set_constraint(conn, user, "location", {"country_codes": ["IN"], "cities": ["pune"]}, "Pune").row[
        "id"
    ]


def _preference(conn, user):
    return service.add_preference(conn, user, "target_title", "A staff role", value="staff engineer", weight=2).row[
        "id"
    ]


# entity, operation, makes the row the proposal targets, the proposal's `after`, what the record must then show
PROPOSALS = {
    "fact create": ("fact", "create", None, {"kind": "skill", "statement": "Helm"}, lambda c, u: ["Helm"]),
    "fact update": (
        "fact",
        "update",
        _fact,
        {"statement": "Kubernetes and Helm"},
        lambda c, u: ["Kubernetes and Helm"],
    ),
    "fact remove": ("fact", "remove", _fact, None, lambda c, u: []),
    "constraint create": ("constraint", "create", None, REMOTE, lambda c, u: ["remote_policy"]),
    "constraint update": (
        "constraint",
        "update",
        _constraint,
        {"kind": "location", "country_codes": ["IN", "US"], "statement": "Anywhere in India or the US"},
        lambda c, u: ["location"],
    ),
    "constraint remove": ("constraint", "remove", _constraint, None, lambda c, u: []),
    "preference create": (
        "preference",
        "create",
        None,
        {"kind": "industry", "value": "fintech", "weight": 1, "statement": "Fintech"},
        lambda c, u: ["fintech"],
    ),
    "preference update": (
        "preference",
        "update",
        _preference,
        {"kind": "target_title", "value": "principal engineer", "weight": 3, "statement": "A principal role"},
        lambda c, u: ["principal engineer"],
    ),
    "preference remove": ("preference", "remove", _preference, None, lambda c, u: []),
}


def _shown(conn, user, entity: str) -> list[str]:
    """What the record shows for this kind of row now: fact statements, constraint kinds or preference values."""
    record = api.get_record(conn, user)
    if entity == "fact":
        return [f["statement"] for f in record["facts"]]
    if entity == "constraint":
        return [c["kind"] for c in record["constraints"]]
    return [p["value"] for p in record["preferences"]]


@pytest.mark.parametrize("name", list(PROPOSALS))
def test_every_kind_of_proposal_is_applied_only_by_its_confirmation(conn, user, name):
    entity, operation, make_target, after, shown = PROPOSALS[name]
    target = None if make_target is None else UUID(make_target(conn, user))
    start_version, shown_before = version(conn, user), _shown(conn, user, entity)

    proposal = events.propose_change(
        conn, user, origin="onboarding", entity=entity, operation=operation, after=after, entity_id=target
    )
    assert (version(conn, user), _shown(conn, user, entity)) == (start_version, shown_before)

    applied = events.confirm(conn, user, proposal)

    assert (applied.entity, applied.record_version) == (entity, start_version + 1)
    assert _shown(conn, user, entity) == shown(conn, user)
    (entry, *_) = log(conn, user)
    assert (entry["entity"], entry["operation"], entry["source"], entry["proposal_event_id"]) == (
        entity,
        operation,
        "confirmed_proposal",
        proposal,
    )


def test_a_confirmed_dealbreaker_proposal_is_saved_soft_and_offers_its_constraint(conn, user):
    proposal = events.propose_change(
        conn,
        user,
        origin="chat",
        entity="preference",
        operation="create",
        after={"kind": "untyped_dealbreaker", "statement": "No remote work"},
    )
    applied = events.confirm(conn, user, proposal)
    assert (applied.row["weight"], applied.row["notice"]) == (-3, "not_guaranteed")
    assert applied.row["suggested_constraint"]["arrangements"] == ["onsite", "hybrid"]
    assert rows(conn, "constraints", user) == 0


@pytest.mark.parametrize(
    ("changes", "refusal"),
    [
        ({"origin": "telepathy"}, InvalidField),
        ({"entity": "document"}, InvalidField),
        ({"operation": "confirm"}, InvalidField),  # confirming a fact is the candidate's own act, not a proposal
        ({"operation": "update"}, InvalidField),  # an update names its row
        ({"operation": "remove"}, InvalidField),  # a removal names its row
        ({"entity_id": uuid4()}, InvalidField),  # a create names none
        ({"after": None}, InvalidField),
        ({"after": {**REMOTE, "note": "x"}}, InvalidField),  # nothing extra is allowed through
        ({"after": {"kind": "industry_exclusion", "statement": "No gambling"}}, ConstraintNotEnforceable),
        ({"after": {**REMOTE, "arrangements": ["anywhere"]}}, InvalidField),
        ({"origin_ref": "free text with spaces"}, InvalidField),  # an id, never text
        ({"reason": "x" * 501}, InvalidField),
        ({"entity": "fact", "after": {"kind": "skill", "statement": "Helm", "attributes": {"a": 1}}}, InvalidField),
        ({"entity": "preference", "after": {"kind": "industry", "statement": "Fintech", "weight": 1}}, InvalidField),
    ],
)
def test_a_malformed_proposal_is_refused_and_stores_nothing(conn, user, changes, refusal):
    before = rows(conn, "profile_events", user)
    with pytest.raises(refusal):
        propose(conn, user, **changes)
    assert rows(conn, "profile_events", user) == before


def test_a_proposal_to_change_or_remove_must_name_a_live_row_of_the_person(conn, user):
    other = make_user(conn, "bob")
    foreign = UUID(_constraint(conn, other))
    mine = UUID(_constraint(conn, user))
    india = {"kind": "location", "country_codes": ["IN"], "statement": "India"}
    for operation, target, after in (("update", foreign, india), ("remove", foreign, None), ("update", uuid4(), india)):
        with pytest.raises(RecordNotFound):
            propose(conn, user, operation=operation, entity_id=target, after=after)
    with pytest.raises(InvalidField) as refused:  # a constraint's kind never changes
        propose(conn, user, operation="update", entity_id=mine, after={**REMOTE})
    assert refused.value.issue == "cannot_change"
    assert rows(conn, "profile_events", user) == 1  # only the constraint's own creation


def test_a_proposal_whose_row_changed_since_can_no_longer_be_applied_but_can_be_declined(conn, user):
    constraint = UUID(_constraint(conn, user))
    update = propose(
        conn,
        user,
        operation="update",
        entity_id=constraint,
        after={"kind": "location", "country_codes": ["US"], "statement": "US"},
    )
    service.remove_constraint(conn, user, constraint)  # the row is gone before the candidate answers
    start = version(conn, user)
    with pytest.raises(RecordNotFound):
        events.confirm(conn, user, update)
    assert version(conn, user) == start and len(log(conn, user, status="proposed")) == 1  # still open
    events.decline(conn, user, update)
    assert log(conn, user, status="proposed") == []

    fact = UUID(_fact(conn, user))
    removal = propose(conn, user, entity="fact", operation="remove", entity_id=fact, after=None)
    service.reject_fact(conn, user, fact)
    with pytest.raises(StateConflict):
        events.confirm(conn, user, removal)


def test_api_confirm_hands_other_modules_ids_and_never_a_row(conn, user):
    proposal = propose(conn, user, entity="fact", after={"kind": "skill", "statement": "Helm"})
    confirmed = api.confirm(conn, user, proposal)
    assert isinstance(confirmed, api.Confirmed)
    assert (confirmed.entity, confirmed.record_version) == ("fact", 1)
    assert str(confirmed.entity_id) == one(conn, "SELECT id::text FROM profile_facts WHERE user_id = :u", u=user)
    retire = propose(conn, user, entity="fact", operation="remove", entity_id=confirmed.entity_id, after=None)
    removed = api.confirm(conn, user, retire)  # a retired fact's content does not come back through the api
    assert {*vars(removed)} == {"entity", "entity_id", "record_version"}
