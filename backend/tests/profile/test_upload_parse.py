"""Plan 03-05, task 1: a stored resume becomes unconfirmed facts with their source lines, and every failure ends well
(REC-01, REC-06, TRU-05).

The file is kept by documents.store_document and parsed by the real profile.parse handler through the real queue, in the
real database. Only the model is a stand-in: it answers from the numbered lines it is sent (see conftest.py). Each test
that breaks the job on purpose names the rule it protects.
"""

import hashlib
import io
import json
import logging
import threading
import zipfile

import pytest
from reportlab.lib.pdfencrypt import StandardEncryption
from sqlalchemy import text

from app.platform import db, queue
from app.platform.llm import ProviderUnavailable, SchemaInvalid
from app.platform.sessions import open_sealed_bytes
from app.profile import documents, jobs, parse_job
from app.profile.documents import MAX_BYTES, PayloadTooLarge, UnsupportedMedia, clean_filename, sniff
from app.profile.extract import workflow
from app.profile.resume.reader import DOCX_MIME, PDF_MIME

ROLES, EDUCATION_SKILLS, SUMMARY = (task.name for task in workflow.TASKS)


def a_zip(*names: str, body: bytes = b"x") -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in names:
            archive.writestr(name, body)
    return out.getvalue()


def upload_and_parse(kit, data, model=None, name="resume.docx"):
    """A person who has agreed to AI processing uploads this file and the worker parses it."""
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, data, name)
    model = model or kit.model()
    assert kit.run(model) == 1
    return user, document_id, model


def on_the_last_attempt(kit, document_id) -> None:
    """The queue has run this job all but once: the next claim is the last attempt."""
    with kit.engine.begin() as c:
        c.execute(
            text("UPDATE queue_jobs SET attempts = max_attempts - 1 WHERE unique_key = :key"),
            {"key": f"profile.parse:{document_id}"},
        )


# ---- storing the file ----------------------------------------------------------------------------


def test_a_stored_file_is_sealed_hashed_and_queued_for_one_parse(kit):
    user, data = kit.person(), kit.docx()
    document_id = kit.store(user, data, "Mara CV.docx")
    row = kit.document(document_id)
    assert (row.state, row.mime_type, row.byte_size, row.original_filename) == (
        "uploaded",
        DOCX_MIME,
        len(data),
        "Mara CV.docx",
    )
    assert bytes(row.sha256) == hashlib.sha256(data).digest()
    assert bytes(row.content) != data and open_sealed_bytes(bytes(row.content)) == data  # kept as a token, not as text
    job = kit.job(document_id)
    assert (job.state, job.kind, job.queue, job.max_attempts) == ("queued", "profile.parse", "interactive", 4)
    assert job.payload == {"document_id": str(document_id)}  # an id, never text
    assert job.unique_key == f"profile.parse:{document_id}" and kit.count("queue_jobs") == 1


def test_a_stored_filename_is_cleaned_before_it_is_kept(kit):
    user = kit.person()
    assert kit.document(kit.store(user, kit.docx(), "C:\\temp\\" + chr(0) + "cv.docx")).original_filename == "cv.docx"
    assert kit.document(kit.store(user, kit.docx(), None)).original_filename is None


def test_the_row_and_its_job_are_written_in_one_transaction(kit):
    user = kit.person()
    with pytest.raises(RuntimeError), db.transaction(user) as conn:
        documents.store_document(conn, user, "cv.docx", kit.docx())
        raise RuntimeError("the request failed after the file was stored")
    assert (kit.count("documents"), kit.count("queue_jobs")) == (0, 0)


def test_a_pdf_and_a_docx_are_recognised_by_their_first_bytes(kit):
    assert sniff(kit.pdf()) == PDF_MIME
    assert sniff(kit.docx()) == DOCX_MIME


