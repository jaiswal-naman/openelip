"""Write the backend's OpenAPI schema to a file, the same way every time, so the committed copy and a CI re-run agree.

    uv run --project backend --frozen --offline python web/scripts/export-openapi.py web/openapi.json
    cd web && pnpm gen:api
"""

import json
import sys
from pathlib import Path

from app.main import app

target = Path(sys.argv[1])
target.write_text(json.dumps(app.openapi(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
