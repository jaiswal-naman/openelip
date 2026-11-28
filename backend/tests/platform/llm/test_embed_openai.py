"""Which embedding model is active (AI_SYSTEM 3.1), and what embed.py asks of the gateway when it is OpenAI's.

The settings are built with config.load, so a real key in a developer's shell never reaches a test, and the gateway is
replaced by a recorder: nothing here loads a model, opens a database or touches the network.
"""

import math

import pytest

from app.platform import config
from app.platform.llm import embed, gateway
from app.platform.llm.failures import AIDisabled, ProviderUnavailable

OPENAI = {"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "k"}


def use(monkeypatch, **env):
    settings = config.load({"APP_ENV": "test", **env})
    monkeypatch.setattr(config, "get", lambda: settings)
    return settings


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"EMBEDDING_PROVIDER": "auto", **OPENAI}, embed.OPENAI_MODEL_ID),
        (
            {"EMBEDDING_PROVIDER": "auto", "LLM_PROVIDER": "openai_compatible", "OPENAI_API_KEY": "k"},
            embed.OPENAI_MODEL_ID,
        ),
        ({**OPENAI}, embed.OPENAI_MODEL_ID),  # auto is the default
        ({"EMBEDDING_PROVIDER": "local", **OPENAI}, embed.LOCAL_MODEL_ID),  # local forces the private choice
        ({"EMBEDDING_PROVIDER": "auto", "LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"}, embed.LOCAL_MODEL_ID),
        ({"EMBEDDING_PROVIDER": "auto", "LLM_PROVIDER": "fake"}, embed.LOCAL_MODEL_ID),
        (
            {"EMBEDDING_PROVIDER": "auto", "LLM_PROVIDER": "openai_compatible", "LLM_BASE_URL": "http://llm:11434/v1"},
            embed.LOCAL_MODEL_ID,
        ),  # a model server of your own is not OpenAI, even though it speaks the same protocol
        ({"EMBEDDING_PROVIDER": "auto", "LLM_PROVIDER": "openai"}, embed.LOCAL_MODEL_ID),  # no key, so no call
        ({"EMBEDDING_PROVIDER": "openai", **OPENAI}, embed.OPENAI_MODEL_ID),
    ],
)
def test_the_active_model_follows_the_provider_the_platform_runs_on(monkeypatch, env, expected):
    use(monkeypatch, **env)

    assert embed.active_model() == expected


