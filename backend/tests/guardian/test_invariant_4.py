"""Invariant 4 (N1, N2, N11; GRD-01, CMP-04): nothing about a candidate reaches an employer unless the candidate
acts, no
agent shares a profile, and a follow-up goes only to the address the candidate typed. These tests fail if a campaign or
Guardian path can share a profile or address a recipient the candidate did not type."""

import ast
from pathlib import Path

import pytest

from app.guardian import checks
from tests.guardian.support import entails, follow_up_payload, make_committed_world, propose_follow_up, rows, scalar

APP = Path(__file__).resolve().parents[2] / "app"
SCANNED = [*sorted((APP / "campaigns").rglob("*.py")), *sorted((APP / "guardian").rglob("*.py"))]
# assumption: a call whose name says it accepts, shares or previews on someone's behalf
FORBIDDEN_CALL = ("accept", "share", "preview")


def hiring_imports(tree: ast.AST) -> list[str]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "app.hiring":
                found += [f"app.hiring.{alias.name}" for alias in node.names]
            elif node.module.startswith("app.hiring."):
                found.append(node.module)
        elif isinstance(node, ast.Import):
            found += [a.name for a in node.names if a.name.startswith("app.hiring")]
    return found


def call_names(tree: ast.AST) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            names.append(fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else "")
    return names


def test_the_scan_covers_the_campaign_and_guardian_modules():
    names = {p.name for p in SCANNED}
    assert {"gate.py", "stages.py", "approvals.py", "checks.py", "sender.py"} <= names


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_campaign_or_guardian_module_imports_hiring_except_its_api(path):
    imports = hiring_imports(ast.parse(path.read_text(encoding="utf-8")))
    assert [i for i in imports if i != "app.hiring.api"] == []


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_campaign_or_guardian_module_calls_an_accept_share_or_preview_function(path):
    called = [
        n
        for n in call_names(ast.parse(path.read_text(encoding="utf-8")))
        if any(w in n.lower() for w in FORBIDDEN_CALL)
    ]
    assert called == []


def test_the_scan_would_catch_a_violation():
    tree = ast.parse(
        "from app.hiring import introductions\nfrom app.hiring.api import introduction_ref\n"
        "introductions.accept_introduction(1)"
    )
    assert hiring_imports(tree) == ["app.hiring.introductions", "app.hiring.api"]
    assert call_names(tree) == ["accept_introduction"]


def test_the_action_type_enum_has_exactly_the_three_types(engine):
    types = [r[0] for r in rows(engine, "SELECT unnest(enum_range(NULL::action_type))::text")]
    assert types == ["submit_application", "send_follow_up", "twin_answer"]
    assert "share_profile" not in types


def test_scope_refuses_a_follow_up_to_the_employers_address_found_in_the_posting(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    posting_address = "recruiting@lumen.example"
    world = make_committed_world(engine, description=f"Build Python pipelines. Questions? Write to {posting_address}.")
    assert posting_address in scalar(engine, "SELECT description FROM jobs")
    blocked = propose_follow_up(world, payload=follow_up_payload(to=posting_address))
    assert (blocked.status, blocked.failed_codes) == ("blocked", ("restricted_data",))
    typed = propose_follow_up(world, step="follow_up:2")
    assert typed.status == "awaiting_approval"


def test_a_follow_up_with_no_address_typed_by_the_candidate_is_refused_whatever_it_says(
    engine, clean, monkeypatch, tmp_path
):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine, follow_up_email=None)
    proposed = propose_follow_up(world)
    assert (proposed.status, proposed.failed_codes) == ("blocked", ("restricted_data",))


def test_a_model_cannot_set_the_recipient_through_the_text(engine, clean, monkeypatch, tmp_path):
    entails(monkeypatch, tmp_path)
    world = make_committed_world(engine)
    payload = follow_up_payload()
    payload["body"] = payload["body"].replace(
        "Best regards", "Please also copy boss@elsewhere.example.\n\nBest regards"
    )
    proposed = propose_follow_up(world, payload=payload)
    assert proposed.status == "blocked"
    assert (
        set(proposed.failed_codes) >= {"citation_unresolved", "restricted_data"}
        or "restricted_data" in proposed.failed_codes
    )


def test_the_scope_check_alone_compares_the_recipient_with_the_typed_address():
    from dataclasses import replace
    from uuid import uuid4

    from app.guardian.policy import SubjectGate

    gate = SubjectGate("campaign", uuid4(), uuid4(), True, None, False, 1, None, "me@mail.example", 1)
    base = checks.CheckInput("send_follow_up", gate, {"body": "Hello."}, {"body": "candidate"}, "me@mail.example")
    assert checks.check_scope(base).passed
    assert checks.check_scope(replace(base, recipient="employer@mail.example")).reason_code == "restricted_data"
