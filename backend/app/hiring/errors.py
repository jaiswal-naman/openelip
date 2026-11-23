"""The database's refusals of the hiring rules, as the API.md 1.11 catalogue words them.

The triggers raise SQLSTATE 23514 with a message that starts with a fixed prefix; a route that writes through them calls
from_db on a failure and raises what it returns, so a refusal from the last line of defence still reads correctly. Any
other error is not ours to word: from_db returns None and the caller lets it through.
"""

from app.platform.errors import ApiError
from app.trust.api import ConsentRequired

CHECK_VIOLATION = "23514"


def from_db(exc: BaseException) -> ApiError | None:
    """The API error for a database refusal (consent_missing, hard_constraint, answer_clock), else None.

    Takes the SQLAlchemy exception or the driver's own; the message is the trigger's, which carries no personal text.
    """
    origin = getattr(exc, "orig", exc)
    if getattr(origin, "sqlstate", None) != CHECK_VIOLATION:
        return None
    message = getattr(getattr(origin, "diag", None), "message_primary", None) or ""
    if message.startswith("consent_missing"):
        purpose = "open_to_introductions" if "not open to introductions" in message else "introduction"
        return ConsentRequired("missing", purpose)
    if message.startswith("hard_constraint"):
        return ApiError(422, "violates_constraint", "This role breaks one of the candidate's hard constraints.")
    if message.startswith("answer_clock"):
        return ApiError(
            409,
            "conflict",
            "This organization has answers overdue, so it cannot ask anyone new until they are answered.",
            {"state": "answers_overdue"},
        )
    return None
