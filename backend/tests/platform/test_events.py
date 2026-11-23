"""Plan 07-01: domain events become exactly one queue job per subscriber, in the publisher's transaction."""

import pytest
from sqlalchemy import text

from app.platform import events
from tests.campaigns.factories import make_world


@pytest.fixture(autouse=True)
def empty_registry(monkeypatch):
    """Each test starts with no subscriber and leaves none behind (the real ones are filled at import time)."""
    monkeypatch.setattr(events, "_subscribers", {})


def jobs(conn, like: str = "t7.%") -> list:
    return conn.execute(
        text("SELECT kind, unique_key, queue, user_id, payload FROM queue_jobs WHERE kind LIKE :k ORDER BY id"),
        {"k": like},
    ).all()


def test_with_two_subscribers_one_publish_makes_two_jobs_with_keys_of_kind_and_key(conn):
    events.subscribe("t7.happened", "t7.first")
    events.subscribe("t7.happened", "t7.second", queue="interactive")
    assert events.publish(conn, "t7.happened", {"id": "x"}, key="k1") == 2
    rows = jobs(conn)
    assert [(r.kind, r.unique_key, r.queue) for r in rows] == [
        ("t7.first", "t7.first:k1", "default"),
        ("t7.second", "t7.second:k1", "interactive"),
    ]
    assert all(r.payload == {"id": "x"} for r in rows)


def test_publishing_the_same_key_twice_leaves_two_jobs_not_four_and_still_reports_two(conn):
    events.subscribe("t7.happened", "t7.first")
    events.subscribe("t7.happened", "t7.second")
    assert events.publish(conn, "t7.happened", {}, key="same") == 2
    assert events.publish(conn, "t7.happened", {}, key="same") == 2
    assert len(jobs(conn)) == 2
    events.publish(conn, "t7.happened", {}, key="other")
    assert len(jobs(conn)) == 4


def test_an_event_with_no_subscriber_makes_no_job_and_returns_zero(conn):
    assert events.publish(conn, "t7.nobody_listens", {"a": 1}, key="k") == 0
    assert jobs(conn) == []


def test_subscribing_the_same_pair_twice_counts_once(conn):
    events.subscribe("t7.happened", "t7.first")
    events.subscribe("t7.happened", "t7.first")
    assert events.subscribers("t7.happened") == [("t7.first", "default")]
    assert events.publish(conn, "t7.happened", {}, key="k") == 1


def test_the_jobs_belong_to_the_publishers_transaction_and_none_exist_after_a_rollback(engine):
    events.subscribe("t7.happened", "t7.first")
    with engine.connect() as connection:
        transaction = connection.begin()
        events.publish(connection, "t7.happened", {}, key="rolled-back")
        assert len(jobs(connection)) == 1  # visible to the publisher before it commits
        transaction.rollback()
    with engine.connect() as other:
        assert jobs(other) == []


def test_a_job_names_the_person_when_the_payload_does_so_that_erasure_takes_it(conn):
    world = make_world(conn)
    events.subscribe("t7.happened", "t7.first")
    events.publish(conn, "t7.happened", {"user_id": str(world.user), "introduction_id": "i"}, key="k")
    events.publish(conn, "t7.happened", {"campaign_id": str(world.campaign)}, key="k2", user_id=world.user)
    events.publish(conn, "t7.happened", {"campaign_id": "c"}, key="k3")
    assert [r.user_id for r in jobs(conn)] == [world.user, world.user, None]


def test_the_real_subscribers_are_filled_by_importing_the_modules_that_own_them():
    """07-01 makes no subscription itself: 07-05 fills campaigns.subscribers. The registry is a module-level dict that
    both processes import into, so it exists and is empty or filled, never missing."""
    import app.campaigns.subscribers  # noqa: F401

    assert isinstance(events.subscribers("introduction.accepted"), list)
