"""TLR-03, TLR-02 (plan 05-05): approving and rejecting passed edits, and the PDF and DOCX files drawn from approved
edits only. Tailored resumes are built by hand with the 05-01 factories (no model); every name and line is invented.
Markers such as ZZapproved make a PDF's extracted text easy to search.
"""

import io

import pdfplumber
import pytest
from docx import Document
from sqlalchemy import text

from app.platform.llm import fake
from app.tailor import privacy, service
from tests.p5_factories import BULLET_A1, BULLET_A2, BULLET_B1, BULLET_B2, Edit, make_tailored_resume
from tests.tailor.fixtures.service import harness
from tests.tailor.fixtures.service.harness import key, sign_in


@pytest.fixture
def w(engine, api_client, monkeypatch):
    harness.empty_tables(engine)
    harness.use_provider(monkeypatch)
    world = harness.world(engine)
    world.client, world.engine = api_client, engine
    sign_in(api_client, world.ada)
    f = world.record.facts
    with engine.begin() as c:
        world.tailored = make_tailored_resume(
            c,
            world.ada,
            world.job,
            edits=(
                Edit(BULLET_A1, "ZZapproved rephrase", (f["bullet_initech_1"],), target_fact_id=f["bullet_initech_1"]),
                Edit(BULLET_B1, "ZZrejected rephrase", (f["bullet_globex_1"],), target_fact_id=f["bullet_globex_1"]),
                Edit(BULLET_B2, "ZZpending rephrase", (f["bullet_globex_2"],), target_fact_id=f["bullet_globex_2"]),
                Edit("x", "ZZdiscarded rephrase", (f["bullet_initech_2"],), status="discarded", reason="new_entity"),
            ),
        )
        ids = c.execute(
            text("SELECT id, ordinal FROM resume_edits WHERE tailored_resume_id = :t ORDER BY ordinal"),
            {"t": world.tailored},
        ).all()
    world.edit = {row.ordinal: str(row.id) for row in ids}
    yield world
    fake.replay_from(None)
    harness.empty_tables(engine)


def ok(response, status=200):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["success"] is True and body["error"] is None
    return body["data"]


def refused(response, status, code):
    assert response.status_code == status, response.text
    body = response.json()
    assert (body["success"], body["data"], body["error"]["code"]) == (False, None, code)
    return body["error"]


def decide(w, *pairs, headers=None, tailored=None):
    body = {"decisions": [{"edit_id": w.edit[n], "decision": d} for n, d in pairs]}
    return w.client.post(
        f"/v1/tailored-resumes/{tailored or w.tailored}/decisions", json=body, headers=headers or key()
    )


def approve_and_reject(w):
    ok(decide(w, (1, "approved"), (2, "rejected")))


def docx_text(content: bytes) -> str:
    doc = Document(io.BytesIO(content))
    return "\n".join(p.text for p in doc.paragraphs)


def pdf_text(content: bytes) -> str:
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def file(w, fmt, tailored=None):
    return w.client.get(f"/v1/tailored-resumes/{tailored or w.tailored}/file", params={"format": fmt})


# ---- decisions ----------------------------------------


def test_each_passed_edit_can_be_approved_or_rejected_with_the_time(w):
    data = ok(decide(w, (1, "approved"), (2, "rejected")))
    by_ordinal = {e["ordinal"]: e for e in data["edits"]}
    assert [by_ordinal[n]["decision"] for n in (1, 2, 3)] == ["approved", "rejected", "pending"]
    assert by_ordinal[1]["decided_at"] and by_ordinal[2]["decided_at"] and by_ordinal[3]["decided_at"] is None
    assert data["discarded_count"] == 1 and data["discarded_by_reason"] == {"new_entity": 1}


def test_an_edit_can_be_decided_once(w):
    approve_and_reject(w)
    error = refused(decide(w, (1, "rejected")), 409, "conflict")
    assert error["details"]["state"] == "already_decided"
    # nothing of a refused request was applied: edit 3 is still open when it comes with a decided one
    refused(decide(w, (3, "approved"), (2, "approved")), 409, "conflict")
    assert ok(w.client.get(f"/v1/tailored-resumes/{w.tailored}"))["edits"][2]["decision"] == "pending"
    # an edit named twice in one request is one decision too many
    refused(decide(w, (3, "approved"), (3, "rejected")), 409, "conflict")


def test_a_discarded_edit_cannot_be_named(w):
    refused(decide(w, (4, "approved")), 404, "not_found")
    with w.engine.connect() as c:
        decision = c.execute(text("SELECT decision::text FROM resume_edits WHERE ordinal = 4")).scalar_one()
    assert decision == "pending"


