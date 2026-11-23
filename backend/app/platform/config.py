"""Settings, read from the environment (ARCHITECTURE 10.1) and from nothing else.

The defaults are the development values, so a checkout runs with no .env file. They are refused as soon as
APP_BASE_URL is not a localhost address (ARCHITECTURE 7.4, threat T-01-04). That one check is also what makes a
missing secret fail on a public deployment: a secret nobody set is the development default, and the development
default is refused there. .env.example repeats the development values.
"""

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

Role = Literal["migrate", "api", "worker", "match"]
AppMode = Literal["local", "server"]

# Public, development-only. A Fernet key is 32 bytes, url-safe base64 encoded.
DEV_DATA_KEY = base64.urlsafe_b64encode(b"openelip-development-only-key-00").decode()
DEV_LOOKUP_KEY = "openelip-development-lookup-key"
DEV_LINK_KEY = "openelip-development-link-key"
DEV_DB_PASSWORDS = ("app_local_only", "app_api_local_only", "app_worker_local_only", "app_match_local_only")
DEFAULT_DATABASE_URLS: dict[Role, str] = {
    "migrate": "postgresql://app:app_local_only@127.0.0.1:5433/app",
    "api": "postgresql://app_api:app_api_local_only@127.0.0.1:5433/app",
    "worker": "postgresql://app_worker:app_worker_local_only@127.0.0.1:5433/app",
    "match": "postgresql://app_match:app_match_local_only@127.0.0.1:5433/app",
}
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
FALSE_WORDS = frozenset({"false", "0", "no", "off"})
# ARCHITECTURE 5.9 item 13 names LANGSMITH_TRACING and LANGCHAIN_TRACING_V2; the installed langsmith also reads the
# other two spellings (langsmith.utils.tracing_is_enabled), so all four are refused.
TRACING_FLAGS = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")
TRACING_KEYS = ("LANGSMITH_API_KEY", "LANGCHAIN_API_KEY")


DEFAULT_MCP_ORIGINS = ("http://127.0.0.1:8080", "http://localhost:8080")
LOOPBACK_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ConfigError(RuntimeError):
    """The environment is unusable; the process should not start."""


@dataclass(frozen=True)
class Settings:
    app_env: str
    app_base_url: str
    product_name: str
    database_url_migrate: str
    database_url_api: str
    database_url_worker: str
    database_url_match: str
    data_keys: tuple[str, ...]  # primary first; every one decrypts (ARCHITECTURE 7.3)
    lookup_key: str
    link_key: str
    smtp_url: str
    mail_from: str
    llm_provider: str
    anthropic_api_key: str | None
    openai_api_key: str | None
    llm_base_url: str | None
    otel_endpoint: str | None
    worker_queues: tuple[str, ...]  # empty means every queue
    tailor_rephrase: bool  # AI_SYSTEM 4.7: model rephrasing of resume lines stays off until audited
    campaign_budget_usd: Decimal  # model spend one campaign may reach before the Guardian pauses it (GRD-07)
    monthly_budget_usd: Decimal  # model spend one person's campaigns may reach in a calendar month (GRD-07)
    research_pages_dir: (
        str | None
    )  # a directory of recorded pages the Researcher reads instead of the web (not in production)
    # local: one person on their own computer is signed in without an email code (POST /v1/auth/local). server: the
    # sign-in this product has always had. Unset means server, so a public deployment never gets the local path by
    # chance.
    app_mode: AppMode = "server"
    llm_provider_is_set: bool = False  # LLM_PROVIDER was given; a model key saved in the app never overrides it
    # The origins a browser may send to /mcp (09-INDEX "Configuration"). Only loopback by default: the MCP server is
    # local-only in both modes. A non-loopback origin needs mcp_allow_remote_origins, or the process does not start.
    mcp_allowed_origins: tuple[str, ...] = DEFAULT_MCP_ORIGINS
    mcp_allow_remote_origins: bool = False

    @property
    def campaign_budget_microusd(self) -> int:
        return _to_microusd(self.campaign_budget_usd)

    @property
    def monthly_budget_microusd(self) -> int:
        return _to_microusd(self.monthly_budget_usd)

    def database_url(self, role: Role) -> str:
        return {
            "migrate": self.database_url_migrate,
            "api": self.database_url_api,
            "worker": self.database_url_worker,
            "match": self.database_url_match,
        }[role]


