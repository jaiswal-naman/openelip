"""The employer's roles: describe, confirm, publish, close (HIR-02, HIR-03, the publish half of HIR-06; plan 06-03).

Every function takes an open connection and the signed-in person's id. Reads and draft edits run on the request's
connection as app_api, so row-level security keeps one organization out of another: a role the caller cannot see is
"not found" (404), never "forbidden". Publish and close run in one worker-role transaction of their own, because the
job index (`jobs`, `job_sources`, `job_requirements`) and the withdrawal of asked introductions are not writable by
app_api; that transaction re-checks membership itself before it touches anything.

A role is a draft until it is published. A draft's requirements start as what the model read from the description
(each with its span, in code points) and become the employer's own once confirmed; items the employer typed carry no
span (INDEX deviation 3). An open or closed role is read-only: its asks carry frozen evidence against those requirements
(INDEX deviation 4).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text
from sqlalchemy.engine import Row
from sqlalchemy.exc import DBAPIError

from app.ingest.api import DirectRequirement, close_direct_job, publish_direct_job
from app.platform import db, queue
from app.platform.errors import ApiError
from app.trust.api import audit_log

from . import errors, intake
from .access import require_member

MAX_REQUIREMENTS = intake.MAX_REQUIREMENTS  # assumption: 30 per role (INDEX)
ADDED_BLOCK_TITLE = "Requirements added by the employer"  # where typed items are appended in the job's text

# The columns an employer sets on a draft, and the SQL type each is cast to ("" for a plain column). The enum columns
# are bound through explicit casts, so the driver's idea of a parameter's type never matters.
_FIELD_CASTS = {
    "title": "",
    "description": "",
    "country": "",
    "city": "",
    "work_arrangements": "work_arrangement[]",
    "employment_type": "employment_type",
    "seniority": "seniority",
    "visa_sponsorship": "visa_sponsorship",
}

_ROLE = """SELECT r.id, r.organization_id, r.title, r.description, r.status::text AS status, r.country, r.city,
                  CAST(r.work_arrangements AS text[]) AS work_arrangements, r.employment_type::text AS employment_type,
                  r.seniority::text AS seniority, r.visa_sponsorship::text AS visa_sponsorship, r.salary_min,
                  r.salary_max, r.salary_currency, r.job_id, r.requirements_confirmed_at, r.created_at, r.opened_at,
                  r.closed_at
           FROM roles r WHERE r.id = :id"""


@dataclass(frozen=True)
class RequirementInput:
    """One line of the employer's list. A span is kept only while the text is still the description at that span."""

    kind: str
    text: str
    importance: str
    is_confirmed: bool
    span: tuple[int, int] | None = None


def _not_found() -> ApiError:
    return ApiError(404, "not_found", "No such resource.")


def _wrong_state(state: str) -> ApiError:
    messages = {
        "open": "This role is open, so it can no longer be changed. Close it and describe a new one.",
        "closed": "This role is closed.",
        "draft": "This role is still a draft, so it has nothing to close.",
    }
    return ApiError(409, "conflict", messages[state], {"state": state})


def blocker_codes(*, has_pay_range: bool, requirements_confirmed: bool) -> list[str]:
    """What stops a role from opening, in the order the API reports it (API.md 3.10)."""
    codes = []
    if not has_pay_range:
        codes.append("pay_range_required")
    if not requirements_confirmed:
        codes.append("requirements_unconfirmed")
    return codes


def _refusal(codes: list[str]) -> ApiError:
    messages = {
        "pay_range_required": "A role cannot open without a pay range. Candidates see it before they decide.",
        "requirements_unconfirmed": "Confirm the requirements before the role opens.",
    }
    return ApiError(422, codes[0], messages[codes[0]], {"blockers": codes})


# ---- reading -------------------------------------------------------------------------------------------------------


def _fetch(conn: Connection, role_id: UUID, *, lock: bool = False) -> Row[Any]:
    """The role, as the caller's row-level security shows it; 404 when it is not theirs or does not exist."""
    row = conn.execute(text(_ROLE + (" FOR UPDATE OF r" if lock else "")), {"id": role_id}).one_or_none()
    if row is None:
        raise _not_found()
    return row


