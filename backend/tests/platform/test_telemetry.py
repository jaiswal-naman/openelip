"""Spans, counters and /metrics (ARCHITECTURE 9.1; requirement PLT-07). No database: a small app of its own."""

import logging
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.trace import StatusCode
from prometheus_client import REGISTRY

from app.platform import api, config, telemetry

LEAK = "canaryboomz7"


def count(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def make_app() -> FastAPI:
    app = FastAPI()
    telemetry.instrument(app)
    api.install(app)

    @app.get("/things/{thing_id}")
    def thing(thing_id: str) -> dict[str, str]:
        return {"id": thing_id}

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError(f"{LEAK} was pasted into an exception message")

    return app


def request_span(spans):
    (found,) = [s for s in spans.get_finished_spans() if s.name == "http.request"]
    return found


def test_a_request_span_carries_the_route_template_and_never_the_raw_path(spans):
    before = count("http_requests_total", method="GET", route="/things/{thing_id}", status="200")
    assert TestClient(make_app()).get("/things/canaryidz7?q=canaryqueryz7").status_code == 200
    done = request_span(spans)
    assert done.attributes == {"method": "GET", "route": "/things/{thing_id}", "status_code": 200}
    assert "canary" not in done.to_json().lower()
    assert count("http_requests_total", method="GET", route="/things/{thing_id}", status="200") == before + 1


def test_an_unknown_path_is_counted_as_unmatched_not_by_its_text(spans):
    before = count("http_requests_total", method="GET", route="unmatched", status="404")
    assert TestClient(make_app()).get("/nowhere/canarypathz7").status_code == 404
    assert request_span(spans).attributes["route"] == "unmatched"
    assert count("http_requests_total", method="GET", route="unmatched", status="404") == before + 1


def test_an_unknown_http_method_is_counted_as_other(spans):
    before = count("http_requests_total", method="OTHER", route="/things/{thing_id}", status="405")
    assert TestClient(make_app()).request("CANARYMETHODZ7", "/things/1").status_code == 405
    assert request_span(spans).attributes["method"] == "OTHER"
    assert count("http_requests_total", method="OTHER", route="/things/{thing_id}", status="405") == before + 1
    assert "canarymethodz7" not in telemetry.metrics().body.decode().lower()


def test_an_unhandled_exception_is_a_500_and_an_error_span_naming_only_the_class(spans):
    before = count("http_requests_total", method="GET", route="/boom", status="500")
    assert TestClient(make_app(), raise_server_exceptions=False).get("/boom").status_code == 500
    done = request_span(spans)
    assert done.attributes["status_code"] == 500 and done.attributes["error.type"] == "RuntimeError"
    assert done.status.status_code == StatusCode.ERROR and done.status.description is None
    assert done.events == () and LEAK not in done.to_json().lower()
    assert count("http_requests_total", method="GET", route="/boom", status="500") == before + 1


def test_metrics_is_prometheus_text_and_is_not_part_of_the_api_schema():
    app = make_app()
    client = TestClient(app)
    client.get("/things/1")
    response = client.get("/metrics")
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/plain")
    assert 'http_requests_total{method="GET",route="/things/{thing_id}",status="200"}' in response.text
    assert "/metrics" not in app.openapi()["paths"]


def test_span_attributes_follow_the_log_rule(spans):
    user = uuid.uuid4()
    with telemetry.span("unit.work", job_id=7, is_ok=True, ratio=0.5, user=user, kind="ingest.fetch", skipped=None):
        pass
    (done,) = spans.get_finished_spans()
    assert done.attributes == {"job_id": 7, "is_ok": True, "ratio": 0.5, "user": str(user), "kind": "ingest.fetch"}


def test_text_under_another_key_raises_in_tests_and_is_dropped_in_production(spans, monkeypatch):
    with pytest.raises(ValueError, match="text is not allowed"), telemetry.span("unit.work", resume="canaryresumez7"):
        pass
    assert spans.get_finished_spans() == ()
    monkeypatch.setenv("APP_ENV", "production")
    with telemetry.span("unit.work", resume="canaryresumez7", job_id=3):
        pass
    (done,) = spans.get_finished_spans()
    assert done.attributes == {"job_id": 3, "redacted": True}


def test_an_exception_ends_the_span_as_an_error_naming_only_its_class(spans):
    before = count("operations_total", operation="unit.fail", outcome="error")
    with pytest.raises(RuntimeError), telemetry.span("unit.fail"):
        raise RuntimeError(f"{LEAK} was pasted here")
    (done,) = spans.get_finished_spans()
    assert done.attributes["error.type"] == "RuntimeError"
    assert done.status.status_code == StatusCode.ERROR and done.status.description is None
    assert done.events == () and LEAK not in done.to_json().lower()
    assert count("operations_total", operation="unit.fail", outcome="error") == before + 1


def test_a_finished_span_counts_once_by_name_and_outcome(spans):
    before = count("operations_total", operation="unit.fine", outcome="ok")
    with telemetry.span("unit.fine"):
        pass
    assert count("operations_total", operation="unit.fine", outcome="ok") == before + 1


def test_setting_an_export_endpoint_warns_that_export_is_off(monkeypatch, caplog):
    settings = config.load({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.invalid:4318"})
    monkeypatch.setattr(config, "get", lambda: settings)
    with caplog.at_level(logging.WARNING, logger="app.event"):
        telemetry.instrument(FastAPI())
    assert "telemetry.export_off" in caplog.text


def test_no_endpoint_means_no_warning(monkeypatch, caplog):
    settings = config.load({})
    monkeypatch.setattr(config, "get", lambda: settings)
    with caplog.at_level(logging.WARNING, logger="app.event"):
        telemetry.instrument(FastAPI())
    assert "telemetry.export_off" not in caplog.text
