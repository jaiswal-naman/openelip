"""The canary test (TRU-02; ARCHITECTURE 8.4, test 3; DATA_MODEL 7.2).

One person is planted with a unique canary in every free-text, jsonb, bytea and array field the product has, a sealed
file and a finished export (a ZIP of everything about them, sealed) included. They delete their account through the
API, the worker erases them, and every text, jsonb, uuid, array and bytea column of every table is scanned for the
person's id, their address and the canary. Sealed files are opened with the instance's data keys and ZIPs are
unpacked first, because a copy of the person that is only encrypted is still a copy. Nothing may be found, except
the bare id in data_requests.subject_user_id, which is kept on purpose.

The scan is itself tested: before the deletion it must find the canary in every place it was planted, and after a
deliberate leftover it must find that, so a scan that went blind would fail here rather than pass.

A table a later phase adds must be seeded here, or listed in SEED_SKIPPED with the reason: see
test_every_cascade_table_is_seeded_or_skipped_with_a_reason.
"""

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import InvalidToken
from sqlalchemy import text

from app.platform import sessions
from app.profile import documents
from app.trust import api, privacy

CANARY = f"CANARY-{uuid4()}"
ADDRESS = "canary.person@mail.test"
SCANNED_TYPES = ["text", "character varying", "character", "jsonb", "json", "uuid", "ARRAY", "bytea"]
NOT_SCANNED = {("data_requests", "subject_user_id")}  # the bare id, kept on purpose so a restore can be erased again

# Registered cascade tables the canary plants nothing in, each with the reason. Empty: at this revision every one has a
# seed. A phase that adds a cascade table seeds it in seed_person below, or lists it here and says why it cannot.
SEED_SKIPPED: dict[str, str] = {}

# Where the canary is planted, as (table, column): the scan must find each one before the deletion.
PLANTED = {
    ("users", "display_name"),
    ("users", "email"),
    ("sessions", "user_id"),
    ("login_codes", "email"),
    ("llm_calls", "user_id"),
    ("outbox", "payload"),
    ("queue_jobs", "user_id"),  # the parse job of an upload: made by the real upload path, so it names the person
    ("idempotency_keys", "response_body"),
    ("consents", "text_version"),
    ("audit_log", "details"),
    ("documents", "content"),  # sealed: found only by opening it
    ("documents", "extracted_text"),
    ("documents", "original_filename"),
    ("profile_events", "before"),
    ("profile_events", "after"),
    ("profile_events", "reason"),
    ("profile_facts", "statement"),
    ("profile_facts", "source_quote"),
    ("profile_facts", "attributes"),
    ("constraints", "statement"),
    ("constraints", "company_names"),  # an array
    ("preferences", "value"),
    ("preferences", "statement"),
    ("match_decisions", "summary"),
    ("match_decisions", "change_note"),
    ("match_evidence", "note"),
    ("feedback_events", "comment"),
    ("org_members", "invited_email"),  # the address the person was invited by
    ("introductions", "share_preview"),
    ("introductions", "employer_answer_reason"),
    ("shortlist_entries", "shared_payload"),
    ("eval_items", "expected"),
    ("eval_items", "actual"),
}
AFTER_THE_EXPORT = {("data_requests", "export_archive")}  # a sealed ZIP of everything: the copy most easily forgotten


@dataclass(frozen=True)
class Person:
    user: UUID
    email: str

    @property
    def needles(self) -> list[str]:
        return [str(self.user), CANARY, self.email]


def as_json(value) -> str | None:
    return None if value is None else json.dumps(value)


