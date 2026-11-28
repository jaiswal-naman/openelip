"""The embeddings call through the gateway (AI_SYSTEM 3.1, 2.7): priced, batched, recorded, never stored with its text.

The OpenAI SDK is replaced by a stub that returns the SDK's own response objects, and the gateway tests run on the
harness database, so the ledger rows are read back exactly as the cost endpoint would read them. No network.
"""

from types import SimpleNamespace

import httpx2
import openai
import pytest
from openai.types import CreateEmbeddingResponse, Embedding
from openai.types.create_embedding_response import Usage
from sqlalchemy import text

from app.platform import config
from app.platform.llm import AIDisabled, ProviderUnavailable, adapters, gateway, registry
from app.platform.llm.adapters import EmbeddingReply

REQUEST = httpx2.Request("POST", "https://provider.example/v1/embeddings")
OPENAI = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k"}
LARGE = [0.5] * 1536


def route() -> registry.Route:
    return registry.route("embed", config.load({"APP_ENV": "test", **OPENAI}))


def response(vectors, tokens, order=None):
    order = order or range(len(vectors))
    data = [Embedding(embedding=v, index=i, object="embedding") for i, v in zip(order, vectors)]
    return CreateEmbeddingResponse(
        data=data, model="text-embedding-3-large", object="list", usage=Usage(prompt_tokens=tokens, total_tokens=tokens)
    )


def stub(monkeypatch, outcome):
    """Replace the SDK's client class; the stub records its constructor and request arguments."""
    seen: dict = {}

    class Client:
        def __init__(self, **kwargs):
            seen["init"] = kwargs
            self.embeddings = SimpleNamespace(create=self.create)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def create(self, **kwargs):
            seen["request"] = kwargs
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(openai, "OpenAI", Client)
    return seen


# ---- the shipped route -----------------------------------------------------------------------------------------------


def test_the_shipped_embed_route_is_openai_large_at_1536_with_a_checked_price():
    found = route()

    assert (found.provider, found.model, found.kind, found.dimensions) == (
        "openai",
        "text-embedding-3-large",
        "embedding",
        1536,
    )
    assert (found.input_microusd_per_mtok, found.output_microusd_per_mtok) == (130_000, 0)  # USD 0.13 per million
    assert found.price_checked_at is not None and found.priced


@pytest.mark.parametrize(
    "env",
    [
        {"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"},
        {"LLM_PROVIDER": "openai_compatible", "LLM_BASE_URL": "http://llm:11434/v1"},
        {"LLM_PROVIDER": "openai"},
    ],
)
def test_no_other_provider_serves_embed_and_no_key_means_no_route(env):
    with pytest.raises(AIDisabled):
        registry.route("embed", config.load({"APP_ENV": "test", **env}))


def test_the_embed_route_is_among_the_active_routes_so_trust_names_it():
    tasks = [r.task for r in registry.active_routes(config.load({"APP_ENV": "test", **OPENAI}))]
    assert "embed" in tasks
    assert "embed" not in [
        r.task
        for r in registry.active_routes(
            config.load({"APP_ENV": "test", "LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"})
        )
    ]


# ---- the adapter -----------------------------------------------------------------------------------------------------


def test_the_adapter_asks_for_1536_dimensions_and_returns_the_vectors_in_input_order(monkeypatch):
    seen = stub(monkeypatch, response([[1.0] + [0.0] * 1535, [0.0, 1.0] + [0.0] * 1534], 12, order=[1, 0]))

    reply = adapters.call_openai_embeddings(route(), ["first", "second"], timeout=9.0, retries=1)

    assert seen["request"] == {"model": "text-embedding-3-large", "input": ["first", "second"], "dimensions": 1536}
    assert (seen["init"]["timeout"], seen["init"]["max_retries"], seen["init"]["api_key"]) == (9.0, 1, "k")
    assert reply == EmbeddingReply(
        [[0.0, 1.0] + [0.0] * 1534, [1.0] + [0.0] * 1535], 12
    )  # index 0 first, whatever order


def test_a_timeout_and_any_other_sdk_error_become_provider_unavailable_without_the_message(monkeypatch):
    stub(monkeypatch, openai.APITimeoutError(REQUEST))
    with pytest.raises(ProviderUnavailable) as timed_out:
        adapters.call_openai_embeddings(route(), ["x"], timeout=1.0, retries=0)
    assert timed_out.value.timed_out

    stub(monkeypatch, openai.APIConnectionError(message="secret text of the request", request=REQUEST))
    with pytest.raises(ProviderUnavailable) as failed:
        adapters.call_openai_embeddings(route(), ["x"], timeout=1.0, retries=0)
    assert not failed.value.timed_out and "secret" not in repr(failed.value.__dict__)


def test_a_reply_that_does_not_cover_every_input_once_is_a_provider_failure(monkeypatch):
    stub(monkeypatch, response([LARGE], 5))
    with pytest.raises(ProviderUnavailable):
        adapters.call_openai_embeddings(route(), ["one", "two"], timeout=1.0, retries=0)


