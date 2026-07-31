from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(ContractModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(ContractModel):
    task_id: str | None = None
    error: ErrorDetail


class HealthResponse(ContractModel):
    status: Literal["ok"]


class ReadyResponse(ContractModel):
    status: Literal["ready", "not_ready"]
    checked_at: str
    checks: dict[str, Any]
    error: ErrorDetail | None = None


class ProviderSchemasResponse(ContractModel):
    items: list[dict[str, Any]]


class CredentialStatus(ContractModel):
    access_key_configured: bool
    access_key_masked: str | None
    secret_key_configured: bool


class ConnectionTest(ContractModel):
    status: str
    tested_at: str
    latency_ms: int | None


class StorageSettingsResponse(ContractModel):
    configured: bool
    provider: str | None
    provider_schema_version: int | None
    revision: int
    config: dict[str, Any] | None
    credentials: CredentialStatus
    last_connection_test: ConnectionTest | None
    activated_at: str | None


class StorageSaveResponse(StorageSettingsResponse):
    previous_revision: int


class StorageTestResponse(ContractModel):
    status: str
    provider: str
    provider_schema_version: int
    tested_at: str
    latency_ms: int
    checks: dict[str, Any]


class StorageCandidateRequest(ContractModel):
    provider: str
    provider_schema_version: int = Field(ge=1)
    config: dict[str, Any]
    credentials: dict[str, str] | None = None
    expected_revision: int | None = Field(default=None, ge=0)


class StorageUpdateRequest(StorageCandidateRequest):
    expected_revision: int = Field(ge=0)


class UploadResponseV1(ContractModel):
    task_id: str
    key: str
    url: str


class ReceiveValidationResponse(ContractModel):
    received: Literal[True]
    uploaded_to_storage: Literal[False]
    recorded_as_task: Literal[False]
    filename: str
    content_type: str
    size_bytes: int
    request_id: str


class TaskItemResponse(ContractModel):
    id: str
    request_id: str
    storage_provider: str | None
    storage_config_revision: int | None
    filename: str
    content_type: str
    object_key: str
    public_url: str | None
    status: str
    size_bytes: int | None
    error_code: str | None
    created_at: str
    finished_at: str | None
    duration_ms: int | None


class TaskDetailResponse(TaskItemResponse):
    idempotency_key: str | None


class TaskListResponse(ContractModel):
    items: list[TaskItemResponse]
    limit: int
    offset: int


class DashboardSummaryResponse(ContractModel):
    range: dict[str, str]
    generated_at: str
    service: dict[str, Any]
    uploads: dict[str, Any]


class DashboardTrafficResponse(ContractModel):
    range: dict[str, str]
    interval: str
    aggregation_timezone: str
    generated_at: str
    points: list[dict[str, Any]]


class LogItemResponse(ContractModel):
    id: int
    created_at: str
    level_no: int
    level_name: str
    event: str
    message: str
    request_id: str | None
    task_id: str | None
    error_code: str | None
    details: dict[str, Any] | None


class DashboardLogsResponse(ContractModel):
    items: list[LogItemResponse]
    limit: int
    before_id: int | None
    next_before_id: int | None


class DashboardStorageResponse(ContractModel):
    enabled: bool
    status: str
    provider: str
    provider_schema_version: int
    storage_config_revision: int
    range: dict[str, str]
    cache: dict[str, Any] | None
    statistics: dict[str, Any] | None = None
    storage_info: dict[str, Any] | None = None
