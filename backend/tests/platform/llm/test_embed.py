"""Local embeddings (AI_SYSTEM 3.1): a small model that runs on this machine, so search needs no key.

The model tests use the real model from the local cache (fastembed downloads it on first use where it is not cached yet,
for example on a fresh CI machine). The rest use a stand-in that records what it was asked to embed.
"""

import math
import os
import re
from pathlib import Path

import pytest

from app.platform.llm import embed

DIMENSIONS = embed.DIMENSIONS
DOCKERFILE = Path(__file__).resolve().parents[3] / "Dockerfile"
CI_WORKFLOW = Path(__file__).resolve().parents[4] / ".github" / "workflows" / "ci.yml"


def length(vector):
    return math.sqrt(sum(x * x for x in vector))


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / (length(a) * length(b))


class RecordingModel:
    """Stands in for fastembed's TextEmbedding: records the calls and returns one zero vector per text."""

    embedding_size = DIMENSIONS

    def __init__(self):
        self.calls = []

    def embed(self, texts, **options):
        self.calls.append(("embed", list(texts), options))
        return iter([[0.0] * DIMENSIONS for _ in texts])

    def passage_embed(self, texts, **options):
        self.calls.append(("passage_embed", list(texts), options))
        return iter([[0.0] * DIMENSIONS for _ in texts])


@pytest.fixture
def no_cached_model():
    """Every test that swaps the model starts and ends with nothing remembered about loading it. A test asks for it
    before `monkeypatch`, so the patch is undone first and the real cached function is the one that is cleared."""
    embed._model.cache_clear()
    yield
    embed._model.cache_clear()


@pytest.fixture
def stand_in(no_cached_model, monkeypatch):
    model = RecordingModel()
    monkeypatch.setattr(embed, "_model", lambda: model)
    return model


def test_passages_are_384_floats_of_unit_length():
    vectors = embed.embed_passages(["a dental hygienist", "a long-haul truck operator"])

    assert len(vectors) == 2
    for vector in vectors:
        assert len(vector) == 384 and all(isinstance(x, float) for x in vector)
        assert math.isclose(length(vector), 1.0, abs_tol=1e-3)
    assert cosine(*vectors) < 0.95  # two different postings do not collapse onto one point


def test_a_query_is_the_passage_vector_of_the_prefix_and_the_text():
    query = embed.embed_query("fix teeth")
    with_prefix = embed.embed_passages([f"{embed.QUERY_PREFIX} fix teeth"])[0]
    bare = embed.embed_passages(["fix teeth"])[0]

    assert len(query) == 384 and math.isclose(length(query), 1.0, abs_tol=1e-3)
    assert cosine(query, with_prefix) > 0.9999
    assert cosine(query, bare) < 0.9999  # and the prefix really changes the vector


def test_the_query_prefix_is_applied_exactly_once(stand_in):
    embed.embed_query("fix teeth")

    [(method, texts, _)] = stand_in.calls
    assert method == "embed"  # fastembed's own query_embed adds no prefix for this model, so it is not used
    assert texts == [f"{embed.QUERY_PREFIX} fix teeth"]
    assert texts[0].count(embed.QUERY_PREFIX) == 1


def test_passages_carry_no_prefix_and_go_through_in_batches_of_64(stand_in):
    embed.embed_passages(["one", "two"])

    [(method, texts, options)] = stand_in.calls
    assert (method, texts, options) == ("passage_embed", ["one", "two"], {"batch_size": 64})


def test_no_text_means_no_model(stand_in):
    assert embed.embed_passages([]) == []
    assert stand_in.calls == []


class BrokenModel:
    """A model that loads and then fails while running."""

    embedding_size = DIMENSIONS

    def embed(self, texts, **options):
        raise RuntimeError("the runtime gave up")

    def passage_embed(self, texts, **options):
        raise RuntimeError("the runtime gave up")