def a_docx(body: str) -> bytes:
    """The smallest file the upload path takes for a DOCX: a ZIP that holds word/document.xml."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", f"<w:document>{body}</w:document>")
    return out.getvalue()


def seed_person(engine) -> Person:
    """One person, with a row in every table that holds something about them, planted with plain SQL."""
    pdf = f"%PDF-1.7 invented resume {CANARY}".encode()
    statement = f"Senior engineer {CANARY} at Initech"
    vector = "[" + ",".join(["0.5"] * 384) + "]"
    with engine.begin() as c:

        def run(sql: str, **params):
            return c.execute(text(sql), params)

        user = run(
            "INSERT INTO users (email, display_name) VALUES (:e, :n) RETURNING id", e=ADDRESS, n=f"Ada {CANARY}"
        ).scalar_one()
        run(
            """UPDATE users SET record_embedding = CAST(:v AS vector), record_embedding_model = 'test-model'
               WHERE id = :u""",
            v=vector,
            u=user,
        )
        for _ in range(2):
            run(
                "INSERT INTO sessions (user_id, token_sha256, expires_at) VALUES (:u, :t, now() + interval '1 day')",
                u=user,
                t=uuid4().bytes * 2,
            )
        run(
            "INSERT INTO login_codes (email, code_hmac, expires_at) VALUES (:e, :h, now() + interval '10 minutes')",
            e=ADDRESS.upper(),  # another spelling: erase_user matches on lower(email)
            h=b"h" * 32,
        )
        model = run(
            """INSERT INTO model_registry (provider, model_name, kind, is_local, retention_note)
               VALUES ('local', :m, 'chat', true, 'test') RETURNING id""",
            m=f"model-{uuid4().hex}",
        ).scalar_one()
        run(
            """INSERT INTO llm_calls (task, prompt_version, model_id, outcome, user_id, latency_ms)
               VALUES ('ping', 'v1', :m, 'ok', :u, 5)""",
            m=model,
            u=user,
        )
        run(
            "INSERT INTO outbox (dedupe_key, user_id, template, payload) VALUES (:k, :u, 'brief_v1', :p)",
            k=f"brief:{uuid4().hex}",
            u=user,
            p=json.dumps({"note": CANARY}),
        )
        run(
            """INSERT INTO idempotency_keys (user_id, key, method, path, request_sha256, response_status, response_body)
               VALUES (:u, 'seed-idempotency-key', 'POST', '/v1/me/facts', :h, 201, :b)""",
            u=user,
            h=b"h" * 32,
            b=json.dumps({"data": {"echo": CANARY}}),
        )
        api.grant_consent(c, user, "terms", f"v-{CANARY}")  # a consent row and an audit row about them
        run(
            """INSERT INTO audit_log (event_type, actor_type, actor_user_id, subject_user_id, entity_table, entity_id,
                                      details)
               VALUES ('seed.canary_planted', 'candidate', :u, :u, 'users', :i, :d)""",
            u=user,
            i=str(user),
            d=json.dumps({"note": CANARY}),
        )
        eval_run = run(
            """INSERT INTO eval_runs (suite, git_sha, status, metrics, finished_at, is_gate_passed)
               VALUES ('harness_smoke', 'seed', 'passed', '{}', now(), true) RETURNING id"""
        ).scalar_one()
        run(
            """INSERT INTO eval_items (eval_run_id, item_key, source_user_id, expected, actual, is_passed)
               VALUES (:r, 'seed-item', :u, :e, :a, true)""",
            r=eval_run,
            u=user,
            e=json.dumps({"text": CANARY}),
            a=json.dumps({"text": CANARY}),
        )
        document = run(
            """INSERT INTO documents (user_id, content, original_filename, mime_type, byte_size, sha256, status,
                                      extracted_text, parsed_at)
               VALUES (:u, :c, :n, 'application/pdf', :s, :h, 'parsed', :t, now()) RETURNING id""",
            u=user,
            c=sessions.seal_bytes(pdf),
            n=f"resume {CANARY}.pdf",
            s=len(pdf),
            h=hashlib.sha256(pdf).digest(),
            t=statement,
        ).scalar_one()
        # The second file goes in through the upload path itself (sealed, with its parse job), because that is what
        # writes queue_jobs.user_id in production: a job planted by hand with a user id would pass whatever the code
        # does. It is never parsed (the job is not due for a day, so the worker leaves it alone) and holds the canary
        # in its sealed bytes only; the erasure must remove the job as well as the file.
        documents.store_document(c, user, f"upload {CANARY}.docx", a_docx(f"docx {CANARY}"))
        run("UPDATE queue_jobs SET run_at = now() + interval '1 day' WHERE user_id = :u", u=user)

        def event(entity: str, operation: str, before, after) -> UUID:
            return run(
                """INSERT INTO profile_events (user_id, event_type, entity, operation, before, after, source, reason,
                                              record_version_after)
                   VALUES (:u, 'applied', :e, :o, CAST(:b AS jsonb), CAST(:a AS jsonb), 'candidate_edit', :r, 1)
                   RETURNING id""",
                u=user,
                e=entity,
                o=operation,
                b=as_json(before),
                a=as_json(after),
                r=f"reason {CANARY}",
            ).scalar_one()

        fact_event = event("fact", "create", None, {"statement": statement})
        constraint_event = event("constraint", "create", None, {"kind": "excluded_company", "note": CANARY})
        event("preference", "update", {"note": f"before {CANARY}"}, {"note": f"after {CANARY}"})
        run(
            """INSERT INTO profile_facts (user_id, kind, statement, attributes, source, document_id, source_span,
                                          source_quote)
               VALUES (:u, 'role', :s, :a, 'resume', :d, int4range(0, :n), :s)""",
            u=user,
            s=statement,
            a=json.dumps({"employer": CANARY}),
            d=document,
            n=len(statement),
        )
        run(
            """INSERT INTO profile_facts (user_id, kind, statement, attributes, source, profile_event_id)
               VALUES (:u, 'skill', :s, :a, 'candidate_edit', :e)""",
            u=user,
            s=f"Kubernetes {CANARY}",
            a=json.dumps({"level": CANARY}),
            e=fact_event,
        )
        run(
            """INSERT INTO constraints (user_id, kind, statement, company_names, source, profile_event_id)
               VALUES (:u, 'excluded_company', :s, CAST(:c AS text[]), 'candidate_edit', :e)""",
            u=user,
            s=f"Not {CANARY}",
            c=[f"Company {CANARY}"],
            e=constraint_event,
        )
        run(
            """INSERT INTO preferences (user_id, kind, value, weight, statement, source, profile_event_id)
               VALUES (:u, 'other', :v, 1, :s, 'candidate_edit', :e)""",
            u=user,
            v=f"value {CANARY}",
            s=f"I would like {CANARY}",
            e=constraint_event,
        )
        # The match and brief tables (plan 04-01). The job and its requirement are public index rows with no canary; the
        # decision, its evidence, the feedback and the brief are about the person.
        source = run(
            """INSERT INTO job_sources (ats, token, company_name)
               VALUES ('lever', 'canary-board', 'Initech') RETURNING id"""
        ).scalar_one()
        job = run(
            """INSERT INTO jobs (source_id, external_id, title, company_name, description, apply_url, content_hash,
                                 dedupe_key, requirements_version)
               VALUES (:s, 'canary-job', 'Engineer', 'Initech', 'Python required.', 'https://example.test/apply', 'h',
                       'canary-job', 1) RETURNING id""",
            s=source,
        ).scalar_one()
        requirement = run(
            """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                             prompt_version)
               VALUES (:j, 1, 1, 'required', 'skill', 'Python', int4range(0, 6), 'seed-v1') RETURNING id""",
            j=job,
        ).scalar_one()
        match_run = run(
            """INSERT INTO match_runs (trigger, user_id, local_date, status, record_version, ranker_version,
                                       finished_at)
               VALUES ('nightly', :u, current_date, 'completed', 1, 'seed', now()) RETURNING id""",
            u=user,
        ).scalar_one()
        decision = run(
            """INSERT INTO match_decisions (match_run_id, user_id, job_id, job_content_version, local_date, outcome,
                                            reason_codes, rank_position, summary, change_note)
               VALUES (:r, :u, :j, 1, current_date, 'shown', '{top_ranked,material_change}', 1, :s, :n)
               RETURNING id""",
            r=match_run,
            u=user,
            j=job,
            s=f"Summary {CANARY}",
            n=f"Changed {CANARY}",
        ).scalar_one()
        run(
            """INSERT INTO match_evidence (match_decision_id, user_id, job_requirement_id, verdict, note)
               VALUES (:d, :u, :q, 'no_evidence', :n)""",
            d=decision,
            u=user,
            q=requirement,
            n=f"Note {CANARY}",
        )
        run(
            """INSERT INTO feedback_events (user_id, job_id, match_decision_id, kind, dismiss_reason, comment)
               VALUES (:u, :j, :d, 'dismiss', 'other', :c)""",
            u=user,
            j=job,
            d=decision,
            c=f"Comment {CANARY}",
        )
        brief = run(
            """INSERT INTO briefs (user_id, local_date, channel, match_run_id)
               VALUES (:u, current_date, 'email', :r) RETURNING id""",
            u=user,
            r=match_run,
        ).scalar_one()
        run(
            """INSERT INTO brief_items (brief_id, position, user_id, kind, match_decision_id)
               VALUES (:b, 1, :u, 'match', :d)""",
            b=brief,
            u=user,
            d=decision,
        )
        # The hiring tables (plan 06-01). The person owns one employer organization, and a second one, "Canary Hiring
        # Demo", asked them about an open role: the ask, its consent, the employer's answer, the shortlist entry and a
        # signal all name them. Every guard of the schema runs on these rows, so each is built the way the product
        # builds it.
        own_organization = run(
            "INSERT INTO organizations (name, created_by) VALUES ('Canary Employer Demo', :u) RETURNING id", u=user
        ).scalar_one()
        run(
            """INSERT INTO org_members (organization_id, user_id, invited_email, role, status, joined_at)
               VALUES (:o, :u, :e, 'owner', 'active', now())""",
            o=own_organization,
            u=user,
            e=ADDRESS,
        )
        hiring = run("INSERT INTO organizations (name) VALUES ('Canary Hiring Demo') RETURNING id").scalar_one()
        direct = run(
            """INSERT INTO job_sources (ats, token, company_name, organization_id, is_active)
               VALUES ('direct', 'canary-hiring', 'Canary Hiring Demo', :o, false) RETURNING id""",
            o=hiring,
        ).scalar_one()
        posting = "SQL is required."
        role_job = run(
            """INSERT INTO jobs (source_id, external_id, title, company_name, description, apply_url, content_hash,
                                 dedupe_key, requirements_version, is_listed)
               VALUES (:s, 'canary-role', 'Platform engineer', 'Canary Hiring Demo', :d, 'https://example.test/apply',
                       'h', 'direct:canary-role', 1, false) RETURNING id""",
            s=direct,
            d=posting,
        ).scalar_one()
        job_requirement = run(
            """INSERT INTO job_requirements (job_id, content_version, ordinal, importance, kind, text, span,
                                             prompt_version)
               VALUES (:j, 1, 1, 'required', 'skill', :t, int4range(0, :n), 'employer-confirmed') RETURNING id""",
            j=role_job,
            t=posting,
            n=len(posting),
        ).scalar_one()
        role = run(
            """INSERT INTO roles (organization_id, title, description, status, salary_min, salary_max, salary_currency,
                                  requirements_confirmed_at, job_id, opened_at)
               VALUES (:o, 'Platform engineer', :d, 'open', 90000, 130000, 'USD', now(), :j, now()) RETURNING id""",
            o=hiring,
            d=posting,
            j=role_job,
        ).scalar_one()
        run(
            """INSERT INTO role_requirements (role_id, organization_id, ordinal, importance, kind, text, span,
                                              is_confirmed)
               VALUES (:r, :o, 1, 'required', 'skill', :t, int4range(0, :n), true)""",
            r=role,
            o=hiring,
            t=posting,
            n=len(posting),
        )
        role_run = run(
            """INSERT INTO match_runs (trigger, role_id, local_date, status, ranker_version, finished_at)
               VALUES ('role', :r, current_date, 'completed', 'seed', now()) RETURNING id""",
            r=role,
        ).scalar_one()
        role_decision = run(
            """INSERT INTO match_decisions (match_run_id, user_id, job_id, job_content_version, local_date, outcome,
                                            reason_codes, summary)
               VALUES (:r, :u, :j, 1, current_date, 'introduced', '{selected_for_role}', 'Meets the required item.')
               RETURNING id""",
            r=role_run,
            u=user,
            j=role_job,
        ).scalar_one()
        run(  # the evidence a shown or introduced decision must carry by the time it commits
            """INSERT INTO match_evidence (match_decision_id, user_id, job_requirement_id, verdict)
               VALUES (:d, :u, :q, 'no_evidence')""",
            d=role_decision,
            u=user,
            q=job_requirement,
        )
        api.grant_consent(c, user, "open_to_introductions", f"v-{CANARY}")  # the consent the ask checks
        preview = {
            "on_accept": {"name": f"Ada {CANARY}", "headline": statement, "evidence": [], "facts": []},
            "on_employer_yes": {"email": ADDRESS},
        }
        introduction = run(
            """INSERT INTO introductions (organization_id, role_id, user_id, match_decision_id, share_preview)
               VALUES (:o, :r, :u, :d, CAST(:p AS jsonb)) RETURNING id""",
            o=hiring,
            r=role,
            u=user,
            d=role_decision,
            p=json.dumps(preview),
        ).scalar_one()
        run(  # accepting needs the person's consent for this employer and role
            """INSERT INTO consents (user_id, purpose, action, text_version, organization_id, role_id)
               VALUES (:u, 'introduction', 'granted', 'v1', :o, :r)""",
            u=user,
            o=hiring,
            r=role,
        )
        run("UPDATE introductions SET status = 'accepted' WHERE id = :i", i=introduction)
        run(
            "UPDATE introductions SET employer_answer = 'yes', employer_answer_reason = :r WHERE id = :i",
            r=f"Reason {CANARY}",
            i=introduction,
        )
        run(
            """INSERT INTO shortlist_entries (organization_id, role_id, introduction_id, user_id, shared_payload,
                                              contact_released_at)
               SELECT organization_id, role_id, id, user_id, share_preview, now() FROM introductions WHERE id = :i""",
            i=introduction,
        )
        run(
            """INSERT INTO interest_signals (sender_side, user_id, organization_id, role_id, sent_by, week_start, slot)
               VALUES ('candidate', :u, :o, :r, :u, date_trunc('week', now() AT TIME ZONE 'UTC')::date, 1)""",
            u=user,
            o=hiring,
            r=role,
        )
    return Person(user, ADDRESS)


# ---- the scan -----------------------------------------------------------------------------------------


def leaks_in_bytes(data: bytes, needles: list[str]) -> bool:
    """Whether the needles are in these bytes as they are, or in what they open to: a sealed file (opened with the data
    keys) and then a ZIP (unpacked, member names and contents)."""
    wanted = [needle.lower().encode() for needle in needles]
    if any(needle in data.lower() for needle in wanted):
        return True
    try:
        plain = sessions.open_sealed_bytes(data)
    except InvalidToken:
        return False
    if any(needle in plain.lower() for needle in wanted):
        return True
    if not zipfile.is_zipfile(io.BytesIO(plain)):
        return False
    with zipfile.ZipFile(io.BytesIO(plain)) as archive:
        return any(
            needle in (name + "\n").lower().encode() + archive.read(name).lower()
            for name in archive.namelist()
            for needle in wanted
        )


def scan(conn, needles: list[str]) -> set[tuple[str, str]]:
    """(table, column) of every place in every table of public and langgraph that holds a needle."""
    quote = conn.dialect.identifier_preparer.quote
    columns = conn.execute(
        text(
            """SELECT c.table_schema, c.table_name, c.column_name, c.data_type
               FROM information_schema.columns c
               JOIN information_schema.tables t ON t.table_schema = c.table_schema AND t.table_name = c.table_name
               WHERE t.table_type = 'BASE TABLE' AND c.table_schema IN ('public', 'langgraph')
                 AND c.data_type = ANY(:types) ORDER BY 1, 2, 3"""
        ),
        {"types": SCANNED_TYPES},
    ).all()
    found = set()
    for schema, table, column, data_type in columns:
        if (table, column) in NOT_SCANNED:
            continue
        where = f"{quote(schema)}.{quote(table)}"
        if data_type == "bytea":
            values = conn.execute(
                text(f"SELECT {quote(column)} FROM {where} WHERE {quote(column)} IS NOT NULL")
            ).scalars()
            hit = any(leaks_in_bytes(bytes(value), needles) for value in values)
        else:
            hit = conn.execute(
                text(
                    f"""SELECT EXISTS (SELECT 1 FROM {where}
                        WHERE EXISTS (SELECT 1 FROM unnest(CAST(:needles AS text[])) AS n(x)
                                      WHERE position(lower(n.x) IN lower({quote(column)}::text)) > 0))"""
                ),
                {"needles": needles},
            ).scalar_one()
        if hit:
            found.add((table if schema == "public" else f"{schema}.{table}", column))
    return found


def cascade_tables() -> list[str]:
    return sorted(
        table for tables in privacy.registry().values() for table, effect in tables.items() if effect == "cascade"
    )


def rows_of(conn, table: str, user: UUID) -> int:
    """How many rows of this table are the person's: by its keys to users, or all of them if a parent leads to it."""
    if table == "users":
        return conn.execute(text("SELECT count(*) FROM users WHERE id = :u"), {"u": user}).scalar_one()
    columns = (
        conn.execute(
            text(
                """SELECT a.attname FROM pg_constraint c
                   JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1]
                   WHERE c.contype = 'f' AND c.confrelid = 'public.users'::regclass
                     AND c.conrelid = CAST(:t AS regclass)"""
            ),
            {"t": f'public."{table}"'},
        )
        .scalars()
        .all()
    )
    if not columns:  # reached only through a parent table: the world holds one person, so every row is theirs
        return conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
    where = " OR ".join(f'"{column}" = :u' for column in columns)
    return conn.execute(text(f'SELECT count(*) FROM "{table}" WHERE {where}'), {"u": user}).scalar_one()