def _requirement_rows(conn: Connection, role_id: UUID) -> list[Row[Any]]:
    return list(
        conn.execute(
            text(
                """SELECT id, ordinal, kind::text AS kind, text, importance::text AS importance, is_confirmed,
                          lower(span) AS span_start, upper(span) AS span_end
                   FROM role_requirements WHERE role_id = :id ORDER BY ordinal"""
            ),
            {"id": role_id},
        )
    )


def _view(conn: Connection, role: Row[Any]) -> dict[str, Any]:
    items = _requirement_rows(conn, role.id)
    has_pay = role.salary_min is not None and role.salary_max is not None and role.salary_currency is not None
    confirmed = role.requirements_confirmed_at is not None
    codes = blocker_codes(has_pay_range=has_pay, requirements_confirmed=confirmed)
    requirements = []
    for item in items:
        has_span = item.span_start is not None
        requirements.append(
            {
                "id": item.id,
                "kind": item.kind,
                "text": item.text,
                "importance": item.importance,
                "span": {"start": item.span_start, "end": item.span_end} if has_span else None,
                # the description's own words at the span (code points), never a copy the employer typed
                "quote": role.description[item.span_start : item.span_end] if has_span else None,
                "is_confirmed": item.is_confirmed,
            }
        )
    return {
        "id": role.id,
        "organization_id": role.organization_id,
        "title": role.title,
        "description": role.description,
        "status": role.status,
        "country": role.country,
        "city": role.city,
        "work_arrangements": list(role.work_arrangements),
        "employment_type": role.employment_type,
        "seniority": role.seniority,
        "visa_sponsorship": role.visa_sponsorship,
        "pay_range": {"min": role.salary_min, "max": role.salary_max, "currency": role.salary_currency}
        if has_pay
        else None,
        "job_id": role.job_id,
        "requirements": requirements,
        "can_publish": role.status == "draft" and not codes,
        "blockers": codes if role.status == "draft" else [],
        "created_at": role.created_at,
        "opened_at": role.opened_at,
        "closed_at": role.closed_at,
    }


def get(conn: Connection, caller: UUID, role_id: UUID) -> dict[str, Any]:
    """One role with its requirements, pay range, can_publish and blockers. 404 for another organization's role."""
    return _view(conn, _fetch(conn, role_id))


def blockers(conn: Connection, caller: UUID, role_id: UUID) -> list[str]:
    """pay_range_required and requirements_unconfirmed, in that order, for a role that is still a draft."""
    result: list[str] = get(conn, caller, role_id)["blockers"]
    return result


def list_for_org(conn: Connection, caller: UUID, org_id: UUID) -> list[dict[str, Any]]:
    """The organization's roles, most recently changed first, with what the employer still owes on each (HIR-06)."""
    require_member(conn, org_id)
    rows = conn.execute(
        text(
            """SELECT r.id, r.title, r.status::text AS status, r.created_at,
                      coalesce(r.closed_at, r.opened_at, r.created_at) AS updated_at,
                      count(i.id) FILTER (WHERE i.status = 'accepted') AS said_yes,
                      count(i.id) FILTER (WHERE i.status = 'accepted' AND i.employer_answer IS NULL) AS answers_owed,
                      count(i.id) FILTER (WHERE i.status = 'accepted' AND i.employer_answer IS NULL
                                            AND i.answer_due_at < now()) AS answers_overdue
               FROM roles r LEFT JOIN introductions i ON i.role_id = r.id
               WHERE r.organization_id = :org
               GROUP BY r.id ORDER BY updated_at DESC, r.id"""
        ),
        {"org": org_id},
    )
    return [dict(row._mapping) for row in rows]


# ---- writing a draft -----------------------------------------------------------------------------------------------


def _insert_requirement(conn: Connection, role_id: UUID, org_id: UUID, ordinal: int, item: RequirementInput) -> None:
    conn.execute(
        text(
            """INSERT INTO role_requirements (role_id, organization_id, ordinal, importance, kind, text, span,
                                              is_confirmed)
               VALUES (:role, :org, :ordinal, CAST(:importance AS requirement_importance),
                       CAST(:kind AS requirement_kind), :text,
                       CASE WHEN CAST(:start AS integer) IS NULL THEN NULL
                            ELSE int4range(CAST(:start AS integer), CAST(:end AS integer)) END,
                       :confirmed)"""
        ),
        {
            "role": role_id,
            "org": org_id,
            "ordinal": ordinal,
            "importance": item.importance,
            "kind": item.kind,
            "text": item.text,
            "start": item.span[0] if item.span else None,
            "end": item.span[1] if item.span else None,
            "confirmed": item.is_confirmed,
        },
    )


