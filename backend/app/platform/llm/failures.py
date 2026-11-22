"""What a gateway call can end in besides an answer (AI_SYSTEM 2.1). None of them carries prompt or response text."""

from ..errors import ApiError

RETRY_AFTER_SECONDS = 30  # a fixed hint for API.md 1.11's dependency_unavailable


class AIDisabled(ApiError):
    """No usable provider for the task, so no call was made. The API answers it as 503 ai_disabled.

    reason is a short code for the log (no_route, no_key, unpriced, fake_in_production); callers never see it."""

    def __init__(self, reason: str = "no_route") -> None:
        super().__init__(503, "ai_disabled", "This feature needs an AI model and none is configured.")
        self.reason = reason


class ProviderUnavailable(ApiError):
    """The provider timed out or failed after the SDK's own retries: the API answers 503 dependency_unavailable."""

    def __init__(self, *, timed_out: bool = False) -> None:
        super().__init__(
            503,
            "dependency_unavailable",
            "The AI model provider is not answering. Try again shortly.",
            {"retry_after_seconds": RETRY_AFTER_SECONDS},
        )
        self.timed_out = timed_out


class SchemaInvalid(Exception):
    """The model's answer did not fit its schema twice, or it refused twice. str() is the outcome, never the answer."""

    def __init__(self, *, refused: bool = False) -> None:
        super().__init__("refused" if refused else "schema_invalid")
        self.refused = refused