NOT_RESUMES = {
    "empty": b"",
    "plain text": b"Mara Quillfeather, backend engineer",
    "program": b"MZ\x90\x00" + bytes(100),
    "image": b"\xff\xd8\xff\xe0" + bytes(100),
    "old word file": bytes.fromhex("d0cf11e0a1b11ae1") + bytes(100),
    "zip with no word document": a_zip("notes.txt"),
    "broken zip": b"PK\x03\x04" + bytes(200),
    "pdf marker not at the start": b" %PDF-1.4",
}


@pytest.mark.parametrize("data", NOT_RESUMES.values(), ids=NOT_RESUMES.keys())
def test_anything_else_is_refused_as_unsupported_media(data):
    with pytest.raises(UnsupportedMedia) as refused:
        sniff(data)
    assert (refused.value.status, refused.value.code) == (415, "unsupported_media_type")
    assert refused.value.details["accepted"] == [PDF_MIME, DOCX_MIME]


def test_the_name_does_not_decide_the_type(kit):
    user = kit.person()
    with pytest.raises(UnsupportedMedia), db.transaction(user) as conn:
        documents.store_document(conn, user, "resume.pdf", NOT_RESUMES["program"])
    assert kit.count("documents") == 0
    assert kit.document(kit.store(user, kit.pdf(), "resume.txt")).mime_type == PDF_MIME


def test_ten_megabytes_exactly_are_kept_and_one_byte_more_is_refused(conn):
    user = conn.execute(text("INSERT INTO users (email) VALUES ('a@example.test') RETURNING id")).scalar_one()
    big = b"%PDF-" + bytes(MAX_BYTES - 5)
    documents.store_document(conn, user, "big.pdf", big)
    with pytest.raises(PayloadTooLarge) as refused:
        documents.store_document(conn, user, "big.pdf", big + b"0")
    assert (refused.value.status, refused.value.code) == (413, "payload_too_large")
    assert refused.value.details == {"max_bytes": MAX_BYTES}
    assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 1  # the second one left nothing


@pytest.mark.parametrize(
    ("given", "shown"),
    [
        ("Mara CV.pdf", "Mara CV.pdf"),
        ("r\u00e9sum\u00e9.docx", "r\u00e9sum\u00e9.docx"),
        ("C:\\Users\\mara\\cv.pdf", "cv.pdf"),
        ("../../etc/passwd", "passwd"),
        ("cv" + chr(0) + chr(31) + ".pdf", "cv.pdf"),
        ("cv" + chr(0x202E) + "txt.pdf", "cvtxt.pdf"),  # a right-to-left override makes a name read backwards
        ("x" * 300, "x" * 255),
        ("folder/", None),
        ("   ", None),
        ("", None),
        (None, None),
    ],
)
def test_the_filename_is_kept_as_display_text_only(given, shown):
    assert clean_filename(given) == shown


# ---- the parse -----------------------------------------------------------------------------------


def test_a_docx_becomes_unconfirmed_facts_each_quoting_its_own_text(kit):
    user, document_id, model = upload_and_parse(kit, kit.docx())
    document = kit.document(document_id)
    assert (document.state, document.parse_error_code, document.hidden_text_chars) == ("parsed", None, 0)
    assert document.extracted_text == "\n".join(kit.RESUME) and document.parsed_at is not None

    facts = kit.facts(document_id)
    assert [f.kind for f in facts] == ["other", "role", "achievement", "achievement", "education"] + ["skill"] * 3
    for fact in facts:
        assert fact.source == "resume"
        assert fact.statement == fact.source_quote == document.extracted_text[fact.start : fact.stop]
        assert (fact.confirmed_at, fact.rejected_at, fact.superseded_at) == (None, None, None)
    role = next(f for f in facts if f.kind == "role")
    assert (role.attributes["employer"], role.attributes["title"]) == ("Veltrix Labs", "Data Engineer")
    assert {f.parent_fact_id for f in facts if f.kind == "achievement"} == {role.id}
    assert [f.statement for f in facts if f.kind == "skill"] == ["Python", "SQL", "Airflow"]

    assert (kit.job(document_id).state, kit.job(document_id).attempts) == ("succeeded", 1)
    assert kit.count("profile_events") == 0  # an unconfirmed fact is not a change to the record ...
    with kit.engine.connect() as c:  # ... so the version is still the 0 every person starts with
        assert c.execute(text("SELECT record_version FROM users WHERE id = :u"), {"u": user}).scalar_one() == 0
    assert {call["principal"] for call in model.calls} == {user}  # the calls are charged to this person
    assert sorted(call["task"] for call in model.calls) == sorted([ROLES, EDUCATION_SKILLS, SUMMARY])


