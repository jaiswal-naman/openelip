"""API composition root: builds the FastAPI app and mounts every module's router (ARCHITECTURE 3.1).

Run:  uvicorn app.main:app --port 8000
"""

from fastapi import FastAPI

from app.brief import privacy as brief_privacy  # noqa: F401  (importing it registers the brief tables and exporter)
from app.brief import routes as brief_routes
from app.ingest import privacy as ingest_privacy  # noqa: F401  (importing it registers the ingest tables)
from app.ingest import routes as ingest_routes
from app.match import privacy as match_privacy  # noqa: F401  (importing it registers the match tables and exporter)
from app.match import routes as match_routes
from app.platform import api, auth, control_routes, errors, telemetry
from app.profile import privacy as profile_privacy  # noqa: F401  (importing it registers the profile tables and exporter)
from app.profile import routes as profile_routes
from app.trust import consent as trust_consent
from app.trust import routes as trust_routes


def create_app() -> FastAPI:
    app = FastAPI(title="Open career agent", version="0.1.0")
    errors.register(app)
    telemetry.instrument(app)
    api.install(app)  # added last, so it is outermost: the request id exists for everything inside it
    # app.platform may not import app.trust, so the account endpoints get the consent rules from here (ARCHITECTURE 3.3)
    auth.hooks.update(consents=trust_consent.current_consents, change_consents=trust_consent.apply_changes)
    for router in (
        api.router,
        ingest_routes.router,
        auth.router,
        control_routes.router,
        trust_routes.router,
        profile_routes.router,
        match_routes.router,
        brief_routes.router,
    ):
        app.include_router(router)
    return app


app = create_app()
