"""The API shell: liveness, the error envelope, request ids and the routes later plans fill in."""

import pytest


def test_healthz_is_live(api_client):
    response = api_client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"ok": True}


def test_an_unknown_route_returns_the_error_envelope(api_client):
    response = api_client.get("/v1/nothing-here")
    body = response.json()
    assert response.status_code == 404
    assert body["success"] is False and body["data"] is None
    assert body["error"] == {"code": "not_found", "message": "No such resource.", "details": {}}
    assert body["meta"]["request_id"].startswith("req_")
    assert body["meta"]["request_id"] == response.headers["x-request-id"]


def test_a_wrong_method_is_an_invalid_request(api_client):
    response = api_client.post("/healthz")
    assert response.status_code == 405 and response.json()["error"]["code"] == "invalid_request"


def test_a_bad_field_is_a_validation_error_that_names_the_field_and_not_the_value(api_client):
    response = api_client.get("/v1/jobs", params={"limit": "999"})
    error = response.json()["error"]
    assert response.status_code == 422 and error["code"] == "validation_failed"
    assert [f["field"] for f in error["details"]["fields"]] == ["limit"]
    assert "999" not in response.text


def test_malformed_json_is_an_invalid_request(api_client):
    response = api_client.post("/v1/auth/codes", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "invalid_request"


def test_an_unhandled_error_is_answered_in_the_envelope_without_its_message(api_client, caplog):
    def boom():
        raise RuntimeError("resume text canary")

    api_client.app.add_api_route("/boom", boom)
    response = api_client.get("/boom")  # the client would re-raise if the error escaped the middleware
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "canary" not in response.text and "canary" not in caplog.text
    assert any("api.unhandled" in record.getMessage() for record in caplog.records)


def test_every_request_gets_its_own_id(api_client):
    ids = {api_client.get("/healthz").headers["x-request-id"] for _ in range(5)}
    assert len(ids) == 5


# The routes and models plans 01-03 and 01-04 fill in. The web types are generated from them, so the fields are fixed.
CONTRACT = [
    ("post", "/v1/auth/codes"),
    ("post", "/v1/auth/sessions"),
    ("delete", "/v1/auth/sessions/current"),
    ("get", "/v1/me"),
    ("patch", "/v1/me"),
    ("get", "/v1/control/costs"),
    ("get", "/v1/privacy/subprocessors"),
    ("get", "/v1/jobs"),
    ("get", "/v1/stats"),
    ("get", "/healthz"),
]
FIELDS = {
    "Me": {"id", "email", "timezone", "created_at", "consents"},  # consents: plan 03-01 (TRU-05)
    "CostRow": {"period", "task", "calls", "cost_usd", "input_tokens", "output_tokens", "avg_latency_ms", "models"},
    "Subprocessor": {"name", "kind", "purpose", "receives", "tasks"},
}


@pytest.fixture
def openapi(api_client):
    return api_client.get("/openapi.json").json()


@pytest.mark.parametrize(("method", "path"), CONTRACT)
def test_the_contract_routes_exist(openapi, method, path):
    assert method in openapi["paths"][path]


@pytest.mark.parametrize("model", sorted(FIELDS))
def test_the_contract_models_have_their_fixed_fields(openapi, model):
    assert set(openapi["components"]["schemas"][model]["properties"]) == FIELDS[model]