# ---- the flow -----------------------------------------------------------------------------------------


def ask(client, method: str, path: str, **kwargs):
    return client.request(method, path, headers={"Idempotency-Key": uuid4().hex}, **kwargs)


@pytest.fixture
def world(api_client, engine, sign_in_as, run_jobs, ledger, mailbox):
    """The planted person, signed in, with a finished export."""
    person = seed_person(engine)
    sign_in_as(person.user)
    assert ask(api_client, "POST", "/v1/privacy/exports", json={}).status_code == 202
    assert run_jobs() == 1
    return person


def delete_and_erase(api_client, emailed_code, run_jobs, person: Person) -> None:
    response = ask(api_client, "POST", "/v1/privacy/deletion", json={"code": emailed_code(person.email)})
    assert response.status_code == 202, response.text
    assert run_jobs() == 1  # the erase job


def test_the_scan_finds_the_canary_in_every_place_it_was_planted(world, engine):
    with engine.connect() as c:
        found = scan(c, world.needles)
    missing = (PLANTED | AFTER_THE_EXPORT) - found
    assert not missing, f"the scan does not see the canary where it was planted: {sorted(missing)}"


def test_the_planted_person_has_what_the_erasure_must_remove(world, engine):
    with engine.connect() as c:
        assert c.execute(
            text("SELECT record_embedding IS NOT NULL FROM users WHERE id = :u"), {"u": world.user}
        ).scalar_one()
        assert c.execute(text("SELECT count(*) FROM documents WHERE user_id = :u"), {"u": world.user}).scalar_one() == 2
        archive = c.execute(text("SELECT export_archive FROM data_requests WHERE kind = 'export'")).scalar_one()
    assert zipfile.is_zipfile(io.BytesIO(sessions.open_sealed_bytes(bytes(archive))))


