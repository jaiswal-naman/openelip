"""Plan 07-03, GRD-01 and GRD-04: the dial, its allowed levels, the undo window and the version. Everything runs as
the API
login for one person."""

import pytest
from sqlalchemy import text

from app.guardian import api as guardian
from app.guardian import autonomy
from app.platform import config, db
from app.platform.errors import ApiError
from tests.guardian.support import make_committed_world, scalar


@pytest.fixture
def world(engine, clean):
    return make_committed_world(engine)


def view(world):
    with db.transaction(world.user) as conn:
        return guardian.get_autonomy(conn, world.user)


def put(world, **kwargs):
    with db.transaction(world.user) as conn:
        return guardian.put_autonomy(conn, world.user, **kwargs)


def test_with_no_rows_every_type_is_ask_first_and_the_view_names_what_each_type_allows(world):
    seen = view(world)
    assert [(r.action_type, r.level, r.allowed) for r in seen.rows] == [
        ("submit_application", "ask_first", ("ask_first",)),
        ("send_follow_up", "ask_first", ("ask_first", "never")),
        ("twin_answer", "ask_first", ("ask_first", "tell_me_after", "never")),
    ]
    assert seen.undo_window_minutes == 10
    assert seen.profile_sharing == "Only when you accept an introduction."
    assert seen.default_budget_microusd == config.get().campaign_budget_microusd == 750_000
    assert len(seen.version) == 16


def test_put_stores_the_levels_and_the_window_and_changes_the_version(world, engine):
    before = view(world)
    after = put(world, levels={"send_follow_up": "never", "twin_answer": "tell_me_after"}, undo_window_minutes=30)
    assert {r.action_type: r.level for r in after.rows} == {
        "submit_application": "ask_first",
        "send_follow_up": "never",
        "twin_answer": "tell_me_after",
    }
    assert after.undo_window_minutes == 30 and after.version != before.version
    assert view(world) == after
    assert scalar(engine, "SELECT undo_window_minutes FROM users WHERE id = :u", u=world.user) == 30
    back = put(world, levels={"send_follow_up": "ask_first"})
    assert back.rows[1].level == "ask_first" and scalar(engine, "SELECT count(*) FROM autonomy_settings") == 2


@pytest.mark.parametrize(
    ("kind", "level"),
    [
        ("submit_application", "tell_me_after"),
        ("submit_application", "never"),
        ("send_follow_up", "tell_me_after"),
        ("twin_answer", "sometimes"),
    ],
)
def test_a_level_the_type_does_not_allow_is_refused_with_autonomy_not_allowed(world, kind, level):
    with pytest.raises(ApiError) as caught:
        put(world, levels={kind: level})
    assert (caught.value.status, caught.value.code) == (422, "autonomy_not_allowed")
    assert caught.value.details["allowed"] == list(autonomy.ALLOWED_LEVELS[kind])


def test_nothing_is_stored_when_one_of_several_levels_is_refused(world, engine):
    with pytest.raises(ApiError):
        put(world, levels={"send_follow_up": "never", "submit_application": "never"})
    assert scalar(engine, "SELECT count(*) FROM autonomy_settings") == 0


@pytest.mark.parametrize("minutes", [0, 61, -5])
def test_a_window_outside_one_to_sixty_is_refused(world, minutes):
    with pytest.raises(ApiError) as caught:
        put(world, undo_window_minutes=minutes)
    assert (caught.value.status, caught.value.code) == (422, "validation_failed")


@pytest.mark.parametrize("minutes", [1, 60])
def test_the_edges_of_the_window_are_allowed(world, minutes):
    assert put(world, undo_window_minutes=minutes).undo_window_minutes == minutes


def test_an_unknown_action_type_is_refused(world):
    with pytest.raises(ApiError) as caught:
        put(world, levels={"share_profile": "ask_first"})
    assert caught.value.code == "validation_failed"


def test_a_stale_version_is_refused_and_a_current_one_accepted(world):
    seen = view(world)
    put(world, levels={"send_follow_up": "never"}, version=seen.version)
    with pytest.raises(ApiError) as caught:
        put(world, levels={"send_follow_up": "ask_first"}, version=seen.version)
    assert (caught.value.status, caught.value.code) == (412, "version_conflict")
    assert caught.value.details["current_version"] == view(world).version


def test_the_level_reads_ask_first_for_a_person_with_no_row_and_their_own_otherwise(world, engine):
    other = make_committed_world(engine, consent=False)
    put(world, levels={"send_follow_up": "never"})
    with db.transaction(world.user) as conn:
        assert autonomy.level_of(conn, world.user, "send_follow_up") == "never"
    with db.transaction(other.user) as conn:
        assert autonomy.level_of(conn, other.user, "send_follow_up") == "ask_first"
        assert autonomy.level_of(conn, other.user, "twin_answer") == "ask_first"


def test_the_database_agrees_that_the_two_always_approved_types_cannot_be_tell_me_after(engine, world):
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="autonomy_never_automatic"), engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO autonomy_settings (user_id, action_type, level) "
                "VALUES (:u, 'send_follow_up', 'tell_me_after')"
            ),
            {"u": world.user},
        )
