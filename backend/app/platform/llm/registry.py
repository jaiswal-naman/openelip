"""config/models.toml (which model answers which task and what it costs) and its copy in model_registry (PLT-05).

The file is read on every call, so an edit needs no restart and no code change. LLM_PROVIDER picks the provider; in
local mode a key saved in the app fills in what the environment leaves out (stored_key.apply).
"""

import tomllib
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from .. import db
from ..config import Role, Settings
from . import stored_key
from .failures import AIDisabled

# The login that writes a call's registry and ledger rows. gateway.complete() sets it for one call: the worker by
# default, the API login for the chat reply, which runs in the API process and has no worker login (05-INDEX 3).
LEDGER: ContextVar[Role] = ContextVar("llm_ledger", default="worker")

PATH = Path(__file__).resolve().parents[3] / "config" / "models.toml"
ADAPTERS = ("anthropic", "openai_compatible", "fake")
DATA_CLASSES = ("public", "de-identified", "identified", "synthetic")  # AI_SYSTEM 2.6
NO_KEY = "not-needed"  # the openai SDK insists on a key string; a local server ignores it
# The values of reasoning_effort in the Chat Completions reference (read 2026-11-28). Which of them a model takes is on
# its own page, so models.toml names one per route and the first live call shows a refusal.
REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


class ModelConfigError(RuntimeError):
    """models.toml is malformed. The message names a task and a key, never a value."""


@dataclass(frozen=True)
class Route:
    """One task served by one provider: everything a call, the ledger and /trust need."""

    task: str
    provider: str  # the entry under [providers]
    label: str
    adapter: str
    model: str
    is_local: bool
    base_url: str | None
    structured_output: str  # json_schema, or json_object for servers that only have JSON mode
    token_limit_param: str  # max_completion_tokens (OpenAI), max_tokens (Ollama documents only this one)
    data_class: str
    description: str
    retention_note: str
    max_tokens: int
    timeout_seconds: float
    input_microusd_per_mtok: int | None
    output_microusd_per_mtok: int | None
    price_checked_at: datetime | None
    # Sent as reasoning_effort to an OpenAI-style server; None leaves the model's own default.
    reasoning_effort: str | None = None
    api_key: str | None = field(default=None, repr=False)

    @property
    def priced(self) -> bool:
        """The registry refuses a model that is not local and has no checked price; so does the gateway."""
        prices = (self.input_microusd_per_mtok, self.output_microusd_per_mtok, self.price_checked_at)
        return self.is_local or None not in prices


def _micro(usd: Any) -> int | None:
    """US dollars per million tokens (TOML) to micro-dollars per million tokens. "unknown" or absent stays None."""
    if usd is None or usd == "unknown":
        return None
    return int((Decimal(str(usd)) * 1_000_000).to_integral_value())


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)
    return None


def table() -> list[Route]:
    """Every (task, provider) pair the file configures, in file order."""
    with PATH.open("rb") as handle:
        doc = tomllib.load(handle)
    providers: dict[str, Any] = doc.get("providers", {})
    routes: list[Route] = []
    for task, entry in doc.get("tasks", {}).items():
        for name, model in entry.get("models", {}).items():
            where = f"models.toml: task {task}, provider {name}"
            try:
                p = providers[name]
                if p["adapter"] not in ADAPTERS or entry["data_class"] not in DATA_CLASSES:
                    raise ModelConfigError(f"{where}: unknown adapter or data_class")
                effort = model.get("reasoning_effort")
                if effort is not None and (effort not in REASONING_EFFORTS or p["adapter"] != "openai_compatible"):
                    raise ModelConfigError(
                        f"{where}: reasoning_effort is not an OpenAI value or not on an OpenAI-style provider"
                    )
                routes.append(
                    Route(
                        task=task,
                        provider=name,
                        label=p["label"],
                        adapter=p["adapter"],
                        model=model["model"],
                        is_local=bool(p.get("is_local", False)),
                        base_url=p.get("base_url"),
                        structured_output=p.get("structured_output", "json_schema"),
                        token_limit_param=p.get("token_limit_param", "max_completion_tokens"),
                        data_class=entry["data_class"],
                        description=entry["description"],
                        retention_note=p["retention_note"],
                        max_tokens=int(entry["max_tokens"]),
                        timeout_seconds=float(entry["timeout_seconds"]),
                        input_microusd_per_mtok=_micro(model.get("input_usd_per_mtok")),
                        output_microusd_per_mtok=_micro(model.get("output_usd_per_mtok")),
                        price_checked_at=_when(model.get("price_checked_at")),
                        reasoning_effort=effort,
                    )
                )
            except KeyError as missing:
                raise ModelConfigError(f"{where}: missing {missing}") from None
    return routes


