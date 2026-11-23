"""The morning brief as an email (template brief_v1; BRF-01, BRF-03): the plain text, and the resolver the relay calls
when the row is due.

The outbox row holds the id of the brief and nothing else. The recipient, the consent, the body and the unsubscribe link
are all worked out here, at send time, from rows the person owns, so an email never carries what the person has since
taken back (ARCHITECTURE 5.5).
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text

from app.platform import config
from app.platform.relay import Outgoing, Refusal, is_undeliverable

from . import unsubscribe, views
from .compose import email_is_on


@dataclass(frozen=True)
class Links:
    """Where the email points. `base` is the public address of the app without a trailing slash; `token` is the signed
    unsubscribe token."""

    base: str
    token: str

    def match(self, decision_id: UUID) -> str:
        return f"{self.base}/app/matches/{decision_id}"

    @property
    def today(self) -> str:
        return f"{self.base}/app/today"

    @property
    def record(self) -> str:
        return f"{self.base}/app/record"

    @property
    def settings(self) -> str:
        return f"{self.base}/app/settings?section=brief"

    @property
    def unsubscribe_page(self) -> str:
        """The visible link: it opens a page with one button, so a scanner that fetches it unsubscribes nobody."""
        return f"{self.base}/unsubscribe?t={self.token}"

    @property
    def unsubscribe_post(self) -> str:
        """The one-click target a mail client POSTs to (RFC 8058); the List-Unsubscribe header names it."""
        return f"{self.base}/v1/briefs/unsubscribe?t={self.token}"


def _line(text: str) -> str:
    """One line of plain text. Job titles and summaries come from outside the system, so no line break survives."""
    return " ".join(text.split())


def render(product_name: str, view: dict[str, Any], links: Links) -> tuple[str, str]:
    """The subject and the plain-text body of a brief, written from brief_view's answer."""
    matches = [d for d in view["decisions"] if d["kind"] == "match" and d["is_assessed"]]
    unassessed = [d for d in view["decisions"] if d["kind"] == "match" and not d["is_assessed"]]
    proposals = next((d for d in view["decisions"] if d["kind"] == "record_proposals"), None)
    subject = "Your morning brief"
    if matches:
        subject += f": {len(matches)} {'match' if len(matches) == 1 else 'matches'}"
    out = [f"Your morning brief for {view['local_date']}.", ""]
    if matches:
        out += [f"Matches ({len(matches)})", ""]
        for number, item in enumerate(matches, start=1):
            out += [f"{number}. {_line(item['title'])}", f"   {_line(item['why'])}"]
            if item["change_note"]:
                out.append(f"   Changed since you saw it: {_line(item['change_note'])}")
            if item["note"]:
                out.append(f"   {_line(item['note'])}.")
            out += [f"   {links.match(item['ref']['decision_id'])}", ""]
    if unassessed:
        out += [
            f"Not yet assessed ({len(unassessed)})",
            "The search ranked these high, but no model has checked them against your record.",
            *(f"- {_line(item['title'])}" for item in unassessed),
            f"  {links.today}",
            "",
        ]
    if proposals:
        out += ["Waiting for you", f"{_line(proposals['title'])}: {links.record}", ""]
    if view["done_since_last_brief"]:
        out += [
            "Done since your last brief",
            *(f"- {_line(line['text'])}" for line in view["done_since_last_brief"]),
            "",
        ]
    out += [
        f"Settings: {links.settings}",
        f"Stop this email: {links.unsubscribe_page}",
        "",
        f"Sent by {product_name}.",
    ]
    return subject, "\n".join(out) + "\n"


def resolve(conn: Connection, payload: dict[str, Any]) -> Outgoing | Refusal:
    """The relay's resolver for brief_v1: who gets what, or why this row must not be sent."""
    try:
        brief_id = UUID(str(payload["brief_id"]))
    except (KeyError, TypeError, ValueError):
        return Refusal("failed", "bad_payload")
    person = conn.execute(
        text(
            """SELECT b.user_id, u.email FROM briefs b JOIN users u ON u.id = b.user_id
               WHERE b.id = :id AND b.channel = 'in_app'"""
        ),
        {"id": brief_id},
    ).one_or_none()
    if person is None:
        return Refusal("cancelled", "stale")  # the brief or the account is gone
    if not email_is_on(conn, person.user_id):
        return Refusal("cancelled", "consent_withdrawn")  # taken back after the brief was composed
    if is_undeliverable(conn, person.email):
        return Refusal("cancelled", "undeliverable")
    view = views.brief_view(conn, brief_id, person.user_id)
    if view is None:
        return Refusal("cancelled", "stale")
    settings = config.get()
    links = Links(settings.app_base_url.rstrip("/"), unsubscribe.sign(person.user_id, now=datetime.now(timezone.utc)))
    subject, body = render(settings.product_name, view, links)
    headers = {"List-Unsubscribe": f"<{links.unsubscribe_post}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}
    return Outgoing(person.email, subject, body, headers)