def test_a_model_that_fails_while_running_is_unavailable_too(no_cached_model, monkeypatch):
    monkeypatch.setattr(embed, "_model", lambda: BrokenModel())

    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_query("anything")
    with pytest.raises(embed.EmbeddingUnavailable):
        embed.embed_passages(["anything"])


def test_a_model_that_cannot_load_is_unavailable_and_is_not_tried_again(no_cached_model, monkeypatch):
    import fastembed

    attempts = []

    class CannotLoad:
        def __init__(self, name):
            attempts.append(name)
            raise ValueError("Could not load model from any source.")

    monkeypatch.setattr(fastembed, "TextEmbedding", CannotLoad)

    for _ in range(2):
        with pytest.raises(embed.EmbeddingUnavailable):
            embed.embed_query("anything")
        with pytest.raises(embed.EmbeddingUnavailable):
            embed.embed_passages(["anything"])

    # A load can take half a minute to give up when the model is missing and the network is down, so it is tried once.
    assert attempts == [embed.LOCAL_MODEL_ID]


def test_a_model_with_the_wrong_number_of_dimensions_is_refused_at_load(no_cached_model, monkeypatch):
    import fastembed

    class WrongSize:
        """A model that works, but makes 768-dimension vectors."""

        embedding_size = 768

        def __init__(self, name):
            pass

        def embed(self, texts, **options):
            return iter([[0.0] * 768 for _ in texts])

    monkeypatch.setattr(fastembed, "TextEmbedding", WrongSize)

    with pytest.raises(embed.EmbeddingUnavailable):  # the vector(384) column would refuse every row
        embed.embed_query("anything")


def test_the_image_downloads_the_model_this_code_asks_for():
    """The Dockerfile names the model once more, to download it when the image is built. The two must not drift apart,
    or a container would start without the model it asks for."""
    named = re.search(r"^ARG EMBEDDING_MODEL=(\S+)", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)

    assert named is not None, "backend/Dockerfile must declare ARG EMBEDDING_MODEL"
    assert named.group(1) == embed.DEFAULT_MODEL


def test_ci_keeps_the_downloaded_model_between_runs_under_the_id_of_the_model_this_code_asks_for():
    """The retrieval tests run the real model, which fastembed downloads from Hugging Face unless it is in the folder
    FASTEMBED_CACHE_PATH names. ci.yml names the model once more, to key that folder's cache, so it must not drift."""
    backend_job = CI_WORKFLOW.read_text(encoding="utf-8").split("\n  eval-gate:")[0]

    assert re.search(rf"^\s+EMBEDDING_MODEL: {re.escape(embed.DEFAULT_MODEL)}(\s|$)", backend_job, re.MULTILINE)
    assert re.search(r"^\s+FASTEMBED_CACHE_PATH: /", backend_job, re.MULTILINE)
    assert re.search(r"uses: actions/cache@[0-9a-f]{40}\b", backend_job)  # pinned to a commit, like every action
    assert "path: ${{ env.FASTEMBED_CACHE_PATH }}" in backend_job
    assert "key: fastembed-${{ env.EMBEDDING_MODEL }}" in backend_job


def test_the_embedding_runtime_never_reports_to_its_vendor():
    # ONNX Runtime 1.29+ sends usage events to mobile.events.data.microsoft.com unless this is set before it loads
    # (measured 2026-11-28: a DNS probe in the backend image saw that lookup by default and none with the switch).
    # Importing the embed module is what every path to the model does first, so the module sets it.
    assert os.environ.get("ORT_DISABLE_TELEMETRY") == "1"


def test_the_image_build_that_downloads_the_model_has_the_switch_too():
    dockerfile = (Path(__file__).parents[3] / "Dockerfile").read_text(encoding="utf-8")
    download = next(line for line in dockerfile.splitlines() if "TextEmbedding(" in line)
    assert "ORT_DISABLE_TELEMETRY=1" in download
