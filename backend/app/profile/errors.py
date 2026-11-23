"""The Career Record's failures. Each is an ApiError, so the existing handler answers with the envelope (API.md 1.11)
and a job or another module catches the class by name. None carries text a person wrote: a message and its details name
a field, a state or a category, never the value that was refused.
"""

from app.platform.errors import ApiError

from .models import CONSTRAINT_KINDS


class ProtectedCharacteristic(ApiError):
    """A free-text field states a protected characteristic (REC-07). Nothing was written.

    Answers 422 validation_failed with the field and the code protected_characteristic in details.fields, the same
    shape as every other field error. The category (age, religion, ...) is on the exception, never the text.
    """

    def __init__(self, category: str, field: str) -> None:
        super().__init__(
            422,
            "validation_failed",
            "This is not kept in your record.",
            {"fields": [{"field": field, "issue": "protected_characteristic"}]},
        )
        self.category = category
        self.field = field


class InvalidField(ApiError):
    """One field cannot be used. issue is a short code: required, too_long, invalid_value, invalid_characters,
    not_used_by_kind, duplicate, unknown_field, cannot_change."""

    def __init__(self, field: str, issue: str) -> None:
        super().__init__(
            422, "validation_failed", "One or more fields are invalid.", {"fields": [{"field": field, "issue": issue}]}
        )
        self.field = field
        self.issue = issue


class RecordNotFound(ApiError):
    """No such row, or it is not this person's: the same answer for both, so ids reveal nothing (API.md 1.2)."""

    def __init__(self) -> None:
        super().__init__(404, "not_found", "No such resource.")


class StateConflict(ApiError):
    """The row's state does not allow the action. details.state is the state it is in."""

    def __init__(self, state: str) -> None:
        super().__init__(409, "conflict", "The resource's state does not allow this.", {"state": state})
        self.state = state


class AlreadyResolved(ApiError):
    """A proposal already has its answer. details.resolution is applied, declined or expired."""

    def __init__(self, resolution: str) -> None:
        super().__init__(409, "already_resolved", "This proposal already has its answer.", {"resolution": resolution})
        self.resolution = resolution


class ConstraintNotEnforceable(ApiError):
    """A hard constraint of a kind the matcher cannot check (API.md 3.2). Offers the statement back as a preference."""

    def __init__(self, statement: str) -> None:
        super().__init__(
            422,
            "constraint_not_enforceable",
            "This cannot be checked against job postings as a hard rule, so it cannot be guaranteed.",
            {
                "supported_kinds": list(CONSTRAINT_KINDS),
                "suggested_preference": {"kind": "untyped_dealbreaker", "statement": statement},
                "hint": "You can save it as a strong preference, or block specific companies with an excluded_company"
                " constraint.",
            },
        )


class BadCursor(ApiError):
    """A pagination cursor that this API did not issue."""

    def __init__(self) -> None:
        super().__init__(400, "invalid_request", "That cursor is not valid.")