def _key(route: Route, settings: Settings) -> str | None:
    if route.adapter == "anthropic":
        return settings.anthropic_api_key
    if route.adapter == "openai_compatible":
        return settings.openai_api_key or (NO_KEY if route.is_local else None)
    return NO_KEY  # the fake needs none


def route(task: str, settings: Settings) -> Route:
    """The route that serves a task under the running configuration. AIDisabled when there is none to call."""
    settings = stored_key.apply(settings)
    name = settings.llm_provider
    if name == "openai_compatible":  # ARCHITECTURE 10.2: the server LLM_BASE_URL names, or OpenAI itself without one
        name = "local" if settings.llm_base_url else "openai"
    routes = table()
    found = [r for r in routes if r.task == task and r.provider == name]
    if (
        not found and name == "fake"
    ):  # the fake serves any task that has a recording, with the first fake route's limits
        found = [replace(r, task=task) for r in routes if r.provider == "fake"][:1]
    if not found:
        raise AIDisabled("no_route")
    chosen = found[0]
    if chosen.adapter == "fake" and settings.app_env == "production":
        raise AIDisabled("fake_in_production")
    key = _key(chosen, settings)
    if key is None:
        raise AIDisabled("no_key")
    if not chosen.priced:
        raise AIDisabled("unpriced")  # never guess a price
    base_url = (settings.llm_base_url or chosen.base_url) if chosen.adapter == "openai_compatible" else None
    return replace(chosen, api_key=key, base_url=base_url)


def active_routes(settings: Settings) -> list[Route]:
    """One route per configured task that would be called right now: what /trust discloses."""
    found: list[Route] = []
    for task in dict.fromkeys(r.task for r in table()):
        try:
            found.append(route(task, settings))
        except AIDisabled:
            continue
    return found


def _params(r: Route) -> dict[str, Any]:
    return {
        "provider": r.provider,
        "model": r.model,
        "is_local": r.is_local,
        "input": r.input_microusd_per_mtok,
        "output": r.output_microusd_per_mtok,
        "note": r.retention_note,
        "checked": r.price_checked_at,
    }


_INSERT = (
    "INSERT INTO model_registry (provider, model_name, kind, is_local, input_microusd_per_mtok, "
    "output_microusd_per_mtok, retention_note, price_checked_at) "
    "VALUES (:provider, :model, 'chat', :is_local, :input, :output, :note, :checked) "
    "ON CONFLICT (provider, model_name) "
)
_ONLY_IF_MISSING = text(_INSERT + "DO NOTHING")
_UPDATE = text(
    _INSERT + "DO UPDATE SET is_local = EXCLUDED.is_local, input_microusd_per_mtok = EXCLUDED.input_microusd_per_mtok, "
    "output_microusd_per_mtok = EXCLUDED.output_microusd_per_mtok, retention_note = EXCLUDED.retention_note, "
    "price_checked_at = EXCLUDED.price_checked_at, is_active = true"
)


def model_id(r: Route) -> UUID:
    """The registry row of a route's model, created on first use so a checkout works before any sync has run."""
    with db.transaction(role=LEDGER.get()) as conn:
        conn.execute(_ONLY_IF_MISSING, _params(r))
        row = conn.execute(
            text("SELECT id FROM model_registry WHERE provider = :p AND model_name = :m"),
            {"p": r.provider, "m": r.model},
        ).scalar_one()
    return row  # type: ignore[no-any-return]


def sync(conn: Connection) -> int:
    """Make model_registry mirror the file: one row per model, removed models marked inactive. Returns the row count."""
    models = {(r.provider, r.model): r for r in table() if r.priced}  # the registry cannot hold an unpriced model
    for r in models.values():
        conn.execute(_UPDATE, _params(r))
    stale = [
        row.id
        for row in conn.execute(text("SELECT id, provider, model_name FROM model_registry"))
        if (row.provider, row.model_name) not in models
    ]
    if stale:
        conn.execute(text("UPDATE model_registry SET is_active = false WHERE id = ANY(:ids)"), {"ids": stale})
    return len(models)
