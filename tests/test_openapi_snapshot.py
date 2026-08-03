import json
from pathlib import Path

from app.main import create_app


def test_openapi_snapshot_matches_current_contract():
    expected = json.loads(Path("docs/current/openapi.json").read_text(encoding="utf-8"))
    assert create_app(background=False).openapi() == expected