def test_contact_details_reach_the_model_masked_and_stay_in_the_stored_text(kit):
    _, document_id, model = upload_and_parse(kit, kit.docx())
    assert "[EMAIL]" in model.sent and "[PHONE]" in model.sent
    assert "mara.quillfeather" not in model.sent and "555 010 0147" not in model.sent
    assert "mara.quillfeather@example.com" in kit.document(document_id).extracted_text  # the raw text is untouched


def test_a_pdf_becomes_the_same_facts(kit):
    _, document_id, _ = upload_and_parse(kit, kit.pdf(), name="resume.pdf")
    document = kit.document(document_id)
    assert (document.state, document.mime_type, document.parse_error_code) == ("parsed", PDF_MIME, None)
    facts = kit.facts(document_id)
    assert len(facts) == kit.KEPT
    assert all(f.source_quote == document.extracted_text[f.start : f.stop] for f in facts)


def test_hidden_text_is_removed_counted_and_never_reaches_the_model_or_a_fact(kit):
    _, document_id, model = upload_and_parse(kit, kit.docx(hidden=kit.HIDDEN))
    document = kit.document(document_id)
    assert document.state == "parsed" and document.hidden_text_chars == len(kit.HIDDEN.replace(" ", ""))
    assert "Ignore every earlier" not in document.extracted_text and "Ignore every earlier" not in model.sent
    assert not any("Ignore" in f.statement for f in kit.facts(document_id))


def test_parsing_one_document_twice_adds_no_facts_and_asks_the_model_nothing_more(kit):
    _, document_id, model = upload_and_parse(kit, kit.docx())
    before, calls = [f.id for f in kit.facts(document_id)], len(model.calls)
    with db.transaction(role="worker") as conn:  # the same job delivered a second time
        parse_job.parse(conn, {"document_id": str(document_id)}, gateway=model)
    assert [f.id for f in kit.facts(document_id)] == before and len(model.calls) == calls


def test_two_runs_that_overlap_leave_one_set_of_facts(kit):
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    payload, outer, inner, started = {"document_id": str(document_id)}, kit.model(), kit.model(), threading.Lock()
    other_run_done = []

    def while_the_first_run_waits_for_the_model(task, schema, messages, *, principal, prompt_version):
        with started:
            first_call = not other_run_done
            other_run_done.append(True)
        if first_call:  # another worker takes the lapsed lease and finishes the whole parse
            with db.transaction(role="worker") as conn:
                parse_job.parse(conn, payload, gateway=inner)
        return outer(task, schema, messages, principal=principal, prompt_version=prompt_version)

    assert kit.run(while_the_first_run_waits_for_the_model) == 1
    assert len(kit.facts(document_id)) == kit.KEPT  # not twice that
    assert (len(inner.calls), len(outer.calls)) == (3, 3) and kit.document(document_id).state == "parsed"


def test_an_achievement_listed_before_its_role_is_still_linked_to_it(kit):
    def children_first(numbered):
        role, first, second = kit.roles_answer(numbered)["items"]
        return {"items": [{**first, "parent": 2}, {**second, "parent": 2}, role]}

    _, document_id, _ = upload_and_parse(kit, kit.docx(), kit.model({ROLES: children_first}))
    facts = kit.facts(document_id)
    role = next(f for f in facts if f.kind == "role")
    assert [f.parent_fact_id for f in facts if f.kind == "achievement"] == [role.id, role.id]


