"""What other modules may call from app.trust (the shared contracts in the Phase 3 index). Nothing else here is a public
interface. The deletion registry has its own module, app.trust.privacy."""

from .audit import audit_log
from .auditlog import ID_WIDTH, AuditEntry, AuditFilter, before_sql, entry_id
from .auditlog import code as audit_code
from .auditlog import source as audit_source
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
from .restricted import Restricted, redact_contact, restricted_matches

__all__ = [
    "ID_WIDTH",
    "INTRODUCTION_TEXT_VERSION",
    "AuditEntry",
    "AuditFilter",
    "ConsentRequired",
    "Restricted",
    "ai_processing_text_version",
    "audit_code",
    "audit_log",
    "audit_source",
    "before_sql",
    "current_consents",
    "entry_id",
    "grant_consent",
    "grant_introduction_consent",
    "redact_contact",
    "require_consent",
    "restricted_matches",
    "withdraw_consent",
]
