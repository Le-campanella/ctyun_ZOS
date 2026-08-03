from __future__ import annotations

import json
import sys
from pathlib import Path

from app.main import create_app

destination = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/current/openapi.json")
destination.write_text(
    json.dumps(create_app(background=False).openapi(), ensure_ascii=False, indent=2)
    + "\n",
    encoding="utf-8",
)
