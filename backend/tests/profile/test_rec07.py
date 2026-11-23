"""REC-07: a protected characteristic is never stored in the record (plan 03-04, threat T-3-14).

Every field in which a candidate (or a chat agent on their behalf) can put free text is tried with a sentence for each
category the screen knows. The operation must be refused with the category, and not one row of any table may be written.
Owner decision G4: only the structured record is screened. Raw resume text stays as written, so documents are not.
"""

import contextlib
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.profile import api, events, protected, service
from app.profile.errors import ConstraintNotEnforceable, ProtectedCharacteristic

# One sentence per category in the label style the narrow screen recognizes (plan 03-02), and one in the plain style
# only the wide screen recognizes (the Phase 3 verification, B1): what a person types is screened with the wide one.
PHRASES = {
    "age": "Date of birth: 04/03/1998",
    "marital_status": "Marital status: single",
    "religion": "Religion: Jainism",
    "gender": "Gender: female",
    "sexual_orientation": "my sexual orientation",
    "nationality": "Nationality: Indian",
    "ethnicity": "Ethnicity: group x",
    "caste": "my caste",
    "disability": "I have a disability",
    "pregnancy": "I am pregnant",
}
PLAIN = {
    "age": "I am 45 years old",
    "marital_status": "Married with two kids",
    "family_status": "I have two kids",
    "religion": "I am a practising Muslim",
    "gender": "I am a woman",
    "sexual_orientation": "I am gay",
    "nationality": "my nationality",
    "ethnicity": "Black woman engineer",
    "caste": "I am a Brahmin",
    "disability": "I use a wheelchair",
    "health": "I have diabetes",
    "pregnancy": "I'm expecting a baby",
}
WRITTEN_TABLES = (
    "documents, profile_events, profile_facts, constraints, preferences, audit_log, outbox, queue_jobs, llm_calls, "
    "idempotency_keys, consents, data_requests"
)
INDIA = {"country_codes": ["IN"]}


def one(conn, sql: str, **params):
    return conn.execute(text(sql), params).scalar_one()


class Ctx:
    """A person with one confirmed fact, one location constraint and one preference, to change."""

    def __init__(self, conn) -> None:
        self.id = one(
            conn, "INSERT INTO users (email) VALUES (:e) RETURNING id", e=f"ada-{uuid4().hex[:8]}@example.test"
        )
        self.fact = UUID(service.add_fact(conn, self.id, "skill", "Kubernetes").row["id"])
        self.constraint = UUID(service.set_constraint(conn, self.id, "location", INDIA, "India").row["id"])
        pref = service.add_preference(conn, self.id, "industry", "Fintech", value="fintech", weight=1)
        self.preference = UUID(pref.row["id"])


def propose(conn, ctx, entity: str, after: dict, **more) -> UUID:
    return events.propose_change(conn, ctx.id, origin="chat", entity=entity, operation="create", after=after, **more)


# field label -> (what it puts the text into, the field the refusal names)
FIELDS = {
    "fact statement (add)": (lambda c, x, t: service.add_fact(c, x.id, "skill", t), "statement"),
    "fact statement (correct)": (lambda c, x, t: service.correct_fact(c, x.id, x.fact, t), "statement"),
    "fact reason (correct)": (
        lambda c, x, t: service.correct_fact(c, x.id, x.fact, "Better words", reason=t),
        "reason",
    ),
    "fact reason (reject)": (lambda c, x, t: service.reject_fact(c, x.id, x.fact, reason=t), "reason"),
    "constraint statement": (
        lambda c, x, t: service.set_constraint(c, x.id, "remote_policy", {"arrangements": ["remote"]}, t),
        "statement",
    ),
    "constraint statement (change)": (
        lambda c, x, t: service.change_constraint(c, x.id, x.constraint, {"statement": t}),
        "statement",
    ),
    "constraint statement (kind not enforceable)": (
        lambda c, x, t: service.set_constraint(c, x.id, "industry_exclusion", {}, t),
        "statement",
    ),
    "constraint company_names": (
        lambda c, x, t: service.set_constraint(
            c, x.id, "excluded_company", {"company_names": ["Acme", t]}, "Not these"
        ),
        "company_names",
    ),
    "constraint cities": (
        lambda c, x, t: service.set_constraint(c, x.id, "location", {**INDIA, "cities": [t]}, "Somewhere"),
        "cities",
    ),
    "preference statement": (lambda c, x, t: service.add_preference(c, x.id, "other", t, weight=1), "statement"),
    "preference value": (
        lambda c, x, t: service.add_preference(c, x.id, "industry", "Fintech", value=t, weight=1),
        "value",
    ),
    "preference statement (change)": (
        lambda c, x, t: service.change_preference(c, x.id, x.preference, {"statement": t}),
        "statement",
    ),
    "preference value (change)": (
        lambda c, x, t: service.change_preference(c, x.id, x.preference, {"value": t}),
        "value",
    ),
    "dealbreaker statement": (lambda c, x, t: service.add_preference(c, x.id, "untyped_dealbreaker", t), "statement"),
    "proposal reason": (
        lambda c, x, t: propose(c, x, "fact", {"kind": "skill", "statement": "Helm"}, reason=t),
        "reason",
    ),
    "proposal fact statement": (lambda c, x, t: propose(c, x, "fact", {"kind": "skill", "statement": t}), "statement"),
    "proposal constraint statement": (
        lambda c, x, t: propose(
            c, x, "constraint", {"kind": "remote_policy", "arrangements": ["remote"], "statement": t}
        ),
        "statement",
    ),
    "proposal constraint company_names": (
        lambda c, x, t: propose(
            c, x, "constraint", {"kind": "excluded_company", "company_names": [t], "statement": "Not these"}
        ),
        "company_names",
    ),
    "proposal preference statement": (
        lambda c, x, t: propose(c, x, "preference", {"kind": "other", "weight": 1, "statement": t}),
        "statement",
    ),
    "proposal preference value": (
        lambda c, x, t: propose(
            c, x, "preference", {"kind": "industry", "weight": 1, "value": t, "statement": "Fintech"}
        ),
        "value",
    ),
}


