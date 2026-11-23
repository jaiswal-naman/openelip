"""Local mode: the model key saved from the app, and the lookup of the one local owner (APP_MODE=local).

Two additions, nothing else changes:

* local_model_key: one row, the model provider and the person's API key sealed with the data keys (APP_DATA_KEYS), so
  the key is never in the database in the clear. The API writes it (only for a signed-in person) and both the API and
  the worker read it, because both processes call models. The worker cannot write it.
* local_owner(create): the one account that is not synthetic. Row-level security hides every other account from a
  request that has no user id yet, which is exactly the state of the request that signs the owner in, so the lookup is a
  SECURITY DEFINER function (like user_for_verified_email). With create it makes the owner on the first call; it
  never reports an account when there are two or more real ones, because that database came from server mode.

The owner's address is owner@example.test. It is not marked synthetic, so nothing excludes the owner from a feature,
and example.test is reserved, so the relay never sends mail to it (relay.is_undeliverable).

Additive. downgrade() exists for the CI migration check on a scratch database; its first statement refuses to run on
the database called app.
"""

from alembic import op

revision = "l1_local_mode"
down_revision = "p8_0801_wind_tunnel"
branch_labels = None
depends_on = None

UPGRADE_SQL = """
CREATE TABLE local_model_key (
  singleton      boolean     PRIMARY KEY DEFAULT true CHECK (singleton),  -- at most one row
  provider       text        NOT NULL CHECK (provider IN ('anthropic', 'openai')),
  api_key_sealed text        NOT NULL,                  -- a Fernet token; the key itself is never stored
  key_last4      text        NOT NULL CHECK (length(key_last4) = 4),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE local_model_key ENABLE ROW LEVEL SECURITY;
CREATE POLICY read_all ON local_model_key FOR SELECT TO app_api, app_worker USING (true);
-- the API may change it only inside a signed-in person's transaction (db.transaction sets app.user_id)
CREATE POLICY write_signed_in ON local_model_key FOR ALL TO app_api
  USING (app_user_id() IS NOT NULL AND app_agent_token_id() IS NULL)
  WITH CHECK (app_user_id() IS NOT NULL AND app_agent_token_id() IS NULL);
GRANT SELECT ON local_model_key TO app_api, app_worker;
GRANT INSERT, UPDATE, DELETE ON local_model_key TO app_api;

CREATE FUNCTION local_owner(p_create boolean) RETURNS TABLE (owner_id uuid, real_accounts integer)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE
  n   integer;
  uid uuid;
BEGIN
  IF p_create THEN  -- two first requests at once must not both make the owner
    PERFORM pg_advisory_xact_lock(hashtextextended('local_owner', 0));
  END IF;
  SELECT count(*) INTO n FROM users WHERE NOT is_synthetic;
  IF n = 1 THEN
    SELECT id INTO uid FROM users WHERE NOT is_synthetic;
  ELSIF n = 0 AND p_create THEN
    INSERT INTO users (email) VALUES ('owner@example.test') RETURNING id INTO uid;
    n := 1;
  END IF;
  RETURN QUERY SELECT uid, n;
END;
$$;
REVOKE EXECUTE ON FUNCTION local_owner(boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION local_owner(boolean) TO app_api;
"""

DOWNGRADE_SQL = """
DO $$ BEGIN IF current_database() = 'app' THEN RAISE EXCEPTION 'refusing a lossy downgrade on app'; END IF; END $$;
DROP FUNCTION local_owner(boolean);
DROP TABLE local_model_key;
"""


def upgrade() -> None:
    op.execute("SET ROLE app_owner")  # every object below is created, and every grant issued, by its owner
    op.execute(UPGRADE_SQL)
    op.execute("RESET ROLE")


def downgrade() -> None:
    op.execute("SET ROLE app_owner")
    op.execute(DOWNGRADE_SQL)
    op.execute("RESET ROLE")
