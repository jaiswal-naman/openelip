"""API composition root: builds the FastAPI app and mounts every module's router (ARCHITECTURE 3.1).

Run:  uvicorn app.main:app --port 8000
"""

from fastapi import FastAPI

from app.apply import privacy as apply_privacy  # noqa: F401  (importing it registers the apply tables and exporter)
from app.apply import routes as apply_routes
from app.brief import privacy as brief_privacy  # noqa: F401  (importing it registers the brief tables and exporter)
from app.brief import routes as brief_routes
from app.campaigns import control_routes as campaign_control_routes
from app.campaigns import privacy as campaign_privacy  # noqa: F401  (importing it registers the campaign tables)
from app.campaigns import routes as campaign_routes
from app.campaigns import stream as campaign_stream  # noqa: F401  (importing it registers the campaigns stream source)
from app.campaigns import subscribers as campaign_subscribers  # noqa: F401  (importing it fills the event registry)
from app.campaigns import view_routes as campaign_view_routes
from app.chat import privacy as chat_privacy  # noqa: F401  (importing it registers the chat tables and exporter)
from app.chat import routes as chat_routes
from app.evals import routes as eval_routes
from app.guardian import privacy as guardian_privacy  # noqa: F401  (importing it registers the Guardian tables)
from app.guardian import routes as guardian_routes
from app.guardian import stream as guardian_stream  # noqa: F401  (importing it registers the Guardian stream source)
from app.hiring import api as hiring_api
from app.hiring import introduction_routes as hiring_introduction_routes
from app.hiring import org_routes as hiring_org_routes
from app.hiring import privacy as hiring_privacy  # noqa: F401  (importing it registers the hiring tables and exporter)
from app.hiring import role_routes as hiring_role_routes
from app.hiring import signal_routes as hiring_signal_routes
from app.hiring import stream as hiring_stream  # noqa: F401  (importing it registers the hiring stream source)
from app.ingest import privacy as ingest_privacy  # noqa: F401  (importing it registers the ingest tables)
from app.ingest import routes as ingest_routes
from app.match import privacy as match_privacy  # noqa: F401  (importing it registers the match tables and exporter)
from app.match import routes as match_routes
from app.platform import api, auth, control_routes, errors, local, stream, telemetry
from app.platform.llm import stored_key
from app.profile import privacy as profile_privacy  # noqa: F401  (importing it registers the profile tables and exporter)
from app.profile import routes as profile_routes
from app.tailor import privacy as tailor_privacy  # noqa: F401  (importing it registers the tailor tables and exporter)
from app.tailor import routes as tailor_routes
from app.trust import consent as trust_consent
from app.trust import routes as trust_routes


def create_app() -> FastAPI:
    app = FastAPI(title="Open career agent", version="0.1.0")
    errors.register(app)
    stored_key.use_login("api")  # this process holds only the API's database login; the worker's default is its own
    telemetry.instrument(app)
    api.install(app)  # added last, so it is outermost: the request id exists for everything inside it
    # app.platform may not import app.trust or app.hiring, so the account endpoints get the consent rules and the
    # memberships from here (ARCHITECTURE 3.3)
    auth.hooks.update(
        consents=trust_consent.current_consents,
        change_consents=trust_consent.apply_changes,
        organizations=hiring_api.my_organizations,
    )
    for router in (
        api.router,
        ingest_routes.router,
        auth.router,
        local.router,
        control_routes.router,
        stream.router,
        trust_routes.router,
        profile_routes.router,
        match_routes.router,
        brief_routes.router,
        tailor_routes.router,
        apply_routes.router,
        chat_routes.router,
        campaign_routes.router,
        campaign_view_routes.router,
        campaign_control_routes.router,
        eval_routes.router,
        guardian_routes.router,
        hiring_org_routes.router,
        hiring_role_routes.router,
        hiring_introduction_routes.router,
        hiring_signal_routes.router,
    ):
        app.include_router(router)
    return app


app = create_app()
