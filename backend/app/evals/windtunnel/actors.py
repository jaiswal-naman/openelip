"""The scripted people of an attempt: a candidate and an employer who act only through the real HTTP API (INDEX
decision 15), following the reaction rules of the scenario file.

A person is a Starlette TestClient over the FastAPI app with the session cookie of a synthetic account, so ownership
checks, idempotency and the API's own rules run as they do for anyone. Every candidate action appends (virtual day,
action) to the attempt's log, from which graders.metrics counts the minutes a person would have spent. There is no
inbound mail: an employer's reply to a match-start campaign reaches the system as the candidate recording a tracker
status (decision 15).

Time here is virtual: a rule's `after_days` is a delay on the attempt's VirtualClock, which the run driver advances when
every attempt is waiting.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.platform import config
from app.platform.sessions import COOKIE_NAME
from app.trust import api as trust

from .clock import VirtualClock
from .graders import Entry, Mail
from .scenario import Scenario
from .world import World


class ActorError(Exception):
    """The API refused a scripted action. The message holds the route, the status and the error code only."""


@dataclass
class Timer:
    due: datetime
    rule: dict[str, Any]
    who: str  # candidate | employer
    context: dict[str, Any] = field(default_factory=dict)


class Person:
    """One signed-in synthetic account talking to the app in process."""

    def __init__(self, asgi_app: FastAPI, token: str) -> None:
        self.client = TestClient(asgi_app, base_url=config.get().app_base_url)
        self.client.cookies.set(COOKIE_NAME, token)

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Idempotency-Key": uuid.uuid4().hex} if method != "GET" else {}
        response = self.client.request(method, path, json=body, headers=headers)
        if response.status_code >= 300:
            code = (response.json().get("error") or {}).get("code") if response.content else None
            raise ActorError(f"{method} {path.split('?')[0]} -> {response.status_code} {code}")
        return response.json()["data"]

    def close(self) -> None:
        self.client.close()


def _rules(rules: list[dict[str, Any]], trigger: str) -> list[dict[str, Any]]:
    return [rule for rule in rules if rule["on"] == trigger]


class Attempt:
    """One attempt's people, rules, timers and what has been observed. react() does one pass of observing and acting and
    says whether it did anything."""

    def __init__(
        self,
        scenario: Scenario,
        world: World,
        number: int,
        clock: VirtualClock,
        asgi_app: FastAPI,
        candidate_token: str,
        employer_token: str | None,
    ) -> None:
        self.scenario, self.world, self.number, self.clock = scenario, world, number, clock
        self.candidate = Person(asgi_app, candidate_token)
        self.employer = Person(asgi_app, employer_token) if employer_token else None
        self.campaign_id: uuid.UUID | None = None
        self.log: list[Entry] = []
        self.captured: list[Mail] = []
        self.timers: list[Timer] = []
        self.pending: list[tuple[str, str, dict[str, Any]]] = []  # (who, trigger, context) not yet turned into timers
        self.started = False
        self.last_stage: str | None = None
        self.last_pause: str | None = None
        self.seen_approvals: set[str] = set()
        self.kit_released = False
        self.introduction_seen = False
        self.application_id: str | None = None
        self.quiet_since = clock.now()
        self.quiet_fired: set[int] = set()
        self.status = "running"  # running | done | error
        self.error: str | None = None

    # ---- what the driver asks ---------------------------------------------------------------------------------

    def deadlines(self) -> list[datetime]:
        """Virtual moments an actor is waiting for: a timer, or the end of a quiet period."""
        later = [t.due for t in self.timers]
        for index, rule in enumerate(self._quiet_rules()):
            if index not in self.quiet_fired:
                later.append(self.quiet_since + timedelta(days=int(rule["on"].partition(":")[2])))
        return later

    def note_mail(self, mail: Mail) -> None:
        """A message of this attempt was captured: the employer's `mail:<template>:<n>` triggers read these."""
        self.captured.append(mail)
        count = sum(1 for m in self.captured if m.template == mail.template)
        self.pending.append(("employer", f"mail:{mail.template}:{count}", {"mail": mail.outbox_id}))
        self._news()

    def close(self) -> None:
        self.candidate.close()
        if self.employer:
            self.employer.close()

    # ---- one pass ---------------------------------------------------------------------------------------------

    def react(self) -> bool:
        acted = False
        if not self.started:
            self.started = True
            self.pending.append(("candidate", "start", {}))
        self._observe()
        quiet_due = self._quiet_due()
        for who, trigger, context in self.pending:
            rules = self.scenario.candidate_rules if who == "candidate" else self.scenario.employer_rules
            for rule in _rules(rules, trigger):
                delay = timedelta(days=float(rule.get("after_days", 0)))
                self.timers.append(Timer(self.clock.now() + delay, rule, who, context))
        self.pending = []
        for rule in quiet_due:
            self.timers.append(Timer(self.clock.now(), rule, "candidate"))
        for timer in sorted(self.timers, key=lambda t: t.due):
            if timer.due <= self.clock.now():
                self.timers.remove(timer)
                self._run(timer)
                acted = True
        return acted

    # ---- observing --------------------------------------------------------------------------------------------

    def _news(self) -> None:
        self.quiet_since = self.clock.now()
        self.quiet_fired = set()

    def _quiet_rules(self) -> list[dict[str, Any]]:
        return [r for r in self.scenario.candidate_rules if r["on"].startswith("quiet_days:")]

    def _quiet_due(self) -> list[dict[str, Any]]:
        due = []
        for index, rule in enumerate(self._quiet_rules()):
            wait = timedelta(days=int(rule["on"].partition(":")[2]))
            if index not in self.quiet_fired and self.clock.now() >= self.quiet_since + wait:
                self.quiet_fired.add(index)
                due.append(rule)
        return due

    def _observe(self) -> None:
        if self.employer and not self.introduction_seen and self.world.org_id:
            accepted = self.employer.call("GET", f"/v1/introductions?org_id={self.world.org_id}&status=accepted")
            if any(item["id"] == str(self.world.introduction_id) for item in accepted):
                self.introduction_seen = True
                self.pending.append(("employer", "introduction_accepted", {}))
                self._news()
        if self.campaign_id is None and self.scenario.start == "introduction" and self.introduction_seen:
            board = self.candidate.call("GET", "/v1/campaigns?include_closed=true")
            for column in board["columns"]:
                for card in column["campaigns"]:
                    if card["from_introduction"]:
                        self.campaign_id = uuid.UUID(card["id"])
        if self.campaign_id is None:
            return
        detail = self.candidate.call("GET", f"/v1/campaigns/{self.campaign_id}")
        if detail["stage"] != self.last_stage:
            self.last_stage = detail["stage"]
            self.pending.append(("candidate", f"stage:{detail['stage']}", {}))
            self._news()
        if detail["pause_reason"] != self.last_pause:
            self.last_pause = detail["pause_reason"]
            if detail["pause_reason"]:
                self.pending.append(("candidate", f"paused:{detail['pause_reason']}", {}))
            self._news()
        for approval in detail["pending_approvals"]:
            if approval["id"] not in self.seen_approvals:
                self.seen_approvals.add(approval["id"])
                self.pending.append(("candidate", f"approval:{approval['kind']}", {"approval_id": approval["id"]}))
                self._news()
        packet = detail.get("packet") or {}
        if not self.kit_released and packet.get("status") == "approved":
            self.kit_released = True
            self.pending.append(("candidate", "kit_released", {}))
            self._news()

    # ---- acting -----------------------------------------------------------------------------------------------

    def _note(self, action: str) -> None:
        self.log.append(Entry(self.clock.day_now(), action))

    def _run(self, timer: Timer) -> None:
        verb, _, argument = str(timer.rule["do"]).partition(" ")
        if timer.who == "employer":
            self._employer(verb, argument)
            return
        handler = {
            "start_campaign": self._start_campaign,
            "accept_introduction": self._accept_introduction,
            "set_follow_up_email": self._set_follow_up_email,
            "approve": lambda: self._decide(timer, approve=True),
            "edit": lambda: self._decide(timer, approve=True, edits=timer.rule.get("edits")),
            "reject": lambda: self._decide(timer, approve=False),
            "record_status": lambda: self._record_status(argument),
            "cancel": lambda: self._cancel(argument),
            "resume": self._resume,
            "none": lambda: None,
        }[verb]
        handler()
        self._news()

    def _start_campaign(self) -> None:
        created = self.candidate.call("POST", "/v1/campaigns", {"job_id": self.world.job_id})
        self.campaign_id = uuid.UUID(created["id"])
        self._note("start_campaign")

    def _accept_introduction(self) -> None:
        asked = self.candidate.call("GET", f"/v1/introductions/{self.world.introduction_id}")
        self.candidate.call(
            "POST",
            f"/v1/introductions/{self.world.introduction_id}/accept",
            {
                "share_preview_hash": asked["share_preview_hash"],
                "consent_text_version": trust.INTRODUCTION_TEXT_VERSION,
            },
        )
        self._note("accept_introduction")

    def _set_follow_up_email(self) -> None:
        self.candidate.call("PATCH", f"/v1/campaigns/{self.campaign_id}", {"follow_up_email": self.world.typed_address})
        self._note("set_follow_up_email")

    def _decide(self, timer: Timer, *, approve: bool, edits: dict[str, Any] | None = None) -> None:
        listed = self.candidate.call("GET", f"/v1/approvals?status=pending&campaign_id={self.campaign_id}")
        wanted = timer.context.get("approval_id")
        for item in listed:
            if wanted is not None and item["id"] != wanted:
                continue
            if approve:
                body: dict[str, Any] = {"payload_hash": item["payload_hash"]}
                if edits:
                    body["edits"] = edits
                self.candidate.call("POST", f"/v1/approvals/{item['id']}/approve", body)
                self._note("edit" if edits else f"approve:{item['kind']}")
            else:
                self.candidate.call("POST", f"/v1/approvals/{item['id']}/reject", {})
                self._note(f"reject:{item['kind']}")

    def _record_status(self, status: str) -> None:
        now = datetime.now(UTC).isoformat()
        if self.application_id is None:
            found = [a for a in self.candidate.call("GET", "/v1/applications") if a["job_id"] == self.world.job_id]
            self.application_id = found[0]["id"] if found else None
        if self.application_id is None:
            created = self.candidate.call("POST", "/v1/applications", {"job_id": self.world.job_id, "occurred_at": now})
            self.application_id = created["id"]
            if status == "applied":
                self._note(f"record_status {status}")
                return
        self.candidate.call(
            "POST", f"/v1/applications/{self.application_id}/status", {"status": status, "occurred_at": now}
        )
        self._note(f"record_status {status}")

    def _cancel(self, reason: str) -> None:
        self.candidate.call("POST", f"/v1/campaigns/{self.campaign_id}/cancel", {"reason": reason})
        self._note(f"cancel {reason}")

    def _resume(self) -> None:
        self.candidate.call("POST", f"/v1/campaigns/{self.campaign_id}/resume", {})
        self._note("resume")

    def _employer(self, verb: str, argument: str) -> None:
        if verb == "reply":
            self.pending.append(("candidate", f"employer_reply:{argument}", {}))
            self._news()
        elif verb == "answer" and self.employer:
            reason = self.scenario.world["employer"]["answer_reason"]
            self.employer.call(
                "POST", f"/v1/introductions/{self.world.introduction_id}/answer", {"answer": argument, "reason": reason}
            )
            self._news()
        # ignore: the employer says nothing
