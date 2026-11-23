"""Scenario files of the wind tunnel (INDEX "Scenario file"): one TOML file and one folder per scenario.

    <scenarios>/<key>.toml     the definition: world, injected text, reaction rules, expected outcomes
    <scenarios>/<key>/recorded/<task>.json   the fake provider's answers for that scenario, one file per gateway task
    <scenarios>/<key>/pages/<host>/...       the recorded company pages (the Researcher's RESEARCH_PAGES_DIR layout)

A scenario is invented and says so (`invented = true`, the agent-drafted label). A definition never changes: a changed
one is a new `version`, and ensure_rows refuses to store a different definition under a stored (key, version).
"""

import json
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import Connection, text

LABEL = "agent-drafted, maintainer review pending"
PERSONAS = ("silent", "lowball", "scam", "hidden_instruction", "cooperative")
STARTS = ("match", "introduction")
STAGES = ("researching", "needs_you", "applied", "talking", "interview", "offer", "closed")
STATUSES = ("applied", "interview", "offer", "rejected", "no_response")
APPROVAL_KINDS = ("submit_application", "send_follow_up")
PAUSE_REASONS = ("candidate", "budget_exhausted", "scam_suspected")
CLOSE_REASONS = ("cancelled", "offer_accepted", "offer_declined")
HOST_SUFFIXES = ("example.test", ".invalid", ".example")  # nothing a scenario names can be a real address
DEFAULT_MAX_VIRTUAL_DAYS = 30
EXPECT_KEYS = (
    "goal",
    "stage_in",
    "closed_reason",
    "paused_reason",
    "follow_ups_sent",
    "guardian_block",
    "dossier_flag",
    "max_approvals",
    "system_mail",
)


class ScenarioInvalid(Exception):
    """The file is not in the agreed format. The message names the key or value; it is the author's text, not a person's."""


class ScenarioChanged(Exception):
    """A stored (key, version) holds a different definition: a definition changes only with a new version."""


@dataclass(frozen=True)
class Scenario:
    key: str
    version: int
    persona: str
    title: str
    start: str
    max_virtual_days: int
    definition: dict[str, Any]  # the whole file, as stored in sim_scenarios.definition
    directory: Path  # <scenarios>/<key>/

    @property
    def world(self) -> dict[str, Any]:
        return dict(self.definition["world"])

    @property
    def expect(self) -> dict[str, Any]:
        return dict(self.definition.get("expect", {}))

    @property
    def inject(self) -> dict[str, str]:
        return dict(self.definition.get("inject", {}))

    @property
    def candidate_rules(self) -> list[dict[str, Any]]:
        return [dict(rule) for rule in self.definition.get("candidate", [])]

    @property
    def employer_rules(self) -> list[dict[str, Any]]:
        return [dict(rule) for rule in self.definition.get("employer", [])]

    @property
    def recorded(self) -> Path:
        return self.directory / "recorded"

    @property
    def pages(self) -> Path:
        return self.directory / "pages"

    @property
    def company_host(self) -> str:
        return urlsplit(self.world["job"]["apply_url"]).hostname or ""


def _fail(key: str, why: str) -> ScenarioInvalid:
    return ScenarioInvalid(f"scenario {key!r}: {why}")


def _need(key: str, table: Mapping[str, Any], name: str, kind: type | tuple[type, ...] = str) -> Any:
    if name not in table or not isinstance(table[name], kind) or table[name] in ("", [], {}):
        raise _fail(key, f"missing or empty {name!r}")
    return table[name]


def _host_ok(host: str) -> bool:
    return any(host == suffix or host.endswith(suffix if suffix.startswith(".") else "." + suffix) for suffix in HOST_SUFFIXES)


def _address_host(address: str) -> str:
    return address.rpartition("@")[2].lower()


def _check_hosts(key: str, definition: Mapping[str, Any]) -> None:
    world = definition["world"]
    host = (urlsplit(world["job"]["apply_url"]).hostname or "").lower()
    if not _host_ok(host):
        raise _fail(key, f"the company host {host!r} must end in example.test, .invalid or .example")
    contact = _address_host(world["employer"]["contact"])
    if not _host_ok(contact):
        raise _fail(key, f"the contact host {contact!r} must end in example.test, .invalid or .example")
    attacker = definition.get("inject", {}).get("attacker_address")
    if attacker and not _host_ok(_address_host(attacker)):
        raise _fail(key, "the attacker address must end in example.test, .invalid or .example")


def _check_rule_trigger(key: str, who: str, on: str) -> None:
    name, _, rest = on.partition(":")
    good = False
    if who == "candidate":
        good = (
            on in ("start", "kit_released")
            or (name == "approval" and rest in APPROVAL_KINDS)
            or (name == "stage" and rest in STAGES)
            or (name == "employer_reply" and rest in STATUSES)
            or (name == "paused" and rest in PAUSE_REASONS)
            or (name == "quiet_days" and rest.isdigit())
        )
    else:
        template, _, number = rest.rpartition(":")
        good = on == "introduction_accepted" or (name == "mail" and bool(template) and number.isdigit())
    if not good:
        raise _fail(key, f"unknown {who} trigger {on!r}")


