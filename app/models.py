from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


PresetKey = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$",
        min_length=1,
        max_length=64,
    ),
]
DisplayName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]


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
    preset_key: str | None
    display_name: str | None
    enabled: bool | None
    is_default: bool | None
    state_revision: int | None
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


class StorageEnvelope(ContractModel):
    provider: str
    provider_schema_version: int = Field(ge=1)
    config: dict[str, Any]
    credentials: dict[str, str] | None = None


class StorageTestRequest(StorageEnvelope):
    preset_key: PresetKey | None = None


class StorageUpdateRequest(StorageEnvelope):
    expected_revision: int = Field(ge=0)


class StoragePresetCreateRequest(StorageEnvelope):
    preset_key: PresetKey
    display_name: DisplayName


class StoragePresetPatchRequest(ContractModel):
    expected_state_revision: int = Field(ge=1)
    display_name: DisplayName | None = None
    enabled: bool | None = None


class StorageDefaultRequest(ContractModel):
    preset_key: PresetKey
    expected_default_preset: PresetKey
    expected_state_revision: int = Field(ge=1)


class StoragePresetSummaryResponse(ContractModel):
    preset_key: str
    display_name: str
    enabled: bool
    is_default: bool
    state_revision: int
    provider: str | None
    provider_schema_version: int | None
    config_revision: int | None
    endpoint_host: str | None
    bucket: str | None
    last_connection_test: ConnectionTest | None
    created_at: str
    updated_at: str


class StoragePresetListResponse(ContractModel):
    items: list[StoragePresetSummaryResponse]


class StoragePresetDetailResponse(StorageSettingsResponse):
    preset_key: str
    display_name: str
    enabled: bool
    is_default: bool
    state_revision: int
    created_at: str
    updated_at: str


class StoragePresetSaveResponse(StoragePresetDetailResponse):
    previous_revision: int


class UploadResponse(ContractModel):
    task_id: str
    storage_preset: str
    key: str
    url: str
    size_bytes: int
    content_type: str
    etag: str | None
    version_id: str | None
    delete_token: str | None


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
    storage_preset: str
    storage_provider: str | None
    storage_config_revision: int | None
    filename: str
    content_type: str
    object_key: str
    public_url: str | None
    status: str
    size_bytes: int | None
    etag: str | None
    version_id: str | None
    object_status: str
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
