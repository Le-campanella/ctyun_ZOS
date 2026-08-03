from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

from .database import Database, utc_now


NOTIFY = 25
logging.addLevelName(NOTIFY, "NOTIFY")
SENSITIVE = re.compile(
    r"(access.?key|secret.?key|admin.?key|client.?key|authorization|cookie|credential|cipher|password|token)",
    re.IGNORECASE,
)


def _clean(value: Any, key: str = "") -> Any:
    if SENSITIVE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item)[:100]: _clean(content, str(item)) for item, content in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value[:100]]
    if isinstance(value, str):
        return "".join(char for char in value if char >= " " or char in "\t")[:1_000]
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)[:1_000]


class EventLogger:
    def __init__(self, database: Database):
        self.database = database
        self._logger = logging.getLogger("zos_upload")
        if not self._logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.INFO)
            self._logger.propagate = False
        self.degraded = False
        self.last_failure_at: str | None = None
        self.last_success_at: str | None = None

    def emit(
        self,
        level: int,
        event: str,
        message: str,
        *,
        request_id: str | None = None,
        task_id: str | None = None,
        error_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        record = {
            "created_at": utc_now(),
            "level_no": level,
            "level_name": logging.getLevelName(level),
            "event": _clean(event),
            "message": _clean(message),
            "request_id": _clean(request_id) if request_id else None,
            "task_id": _clean(task_id) if task_id else None,
            "error_code": _clean(error_code) if error_code else None,
            "details": _clean(details) if details else None,
        }
        self._logger.log(level, json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        if level >= NOTIFY:
            try:
                self.database.write_log(record)
            except Exception:
                self.degraded = True
                self.last_failure_at = utc_now()
            else:
                self.degraded = False
                self.last_success_at = utc_now()