def _to_microusd(usd: Decimal) -> int:
    """Whole micro-dollars: budgets are stored and compared in them, never as floats."""
    return int(usd * 1_000_000)


def _budget(name: str, value: str | None, default: str) -> Decimal:
    """A positive dollar amount. Anything else stops the process; the value itself is never echoed."""
    try:
        amount = Decimal((value or default).strip())
    except InvalidOperation:
        raise ConfigError(f"{name} must be a number of dollars greater than zero") from None
    if not amount.is_finite() or amount <= 0 or _to_microusd(amount) < 1:
        raise ConfigError(f"{name} must be a number of dollars greater than zero")
    return amount


def _split(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def is_local(base_url: str) -> bool:
    return (urlsplit(base_url).hostname or "") in LOCAL_HOSTS


def _app_mode(value: str | None) -> AppMode:
    """local or server. Unset or empty is server; any other spelling stops the process instead of being guessed."""
    word = (value or "").strip().lower() or "server"
    if word == "local":
        return "local"
    if word == "server":
        return "server"
    raise ConfigError("APP_MODE must be local or server")  # the value itself is never echoed


def _flag(name: str, value: str | None) -> bool:
    """A true/false setting. Unset or empty is false; any other spelling stops the process instead of being guessed."""
    word = (value or "").strip().lower()
    if not word:
        return False
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    raise ConfigError(f"{name} must be true or false")  # the value itself is never echoed


def load(environ: Mapping[str, str]) -> Settings:
    """Build the settings from a mapping, so tests can try any environment without touching the real one."""

    def get(name: str) -> str | None:
        return environ.get(name) or None

    settings = Settings(
        app_env=get("APP_ENV") or "development",
        app_base_url=get("APP_BASE_URL") or "http://localhost:8080",
        product_name=get("APP_PRODUCT_NAME") or "Open career agent",
        database_url_migrate=get("DATABASE_URL_MIGRATE") or DEFAULT_DATABASE_URLS["migrate"],
        database_url_api=get("DATABASE_URL_API") or DEFAULT_DATABASE_URLS["api"],
        database_url_worker=get("DATABASE_URL_WORKER") or DEFAULT_DATABASE_URLS["worker"],
        database_url_match=get("DATABASE_URL_MATCH") or DEFAULT_DATABASE_URLS["match"],
        data_keys=_split(get("APP_DATA_KEYS")) or (DEV_DATA_KEY,),
        lookup_key=get("APP_LOOKUP_KEY") or DEV_LOOKUP_KEY,
        link_key=get("APP_LINK_KEY") or DEV_LINK_KEY,
        smtp_url=get("SMTP_URL") or "smtp://mail:1025",
        mail_from=get("MAIL_FROM") or "no-reply@localhost",
        llm_provider=get("LLM_PROVIDER") or "anthropic",
        anthropic_api_key=get("ANTHROPIC_API_KEY"),
        openai_api_key=get("OPENAI_API_KEY"),
        llm_base_url=get("LLM_BASE_URL"),
        otel_endpoint=get("OTEL_EXPORTER_OTLP_ENDPOINT"),
        worker_queues=_split(get("WORKER_QUEUES")),
        tailor_rephrase=_flag("TAILOR_REPHRASE", get("TAILOR_REPHRASE")),  # default false (AI_SYSTEM 4.7)
        campaign_budget_usd=_budget("APP_CAMPAIGN_BUDGET_USD", get("APP_CAMPAIGN_BUDGET_USD"), "0.75"),  # assumption
        monthly_budget_usd=_budget("APP_MONTHLY_BUDGET_USD", get("APP_MONTHLY_BUDGET_USD"), "5"),  # assumption
        research_pages_dir=get("RESEARCH_PAGES_DIR"),
        app_mode=_app_mode(get("APP_MODE")),
        llm_provider_is_set=get("LLM_PROVIDER") is not None,
        mcp_allowed_origins=_split(get("MCP_ALLOWED_ORIGINS")) or DEFAULT_MCP_ORIGINS,
        mcp_allow_remote_origins=_flag("MCP_ALLOW_REMOTE_ORIGINS", get("MCP_ALLOW_REMOTE_ORIGINS")),
    )
    _refuse_local_mode_off_localhost(settings)
    _refuse_remote_mcp_origins(settings)
    _refuse_recorded_pages_in_production(settings)
    _check_keys_are_usable(settings)
    _refuse_development_secrets_off_localhost(settings)
    _refuse_tracing(environ)
    return settings


def _refuse_local_mode_off_localhost(settings: Settings) -> None:
    """Local mode signs whoever reaches the API in as the owner, so it is only for an address nobody else can reach."""
    if settings.app_mode == "local" and not is_local(settings.app_base_url):
        raise ConfigError("APP_MODE=local is refused when APP_BASE_URL is not a localhost address; use APP_MODE=server")


def _refuse_remote_mcp_origins(settings: Settings) -> None:
    """The MCP server is reachable from this computer only. An origin that is not loopback (any port) is refused unless
    MCP_ALLOW_REMOTE_ORIGINS=true says the owner means it. The origin itself is never echoed."""
    if settings.mcp_allow_remote_origins:
        return
    for origin in settings.mcp_allowed_origins:
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or (parts.hostname or "") not in LOOPBACK_ORIGIN_HOSTS:
            raise ConfigError(
                "MCP_ALLOWED_ORIGINS holds an origin that is not on this computer; "
                "set MCP_ALLOW_REMOTE_ORIGINS=true only if you mean to allow it"
            )


def _refuse_recorded_pages_in_production(settings: Settings) -> None:
    """RESEARCH_PAGES_DIR makes the Researcher read recorded pages, so a production process must not have it."""
    if settings.research_pages_dir and settings.app_env == "production":
        raise ConfigError("RESEARCH_PAGES_DIR is for tests, evals and the demo; it is refused when APP_ENV=production")


def _check_keys_are_usable(settings: Settings) -> None:
    for key in settings.data_keys:
        try:
            Fernet(key)
        except ValueError as error:
            # The key itself is never echoed.
            raise ConfigError("APP_DATA_KEYS holds a value that is not a Fernet key") from error


def _refuse_development_secrets_off_localhost(settings: Settings) -> None:
    if is_local(settings.app_base_url):
        return
    in_use = []
    if DEV_DATA_KEY in settings.data_keys:
        in_use.append("APP_DATA_KEYS")
    if settings.lookup_key == DEV_LOOKUP_KEY:
        in_use.append("APP_LOOKUP_KEY")
    if settings.link_key == DEV_LINK_KEY:
        in_use.append("APP_LINK_KEY")
    for name, url in (
        ("DATABASE_URL_MIGRATE", settings.database_url_migrate),
        ("DATABASE_URL_API", settings.database_url_api),
        ("DATABASE_URL_WORKER", settings.database_url_worker),
        ("DATABASE_URL_MATCH", settings.database_url_match),
    ):
        if urlsplit(url).password in DEV_DB_PASSWORDS:
            in_use.append(name)
    if in_use:
        raise ConfigError(
            "development secrets are in use while APP_BASE_URL is not a localhost address: " + ", ".join(in_use)
        )


def _refuse_tracing(environ: Mapping[str, str]) -> None:
    """ARCHITECTURE 5.9 item 13: LangSmith traces would carry prompts and state (against 9.2), so none may be on.

    Only variable names are reported, never a value (a key is a secret).
    """
    on = [name for name in TRACING_FLAGS if _flag(name, environ.get(name))]
    on += [name for name in TRACING_KEYS if (environ.get(name) or "").strip()]
    if on:
        raise ConfigError("LangSmith and LangChain tracing may not be configured; unset " + ", ".join(on))


@lru_cache(maxsize=1)
def get() -> Settings:
    """The process's settings, read once."""
    return load(os.environ)