def test_an_unknown_edit_or_tailored_resume_is_404_and_so_is_another_persons(w):
    body = {"decisions": [{"edit_id": "00000000-0000-4000-8000-000000000000", "decision": "approved"}]}
    refused(w.client.post(f"/v1/tailored-resumes/{w.tailored}/decisions", json=body, headers=key()), 404, "not_found")
    refused(decide(w, (1, "approved"), tailored="00000000-0000-4000-8000-000000000000"), 404, "not_found")
    sign_in(w.client, w.bob)
    refused(decide(w, (1, "approved")), 404, "not_found")
    refused(file(w, "docx"), 404, "not_found")
    assert ok(w.client.get("/v1/tailored-resumes")) == []


def test_the_same_idempotency_key_replays_the_decision(w):
    headers = key()
    first = decide(w, (1, "approved"), headers=headers)
    again = decide(w, (1, "approved"), headers=headers)
    assert again.status_code == 200 and again.json()["data"] == first.json()["data"]
    assert again.json()["meta"].get("idempotent_replay") is True


def test_a_decision_needs_at_least_one_item_and_a_known_value(w):
    url = f"/v1/tailored-resumes/{w.tailored}/decisions"
    refused(w.client.post(url, json={"decisions": []}, headers=key()), 422, "validation_failed")
    body = {"decisions": [{"edit_id": w.edit[1], "decision": "pending"}]}
    refused(w.client.post(url, json=body, headers=key()), 422, "validation_failed")


# ---- the files ----------------------------------------


@pytest.mark.parametrize("fmt", ["docx", "pdf"])
def test_a_file_holds_approved_edits_only(w, fmt):
    approve_and_reject(w)
    response = file(w, fmt)
    assert response.status_code == 200
    read = docx_text if fmt == "docx" else pdf_text
    resume = read(response.content)
    assert "ZZapproved rephrase" in resume
    assert BULLET_A1 not in resume  # the approved edit replaced the bullet it rephrases
    for marker in ("ZZrejected", "ZZpending", "ZZdiscarded"):
        assert marker not in resume
    # where an edit was not approved the base text stays
    assert BULLET_B1.rstrip(".") in resume.replace("\n", " ") and BULLET_B2.split()[0] in resume


@pytest.mark.parametrize(
    ("fmt", "media"),
    [("docx", "wordprocessingml.document"), ("pdf", "application/pdf")],
)
def test_the_file_is_an_attachment_named_by_job_and_rendered_on_every_request(w, fmt, media):
    approve_and_reject(w)
    response = file(w, fmt)
    assert media in response.headers["content-type"]
    disposition = response.headers["content-disposition"]
    assert disposition == f'attachment; filename="tailored-resume-job-{w.job.id}.{fmt}"'
    assert response.content and file(w, fmt).content  # nothing is stored: two requests, two renderings
    with w.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM tailored_resumes")).scalar_one() == 1


def test_nothing_approved_gives_the_base_resume_without_any_edit_text(w):
    resume = docx_text(file(w, "docx").content)
    assert BULLET_A1 in resume and "ZZ" not in resume


def test_a_character_the_pdf_cannot_draw_is_422_and_the_docx_carries_it(w):
    with w.engine.begin() as c:
        c.execute(text("SELECT set_config('app.maintenance', 'on', true)"))
        c.execute(text("UPDATE resume_edits SET after_text = 'ZZarrow → shown' WHERE id = :id"), {"id": w.edit[1]})
    ok(decide(w, (1, "approved")))
    error = refused(file(w, "pdf"), 422, "validation_failed")
    assert error["details"]["fields"] == [{"field": "format", "issue": "pdf_unsupported_characters"}]
    assert "→" in docx_text(file(w, "docx").content)


def test_an_unknown_format_is_422_and_a_resume_that_is_not_ready_is_409(w):
    refused(file(w, "rtf"), 422, "validation_failed")
    with w.engine.begin() as c:
        c.execute(text("UPDATE tailored_resumes SET status = 'generating', prompt_version = NULL"))
    error = refused(file(w, "docx"), 409, "conflict")
    assert error["details"]["state"] == "not_ready"


def test_an_approved_edit_whose_fact_was_corrected_since_is_left_out_of_the_file(w):
    approve_and_reject(w)
    with w.engine.begin() as c:
        c.execute(
            text("UPDATE profile_facts SET superseded_at = now() WHERE id = :f"),
            {"f": w.record.facts["bullet_initech_1"]},
        )
    resume = docx_text(file(w, "docx").content)
    assert "ZZapproved" not in resume  # the record no longer holds that bullet, so neither does the file
    assert BULLET_A1 not in resume


# ---- the data export ----------------------------------------