def test_what_the_code_could_not_confirm_is_kept_as_unconfirmed_facts_with_the_reason(kit):
    def a_wrong_employer_and_lines_that_do_not_exist(numbered):
        answer = kit.roles_answer(numbered)
        answer["items"][0]["employer"] = "Globex"  # not written in the cited lines
        answer["items"].append({**answer["items"][0], "line_start": 999, "line_end": 999})  # no such lines
        return answer

    _, document_id, _ = upload_and_parse(
        kit, kit.docx(), kit.model({ROLES: a_wrong_employer_and_lines_that_do_not_exist})
    )
    document, facts = kit.document(document_id), kit.facts(document_id)
    assert document.state == "parsed" and document.parse_error_code is None
    unverified = [f for f in facts if f.attributes.get("unverified")]
    assert sorted(f.attributes["reason"] for f in unverified) == ["employer_not_in_text"] + ["parent_not_kept"] * 2
    for fact in unverified:  # a quote of the lines the model pointed at, to be checked by the candidate
        assert fact.kind == "other" and fact.statement == fact.source_quote
        assert fact.source_quote == document.extracted_text[fact.start : fact.stop]
        assert (fact.confirmed_at, fact.rejected_at) == (None, None)
    assert {f.kind for f in facts} == {"other", "education", "skill"}  # nothing unverified became a role
    assert len(facts) == 3 + 5  # the three above, then the education, three skills and the summary; no row for line 999


def test_the_same_item_pointed_at_twice_is_kept_once(kit):
    def the_role_twice(numbered):
        role = {**kit.roles_answer(numbered)["items"][0], "employer": "Globex"}  # wrong, so it comes back unverified
        return {"items": [role, role]}

    _, document_id, _ = upload_and_parse(kit, kit.docx(), kit.model({ROLES: the_role_twice}))
    unverified = [f for f in kit.facts(document_id) if f.attributes.get("unverified")]
    assert [f.attributes["reason"] for f in unverified] == ["employer_not_in_text"]


def test_a_resume_the_model_finds_nothing_in_is_parsed_with_no_facts(kit):
    nothing = {"items": []}
    model = kit.model({ROLES: nothing, EDUCATION_SKILLS: nothing, SUMMARY: nothing})
    _, document_id, _ = upload_and_parse(kit, kit.docx(), model)
    document = kit.document(document_id)
    assert (document.state, document.parse_error_code) == ("parsed", None) and kit.facts(document_id) == []


def test_a_line_too_long_to_be_a_statement_is_dropped_not_stored(kit):
    long_line = " ".join(["detail"] * 350)  # 2,449 characters, over the 2,000 a statement may hold

    def cite_the_long_line(numbered):
        number = kit.line_of(numbered, "detail")
        return {"items": [{"line_start": number, "line_end": number}]}

    _, document_id, _ = upload_and_parse(
        kit, kit.docx([*kit.RESUME, long_line]), kit.model({SUMMARY: cite_the_long_line})
    )
    facts = kit.facts(document_id)
    assert kit.document(document_id).state == "parsed" and long_line in kit.document(document_id).extracted_text
    assert len(facts) == kit.KEPT - 1 and all(len(f.statement) <= 2000 for f in facts)


def test_one_failed_call_leaves_a_parsed_document_marked_partial(kit):
    _, document_id, model = upload_and_parse(kit, kit.docx(), kit.model({ROLES: SchemaInvalid()}))
    document = kit.document(document_id)
    assert (document.state, document.parse_error_code) == ("parsed", "partial_extraction")
    assert {f.kind for f in kit.facts(document_id)} == {"education", "skill", "other"}  # what the other two calls found
    assert len(model.calls) == 3