def _extracted(rows: Sequence[intake.RequirementRow]) -> list[RequirementInput]:
    return [RequirementInput(r.kind, r.text, r.importance, False, (r.start, r.end)) for r in rows]


def create(
    conn: Connection,
    caller: UUID,
    *,
    org_id: UUID,
    title: str,
    description: str,
    fields: Mapping[str, Any] | None = None,
    pay_range: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """A draft role with the requirements read from its description (extraction state in the answer's `extraction`).

    With no model available the role is stored with no requirements and extraction.state `ai_disabled`: the employer
    types them. Any member of the organization may create a role; `created_by` is the caller.
    """
    require_member(conn, org_id)
    values: dict[str, Any] = {"title": title, "description": description, **(fields or {})}
    columns = {**_column_values(values), **_pay_values(pay_range)}
    names = ", ".join(columns)
    placeholders = ", ".join(_placeholder(name) for name in columns)
    role_id: UUID = conn.execute(
        text(
            f"""INSERT INTO roles (organization_id, created_by, {names})
                VALUES (:org, :caller, {placeholders}) RETURNING id"""
        ),
        {"org": org_id, "caller": caller, **columns},
    ).scalar_one()
    result = intake.extract(description, caller)
    for ordinal, item in enumerate(_extracted(result.rows), start=1):
        _insert_requirement(conn, role_id, org_id, ordinal, item)
    view = _view(conn, _fetch(conn, role_id))
    view["extraction"] = {"state": result.state, "dropped": result.dropped}
    return view


def _column_values(values: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(values) - set(_FIELD_CASTS)
    if unknown:
        raise ValueError(f"not a role field: {sorted(unknown)}")
    return dict(values)


def _pay_values(pay_range: Mapping[str, Any] | None) -> dict[str, Any]:
    pay = pay_range or {}
    return {"salary_min": pay.get("min"), "salary_max": pay.get("max"), "salary_currency": pay.get("currency")}


def _placeholder(name: str) -> str:
    cast = _FIELD_CASTS.get(name, "")
    return f"CAST(:{name} AS {cast})" if cast else f":{name}"


def update(conn: Connection, caller: UUID, role_id: UUID, changes: Mapping[str, Any]) -> dict[str, Any]:
    """Change fields of a draft. `changes` holds only what the caller sent; `pay_range` (a dict, or None to clear it)
    sets the three salary columns together. A changed description is read again: the extracted items are replaced, the
    typed items stay, and every confirmation is cleared. An open or closed role is 409 conflict with its state."""
    role = _fetch(conn, role_id, lock=True)
    if role.status != "draft":
        raise _wrong_state(role.status)
    fields = {name: value for name, value in changes.items() if name != "pay_range"}
    columns = _column_values(fields)
    if "pay_range" in changes:
        columns.update(_pay_values(changes["pay_range"]))
    if columns:
        assignments = ", ".join(f"{name} = {_placeholder(name)}" for name in columns)
        conn.execute(text(f"UPDATE roles SET {assignments} WHERE id = :id"), {"id": role_id, **columns})
    extraction: dict[str, Any] | None = None
    if "description" in changes and changes["description"] != role.description:
        extraction = _read_again(conn, caller, role, changes["description"])
    view = _view(conn, _fetch(conn, role_id))
    view["extraction"] = extraction
    return view


def _read_again(conn: Connection, caller: UUID, role: Row[Any], description: str) -> dict[str, Any]:
    """Replace the extracted items with what the new description yields; typed items keep their place after them."""
    result = intake.extract(description, caller)
    typed = conn.execute(
        text("SELECT count(*) FROM role_requirements WHERE role_id = :id AND span IS NULL"), {"id": role.id}
    ).scalar_one()
    room = max(MAX_REQUIREMENTS - typed, 0)
    fresh = _extracted(result.rows[:room])
    dropped = result.dropped + len(result.rows) - len(fresh)
    # Typed rows move out of the way first (ordinals are unique per role), the old extracted rows go, the new ones take
    # 1..k, and the typed rows follow them in their old order.
    conn.execute(
        text("UPDATE role_requirements SET ordinal = ordinal + 1000 WHERE role_id = :id AND span IS NULL"),
        {"id": role.id},
    )
    conn.execute(text("DELETE FROM role_requirements WHERE role_id = :id AND span IS NOT NULL"), {"id": role.id})
    for ordinal, item in enumerate(fresh, start=1):
        _insert_requirement(conn, role.id, role.organization_id, ordinal, item)
    conn.execute(
        text(
            """UPDATE role_requirements t SET ordinal = n.rank + :base
               FROM (SELECT id, row_number() OVER (ORDER BY ordinal) AS rank
                     FROM role_requirements WHERE role_id = :id AND span IS NULL) n
               WHERE t.id = n.id"""
        ),
        {"id": role.id, "base": len(fresh)},
    )
    conn.execute(text("UPDATE role_requirements SET is_confirmed = false WHERE role_id = :id"), {"id": role.id})
    conn.execute(text("UPDATE roles SET requirements_confirmed_at = NULL WHERE id = :id"), {"id": role.id})
    return {"state": result.state, "dropped": dropped}


def set_requirements(
    conn: Connection, caller: UUID, role_id: UUID, items: Sequence[RequirementInput]
) -> dict[str, Any]:
    """Replace a draft's requirement list (ordinals 1..n). An item whose text is no longer the description at its span
    loses the span. The list counts as confirmed only when it is not empty and every item is confirmed."""
    role = _fetch(conn, role_id, lock=True)
    if role.status != "draft":
        raise _wrong_state(role.status)
    if len(items) > MAX_REQUIREMENTS:
        raise ApiError(
            422,
            "validation_failed",
            f"A role has at most {MAX_REQUIREMENTS} requirements.",
            {"fields": [{"field": "requirements", "issue": f"at most {MAX_REQUIREMENTS} items"}]},
        )
    conn.execute(text("DELETE FROM role_requirements WHERE role_id = :id"), {"id": role_id})
    for ordinal, item in enumerate(items, start=1):
        _insert_requirement(conn, role_id, role.organization_id, ordinal, _kept_span(role.description, item))
    all_confirmed = bool(items) and all(item.is_confirmed for item in items)
    conn.execute(
        text("UPDATE roles SET requirements_confirmed_at = CASE WHEN :ok THEN now() END WHERE id = :id"),
        {"ok": all_confirmed, "id": role_id},
    )
    return _view(conn, _fetch(conn, role_id))


def _kept_span(description: str, item: RequirementInput) -> RequirementInput:
    if item.span is None:
        return item
    start, end = item.span
    if 0 <= start < end <= len(description) and description[start:end] == item.text:
        return item
    return RequirementInput(item.kind, item.text, item.importance, item.is_confirmed, None)


# ---- publish and close ---------------------------------------------------------------------------------------------


def _locked_for_member(conn: Connection, role_id: UUID) -> Row[Any]:
    """The role, locked, only when the caller is an active member of its organization (the worker role sees every row,
    so this is the whole of the authorization: 404 for anyone else)."""
    row = conn.execute(
        text(_ROLE + " AND app_is_org_member(r.organization_id) FOR UPDATE OF r"), {"id": role_id}
    ).one_or_none()
    if row is None:
        raise _not_found()
    return row


def _job_text(description: str, items: Sequence[Row[Any]]) -> tuple[str, list[DirectRequirement]]:
    """The job's description and one requirement per item, every span resolving in it (INDEX deviation 3).

    Extracted items keep their spans in the role's own text. Typed items (and any whose text is no longer at its span)
    are appended under a heading, one per line, and get their spans there."""
    body = description
    pending: list[tuple[int, Row[Any]]] = []
    spans: dict[int, tuple[int, int]] = {}
    for number, item in enumerate(items, start=1):
        start, end = item.span_start, item.span_end
        if start is not None and description[start:end] == item.text:
            spans[number] = (start, end)
        else:
            pending.append((number, item))
    if pending:
        body = f"{description.rstrip()}\n\n{ADDED_BLOCK_TITLE}\n"
        for number, item in pending:
            start = len(body)
            body += item.text
            spans[number] = (start, len(body))
            body += "\n"
    found = [
        DirectRequirement(number, item.importance, item.kind, item.text, *spans[number])
        for number, item in enumerate(items, start=1)
    ]
    return body, found


def publish(caller: UUID, role_id: UUID) -> None:
    """Open a confirmed draft with a pay range as an unlisted job, queue the one ask job and write the audit row, all in
    one transaction. 404 for another organization's role; 409 conflict (open, closed) for a role that is not a draft;
    422 pay_range_required or requirements_unconfirmed (every blocker listed); 409 conflict answers_overdue while an
    answer is overdue. Nothing changes on a refusal."""
    try:
        # The worker role, because jobs and job_sources are not writable by app_api; membership is checked below.
        with db.transaction(caller, role="worker") as conn:
            role = _locked_for_member(conn, role_id)
            if role.status != "draft":
                raise _wrong_state(role.status)
            items = _requirement_rows(conn, role_id)
            has_pay = role.salary_min is not None and role.salary_max is not None and role.salary_currency is not None
            confirmed = (
                role.requirements_confirmed_at is not None and bool(items) and all(i.is_confirmed for i in items)
            )
            codes = blocker_codes(has_pay_range=has_pay, requirements_confirmed=confirmed)
            if codes:
                raise _refusal(codes)
            if conn.execute(
                text("SELECT organization_has_overdue_answers(:org)"), {"org": role.organization_id}
            ).scalar_one():
                raise ApiError(
                    409,
                    "conflict",
                    "This organization has answers overdue, so it cannot open a role until they are answered.",
                    {"state": "answers_overdue"},
                )
            company = conn.execute(
                text("SELECT name FROM organizations WHERE id = :id"), {"id": role.organization_id}
            ).scalar_one()
            description, requirements = _job_text(role.description, items)
            job_id = publish_direct_job(
                conn,
                organization_id=role.organization_id,
                company_name=company,
                role_id=role_id,
                title=role.title,
                description=description,
                country=role.country,
                city=role.city,
                work_arrangements=role.work_arrangements,
                employment_type=role.employment_type,
                seniority=role.seniority,
                salary_min=role.salary_min,
                salary_max=role.salary_max,
                salary_currency=role.salary_currency,
                visa_sponsorship=role.visa_sponsorship,
                requirements=requirements,
            )
            conn.execute(
                text("UPDATE roles SET job_id = :job, status = 'open', opened_at = now() WHERE id = :id"),
                {"job": job_id, "id": role_id},
            )
            queue.enqueue(
                conn,
                "hiring.ask_candidates",
                {"role_id": str(role_id)},
                unique_key=f"hiring.ask:{role_id}:published",
                queue="llm",
            )
            audit_log(
                conn,
                "hiring.role_published",
                "employer",
                None,
                {"role_id": str(role_id), "job_id": job_id},
                actor_user_id=caller,
                entity_table="roles",
                entity_id=str(role_id),
                organization_id=role.organization_id,
            )
    except DBAPIError as exc:
        raise errors.from_db(exc) or exc from exc


def close(caller: UUID, role_id: UUID) -> None:
    """Close an open role: its asked introductions are withdrawn, accepted ones keep their answer obligation, the job
    is closed, and one audit row is written. 409 conflict (draft, closed) for a role that is not open."""
    try:
        with db.transaction(caller, role="worker") as conn:  # worker: asked introductions and jobs are not app_api's
            role = _locked_for_member(conn, role_id)
            if role.status != "open":
                raise _wrong_state(role.status)
            withdrawn = conn.execute(
                text("UPDATE introductions SET status = 'withdrawn' WHERE role_id = :id AND status = 'asked'"),
                {"id": role_id},
            ).rowcount
            conn.execute(text("UPDATE roles SET status = 'closed', closed_at = now() WHERE id = :id"), {"id": role_id})
            close_direct_job(conn, role.job_id)
            audit_log(
                conn,
                "hiring.role_closed",
                "employer",
                None,
                {"role_id": str(role_id), "withdrawn": withdrawn},
                actor_user_id=caller,
                entity_table="roles",
                entity_id=str(role_id),
                organization_id=role.organization_id,
            )
    except DBAPIError as exc:
        raise errors.from_db(exc) or exc from exc