# ---- the gateway -----------------------------------------------------------------------------------------------------


class Adapter:
    """Replaces the adapter: records each request and answers every input with LARGE, 100 tokens an input."""

    def __init__(self, fail_on=None):
        self.requests = []
        self.fail_on = fail_on

    def __call__(self, found, texts, *, timeout, retries):
        self.requests.append({"texts": list(texts), "timeout": timeout, "retries": retries, "model": found.model})
        if self.fail_on == len(self.requests):
            raise ProviderUnavailable(timed_out=True)
        return EmbeddingReply([LARGE for _ in texts], 100 * len(texts))


@pytest.fixture
def adapter(monkeypatch):
    stand_in = Adapter()
    monkeypatch.setattr(adapters, "call_openai_embeddings", stand_in)
    return stand_in


def test_texts_go_in_batches_of_at_most_100_and_every_request_leaves_one_priced_ledger_row(harness, adapter):
    harness.configure(**OPENAI)
    user = harness.user()
    texts = [f"text {n}" for n in range(250)]

    vectors = gateway.embed_texts(texts, principal=user)

    assert [len(r["texts"]) for r in adapter.requests] == [100, 100, 50]
    assert [t for r in adapter.requests for t in r["texts"]] == texts  # in order, none twice
    assert len(vectors) == 250 and all(len(v) == 1536 for v in vectors)
    rows = harness.ledger()
    assert [(r["task"], r["outcome"], r["user_id"], r["provider"], r["model_name"]) for r in rows] == [
        ("embed", "ok", user, "openai", "text-embedding-3-large")
    ] * 3
    assert [(r["input_tokens"], r["output_tokens"], r["cost_microusd"]) for r in rows] == [
        (10000, 0, 1300),
        (10000, 0, 1300),
        (5000, 0, 650),
    ]  # tokens x USD 0.13 per million, in micro-dollars
    assert {r["prompt_version"] for r in rows} == {gateway.EMBED_PROMPT_VERSION}


def test_the_model_is_registered_as_an_embedding_model_with_its_dimensions(harness, adapter):
    harness.configure(**OPENAI)

    gateway.embed_texts(["a"], principal=None)

    with harness.engine.connect() as conn:
        found = conn.execute(
            text("SELECT kind::text AS kind, embedding_dimensions, is_local FROM model_registry")
        ).one()
    assert (found.kind, found.embedding_dimensions, found.is_local) == ("embedding", 1536, False)


def test_the_full_run_and_the_quick_run_use_the_route_timeout_and_a_short_one_without_retries(harness, adapter):
    harness.configure(**OPENAI)

    gateway.embed_texts(["a"], principal=None)
    gateway.embed_texts(["a"], principal=None, quick=True)

    full, quick = adapter.requests
    assert (full["timeout"], full["retries"]) == (route().timeout_seconds, adapters.SDK_RETRIES)
    assert (quick["timeout"], quick["retries"]) == (gateway.QUERY_TIMEOUT_SECONDS, 0)
    assert gateway.QUERY_TIMEOUT_SECONDS <= 5  # a search waits for this, once


def test_a_failed_request_is_recorded_and_stops_the_run_after_the_batches_that_did_succeed(harness, monkeypatch):
    harness.configure(**OPENAI)
    adapter = Adapter(fail_on=2)
    monkeypatch.setattr(adapters, "call_openai_embeddings", adapter)

    with pytest.raises(ProviderUnavailable):
        gateway.embed_texts([f"t{n}" for n in range(250)], principal=None)

    assert len(adapter.requests) == 2  # the third batch is never tried
    rows = harness.ledger()
    assert [(r["outcome"], r["input_tokens"], r["cost_microusd"]) for r in rows] == [
        ("ok", 10000, 1300),
        ("timeout", 0, 0),
    ]


def test_a_route_that_is_not_there_calls_nothing_and_writes_nothing(harness, adapter):
    harness.configure(LLM_PROVIDER="anthropic", ANTHROPIC_API_KEY="k")

    with pytest.raises(AIDisabled):
        gateway.embed_texts(["a"], principal=None)

    assert adapter.requests == [] and harness.ledger() == []


def test_no_text_is_no_request(harness, adapter):
    harness.configure(**OPENAI)

    assert gateway.embed_texts([], principal=None) == []
    assert adapter.requests == [] and harness.ledger() == []


def test_a_call_made_in_the_api_process_is_recorded_by_the_api_login_as_the_signed_in_person(harness, adapter):
    harness.configure(**OPENAI)
    user = harness.user()

    gateway.embed_texts(["a search"], principal=user, ledger="api", quick=True)

    [row] = harness.ledger()
    assert (row["task"], row["user_id"], row["outcome"]) == ("embed", user, "ok")


def test_the_ledger_never_holds_the_text_that_was_embedded(harness, adapter):
    harness.configure(**OPENAI)

    gateway.embed_texts(["the private words of a person"], principal=harness.user())

    assert "private words" not in harness.everything_stored()
