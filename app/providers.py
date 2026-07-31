from __future__ import annotations

import re
import socket
from abc import ABC, abstractmethod
from dataclasses import dataclass
from time import monotonic
from typing import Any, BinaryIO
from urllib.parse import quote, urlsplit, urlunsplit

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from .config import Settings


BUCKET_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$")


class ProviderError(Exception):
    def __init__(self, code: str, message: str, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.uncertain = uncertain


@dataclass(frozen=True)
class ProbeResult:
    tested_at: str
    latency_ms: int


@dataclass(frozen=True)
class ObjectMetadata:
    size_bytes: int
    etag: str | None
    version_id: str | None
    content_type: str | None
    last_modified: str | None


def require_upload_metadata(metadata: ObjectMetadata, expected_size: int) -> None:
    if metadata.size_bytes != expected_size:
        raise ProviderError(
            "OBJECT_SIZE_MISMATCH",
            "远端对象大小与接收文件不一致",
            uncertain=True,
        )
    if not metadata.etag:
        raise ProviderError(
            "UPLOAD_CONFIRMATION_FAILED",
            "Storage Provider 返回的对象元数据不完整",
            uncertain=True,
        )


def matches_object_metadata(
    metadata: ObjectMetadata,
    expected_size: int | None,
    expected_etag: str | None,
    expected_version_id: str | None,
) -> bool:
    return (
        expected_size is not None
        and expected_etag is not None
        and metadata.size_bytes == expected_size
        and metadata.etag == expected_etag
        and (
            expected_version_id is None
            or metadata.version_id == expected_version_id
        )
    )


class StorageProvider(ABC):
    provider_id: str
    schema_version: int

    @classmethod
    @abstractmethod
    def settings_schema(cls) -> dict[str, Any]: ...

    @classmethod
    @abstractmethod
    def validate(
        cls, config: dict[str, Any], credentials: dict[str, str]
    ) -> tuple[dict[str, Any], dict[str, str]]: ...

    @abstractmethod
    def test_connection(self) -> int: ...

    @abstractmethod
    def upload_file(
        self, fileobj: BinaryIO, object_key: str, content_type: str
    ) -> None: ...

    @abstractmethod
    def head_object(
        self, object_key: str, version_id: str | None = None
    ) -> ObjectMetadata | None: ...

    @abstractmethod
    def delete_object(
        self, object_key: str, version_id: str | None = None
    ) -> None: ...

    @abstractmethod
    def build_public_url(self, object_key: str) -> str: ...

    def get_metrics(self, _from_time: str, _to_time: str) -> dict[str, Any]:
        raise ProviderError(
            "STORAGE_METRICS_UNAVAILABLE", "Provider 不支持原生指标"
        )


def _normalize_url(value: Any, *, allow_path: bool) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("STORAGE_CONFIG_INVALID", "URL 不能为空")
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderError("STORAGE_CONFIG_INVALID", "URL 格式不合法")
    if not allow_path and parsed.path not in {"", "/"}:
        raise ProviderError("STORAGE_CONFIG_INVALID", "Endpoint 不允许包含路径")
    path = parsed.path.rstrip("/") if allow_path else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _integer(config: dict, name: str, minimum: int, maximum: int) -> int:
    value = config.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProviderError("STORAGE_CONFIG_INVALID", f"{name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ProviderError(
            "STORAGE_CONFIG_INVALID", f"{name} 必须在 {minimum} 到 {maximum} 之间"
        )
    return value


class CtyunZosProvider(StorageProvider):
    provider_id = "ctyun_zos"
    schema_version = 1

    def __init__(
        self,
        config: dict[str, Any],
        credentials: dict[str, str],
        settings: Settings,
        client: Any | None = None,
    ):
        self.config, self.credentials = self.validate(config, credentials)
        self.bucket = self.config["bucket"]
        self._transfer = TransferConfig(
            multipart_threshold=settings.s3_multipart_threshold_bytes,
            multipart_chunksize=settings.s3_multipart_chunk_bytes,
            max_concurrency=settings.s3_transfer_max_concurrency,
            use_threads=True,
        )
        self.client = client or self._create_client(settings)

    def _create_client(self, settings: Settings) -> Any:
        retries = self.config["max_attempts"]
        pool_size = (
            settings.max_concurrent_uploads * settings.s3_transfer_max_concurrency + 4
        )
        return boto3.client(
            "s3",
            aws_access_key_id=self.credentials["access_key"],
            aws_secret_access_key=self.credentials["secret_key"],
            endpoint_url=self.config["endpoint_url"],
            verify=self.config["verify_tls"],
            config=Config(
                connect_timeout=self.config["connect_timeout_seconds"],
                read_timeout=self.config["read_timeout_seconds"],
                retries={"max_attempts": retries, "mode": "standard"},
                max_pool_connections=pool_size,
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    @classmethod
    def settings_schema(cls) -> dict[str, Any]:
        return {
            "id": cls.provider_id,
            "display_name": "天翼云对象存储 ZOS",
            "description": "天翼云 ZOS，支持标准上传、对象校验、严格删除和可选 Bucket 指标。",
            "schema_version": cls.schema_version,
            "capabilities": {
                "s3_compatible": True,
                "public_read": True,
                "bucket_metrics": True,
            },
            "config_fields": [
                {
                    "name": "endpoint_url",
                    "type": "url",
                    "required": True,
                    "secret": False,
                    "label": "ZOS Endpoint（SDK 上传接口地址）",
                },
                {
                    "name": "bucket",
                    "type": "string",
                    "required": True,
                    "secret": False,
                    "label": "Bucket 名称",
                },
                {
                    "name": "public_base_url",
                    "type": "url",
                    "required": True,
                    "secret": False,
                    "label": "对象访问根地址",
                    "hint": "Bucket 外网访问域名、CDN 或自定义访问根地址",
                    "suggested_value_template": "https://{bucket}.{endpoint_host}",
                },
                {
                    "name": "connect_timeout_seconds",
                    "type": "integer",
                    "required": True,
                    "default": 5,
                },
                {
                    "name": "read_timeout_seconds",
                    "type": "integer",
                    "required": True,
                    "default": 300,
                },
                {
                    "name": "max_attempts",
                    "type": "integer",
                    "required": True,
                    "default": 2,
                },
                {
                    "name": "verify_tls",
                    "type": "boolean",
                    "required": True,
                    "default": True,
                },
                {
                    "name": "enable_bucket_metrics",
                    "type": "boolean",
                    "required": True,
                    "default": False,
                },
            ],
            "credential_fields": [
                {
                    "name": "access_key",
                    "type": "secret",
                    "required_on_create": True,
                    "label": "Access Key（AK）",
                },
                {
                    "name": "secret_key",
                    "type": "secret",
                    "required_on_create": True,
                    "label": "Secret Key（SK）",
                },
            ],
        }

    @classmethod
    def validate(
        cls, config: dict[str, Any], credentials: dict[str, str]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        if not isinstance(config, dict) or not isinstance(credentials, dict):
            raise ProviderError("STORAGE_CONFIG_INVALID", "存储配置格式不合法")
        expected = {
            "endpoint_url",
            "bucket",
            "public_base_url",
            "connect_timeout_seconds",
            "read_timeout_seconds",
            "max_attempts",
            "verify_tls",
            "enable_bucket_metrics",
        }
        if set(config) != expected:
            raise ProviderError("STORAGE_CONFIG_INVALID", "存储配置字段不完整")
        bucket = config.get("bucket")
        if not isinstance(bucket, str) or not BUCKET_PATTERN.fullmatch(bucket):
            raise ProviderError("STORAGE_CONFIG_INVALID", "Bucket 名称不合法")
        if not all(
            isinstance(credentials.get(name), str) and credentials[name]
            for name in ("access_key", "secret_key")
        ):
            raise ProviderError("STORAGE_CREDENTIALS_REQUIRED", "必须提供完整 AK/SK")
        if any(value != value.strip() for value in credentials.values()):
            raise ProviderError("STORAGE_CONFIG_INVALID", "凭证不能包含首尾空白")
        verify_tls = config.get("verify_tls")
        metrics = config.get("enable_bucket_metrics")
        if not isinstance(verify_tls, bool) or not isinstance(metrics, bool):
            raise ProviderError("STORAGE_CONFIG_INVALID", "布尔配置格式不合法")
        normalized = {
            "endpoint_url": _normalize_url(
                config["endpoint_url"], allow_path=False
            ),
            "bucket": bucket,
            "public_base_url": _normalize_url(
                config["public_base_url"], allow_path=True
            ),
            "connect_timeout_seconds": _integer(
                config, "connect_timeout_seconds", 1, 60
            ),
            "read_timeout_seconds": _integer(
                config, "read_timeout_seconds", 1, 3_600
            ),
            "max_attempts": _integer(config, "max_attempts", 0, 5),
            "verify_tls": verify_tls,
            "enable_bucket_metrics": metrics,
        }
        return normalized, {
            "access_key": credentials["access_key"],
            "secret_key": credentials["secret_key"],
        }

    def test_connection(self) -> int:
        started = monotonic()
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except (ConnectTimeoutError, EndpointConnectionError, socket.gaierror) as exc:
            raise ProviderError(
                "STORAGE_ENDPOINT_UNREACHABLE", "无法连接 Storage Endpoint"
            ) from exc
        except ReadTimeoutError as exc:
            raise ProviderError("STORAGE_ENDPOINT_UNREACHABLE", "Storage Endpoint 超时") from exc
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code", "")
            if status in {401, 403} or code in {
                "InvalidAccessKeyId",
                "SignatureDoesNotMatch",
                "AccessDenied",
            }:
                error = "STORAGE_CREDENTIALS_REJECTED"
                message = "对象存储服务拒绝了当前访问凭证"
            else:
                error = "STORAGE_BUCKET_UNAVAILABLE"
                message = "Bucket 不存在或当前凭证不可访问"
            raise ProviderError(error, message) from exc
        return round((monotonic() - started) * 1_000)

    def upload_file(
        self, fileobj: BinaryIO, object_key: str, content_type: str
    ) -> None:
        try:
            self.client.upload_fileobj(
                fileobj,
                self.bucket,
                object_key,
                ExtraArgs={"ContentType": content_type, "ACL": "public-read"},
                Config=self._transfer,
            )
        except (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError) as exc:
            raise ProviderError(
                "STORAGE_TIMEOUT", "Storage Provider 上传超时", uncertain=True
            ) from exc
        except Exception as exc:
            raise ProviderError("UPLOAD_FAILED", "Storage Provider 上传失败") from exc

    def head_object(
        self, object_key: str, version_id: str | None = None
    ) -> ObjectMetadata | None:
        request = {"Bucket": self.bucket, "Key": object_key}
        if version_id is not None:
            request["VersionId"] = version_id
        try:
            response = self.client.head_object(**request)
            size = response.get("ContentLength")
            if isinstance(size, bool) or not isinstance(size, int) or size < 0:
                raise ProviderError(
                    "UPLOAD_CONFIRMATION_FAILED",
                    "Storage Provider 返回的对象大小无效",
                    uncertain=True,
                )
            last_modified = response.get("LastModified")
            return ObjectMetadata(
                size_bytes=size,
                etag=response.get("ETag"),
                version_id=response.get("VersionId"),
                content_type=response.get("ContentType"),
                last_modified=last_modified.isoformat()
                if hasattr(last_modified, "isoformat")
                else None,
            )
        except (ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError) as exc:
            raise ProviderError(
                "STORAGE_TIMEOUT", "Storage Provider 对象确认超时", uncertain=True
            ) from exc
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise ProviderError(
                "UPLOAD_CONFIRMATION_FAILED", "暂时无法确认远端对象", uncertain=True
            ) from exc
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(
                "UPLOAD_CONFIRMATION_FAILED", "暂时无法确认远端对象", uncertain=True
            ) from exc

    def delete_object(
        self, object_key: str, version_id: str | None = None
    ) -> None:
        request = {"Bucket": self.bucket, "Key": object_key}
        if version_id is not None:
            request["VersionId"] = version_id
        try:
            self.client.delete_object(**request)
        except (
            ConnectionClosedError,
            ConnectTimeoutError,
            ReadTimeoutError,
            EndpointConnectionError,
            socket.gaierror,
        ) as exc:
            raise ProviderError(
                "DELETE_PENDING", "删除结果暂时无法确认", uncertain=True
            ) from exc
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if isinstance(status, int) and status >= 500:
                raise ProviderError(
                    "DELETE_PENDING", "删除结果暂时无法确认", uncertain=True
                ) from exc
            raise ProviderError("DELETE_FAILED", "Storage Provider 拒绝删除") from exc
        except Exception as exc:
            raise ProviderError(
                "DELETE_PENDING", "删除结果暂时无法确认", uncertain=True
            ) from exc

    def build_public_url(self, object_key: str) -> str:
        return f"{self.config['public_base_url']}/{quote(object_key, safe='/')}"

    def get_metrics(self, from_time: str, to_time: str) -> dict[str, Any]:
        if not self.config["enable_bucket_metrics"]:
            return {"enabled": False, "status": "disabled"}
        if not all(
            hasattr(self.client, method)
            for method in ("get_bucket_statistics", "get_bucket_storage_info")
        ):
            raise ProviderError(
                "STORAGE_METRICS_UNAVAILABLE", "当前 SDK 不支持 ZOS Bucket 指标"
            )
        try:
            statistics = self.client.get_bucket_statistics(
                Bucket=self.bucket, StartDate=from_time, EndDate=to_time
            )
            storage_info = self.client.get_bucket_storage_info(Bucket=self.bucket)
        except Exception as exc:
            raise ProviderError(
                "STORAGE_METRICS_UNAVAILABLE", "ZOS Bucket 指标暂时不可用"
            ) from exc
        return {
            "enabled": True,
            "status": "ok",
            "statistics": statistics,
            "storage_info": storage_info,
        }


class S3CompatibleProvider(CtyunZosProvider):
    provider_id = "s3_compatible"

    @classmethod
    def settings_schema(cls) -> dict[str, Any]:
        schema = super().settings_schema()
        schema.update(
            id=cls.provider_id,
            display_name="S3 兼容对象存储",
            description="用于其他支持 S3 API、HeadObject、DeleteObject 和 public-read ACL 的对象存储服务。",
            capabilities={
                "s3_compatible": True,
                "public_read": True,
                "bucket_metrics": False,
            },
        )
        schema["config_fields"][0]["label"] = "S3 API Endpoint"
        return schema

    @classmethod
    def validate(
        cls, config: dict[str, Any], credentials: dict[str, str]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        normalized, validated_credentials = super().validate(config, credentials)
        if normalized["enable_bucket_metrics"]:
            raise ProviderError(
                "STORAGE_CONFIG_INVALID",
                "通用 S3 Provider 不支持天翼云 Bucket 扩展指标",
            )
        return normalized, validated_credentials


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, type[StorageProvider]] = {}

    def register(self, provider: type[StorageProvider]) -> None:
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str, schema_version: int) -> type[StorageProvider]:
        provider = self._providers.get(provider_id)
        if provider is None or provider.schema_version != schema_version:
            raise ProviderError(
                "STORAGE_CONFIG_INVALID", "未知 Provider 或 schema version"
            )
        return provider

    def schemas(self) -> list[dict[str, Any]]:
        return [provider.settings_schema() for provider in self._providers.values()]


def default_registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(CtyunZosProvider)
    registry.register(S3CompatibleProvider)
    return registry