# ---- files that cannot be read -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("make", "code"),
    [
        pytest.param(lambda kit: b"%PDF-1.4\nthis is not a pdf at all" * 20, "corrupt", id="corrupt-pdf"),
        pytest.param(lambda kit: a_zip("word/document.xml", body=b"<not xml"), "corrupt", id="corrupt-docx"),
        pytest.param(lambda kit: kit.pdf(encrypt=StandardEncryption("user", "owner")), "encrypted", id="locked-pdf"),
        pytest.param(lambda kit: kit.pdf(["Mara"]), "scanned", id="scanned-pdf"),
        pytest.param(lambda kit: kit.docx(["Mara"]), "scanned", id="nearly-empty-docx"),
    ],
)
def test_an_unreadable_file_ends_failed_with_its_code_no_facts_and_no_retry(kit, make, code):
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, make(kit))
    model = kit.model()
    assert kit.run(model) == 1
    document = kit.document(document_id)
    assert (document.state, document.parse_error_code, document.extracted_text) == ("failed", code, None)
    assert kit.facts(document_id) == [] and model.calls == []
    assert (kit.job(document_id).state, kit.job(document_id).attempts) == ("succeeded", 1)  # done: a retry cannot help


# ---- consent and a model that is not there -------------------------------------------------------


def test_a_person_who_never_agreed_has_nothing_sent_to_the_model(kit):
    user = kit.person()
    document_id = kit.store(user, kit.docx())
    model = kit.model()
    assert kit.run(model) == 1
    document = kit.document(document_id)
    assert (document.state, document.parse_error_code) == ("failed", "consent_required")
    assert model.calls == [] and kit.facts(document_id) == []
    assert (kit.job(document_id).state, kit.job(document_id).attempts) == ("succeeded", 1)  # not retried


def test_with_no_model_configured_the_real_gateway_fails_the_document_as_ai_disabled(kit):
    kit.provider(None)
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    assert kit.run() == 1  # the handler as the worker registers it, with the real gateway
    document = kit.document(document_id)
    assert (document.state, document.parse_error_code) == ("failed", "ai_disabled")
    assert kit.facts(document_id) == [] and kit.count("llm_calls") == 0
    assert (kit.job(document_id).state, kit.job(document_id).attempts) == ("succeeded", 1)


# ---- failures the queue retries ------------------------------------------------------------------


def test_a_provider_that_does_not_answer_is_retried_and_the_document_is_still_waiting(kit):
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    assert kit.run(kit.model({ROLES: ProviderUnavailable(timed_out=True)})) == 1
    job, document = kit.job(document_id), kit.document(document_id)
    assert (job.state, job.attempts, job.last_error_code) == ("queued", 1, "ProviderUnavailable")
    assert (document.state, document.parse_error_code, document.extracted_text) == ("parsing", None, None)
    assert kit.facts(document_id) == []


def test_on_the_last_attempt_a_silent_provider_fails_the_document_and_dead_letters_the_job(kit):
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    on_the_last_attempt(kit, document_id)
    assert kit.run(kit.model({ROLES: ProviderUnavailable()})) == 1
    document, job = kit.document(document_id), kit.job(document_id)
    assert (document.state, document.parse_error_code) == ("failed", "provider_unavailable")
    assert (job.state, job.attempts, job.last_error_code) == ("dead", 4, "ProviderUnavailable")


def test_an_unexpected_error_is_retried_and_on_the_last_attempt_fails_the_document(kit):
    secret = "Mara Quillfeather pasted her resume into this error"
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    assert kit.run(kit.model({SUMMARY: RuntimeError(secret)})) == 1
    assert (kit.job(document_id).state, kit.document(document_id).state) == ("queued", "parsing")
    on_the_last_attempt(kit, document_id)
    assert kit.run(kit.model({SUMMARY: RuntimeError(secret)})) == 0  # still waiting out its delay
    with kit.engine.begin() as c:
        c.execute(text("UPDATE queue_jobs SET run_at = now()"))
    assert kit.run(kit.model({SUMMARY: RuntimeError(secret)})) == 1
    document, job = kit.document(document_id), kit.job(document_id)
    assert (document.state, document.parse_error_code) == ("failed", "parse_failed")
    assert (job.state, job.last_error_code) == ("dead", "RuntimeError")  # the class, never the message
    assert secret not in str(tuple(document) + tuple(job))


