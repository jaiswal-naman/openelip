# The backend image. The API, the worker, the migration step and the operator command line all run from it (ARCHITECTURE 2.3).
#
# The build context is the REPOSITORY ROOT, because the code reads files that sit beside backend/ (seed_candidates.txt in
# data/, the gold sets in evals/, the migrations in db/):
#     docker build -f backend/Dockerfile -t openelip-backend:dev .
# Inside the image the folders keep the repository's layout, which is what the code expects:
#     /srv/backend  /srv/db  /srv/data  /srv/evals
#
# Both base images are pinned by digest, so a rebuild cannot silently pick up different code. To move one, pull the new tag,
# read its digest (docker buildx imagetools inspect <image>:<tag>) and change it below. uv is the version that wrote uv.lock.
ARG PYTHON_IMAGE=python:3.11.18-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.10.4@sha256:4cac394b6b72846f8a85a7a0e577c6d61d4e17fe2ccee65d9451a8b3c9efb4ac
# The embedding model search uses (AI_SYSTEM 3.1). It is downloaded while the image is built and never at run time, so a
# container works with no network. A test checks that this default is the one app.platform.llm.embed asks for.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5

FROM ${UV_IMAGE} AS uv

# --- build: the locked runtime dependencies first (this layer is reused until pyproject.toml or uv.lock changes), then the code
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /srv/backend
COPY backend/pyproject.toml backend/uv.lock ./
# --frozen: install exactly what uv.lock says, never re-resolve. --no-dev: no test or lint tools in the image.
RUN uv sync --frozen --no-dev --no-install-project
# The embedding model goes into /srv/models. This layer comes before the code, so it is reused until the model name or the
# locked dependencies change, and the 65 MB download is not repeated on every code change.
ARG EMBEDDING_MODEL
RUN FASTEMBED_CACHE_PATH=/srv/models .venv/bin/python -c "import os; from fastembed import TextEmbedding; TextEmbedding(os.environ['EMBEDDING_MODEL'])"
COPY backend/ ./
RUN uv sync --frozen --no-dev

# --- runtime: the same base image, the finished environment and the code, run by a user with no shell login
FROM ${PYTHON_IMAGE} AS runtime
ARG EMBEDDING_MODEL
# HF_HUB_OFFLINE: the model is already in /srv/models, so a missing one fails at once (and search answers by keywords)
# instead of trying to download it.
ENV PATH=/srv/backend/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    EMBEDDING_MODEL=${EMBEDDING_MODEL} FASTEMBED_CACHE_PATH=/srv/models HF_HUB_OFFLINE=1
RUN groupadd --system --gid 1001 app && useradd --system --uid 1001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /srv/backend
COPY --from=build /srv/backend /srv/backend
COPY --from=build /srv/models /srv/models
COPY db /srv/db
COPY data /srv/data
COPY evals /srv/evals
USER app
EXPOSE 8000
# No access log: a request line carries the query string, which can hold what someone typed into a search box.
# Compose overrides this command for the worker and the migration step, and adds the proxy flags for the API.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