def rows_everywhere(conn, user: UUID) -> dict:
    counts = {t: one(conn, f"SELECT count(*) FROM {t}") for t in WRITTEN_TABLES.split(", ")}
    return {**counts, "version": one(conn, "SELECT record_version FROM users WHERE id = :u", u=user)}


def test_the_table_has_a_sentence_for_every_category_the_screen_knows_and_each_is_recognized():
    assert set(PLAIN) == set(protected.CATEGORIES)  # a category added to the screen needs a row here
    assert {category: protected.screen_typed(phrase) for category, phrase in PLAIN.items()} == {c: c for c in PLAIN}
    assert {category: protected.screen_typed(phrase) for category, phrase in PHRASES.items()} == {c: c for c in PHRASES}


@pytest.mark.parametrize("field", sorted(FIELDS))
@pytest.mark.parametrize(
    ("category", "sentence"),
    [(c, PHRASES[c]) for c in sorted(PHRASES)] + [(c, PLAIN[c]) for c in sorted(PLAIN)],
    ids=[f"label-{c}" for c in sorted(PHRASES)] + [f"plain-{c}" for c in sorted(PLAIN)],
)
def test_a_protected_characteristic_in_any_free_text_field_is_refused_and_nothing_is_written(
    conn, field, category, sentence
):
    ctx = Ctx(conn)
    before = rows_everywhere(conn, ctx.id)
    operation, named_field = FIELDS[field]

    with pytest.raises(ProtectedCharacteristic) as refused:
        operation(conn, ctx, sentence)

    assert (refused.value.category, refused.value.field) == (category, named_field)
    assert (refused.value.status, refused.value.code) == (422, "validation_failed")
    assert refused.value.details == {"fields": [{"field": named_field, "issue": "protected_characteristic"}]}
    assert sentence not in repr(vars(refused.value)) + str(refused.value)  # a code, never the text
    assert rows_everywhere(conn, ctx.id) == before  # no row of any table, and no version


@pytest.mark.parametrize(
    "harmless",
    [
        "Chief Age Officer tooling",
        "Authorized to work in India",
        "gender pay gap dashboard",
        "Led the Catholic Charities website rebuild",
        "Managed a team of 40",
    ],
)
@pytest.mark.parametrize("field", sorted(FIELDS))
def test_the_screen_does_not_refuse_ordinary_words_that_merely_contain_a_keyword(conn, field, harmless):
    """Work authorization is a hard constraint, not a protected characteristic, and job words are not people words."""
    operation, _ = FIELDS[field]
    with contextlib.suppress(ConstraintNotEnforceable):  # a kind the matcher cannot enforce refuses any statement
        operation(conn, Ctx(conn), harmless)


def test_a_protected_characteristic_is_refused_before_the_kind_is_looked_at(conn):
    """An unsupported constraint kind would be offered back as a preference, which would echo the text. It is not."""
    ctx = Ctx(conn)
    with pytest.raises(ProtectedCharacteristic):
        service.set_constraint(conn, ctx.id, "industry_exclusion", {}, PHRASES["religion"])


def test_raw_resume_text_stays_as_written_and_only_the_structured_record_is_screened(conn):
    """Owner decision G4: the person's own document is exported and deleted with the account, not rewritten."""
    ctx = Ctx(conn)
    body = "Asha Example\nDate of birth: 04/03/1998\nSenior Backend Engineer, Globex Example Ltd"
    conn.execute(
        text(
            """INSERT INTO documents (user_id, content, mime_type, byte_size, sha256, status, extracted_text, parsed_at)
               VALUES (:u, :c, 'application/pdf', 100, :h, 'parsed', :t, now())"""
        ),
        {"u": ctx.id, "c": b"sealed", "h": b"h" * 32, "t": body},
    )
    assert one(conn, "SELECT extracted_text FROM documents WHERE user_id = :u", u=ctx.id) == body
    record = api.get_record(conn, ctx.id)  # and the record a consumer reads holds none of it
    assert "04/03/1998" not in repr(record) and "Date of birth" not in repr(record)
