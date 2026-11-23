"""What other modules may call from app.trust (the shared contracts in the Phase 3 index). Nothing else here is a public
interface. The deletion registry has its own module, app.trust.privacy."""

from .audit import audit_log
from .consent import (
    INTRODUCTION_TEXT_VERSION,
    ConsentRequired,
    ai_processing_text_version,
    current_consents,
    grant_consent,
    grant_introduction_consent,
    require_consent,
    withdraw_consent,
)

__all__ = [
    "INTRODUCTION_TEXT_VERSION",
    "ConsentRequired",
    "ai_processing_text_version",
    "audit_log",
    "current_consents",
    "grant_consent",
    "grant_introduction_consent",
    "require_consent",
    "withdraw_consent",
]