@pytest.mark.parametrize(
    "env",
    [
        {"EMBEDDING_PROVIDER": "openai"},  # no key at all
        {"EMBEDDING_PROVIDER": "openai", "LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "k"},
        {"EMBEDDING_PROVIDER": "openai", "LLM_PROVIDER": "fake"},
    ],
)
def test_openai_without_a_usable_openai_route_is_unavailable_and_never_becomes_the_local_model(monkeypatch, env):
    """The person asked for OpenAI vectors; bge vectors cannot be compared with them, so nothing is quietly swapped."""
    use(monkeypatch, **env)
    monkeypatch.setattr(embed, "_model", lambda: pytest.fail("the local model must not be loaded"))
    monkeypatch.setattr(gateway, "embed_texts", lambda *a, **k: pytest.fail("there is no route to call"))

    assert embed.active_model() == embed.OPENAI_MODEL_ID
    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_passages(["a posting"])
    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_query("a query")


class Gateway:
    """Stands in for gateway.embed_texts: records what it was asked and answers with the vectors it was given."""

    def __init__(self, vectors=None, error=None):
        self.calls = []
        self.vectors = vectors
        self.error = error

    def __call__(self, texts, **options):
        self.calls.append((list(texts), options))
        if self.error:
            raise self.error
        return self.vectors if self.vectors is not None else [unit(embed.OPENAI_DIMENSIONS) for _ in texts]


def unit(dimensions, scale=1.0):
    vector = [0.0] * dimensions
    vector[0] = scale
    return vector


@pytest.fixture
def openai_gateway(monkeypatch):
    use(monkeypatch, **OPENAI)
    stand_in = Gateway()
    monkeypatch.setattr(gateway, "embed_texts", stand_in)
    monkeypatch.setattr(embed, "_model", lambda: pytest.fail("the local model must not be loaded"))
    return stand_in


def test_passages_go_to_the_gateway_as_they_are_for_the_ledger_login_asked(openai_gateway):
    person = object()

    vectors = embed.embed_passages(["one", "two"], principal=person, ledger="match")

    assert [len(v) for v in vectors] == [1536, 1536]
    assert openai_gateway.calls == [(["one", "two"], {"principal": person, "ledger": "match", "quick": False})]


def test_a_query_has_no_prefix_and_takes_the_quick_path(openai_gateway):
    person = object()

    embed.embed_query("fix teeth", principal=person, ledger="api")

    assert openai_gateway.calls == [(["fix teeth"], {"principal": person, "ledger": "api", "quick": True})]
    assert embed.QUERY_PREFIX not in openai_gateway.calls[0][0][0]  # only bge is trained on that instruction


def test_the_api_login_cannot_record_a_call_that_is_nobodys_so_a_search_with_nobody_signed_in_sends_nothing(
    openai_gateway,
):
    """The API's ledger rows must be the signed-in person's own (row-level security on llm_calls). A paid request that
    could not be recorded must not be made, so an anonymous visitor's search stays on keywords."""
    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_query("fix teeth", principal=None, ledger="api")

    assert openai_gateway.calls == []
    embed.embed_passages(["a posting"], principal=None, ledger="worker")  # the worker may record a call with no person
    assert len(openai_gateway.calls) == 1


def test_no_text_means_no_request(openai_gateway):
    assert embed.embed_passages([]) == []
    assert openai_gateway.calls == []


def test_a_caller_that_expects_another_model_is_refused_before_anything_is_sent(openai_gateway):
    """The sweep reads the model once and writes it beside every vector. If the key appeared in between, the vectors
    would be OpenAI's under bge's name, so the embedder refuses to answer for a model it is no longer running."""
    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_passages(["a posting"], model=embed.LOCAL_MODEL_ID)

    assert openai_gateway.calls == []
    assert len(embed.embed_passages(["a posting"], model=embed.OPENAI_MODEL_ID)) == 1


def test_a_caller_that_expects_the_local_model_is_refused_when_the_platform_has_moved_to_openai(monkeypatch):
    use(monkeypatch, **OPENAI)
    monkeypatch.setattr(embed, "_model", lambda: pytest.fail("not this one"))

    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_query("q", model=embed.LOCAL_MODEL_ID)


@pytest.mark.parametrize("error", [ProviderUnavailable(), ProviderUnavailable(timed_out=True), AIDisabled("no_key")])
def test_a_provider_that_fails_makes_the_embedder_unavailable_for_both_paths(monkeypatch, error):
    use(monkeypatch, **OPENAI)
    monkeypatch.setattr(gateway, "embed_texts", Gateway(error=error))

    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_passages(["a posting"])
    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_query("a query")


def test_a_vector_that_is_not_1536_wide_is_refused_because_the_index_and_the_query_cannot_use_it(
    monkeypatch, openai_gateway
):
    openai_gateway.vectors = [unit(1000)]

    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_passages(["a posting"])


def test_a_reply_with_a_different_count_than_the_request_is_refused(monkeypatch, openai_gateway):
    openai_gateway.vectors = [unit(1536)]

    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_passages(["one", "two"])


def test_vectors_that_arrive_unit_length_are_passed_on_unchanged(openai_gateway):
    arrived = unit(1536)

    assert embed.embed_passages(["a posting"]) == [arrived]


def test_vectors_that_are_not_unit_length_are_scaled_to_it_and_a_zero_vector_is_refused(monkeypatch, openai_gateway):
    """The API's vectors are unit length by its own account; this checks that, so a change on its side cannot slip a
    differently scaled vector into the column unnoticed. Cosine ranking does not need it; the convention does."""
    openai_gateway.vectors = [unit(1536, scale=3.0)]
    [scaled] = embed.embed_passages(["a posting"])
    assert math.isclose(math.sqrt(sum(x * x for x in scaled)), 1.0, abs_tol=1e-9)

    openai_gateway.vectors = [[0.0] * 1536]
    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_passages(["a posting"])


def test_the_local_model_is_still_the_one_that_answers_when_openai_is_not_active(monkeypatch):
    use(monkeypatch, EMBEDDING_PROVIDER="local", **OPENAI)
    monkeypatch.setattr(gateway, "embed_texts", lambda *a, **k: pytest.fail("the local model needs no gateway"))
    seen = []

    class Local:
        embedding_size = embed.DIMENSIONS

        def passage_embed(self, texts, **options):
            seen.append(list(texts))
            return iter([[0.0] * embed.DIMENSIONS for _ in texts])

        def embed(self, texts, **options):
            seen.append(list(texts))
            return iter([[0.0] * embed.DIMENSIONS for _ in texts])

    monkeypatch.setattr(embed, "_model", lambda: Local())

    embed.embed_passages(["p"], model=embed.LOCAL_MODEL_ID, principal=object(), ledger="api")
    embed.embed_query("q", model=embed.LOCAL_MODEL_ID)

    assert seen == [["p"], [f"{embed.QUERY_PREFIX} q"]]
