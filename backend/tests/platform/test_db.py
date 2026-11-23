"""db.transaction: one transaction per unit of work, with the signed-in person set for that transaction only."""

import uuid

import pytest
from sqlalchemy import text

from app.platform import db


def test_the_application_connects_as_its_own_role_on_the_test_database(database_url):
    with db.transaction() as conn:
        assert conn.execute(text("SELECT current_user, current_database()")).one() == ("app_api", database_url.database)
    with db.transaction(role="worker") as conn:
        assert conn.execute(text("SELECT current_user")).scalar_one() == "app_worker"


def test_the_user_id_is_visible_inside_the_transaction_and_gone_after_it(database_url):
    person = uuid.uuid4()
    with db.transaction(user_id=person) as conn:
        assert conn.execute(text("SELECT app_user_id()")).scalar_one() == person
    # Every pooled connection is reused; none may carry the id into the next request.
    for _ in range(3):
        with db.transaction() as conn:
            assert conn.execute(text("SELECT app_user_id()")).scalar_one() is None


def test_a_transaction_that_raises_leaves_nothing_behind(engine):
    insert = text("INSERT INTO login_codes (email, code_hmac, expires_at) VALUES ('a@example.test', 'x', now())")
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(insert)
            raise RuntimeError("stop")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM login_codes")).scalar_one() == 0
    with db.transaction() as conn:  # and a transaction that ends normally commits
        conn.execute(insert)
    with engine.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM login_codes")).scalar_one() == 1
        conn.execute(text("TRUNCATE login_codes"))
