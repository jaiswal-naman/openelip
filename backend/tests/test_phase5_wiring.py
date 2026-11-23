"""Plan 05-01 wired the chat, tailor and apply modules into both composition roots once, so the plans that fill them
(05-04 and 05-05) never edit a root: the registrations, the routers and the handler modules are all loaded already."""

from app import main, worker
from app.apply import jobs as apply_jobs
from app.apply import privacy as apply_privacy
from app.chat import privacy as chat_privacy
from app.tailor import jobs as tailor_jobs
from app.tailor import privacy as tailor_privacy
from app.trust import privacy


def test_both_roots_import_the_three_registrations_so_the_registry_is_filled_in_each_process():
    for root in (main, worker):
        assert root.apply_privacy is apply_privacy
        assert root.chat_privacy is chat_privacy
        assert root.tailor_privacy is tailor_privacy
    assert {"apply", "chat", "tailor"} <= set(privacy.exporters())


def test_the_worker_loads_the_two_handler_modules_the_api_never_runs():
    assert worker.apply_jobs is apply_jobs and worker.tailor_jobs is tailor_jobs
    assert apply_jobs.__name__ == "app.apply.jobs" and tailor_jobs.__name__ == "app.tailor.jobs"


def test_the_api_mounts_the_three_routers_and_the_tracker_routes_are_in_its_schema():
    assert [main.chat_routes.__name__, main.tailor_routes.__name__, main.apply_routes.__name__] == [
        "app.chat.routes",
        "app.tailor.routes",
        "app.apply.routes",
    ]
    paths = main.create_app().openapi()["paths"]
    assert set(paths["/v1/applications"]) == {"get", "post"}
    assert set(paths["/v1/applications/{application_id}/status"]) == {"post"}
