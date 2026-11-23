"""The profile tables in the deletion registry and in the export (TRU-01, TRU-02; ARCHITECTURE 8.3 and 8.4).

Registered on import. Both composition roots import this module, because the export job runs in the worker and the
registry is filled by registration at start-up, not read from the schema.

The five tables are erased by cascade from users (DATA_MODEL 7.2). The export holds every row of every status, the
change log, and each uploaded file as it was uploaded: the Fernet token in documents.content is opened with the
instance's data keys.
"""

import re
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, MultiFernet
from sqlalchemy import Connection, text

from app.platform import config
from app.trust.privacy import ModuleExport, register

from .service import FACT_STATUS

ROWS = {
    # A document's file is a column of its row and goes with it; its hash is shown as hex, its bytes only as the file.
    "documents": """SELECT to_jsonb(d) - 'content' - 'sha256' || jsonb_build_object('sha256', encode(d.sha256, 'hex'))
                    FROM documents d WHERE d.user_id = :u ORDER BY d.created_at, d.id""",
    "profile_events": "SELECT to_jsonb(e) FROM profile_events e WHERE e.user_id = :u ORDER BY e.created_at, e.id",
    "profile_facts": f"""SELECT to_jsonb(f) || jsonb_build_object('status', {FACT_STATUS}) FROM profile_facts f
                         WHERE f.user_id = :u ORDER BY f.created_at, f.id""",
    "constraints": "SELECT to_jsonb(c) FROM constraints c WHERE c.user_id = :u ORDER BY c.created_at, c.id",
    "preferences": "SELECT to_jsonb(p) FROM preferences p WHERE p.user_id = :u ORDER BY p.created_at, p.id",
}


def _open(token: bytes) -> bytes:
    """The instance's key ring: the first key encrypts, any key decrypts (ARCHITECTURE 7.3). Phase 1 has no
    platform.crypto, so this is the same ring the sign-in codes are sealed with (platform.sessions)."""
    return MultiFernet([Fernet(key) for key in config.get().data_keys]).decrypt(token)


def _archive_name(document_id: UUID, filename: str | None, mime_type: str) -> str:
    """A path in the archive made only of safe characters: the name is the person's, the extension follows the type."""
    extension = ".pdf" if mime_type == "application/pdf" else ".docx"
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", (filename or "resume").rsplit(".", 1)[0]).strip("._")[:80] or "resume"
    return f"files/documents/{document_id}-{stem}{extension}"


def export_profile(conn: Connection, user_id: UUID) -> ModuleExport:
    """Everything the profile module holds about a person: the rows as JSON, and each original file decrypted. Rows are
    selected by the person's id whatever role runs this, so the worker exports what the API would show them."""
    data: dict[str, Any] = {
        table: list(conn.execute(text(sql), {"u": user_id}).scalars()) for table, sql in ROWS.items()
    }
    files = {
        _archive_name(row.id, row.original_filename, row.mime_type): _open(bytes(row.content))
        for row in conn.execute(
            text(
                """SELECT id, original_filename, mime_type, content FROM documents
                   WHERE user_id = :u ORDER BY created_at, id"""
            ),
            {"u": user_id},
        )
    }
    return ModuleExport(data=data, files=files)


register("profile", dict.fromkeys(ROWS, "cascade"), exporter=export_profile)
