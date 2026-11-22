"""log.event is the only way the application logs, and it keeps content out of logs (PLT-07)."""

import json
import logging
import uuid
from datetime import datetime, timezone
from enum import Enum

import pytest

from app.platform import log


def _line(caplog: pytest.LogCaptureFixture) -> dict:
    (record,) = [r for r in caplog.records if r.name == "app.event"]
    return json.loads(record.getMessage())


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="app.event")


def test_an_event_is_one_json_line_with_its_fields(caplog):
    job = uuid.uuid4()
    log.event("job.done", job_id=job, duration_ms=12, status="ok")
    line = _line(caplog)
    assert line["event"] == "job.done" and line["level"] == "info" and "ts" in line
    assert line["job_id"] == str(job) and line["duration_ms"] == 12 and line["status"] == "ok"


def test_numbers_booleans_ids_enums_and_datetimes_are_accepted_under_any_key(caplog):
    class Color(Enum):
        RED = "red"

    log.event(
        "x.y",
        attempts=3,
        ratio=0.5,
        ok=True,
        nothing=None,
        who=uuid.UUID(int=1),
        color=Color.RED,
        at=datetime(2026, 11, 22, tzinfo=timezone.utc),
    )
    line = _line(caplog)
    assert line["attempts"] == 3 and line["ratio"] == 0.5 and line["ok"] is True and line["nothing"] is None
    assert line["who"] == str(uuid.UUID(int=1)) and line["color"] == "red" and line["at"].startswith("2026-11-22")


def test_every_identifier_key_accepts_a_string(caplog):
    log.event("x.y", **{key: "value" for key in log.IDENTIFIER_KEYS})
    assert set(_line(caplog)) >= log.IDENTIFIER_KEYS


def test_text_under_an_unlisted_key_is_refused_in_tests():
    with pytest.raises(ValueError) as refused:
        log.event("auth.failed", email="canary@example.test")
    assert "email" in str(refused.value)  # the key is named
    assert "canary@example.test" not in str(refused.value)  # the value never is


def test_text_under_an_unlisted_key_is_dropped_and_flagged_outside_tests(caplog, monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    log.event("auth.failed", email="canary@example.test", outcome="wrong_code")
    line = _line(caplog)
    assert "email" not in line and line["redacted"] is True and line["outcome"] == "wrong_code"
    assert "canary@example.test" not in json.dumps(line)


def test_a_list_or_a_dict_is_text_too(caplog):
    with pytest.raises(ValueError):
        log.event("x.y", things=["a"])
    with pytest.raises(ValueError):
        log.event("x.y", things={"a": 1})


def test_an_exception_is_logged_by_its_class_name_never_its_message(caplog):
    log.event("queue.job", error=RuntimeError("resume text canary"))
    line = _line(caplog)
    assert line["error"] == "RuntimeError"
    assert "canary" not in json.dumps(line)


def test_a_line_logged_during_a_request_carries_the_request_id(caplog):
    token = log.request_id.set("req_abc123")
    try:
        log.event("x.y")
    finally:
        log.request_id.reset(token)
    assert _line(caplog)["request_id"] == "req_abc123"