def make_the_fact_insert_fail(monkeypatch) -> None:
    """The last fact gets an empty statement, which the facts table refuses: the error comes from the database, in the
    middle of the transaction that has already written the text."""
    real = parse_job._fact_rows

    def last_fact_is_empty(*args):
        rows = real(*args)
        rows[-1] = {**rows[-1], "statement": ""}
        return rows

    monkeypatch.setattr(parse_job, "_fact_rows", last_fact_is_empty)


def test_a_crash_while_writing_the_facts_leaves_nothing_not_even_the_text(kit, monkeypatch):
    make_the_fact_insert_fail(monkeypatch)
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    assert kit.run(kit.model()) == 1
    document = kit.document(document_id)
    assert (document.state, document.extracted_text, document.parsed_at) == ("parsing", None, None)
    assert kit.facts(document_id) == [] and kit.job(document_id).state == "queued"  # all or nothing; a retry is due


def test_a_database_error_on_the_last_attempt_still_fails_the_document(kit, monkeypatch):
    make_the_fact_insert_fail(monkeypatch)  # the failed transaction must be rolled back before the document is failed
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    on_the_last_attempt(kit, document_id)
    assert kit.run(kit.model()) == 1
    document, job = kit.document(document_id), kit.job(document_id)
    assert (document.state, document.parse_error_code, document.extracted_text) == ("failed", "parse_failed", None)
    assert (job.state, job.last_error_code) == ("dead", "IntegrityError") and kit.facts(document_id) == []


# ---- when the document says running, and what the worker holds while the model answers ------------


def test_the_document_says_running_while_its_file_is_being_read(kit, monkeypatch):
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    seen, real = [], parse_job.read_resume

    def read_and_look(data, mime):
        seen.append(kit.document(document_id).state)  # on another connection, as a poll would
        return real(data, mime)

    monkeypatch.setattr(parse_job, "read_resume", read_and_look)
    assert kit.run(kit.model()) == 1
    assert seen == ["parsing"]


def test_no_transaction_stays_open_while_the_model_answers(kit):
    user = kit.person()
    kit.agree(user)
    document_id = kit.store(user, kit.docx())
    scripted, open_transactions, one_at_a_time = kit.model(), [], threading.Lock()
    idle_in_transaction = text(
        """SELECT count(*) FROM pg_stat_activity
           WHERE datname = current_database() AND usename = 'app_worker' AND state LIKE 'idle in transaction%'"""
    )

    def answer_and_look(task, schema, messages, *, principal, prompt_version):
        with one_at_a_time, kit.engine.connect() as c:
            open_transactions.append(c.execute(idle_in_transaction).scalar_one())
        return scripted(task, schema, messages, principal=principal, prompt_version=prompt_version)

    assert kit.run(answer_and_look) == 1
    assert open_transactions == [0, 0, 0] and kit.document(document_id).state == "parsed"


# ---- what is logged, and how the worker finds the handler ----------------------------------------


def test_the_job_logs_ids_and_counts_and_never_resume_text(kit, caplog):
    caplog.set_level(logging.INFO, logger="app.event")
    _, document_id, _ = upload_and_parse(kit, kit.docx(hidden=kit.HIDDEN))
    lines = [json.loads(record.getMessage()) for record in caplog.records if record.name == "app.event"]
    done = next(line for line in lines if line["event"] == "profile.parse")
    assert done["document_id"] == str(document_id)
    assert (done["status"], done["kept"], done["unverified"]) == ("parsed", kit.KEPT, 0)
    assert done["hidden_chars"] == len(kit.HIDDEN.replace(" ", ""))
    for fragment in (*kit.RESUME, kit.HIDDEN, "Quillfeather", "example.com", "Veltrix"):
        assert fragment not in caplog.text


def test_the_worker_registers_the_parse_handler(monkeypatch):
    monkeypatch.setattr(queue, "_handlers", {})
    jobs.register()
    assert queue._handlers == {"profile.parse": parse_job.parse}
