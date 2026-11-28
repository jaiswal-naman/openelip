"""Embedding models: jobs.embedding holds a vector of any one width, with an HNSW index for each model (AI_SYSTEM 3.1).

The column was vector(384), the width of the local model. OpenAI's text-embedding-3-large, shortened to 1536 by the API,
needs another width in the same column, and an HNSW index needs a fixed width, so:

* jobs.embedding becomes plain `vector`. Every row keeps its vector and its embedding_model; the table's CHECK that the
  two are null together stays.
* jobs_embedding_hnsw (on the whole column) is replaced by two partial expression indexes, one per model, as pgvector's
  README describes for mixed widths: the cast gives the index its width and the WHERE keeps the other model's rows out.
  A query uses one only if it ranks by `embedding::vector(N)` directly over the jobs table and names the model in its
  WHERE; the search and retrieval statements rank inside a MATERIALIZED CTE, as an exact scan over the rows the filters
  kept (DATA_MODEL 6.1), so today neither index serves them. They are here for the day that changes.

Nothing is re-embedded here: the embed sweep does that, once the application's model is another (a row of another model
is "not yet embedded" to it). The ids of the two models are repeated from app/platform/llm/embed.py, because a migration
cannot import the application; EMBEDDING_MODEL overrides the local one, as it does for the application.

One transaction as app_owner, with a lock timeout, so a failure leaves the database as it was. Building an index is
quick at this size, but pgvector's parallel HNSW build asks for shared memory that a container does not have (see
p2_0201_job_index.py), so no parallel workers. downgrade() exists for the CI migration check on a scratch database;
it is lossy (vectors of any width but 384 are cleared) and its first statement refuses to run on the database
called app.
"""

import os

from alembic import op

revision = "e1_embedding_models"
down_revision = "p9_0901_twin_agentnet"
branch_labels = None
depends_on = None

LOCK_TIMEOUT = "15s"  # as in p2_0201_job_index: stop and roll back rather than queue behind a long query
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-en-v1.5"
OPENAI_MODEL = "openai/text-embedding-3-large@1536"


def _local_model() -> str:
    model = os.environ.get("EMBEDDING_MODEL") or DEFAULT_LOCAL_MODEL
    return model.replace("'", "''")  # it goes into an index predicate, which cannot take a bound parameter


def _upgrade_sql() -> str:
    return f"""
DROP INDEX jobs_embedding_hnsw;
ALTER TABLE jobs ALTER COLUMN embedding TYPE vector;
CREATE INDEX jobs_embedding_local_hnsw ON jobs USING hnsw ((embedding::vector(384)) vector_cosine_ops)
  WHERE embedding_model = '{_local_model()}';
CREATE INDEX jobs_embedding_openai_hnsw ON jobs USING hnsw ((embedding::vector(1536)) vector_cosine_ops)
  WHERE embedding_model = '{OPENAI_MODEL}';
"""


DOWNGRADE_SQL = """
DO $$ BEGIN IF current_database() = 'app' THEN RAISE EXCEPTION 'refusing a lossy downgrade on app'; END IF; END $$;
DROP INDEX jobs_embedding_local_hnsw, jobs_embedding_openai_hnsw;
-- A vector(384) column holds nothing wider: those vectors are cleared with their model name (the two are null
-- together) and the sweep makes them again.
UPDATE jobs SET embedding = NULL, embedding_model = NULL WHERE vector_dims(embedding) <> 384;
ALTER TABLE jobs ALTER COLUMN embedding TYPE vector(384);
CREATE INDEX jobs_embedding_hnsw ON jobs USING hnsw (embedding vector_cosine_ops)
  WHERE status = 'open' AND canonical_job_id IS NULL;
"""


def _begin() -> None:
    op.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
    op.execute("SET LOCAL max_parallel_maintenance_workers = 0")
    op.execute("SET ROLE app_owner")  # the indexes are created by the table's owner


def _end() -> None:
    op.execute("RESET ROLE")  # Alembic writes alembic_version next, as the login that started the migration
    op.execute("RESET max_parallel_maintenance_workers")
    op.execute("RESET lock_timeout")


def upgrade() -> None:
    _begin()
    op.execute(_upgrade_sql())
    _end()


def downgrade() -> None:
    _begin()
    op.execute(DOWNGRADE_SQL)
    _end()