def test_erasure_leaves_no_trace_of_the_person_and_only_the_bare_id_in_data_requests(
    world, engine, api_client, emailed_code, run_jobs, ledger, mailbox
):
    delete_and_erase(api_client, emailed_code, run_jobs, world)

    with engine.connect() as c:
        left = scan(c, world.needles)
        assert not left, f"after erasure the person is still in: {sorted(left)}"
        assert c.execute(text("SELECT count(*) FROM users")).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM documents")).scalar_one() == 0  # all of them, sealed bytes and all
        assert c.execute(text("SELECT count(*) FROM users WHERE record_embedding IS NOT NULL")).scalar_one() == 0
        requests = c.execute(
            text("SELECT kind, status, user_id, subject_user_id, export_archive FROM data_requests")
        ).all()
    # what stays: one row per request, with the bare id and no link to a person, and no archive
    assert sorted((r.kind, r.status) for r in requests) == [("deletion", "completed"), ("export", "completed")]
    assert all(r.user_id is None and r.subject_user_id == world.user and r.export_archive is None for r in requests)
    # the ledger has the id and the time and nothing else; the one message went to the address and holds no one's id
    (line,) = ledger.read_text(encoding="utf-8").splitlines()
    assert set(json.loads(line)) == {"user_id", "deleted_at"} and json.loads(line)["user_id"] == str(world.user)
    (message,) = mailbox
    assert (
        message["To"] == world.email
        and str(world.user) not in message.get_content()
        and CANARY not in message.get_content()
    )


