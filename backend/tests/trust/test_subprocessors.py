"""GET /v1/privacy/subprocessors is generated from the running configuration, never hard-coded (TRU-04, T-01-20)."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.platform import config
from app.platform.llm import registry
from app.trust.routes import RECEIVES

SECOND_TASK = """
[providers.fake]
label = "Recorded responses"
adapter = "fake"
is_local = true
retention_note = "No model is called."
[tasks.ping]
description = "Self-test."
data_class = "synthetic"
max_tokens = 8
timeout_seconds = 1
[tasks.ping.models.fake]
model = "recorded"
[tasks.read_jobs]
description = "Reads job postings."
data_class = "public"
max_tokens = 8
timeout_seconds = 1
[tasks.read_jobs.models.fake]
model = "recorded"
"""
PING_ONLY = SECOND_TASK.split("[tasks.read_jobs]")[0]


@pytest.fixture
def serve(monkeypatch, tmp_path):
    def go(toml: str | None = None, **env: str):
        settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "fake", **env})
        monkeypatch.setattr(config, "get", lambda: settings)
        if toml:
            (tmp_path / "models.toml").write_text(toml, encoding="utf-8")
            monkeypatch.setattr(registry, "PATH", tmp_path / "models.toml")
        return TestClient(create_app()).get("/v1/privacy/subprocessors")  # no session: it is public

    return go


def test_the_fake_provider_and_the_email_host_are_listed_for_anyone(serve):
    response = serve()
    data = response.json()["data"]
    assert response.status_code == 200 and response.json()["success"] is True
    assert [(i["name"], i["kind"]) for i in data["items"]] == [
        ("Recorded responses", "model_provider"),
        ("mail", "email"),
    ]
    assert "ping" in data["items"][0]["tasks"] and data["items"][1]["tasks"] == []
    assert "cannot be reached" in data["outside_copies_statement"]


def test_a_configured_provider_is_listed_with_what_it_receives_and_keeps(serve):
    response = serve(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-secret-123")
    item = response.json()["data"]["items"][0]
    assert item["name"] == "Anthropic" and "ping" in item["tasks"]
    for fact in ("Invented test messages only.", "29 days", "24 hours", "2 years"):
        assert fact in item["receives"]
    assert "sk-secret-123" not in response.text


def test_a_provider_with_no_key_receives_nothing_so_it_is_not_listed(serve):
    assert [i["kind"] for i in serve(LLM_PROVIDER="anthropic").json()["data"]["items"]] == ["email"]


def test_a_local_server_is_named_as_such(serve):
    items = serve(LLM_PROVIDER="openai_compatible", LLM_BASE_URL="http://llm:11434/v1").json()["data"]["items"]
    assert items[0]["name"] == "Local model server"


def test_the_list_changes_when_the_toml_changes(serve):
    before = serve(PING_ONLY).json()["data"]["items"][0]
    after = serve(SECOND_TASK).json()["data"]["items"][0]
    assert before["tasks"] == ["ping"] and after["tasks"] == ["ping", "read_jobs"]
    assert RECEIVES["public"] in after["receives"] and RECEIVES["public"] not in before["receives"]


def test_the_email_host_is_named_without_its_credentials(serve):
    response = serve(SMTP_URL="smtp://user:hunter2@smtp.example.org:587")
    assert response.json()["data"]["items"][-1]["name"] == "smtp.example.org" and "hunter2" not in response.text


def test_every_data_class_has_its_plain_words():
    assert set(RECEIVES) == set(registry.DATA_CLASSES)


def test_openai_is_listed_as_receiving_search_queries_and_profile_passages_through_the_embed_task(serve):
    """The query typed into the job search and, for a person who agreed, their profile passage are sent to OpenAI by the
    embed task, so it must be among the tasks OpenAI is disclosed for, and its words say what is sent."""
    item = serve(LLM_PROVIDER="openai", OPENAI_API_KEY="sk-secret-123").json()["data"]["items"][0]

    assert item["name"] == "OpenAI" and "embed" in item["tasks"]
    assert "search queries" in item["purpose"] and "profile passage" in item["purpose"]
    assert RECEIVES["identified"] in item["receives"] and "30 days" in item["receives"]


def test_an_anthropic_installation_discloses_no_embed_task_because_nothing_is_sent_for_embedding(serve):
    item = serve(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="sk-secret-123").json()["data"]["items"][0]

    assert "embed" not in item["tasks"]


def test_the_embed_task_does_not_change_the_ai_processing_wording_people_already_agreed_to(monkeypatch):
    """The version is hashed from the provider, the data classes it receives and its retention note, not from the task
    list, and OpenAI's classes already include identified (the resume tasks), so adding embed asks nobody to agree
    again."""
    from app.trust import consent

    settings = config.load({"APP_ENV": "test", "LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k"})
    monkeypatch.setattr(config, "get", lambda: settings)
    with_embed = consent.ai_processing_text_version()
    every_route = registry.table
    monkeypatch.setattr(registry, "table", lambda: [r for r in every_route() if r.task != "embed"])

    assert consent.ai_processing_text_version() == with_embed
