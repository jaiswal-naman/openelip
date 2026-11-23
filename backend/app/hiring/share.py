"""The hash of a share preview (API.md 3.12).

A candidate sends the hash of the preview they were shown when they accept, and the ask is refused with 409
preview_changed if it no longer matches what is stored, so nothing is shared that they did not see.
"""

import hashlib
import json
from typing import Any


def preview_hash(preview: dict[str, Any]) -> str:
    """The SHA-256, in hex, of the preview as compact JSON with sorted keys and non-ASCII characters kept as written."""
    canonical = json.dumps(preview, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
