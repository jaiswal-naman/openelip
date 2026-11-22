"""The model gateway (ARCHITECTURE 5.8, AI_SYSTEM 2). Callers name a task and get a validated answer from complete()."""

from .failures import AIDisabled, ProviderUnavailable, SchemaInvalid
from .gateway import complete

__all__ = ["AIDisabled", "ProviderUnavailable", "SchemaInvalid", "complete"]