def _check_rule_action(key: str, who: str, do: str) -> None:
    verb, _, argument = do.partition(" ")
    if who == "candidate":
        plain = ("start_campaign", "accept_introduction", "set_follow_up_email", "approve", "edit", "reject")
        good = (
            (verb in (*plain, "resume", "none") and not argument)
            or (verb == "record_status" and argument in STATUSES)
            or (verb == "cancel" and argument in CLOSE_REASONS)
        )
    else:
        good = (
            (verb == "reply" and argument in STATUSES)
            or (verb == "answer" and argument in ("yes", "no"))
            or do == "ignore"
        )
    if not good:
        raise _fail(key, f"unknown {who} action {do!r}")


def load(path: Path) -> Scenario:
    """Read one scenario file and refuse anything outside the agreed format."""
    key = path.stem
    try:
        definition: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise _fail(key, f"not valid TOML ({error})") from None
    if definition.get("key") != key:
        raise _fail(key, "the file name and `key` differ")
    version = _need(key, definition, "version", int)
    persona = _need(key, definition, "persona")
    if persona not in PERSONAS:
        raise _fail(key, f"unknown persona {persona!r}")
    title = _need(key, definition, "title")
    start = _need(key, definition, "start")
    if start not in STARTS:
        raise _fail(key, f"unknown start {start!r}")
    if definition.get("labels") != LABEL:
        raise _fail(key, f"labels must be {LABEL!r}")
    if definition.get("invented") is not True:
        raise _fail(key, "invented must be true")
    days = definition.get("max_virtual_days", DEFAULT_MAX_VIRTUAL_DAYS)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        raise _fail(key, "max_virtual_days must be a whole number of days from 1 to 365")
    world = _need(key, definition, "world", dict)
    job = _need(key, world, "job", dict)
    for name in ("title", "company", "description", "apply_url"):
        _need(key, job, name)
    _need(key, job, "requirements", list)
    candidate = _need(key, world, "candidate", dict)
    _need(key, candidate, "name")
    _need(key, candidate, "resume", list)
    _need(key, candidate, "facts", list)
    canaries = _need(key, world, "canaries", dict)
    for name in ("phone", "current_pay", "salary_floor"):
        _need(key, canaries, name)
    employer = _need(key, world, "employer", dict)
    _need(key, employer, "contact")
    if start == "introduction":
        _need(key, employer, "organization")
        _need(key, employer, "role", dict)
        _need(key, employer, "answer_reason")
    _check_hosts(key, definition)
    for who in ("candidate", "employer"):
        for rule in definition.get(who, []):
            _check_rule_trigger(key, who, str(rule.get("on", "")))
            _check_rule_action(key, who, str(rule.get("do", "")))
    unknown = set(definition.get("expect", {})) - set(EXPECT_KEYS)
    if unknown:
        raise _fail(key, f"unknown expectation {sorted(unknown)[0]!r}")
    if "goal" not in definition.get("expect", {}):
        raise _fail(key, "missing expect.goal")
    directory = path.parent / key
    if not (directory / "recorded").is_dir():
        raise _fail(key, "no recorded/ folder beside the file")
    return Scenario(key, version, persona, title, start, days, definition, directory)


def load_dir(directory: Path, keys: Iterable[str] | None = None) -> list[Scenario]:
    """Every scenario of the folder (or the named ones), sorted by key. Two scenarios may not share a company host:
    their pages are merged into one folder."""
    wanted = None if keys is None else set(keys)
    found = [load(path) for path in sorted(directory.glob("*.toml")) if wanted is None or path.stem in wanted]
    if wanted is not None and wanted - {s.key for s in found}:
        raise ScenarioInvalid(f"no scenario file for {sorted(wanted - {s.key for s in found})[0]!r}")
    hosts: dict[str, str] = {}
    for scenario in found:
        if scenario.company_host in hosts:
            raise _fail(scenario.key, f"shares its company host with {hosts[scenario.company_host]!r}")
        hosts[scenario.company_host] = scenario.key
    return sorted(found, key=lambda s: s.key)


def ensure_rows(conn: Connection, scenarios: Iterable[Scenario]) -> dict[str, UUID]:
    """The sim_scenarios row of each (key, version), inserted when missing; returns key -> id. A stored row with a
    different definition raises ScenarioChanged and nothing is written for it."""
    ids: dict[str, UUID] = {}
    for scenario in scenarios:
        stored = conn.execute(
            text("SELECT id, definition FROM sim_scenarios WHERE key = :k AND version = :v"),
            {"k": scenario.key, "v": scenario.version},
        ).one_or_none()
        wanted = json.loads(json.dumps(scenario.definition))
        if stored is None:
            ids[scenario.key] = conn.execute(
                text(
                    """INSERT INTO sim_scenarios (key, version, persona, title, definition)
                       VALUES (:k, :v, CAST(:persona AS sim_persona), :title, CAST(:definition AS jsonb))
                       RETURNING id"""
                ),
                {
                    "k": scenario.key,
                    "v": scenario.version,
                    "persona": scenario.persona,
                    "title": scenario.title,
                    "definition": json.dumps(wanted),
                },
            ).scalar_one()
        elif stored.definition != wanted:
            raise ScenarioChanged(f"{scenario.key} version {scenario.version} is stored with another definition")
        else:
            ids[scenario.key] = stored.id
    return ids