def test_the_data_export_carries_a_docx_for_each_tailored_resume_with_an_approved_edit(w):
    with w.engine.connect() as c:
        before = privacy.export_tailor(c, w.ada)
    assert not [name for name in before.files if name.endswith(".docx")]  # nothing approved yet
    approve_and_reject(w)
    with w.engine.connect() as c:
        export = privacy.export_tailor(c, w.ada)
        direct = service.export_doc(c, w.ada, w.tailored)
        other = privacy.export_tailor(c, w.bob)
    assert [n for n in export.files if n.endswith(".docx")] == [
        f"files/tailored-resumes/tailored-resume-{w.tailored}.docx"
    ]
    assert "ZZapproved rephrase" in docx_text(next(iter(export.files.values())))
    assert direct is not None and other.files == {}
    # the archive's rows are the person's own, discarded edits included (05-01 deviation 10)
    assert len(export.data["resume_edits"]) == 4


# ---- a fact that changed after approval (fix A1) ----------------------------------------


def retire(w, name):
    with w.engine.begin() as c:
        c.execute(text("UPDATE profile_facts SET superseded_at = now() WHERE id = :f"), {"f": w.record.facts[name]})


@pytest.fixture
def w2(w):
    """Two more passed edits: (a) an addition of the skill ZZQuill, (b) a rephrase resting on a second fact."""
    f = w.record.facts
    with w.engine.begin() as c:
        w.tailored2 = make_tailored_resume(
            c,
            w.ada,
            w.job,
            edits=(
                Edit("", "ZZaddedskill", (f["skill_quillscript"],), section="skills"),
                Edit(
                    BULLET_A2,
                    "ZZtwofacts rephrase",
                    (f["bullet_initech_2"], f["skill_python"]),
                    target_fact_id=f["bullet_initech_2"],
                ),
            ),
        )
        ids = c.execute(
            text("SELECT id, ordinal FROM resume_edits WHERE tailored_resume_id = :t ORDER BY ordinal"),
            {"t": w.tailored2},
        ).all()
    w.edit2 = {row.ordinal: str(row.id) for row in ids}
    return w


def decide2(w, *pairs):
    body = {"decisions": [{"edit_id": w.edit2[n], "decision": d} for n, d in pairs]}
    return w.client.post(f"/v1/tailored-resumes/{w.tailored2}/decisions", json=body, headers=key())


def test_an_approved_addition_whose_fact_was_retired_is_not_exported(w2):
    ok(decide2(w2, (1, "approved"), (2, "approved")))
    retire(w2, "skill_quillscript")  # the base no longer shows it, so apply_edits alone would append the edit's text
    assert "ZZaddedskill" not in docx_text(file(w2, "docx", w2.tailored2).content)
    assert "ZZaddedskill" not in pdf_text(file(w2, "pdf", w2.tailored2).content)


def test_an_approved_rephrase_whose_second_fact_was_retired_is_not_exported(w2):
    ok(decide2(w2, (1, "approved"), (2, "approved")))
    assert "ZZtwofacts rephrase" in docx_text(file(w2, "docx", w2.tailored2).content)
    retire(w2, "skill_python")
    resume = docx_text(file(w2, "docx", w2.tailored2).content)
    assert "ZZtwofacts" not in resume and BULLET_A2 in resume  # the base bullet stays


def test_the_archive_drops_such_an_edit_from_the_docx_and_labels_the_row(w2):
    ok(decide2(w2, (1, "approved"), (2, "approved")))
    retire(w2, "skill_quillscript")
    with w2.engine.connect() as c:
        export = privacy.export_tailor(c, w2.ada)
    docx = export.files[f"files/tailored-resumes/tailored-resume-{w2.tailored2}.docx"]
    assert "ZZaddedskill" not in docx_text(docx) and "ZZtwofacts rephrase" in docx_text(docx)
    labels = {r["after_text"]: r["archive_label"] for r in export.data["resume_edits"]}
    assert labels["ZZaddedskill"] == "not_exported_fact_not_usable"
    assert labels["ZZtwofacts rephrase"] == "kept" and labels["ZZdiscarded rephrase"] == "discarded"


def test_approving_an_edit_whose_fact_is_no_longer_usable_is_409_fact_not_usable(w2):
    retire(w2, "skill_python")
    error = refused(decide2(w2, (2, "approved")), 409, "conflict")
    assert error["details"] == {"reason": "fact_not_usable"}
    with w2.engine.connect() as c:
        assert (
            c.execute(text("SELECT decision::text FROM resume_edits WHERE id = :i"), {"i": w2.edit2[2]}).scalar_one()
            == "pending"
        )
    ok(decide2(w2, (2, "rejected")))  # rejecting it is still allowed


# ---- the archive says what it holds (fix A6) ----------------------------------------


def test_the_archive_readme_says_discarded_edit_text_is_included_and_labelled(w):
    with w.engine.connect() as c:
        export = privacy.export_tailor(c, w.ada)
    readme = export.files["files/tailored-resumes/README.txt"].decode()
    assert "DISCARDED" in readme and "archive_label" in readme and "included" in readme
    discarded = [r for r in export.data["resume_edits"] if r["archive_label"] == "discarded"]
    assert [r["after_text"] for r in discarded] == ["ZZdiscarded rephrase"]
