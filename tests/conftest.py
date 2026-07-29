from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.database import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        encryption_key=Fernet.generate_key().decode(),
        database_path=tmp_path / "service.db",
        temp_dir=tmp_path / "tmp",
        max_upload_bytes=100,
        max_request_body_bytes=2_048,
        max_concurrent_uploads=1,
        temp_min_free_bytes=0,
        s3_multipart_threshold_bytes=5_242_880,
        s3_multipart_chunk_bytes=5_242_880,
        s3_transfer_max_concurrency=1,
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database