def test_the_scan_still_finds_a_leftover_after_an_erasure(
    world, engine, api_client, emailed_code, run_jobs, ledger, mailbox
):
    """The check above can fail: a copy left behind in a kept row, plain or sealed, is found."""
    delete_and_erase(api_client, emailed_code, run_jobs, world)
    sealed_zip = io.BytesIO()
    with zipfile.ZipFile(sealed_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("profile.json", json.dumps({"note": CANARY}))
    with engine.begin() as c:
        c.execute(
            text(
                """INSERT INTO data_requests (subject_user_id, kind, due_at, export_archive)
                   VALUES (:u, 'export', now(), :a)"""
            ),
            {"u": world.user, "a": sessions.seal_bytes(sealed_zip.getvalue())},
        )
        c.execute(
            text("INSERT INTO outbox (dedupe_key, template, payload) VALUES ('leftover', 'x', :p)"),
            {"p": json.dumps({"to": world.email})},
        )
    with engine.connect() as c:
        assert scan(c, world.needles) == {("data_requests", "export_archive"), ("outbox", "payload")}


def test_the_scan_opens_sealed_files_and_zips_and_ignores_what_it_cannot_open():
    needles = [CANARY]
    zipped = io.BytesIO()
    with zipfile.ZipFile(zipped, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("a.json", f"x {CANARY.lower()} y")  # a different letter case is still the canary
    assert leaks_in_bytes(CANARY.encode(), needles)  # in the clear
    assert leaks_in_bytes(sessions.seal_bytes(CANARY.encode()), needles)  # sealed
    assert leaks_in_bytes(
        sessions.seal_bytes(zipped.getvalue()), needles
    )  # a ZIP inside a seal: compressed, then sealed
    assert not leaks_in_bytes(sessions.seal_bytes(b"nothing here"), needles)
    assert not leaks_in_bytes(b"not a token at all", needles)


def test_every_cascade_table_is_seeded_or_skipped_with_a_reason(engine, api_client):
    # api_client is here only for its clean-up: the person below is committed, and it empties the tables afterwards.
    person = seed_person(engine)
    with engine.connect() as c:
        unseeded = [t for t in cascade_tables() if t not in SEED_SKIPPED and rows_of(c, t, person.user) == 0]
    assert not unseeded, (
        f"{unseeded} are registered cascade tables but the canary plants nothing in them. A table that holds data "
        "about a person must be seeded in seed_person (with the canary in each text field) so the scan can prove "
        "erasure reaches it; if it truly cannot be, list it in SEED_SKIPPED with the reason."
    )
    assert all(reason.strip() for reason in SEED_SKIPPED.values())
    assert set(SEED_SKIPPED) <= set(cascade_tables())  # a reason for a table that is not one is left over
