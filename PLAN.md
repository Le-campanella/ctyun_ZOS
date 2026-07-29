# 局域网轻量文件上传服务实施计划（ZOS v1）

> 状态：v4 方案已确认，实施中。
>
> 完整调用方与 Dashboard 接口契约见 [API.md](API.md)。本文与 `API.md` 已完成同步。

## 1. 目标

建设一个仅在受控局域网运行的轻量 HTTP 服务，完成以下能力：

1. 接收局域网内其他服务上传的单个文件。
2. 通过可替换的存储 Provider 将文件同步上传到对象存储；第一版提供天翼云 ZOS 预设。
3. 上传成功后返回任务 ID、对象 Key 和公网 URL；失败时返回稳定错误码。
4. 使用 SQLite 保存上传任务台账，支持任务列表和单任务详情查询。
5. 提供 Web Dashboard，显示上传流量、成功率、任务状态、服务状态和近期任务。
6. Dashboard 提供存储设置页，可测试、保存并激活 ZOS Endpoint（SDK 上传接口地址）、Bucket、对象访问根地址、AK、SK 和连接参数。
7. 保存并展示 `NOTIFY` 及以上级别的结构化运行日志。
8. 在服务异常重启后，通过存储 Provider 的对象元数据接口恢复可确认的上传结果。

## 2. 信任边界与范围

### 2.1 网络边界

- 服务仅发布到受控局域网地址或容器内部网络。
- API 与 Dashboard 共用同一个局域网端口。
- 服务不设置调用方认证、登录、用户、角色或权限系统。
- 局域网内能够访问该端口的客户端可以读取监控信息，也可以修改存储设置。
- 局域网、防火墙、交换机 VLAN 和部署平台的端口暴露规则构成访问边界。
- 部署配置禁止公网入口、端口转发和公有负载均衡器。
- CORS 默认关闭，Dashboard 通过同源接口读写设置。
- 设置写接口只接受 JSON 和自定义 Header，用于降低浏览器跨站误提交风险；该机制不承担身份认证。
- 设置请求会传输 AK/SK。正式部署应通过内网 HTTPS 暴露 Dashboard 与设置 API，或将其限制在隔离的管理 VLAN / 管理主机；使用 HTTP 时，能够监听局域网流量的设备也属于信任边界。服务仍保持无身份认证。

### 2.2 服务管理范围

服务管理上传过程、任务记录、运行日志和统计视图。对象存储中的对象继续由对应 Bucket 配置管理。

服务包含：

- 上传文件。
- 查询上传任务。
- 查询任务详情。
- 查询上传流量统计。
- 查询 `NOTIFY` 及以上日志。
- 查看 Dashboard 监控页面。
- 查看、测试和修改当前存储 Provider 设置。
- 健康检查、就绪检查和中断任务恢复。

服务范围之外：

- 持久化保存文件本体。
- 代理下载、更新、删除、重命名或列出 ZOS 对象。
- 由调用方指定 Bucket、对象 ACL 或任意对象 Key。
- 文件去重、内容搜索、文件分类、版本管理和素材管理。
- Dashboard 内执行文件上传、任务重试、对象删除或 Bucket 权限管理。
- 通过 Dashboard 创建、删除或修改 ZOS Bucket、生命周期、ACL、策略、版本控制或 CORS。
- 消息队列、独立异步 Worker、ORM 和完整数据库迁移框架。

文件过期、历史版本清理和未完成分段上传清理由 ZOS Bucket 生命周期规则负责。

## 3. 技术方案

- Python 3.11
- FastAPI
- Uvicorn
- `boto3` / `botocore` 标准 S3 Client
- 可选的 ZOS 官方 Python SDK 扩展统计接口
- Python 标准库 `sqlite3`
- `cryptography`，用于持久化凭证的认证加密
- Jinja2 模板
- 原生 JavaScript
- 本地打包的 Chart.js 静态资源
- 单容器、单进程、单实例
- SQLite 使用持久化卷；临时目录使用独立、可写、容量受控的临时卷或宿主机目录

Python 3.11 同时满足常规运行环境和当前 ZOS 官方 Python SDK 的版本兼容范围。阻塞式 S3 调用在受控线程池中执行，外层上传信号量限制总并发，FastAPI 事件循环继续响应 Dashboard 和查询请求。

## 4. 总体结构

```text
局域网调用方
    │
    │ POST /v1/uploads
    ▼
┌───────────────────────────────────────────────────────────┐
│ ZOS 轻量上传服务                                          │
│                                                           │
│  上传 API ─────────┐                                      │
│  查询 API ─────────┼──> SQLite                            │
│  Dashboard API ────┤    - storage_configs                 │
│  设置 API ─────────┤    - upload_tasks                    │
│  日志模块 ─────────┘    - service_logs                    │
│                                                           │
│  Dashboard HTML / JS / Chart.js                           │
│                                                           │
│  Storage Provider Registry                                │
│      └── ctyun_zos adapter ───────> 天翼云 ZOS Bucket     │
└───────────────────────────────────────────────────────────┘
                                              │
                                              │ 公网 HTTPS URL
                                              ▼
                                        第三方公网 API
```

上传 API 与调用方契约保持 Provider 无关。后续增加其他对象存储时，新增 Provider adapter 和设置预设，局域网调用方继续使用相同的 `/v1/uploads`、任务查询和错误结构。

文件只在当前请求期间存在于受控临时文件中。请求完成、失败或客户端断开后立即关闭并删除临时文件。

## 5. 文件与对象约定

- 单文件最大 `200 MiB`，即 `209715200` 字节。
- 接受所有文件类型。
- 每次请求只允许一个 `file` 字段。
- 空文件返回 `FILE_EMPTY`。
- 原始文件名只用于记录和展示，不直接进入对象 Key。
- 原始文件名最大保存 255 个 Unicode 字符，超出部分截断。
- Content-Type 缺失或无效时使用 `application/octet-stream`。
- 对象目录日期按 `Asia/Shanghai` 计算。
- 安全扩展名取原始文件名最后一个后缀，转为小写，仅保留 `a-z`、`0-9`，长度限制为 1 至 10 个字符。
- 无安全扩展名时对象名只使用任务 UUID。

对象 Key：

```text
YYYY/MM/DD/{task_id}.{safe_extension}
```

示例：

```text
2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf
```

公网 URL 由任务绑定的 storage config revision 中的 `public_base_url` 与 `object_key` 生成，并将完整结果写入任务记录。调用方继续把 URL 当作不透明字符串使用。

## 6. 请求接收、临时文件与并发控制

### 6.1 临时文件策略

上传采用两阶段处理：

1. 将请求文件按固定块大小写入 `SpooledTemporaryFile`，同时累计字节数并校验上限。
2. 校验通过后将临时文件指针复位，再交给 S3 Transfer Manager 上传。

默认参数：

```text
UPLOAD_READ_CHUNK_BYTES=1048576
UPLOAD_SPOOL_THRESHOLD_BYTES=8388608
MAX_UPLOAD_BYTES=209715200
```

前 8 MiB 可以保存在内存，超过阈值后自动写入 `TEMP_DIR`。上传结束后通过 `finally` 统一清理。

### 6.2 请求体限制

- 在 multipart 解析前执行路径级请求体计数。
- `MAX_REQUEST_BODY_BYTES` 默认设置为 `MAX_UPLOAD_BYTES + 4 MiB`，为 multipart 边界和请求头预留空间。
- 同时使用流式字节计数校验文件本体大小，覆盖缺失或错误的 `Content-Length`。
- 读取到 `MAX_UPLOAD_BYTES + 1` 时立即停止接收，返回 `413 FILE_TOO_LARGE`。
- 客户端断开时任务进入 `failed`，错误码为 `CLIENT_DISCONNECTED`。

### 6.3 并发限制

- `MAX_CONCURRENT_UPLOADS` 默认值为 `4`。
- 信号量在 multipart 解析前获取，限制接收、临时写入和 ZOS 上传的完整链路。
- 容量已满时返回 `503 UPLOAD_CAPACITY_EXCEEDED`，并带 `Retry-After` 响应头。
- Dashboard、任务查询和健康检查不占用上传信号量。
- `TEMP_DIR` 可用空间至少满足：

```text
MAX_CONCURRENT_UPLOADS × MAX_UPLOAD_BYTES × 1.2
```

就绪检查会验证临时目录可写和剩余空间。

## 7. SQLite 数据模型

数据库包含三张业务表。使用 `PRAGMA user_version` 管理轻量级手写 schema 升级。

### 7.1 存储配置表

每次成功激活设置都会创建一条不可变配置 revision。表结构使用 Provider 通用 envelope，ZOS 专属字段保存在 `config_json`，凭证作为一个整体加密保存。后续增加其他对象存储 Provider 时，只需增加 adapter、preset 和 schema 校验，无需为每个 Provider 增加数据库列。

```sql
CREATE TABLE storage_configs (
    id                       TEXT PRIMARY KEY,
    revision                 INTEGER NOT NULL UNIQUE CHECK (revision >= 1),
    provider                 TEXT NOT NULL,
    provider_schema_version  INTEGER NOT NULL CHECK (provider_schema_version >= 1),
    config_json              TEXT NOT NULL,
    credentials_ciphertext   BLOB NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    created_at               TEXT NOT NULL,
    activated_at             TEXT NOT NULL,
    last_tested_at           TEXT NOT NULL,
    last_test_latency_ms     INTEGER
);

CREATE UNIQUE INDEX uq_storage_configs_active
ON storage_configs(status)
WHERE status = 'active';

CREATE INDEX idx_storage_configs_revision
ON storage_configs(revision DESC);

CREATE INDEX idx_storage_configs_provider_revision
ON storage_configs(provider, revision DESC);
```

字段约定：

| 字段 | 说明 |
|---|---|
| `id` | 配置 UUID，供任务引用 |
| `revision` | 从 1 开始递增的可见版本号 |
| `provider` | Provider ID；第一版为 `ctyun_zos` |
| `provider_schema_version` | 该 Provider 设置结构的版本；`ctyun_zos` 第一版为 `1` |
| `config_json` | Provider 专属的非敏感配置；ZOS 包含 Endpoint、Bucket、`public_base_url`、超时、重试、TLS 和指标开关 |
| `credentials_ciphertext` | Provider credential envelope 的认证密文；ZOS 包含 AK 和 SK |
| `status` | `active` 或 `inactive`，任意时刻最多一个 active revision |
| `last_tested_at` | 激活前最后一次连接测试时间 |
| `last_test_latency_ms` | Provider 连接测试耗时 |

Provider registry 负责按照 `provider + provider_schema_version` 校验 `config_json` 和解密后的 credential envelope。未知 Provider、未知 schema version、缺失字段或非法字段均拒绝加载。历史 revision 保持不可变，任务恢复始终使用任务创建时绑定的配置。

`config_json` 使用 UTF-8 canonical JSON，禁止包含 Provider preset 标记为 secret 的字段。`credentials_ciphertext` 解密后的对象只在创建 Provider Client 时短暂存在于进程内存中。AK、SK 使用 `SETTINGS_ENCRYPTION_KEY` 和 `cryptography.fernet.Fernet` 进行认证加密，数据库、页面、API 和日志均不保存或返回明文凭证。

Provider ID 不使用数据库枚举约束，新增 adapter 和 preset 即可引入新 Provider。仍有任务引用历史 revision 时，对应 Provider adapter 和 schema 解析器必须继续保留。

### 7.2 上传任务表

```sql
CREATE TABLE upload_tasks (
    id                 TEXT PRIMARY KEY,
    request_id         TEXT NOT NULL,
    idempotency_key    TEXT,
    storage_config_id  TEXT NOT NULL REFERENCES storage_configs(id),
    filename           TEXT NOT NULL,
    content_type       TEXT NOT NULL,
    object_key         TEXT NOT NULL,
    public_url         TEXT,
    status             TEXT NOT NULL CHECK (
        status IN ('uploading', 'unknown', 'succeeded', 'failed')
    ),
    size_bytes         INTEGER,
    error_code         TEXT,
    created_at         TEXT NOT NULL,
    finished_at        TEXT,
    duration_ms        INTEGER
);

CREATE UNIQUE INDEX uq_upload_tasks_idempotency_key
ON upload_tasks(idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX idx_upload_tasks_created_at_id
ON upload_tasks(created_at DESC, id DESC);

CREATE INDEX idx_upload_tasks_status_created_at
ON upload_tasks(status, created_at DESC);

CREATE INDEX idx_upload_tasks_request_id
ON upload_tasks(request_id);

CREATE INDEX idx_upload_tasks_storage_config_id
ON upload_tasks(storage_config_id);
```

字段约定：

| 字段 | 说明 |
|---|---|
| `id` | 任务 UUID，同时用于生成对象 Key |
| `request_id` | 请求追踪 ID |
| `idempotency_key` | 调用方可选幂等键 |
| `storage_config_id` | 创建任务时使用的存储配置 revision |
| `filename` | 经过长度限制的原始文件名 |
| `content_type` | 上传到对象存储的 Content-Type |
| `object_key` | 稳定的对象 Key，上传开始前写入 |
| `public_url` | 成功后返回的完整 URL |
| `status` | `uploading`、`unknown`、`succeeded`、`failed` |
| `size_bytes` | 已确认的文件大小；无法确认时为空 |
| `error_code` | 当前错误或恢复状态码 |
| `created_at` | UTC ISO 8601 创建时间 |
| `finished_at` | UTC ISO 8601 完成时间 |
| `duration_ms` | 完整请求处理耗时 |

### 7.3 运行日志表

```sql
CREATE TABLE service_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    level_no      INTEGER NOT NULL,
    level_name    TEXT NOT NULL,
    event         TEXT NOT NULL,
    message       TEXT NOT NULL,
    request_id    TEXT,
    task_id       TEXT,
    error_code    TEXT,
    details_json  TEXT
);

CREATE INDEX idx_service_logs_created_at_id
ON service_logs(created_at DESC, id DESC);

CREATE INDEX idx_service_logs_level_created_at
ON service_logs(level_no, created_at DESC);

CREATE INDEX idx_service_logs_request_id
ON service_logs(request_id);

CREATE INDEX idx_service_logs_task_id
ON service_logs(task_id);
```

`details_json` 只保存已经清洗的结构化上下文，不保存文件内容、AK、SK、密文、Authorization、Cookie 或完整环境变量。

### 7.4 SQLite 运行参数

每个请求或工作线程使用独立连接，并设置：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

数据库写事务保持短小。激活新配置时，在一个事务内将旧 revision 设为 `inactive` 并插入新 `active` revision。统计查询和 Dashboard 查询使用只读连接。日志写入失败不会改变上传结果，结构化日志仍会输出到 stdout/stderr，并将服务状态标记为 degraded。

## 8. 上传状态流与一致性

### 8.1 正常流程

1. 接收请求头，确定最终 `request_id`。
2. 处理可选 `Idempotency-Key`；成功任务的幂等重放无需依赖当前 active 配置。
3. 读取当前 active `storage_config`，创建不可变配置快照；缺失时返回 `503 STORAGE_NOT_CONFIGURED`。
4. 获取上传并发槽位。
5. 解析 `file`，生成任务 UUID、对象 Key 和公网 URL。
6. 在 SQLite 中原子写入 `uploading` 任务，同时记录 `storage_config_id`。
7. 写入临时文件，同时校验空文件、文件大小和客户端连接状态。
8. 通过该配置对应的 Provider adapter 上传文件。
9. Provider 返回成功后，将任务更新为 `succeeded`，写入大小、完成时间和耗时。
10. 数据库更新成功后向调用方返回 `201`。
11. 任一步骤失败时，将可定位任务更新为 `failed`，写入稳定错误码并清理临时文件。

对象 Key、配置 revision 和公网 URL 在上传前持久化。设置切换不会改变已经创建任务的目标位置。

### 8.2 配置激活与并发上传

保存设置时执行以下流程：

1. 校验 Provider envelope、`provider_schema_version`、URL、Bucket 名称、超时和重试参数。
2. 合并当前已保存凭证与本次提交的凭证；首次配置必须提交 AK 和 SK。Provider、`provider_schema_version` 或 `endpoint_url` 发生变化时必须重新提交完整 AK/SK，禁止把旧凭证自动发送到新的设置结构或 Endpoint。
3. 创建候选 `ctyun_zos` Client，并调用 `HeadBucket` 测试 Endpoint、凭证和 Bucket 可访问性。
4. 测试成功后，将 Provider credential envelope（ZOS 为 AK/SK）整体加密。
5. 在单个 SQLite 事务内创建新 revision，并将旧 revision 设为 `inactive`。
6. 原子替换进程内 active Provider 快照和 Client 缓存。
7. 写入 `storage_config_activated` NOTIFY 日志；日志只包含 Provider、revision、Endpoint 主机名、Bucket 和来源 IP。

正在执行的上传持有旧配置快照并继续完成；新任务使用新 revision。旧 revision 在仍被任务引用时保留。连接测试只确认 Client 可创建且 `HeadBucket` 成功，不承诺 `PutObject` 权限或公网 URL 可读取性，真实上传能力由集成测试和实际上传继续验证。

### 8.3 数据库更新失败

对象上传成功后，SQLite 成功更新任务之前发生数据库错误时：

- 服务返回 `500 DATABASE_ERROR`。
- 任务记录保留 `uploading` 或进入 `unknown`。
- 服务写入 `CRITICAL` 日志。
- 启动恢复或周期恢复通过任务引用的 Provider revision 查询对象元数据。

### 8.4 启动恢复

服务启动时扫描 `uploading` 和 `unknown` 任务：

1. 读取任务的 `storage_config_id` 并解密对应凭证。
2. 通过对应 Provider 调用对象元数据接口；`ctyun_zos` 使用 `HeadObject(Bucket, object_key)`。
3. 对象存在：更新为 `succeeded`，写入返回的对象大小、完成时间和既有 URL。
4. Provider 明确返回对象不存在：更新为 `failed`，错误码为 `SERVICE_RESTARTED_OBJECT_NOT_FOUND`。
5. 超时、网络异常、认证异常或服务端错误：更新为 `unknown`，错误码为 `RECOVERY_PENDING`。
6. 进程内周期任务继续扫描全部 `unknown` 任务，以及超过 `STALE_UPLOAD_SECONDS` 的 `uploading` 任务，默认每 60 秒重试。

旧 revision 的 AK/SK 已失效且当前 active revision 指向同一 Provider、Endpoint 和 Bucket 时，恢复器可以使用 active revision 再尝试一次。`unknown` 表示当前无法确认远端结果，可以继续转为 `succeeded` 或 `failed`。

### 8.5 正常上传确认

正常请求以 Provider 上传方法的成功返回作为上传确认，不为每次成功上传额外增加对象元数据请求。对象元数据接口专用于异常恢复、连接诊断和运维检查。

## 9. 幂等语义

调用方可以传入：

```http
Idempotency-Key: opaque-key-up-to-128-chars
```

规则：

- Header 可选，最大 128 个字符。
- 第一次出现的幂等键创建新任务。
- 已有任务为 `succeeded` 时，返回同一个任务、Key 和 URL，状态码为 `200`，响应头包含 `Idempotency-Replayed: true`。
- 已有任务为 `uploading` 或 `unknown` 时，返回 `409 UPLOAD_IN_PROGRESS` 并包含 `task_id`。
- 已有任务为 `failed` 时，返回 `409 IDEMPOTENCY_KEY_REUSED`；新的上传尝试使用新的幂等键。
- 幂等键绑定第一次请求意图，服务不读取完整文件来比较重复请求的内容。
- 未传幂等键时，每次请求继续生成新的任务 UUID 和对象 Key。

幂等键的唯一性通过 SQLite 唯一索引和短事务保证。

## 10. API

### 10.1 上传文件

```http
POST /v1/uploads
Content-Type: multipart/form-data
X-Request-ID: optional
Idempotency-Key: optional

file=<binary>
```

成功响应保持现有结构：

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "key": "2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf",
  "url": "https://public-bucket.example.com/2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf"
}
```

### 10.2 查询任务列表

```http
GET /v1/upload-tasks?limit=50&offset=0&status=succeeded&from=...&to=...
```

- `limit` 默认 50，最大 200。
- `offset` 默认 0。
- 可选按状态和 UTC 时间范围筛选。
- 排序固定为 `created_at DESC, id DESC`。
- 列表增加 `request_id`、`content_type`、`object_key`、`duration_ms`、`storage_provider` 和 `storage_config_revision` 字段。
- Dashboard 的近期上传表直接复用该接口。

### 10.3 查询单个任务

```http
GET /v1/upload-tasks/{task_id}
```

返回完整任务字段。任务不存在时返回 `404 TASK_NOT_FOUND`。

### 10.4 健康检查

```http
GET /healthz
```

只表示进程可以响应，返回 `200`。

### 10.5 就绪检查

```http
GET /readyz
```

检查：

- 必要配置已加载。
- SQLite 可读写。
- 临时目录可写且剩余空间满足阈值。
- 启动 schema 初始化完成。
- 启动恢复扫描完成。
- 最近一次 active Storage Provider 探测成功且未超过缓存有效期；`ctyun_zos` 使用 `HeadBucket`。

任一关键项失败时返回 `503` 和各依赖项状态。

### 10.6 Dashboard 数据接口

```http
GET /v1/dashboard/summary?from=...&to=...
GET /v1/dashboard/traffic?from=...&to=...&interval=hour|day
GET /v1/dashboard/logs?min_level=NOTIFY&limit=100&before_id=...
GET /v1/dashboard/storage?from=...&to=...
```

`/v1/dashboard/storage` 是否启用由 active storage config 的 Provider 能力和 `enable_bucket_metrics` 决定，关闭时返回明确的 disabled 状态。

### 10.7 存储设置接口

```http
GET  /v1/settings/storage/providers
GET  /v1/settings/storage
POST /v1/settings/storage/test
PUT  /v1/settings/storage
```

- Provider 列表接口用于描述当前支持的设置预设与 `provider_schema_version`；第一版只返回 `ctyun_zos` schema version `1`。
- 当前设置接口只返回非敏感字段、masked AK 和凭证是否已配置。
- 测试接口不持久化数据，使用候选设置执行 `HeadBucket`。
- 保存接口使用 `expected_revision` 防止并发覆盖，测试通过后创建新 revision 并原子激活。
- `POST` 和 `PUT` 必须使用 `application/json` 并携带 `X-Settings-Request: true`。
- 设置接口无身份认证，访问边界仍由局域网和端口暴露规则提供。

所有响应时间字段使用 UTC ISO 8601。Dashboard 按 `APP_TIMEZONE` 显示。

### 10.8 局域网文件接收测试

```http
POST /v1/uploads/validate
```

该接口复用正式上传的 multipart、单文件、请求体、200 MiB 上限、并发容量和临时文件读写规则，但不调用 Storage Provider、不创建上传任务、不返回公网 URL。它用于在尚未配置 ZOS 时验证局域网调用方能否正确把文件发送到本服务。

## 11. Web Dashboard

### 11.1 页面入口

```http
GET /dashboard
GET /dashboard/settings
```

Dashboard 与 API 同源、无登录，并使用本地静态资源。监控区域为只读，设置页面可以测试和激活存储配置。

### 11.2 监控页面内容

监控页面包含六个区域：

1. **局域网文件接收测试**
   - 选择单个文件并提交到 `/v1/uploads/validate`。
   - 在一旁的只读文本框中显示成功或错误响应。
   - 明确提示该操作不上传 ZOS，也不写入上传任务表。

2. **服务状态**
   - 进程状态。
   - SQLite 状态。
   - 临时目录状态。
   - active Provider、配置 revision、存储连通性及最近探测时间。
   - 启动恢复是否完成。

3. **上传概览**
   - 上传任务总数。
   - 成功、失败、处理中、待确认数量。
   - 成功率。
   - 成功上传字节数。
   - 平均耗时和 P95 耗时。

4. **上传流量图**
   - 按小时或按天显示成功上传字节数。
   - 同时显示上传尝试数、成功数和失败数。
   - 时间范围提供 24 小时、7 天、30 天和自定义范围。

5. **近期上传任务**
   - 文件名、状态、大小、Content-Type、耗时、创建时间、错误码、Storage Provider 和配置 revision。
   - 成功任务显示对象 Key 和公网 URL。
   - URL 只作为外部链接展示，服务不代理文件内容。

6. **运行日志**
   - 默认显示 `NOTIFY`、`WARNING`、`ERROR`、`CRITICAL`。
   - 支持按级别、时间、事件、请求 ID、任务 ID 和错误码筛选。
   - 使用 `before_id` 进行稳定的向前分页。

### 11.3 设置页面内容

第一版提供 **天翼云对象存储 ZOS** 预设：

| 设置项 | 必填 | 说明 |
|---|---:|---|
| `provider` | 是 | 第一版固定为 `ctyun_zos`；未来可增加其他 Provider |
| `provider_schema_version` | 是 | `ctyun_zos` 第一版为 `1`；用于校验 Provider 专属设置结构 |
| `endpoint_url` | 是 | SDK 上传接口地址；创建 S3/ZOS Client 使用的地域 Endpoint 或内网 Endpoint |
| `bucket` | 是 | 目标 Bucket 名称 |
| `public_base_url` | 是 | 返回给调用方的对象访问根地址；可使用 Bucket 外网访问域名或自定义域名 |
| `access_key` | 首次必填 | 同一 Provider、schema version 和 Endpoint 下更新时可留空，前端省略该字段以保留现有 AK；任一项变化时必填 |
| `secret_key` | 首次必填 | 同一 Provider、schema version 和 Endpoint 下更新时可留空，前端省略该字段以保留现有 SK；任一项变化时必填 |
| `connect_timeout_seconds` | 是 | 默认 5 秒 |
| `read_timeout_seconds` | 是 | 默认 300 秒 |
| `max_attempts` | 是 | SDK 最大重试次数，不含首次请求；默认 2 |
| `verify_tls` | 是 | 默认开启 |
| `enable_bucket_metrics` | 是 | 是否启用 ZOS Bucket Statistics 和 Storage Info |

页面显示：

- 当前 Provider 和 revision。
- 当前 Endpoint、Bucket、public base URL 与连接参数。
- masked AK，例如 `****A1B2`。
- SK 是否已配置，永远不显示原值。
- 最近一次连接测试状态、时间和耗时。
- “测试连接”和“保存并激活”两个操作。
- 当 `public_base_url` 为空时，页面可根据 Bucket 与外网 Endpoint 建议 `https://{bucket}.{endpoint-host}`，用户仍可改为控制台显示的 Bucket 外网访问域名、CDN 或自定义域名。

保存前显示目标 Endpoint、Bucket 和新 revision 的确认信息。任何局域网客户端只要可以访问该服务端口，就可以执行这些设置操作。

### 11.4 刷新策略

- 服务状态和上传概览每 30 秒刷新。
- 上传流量图每 60 秒刷新。
- 近期任务每 15 秒刷新。
- 日志每 10 秒拉取新记录。
- 设置页只在打开、测试或保存后刷新，不轮询明文凭证。
- 第一版使用 HTTP 轮询，不引入 WebSocket 或 SSE。

### 11.5 页面安全与健壮性

- 所有文件名、日志消息和动态字段执行 HTML 转义。
- 页面不加载公网 CDN、字体或第三方脚本。
- 监控 API 使用 GET；设置修改使用 JSON `POST` / `PUT`。
- 设置写请求必须携带 `X-Settings-Request: true`，CORS 继续关闭。
- 该自定义 Header 用于降低浏览器跨站表单误提交风险，不代表调用方认证。
- GET 响应、HTML、JavaScript、日志和浏览器存储中不出现 AK/SK 明文。
- AK/SK 输入框使用 `type=password`，关闭自动填充；提交完成后立即清空页面内存中的输入值。
- 设置响应使用 `Cache-Control: no-store`。
- 正式部署通过内网 HTTPS 传输设置请求；HTTP 只用于本机开发或已隔离的管理网络。
- 查询时间范围、分页和筛选参数设置上限。
- 单次日志响应默认 100 条，最大 500 条。

## 12. 上传流量统计

### 12.1 服务本地统计

`upload_tasks` 是 Dashboard 上传流量的主数据源。统计只计算本服务实际处理的任务。

定义：

- `attempt_count`：时间范围内创建的任务数。
- `success_count`：`status='succeeded'` 的任务数。
- `failure_count`：`status='failed'` 的任务数。
- `uploading_count`：`status='uploading'` 的任务数。
- `unknown_count`：`status='unknown'` 的任务数。
- `successful_upload_bytes`：成功任务 `size_bytes` 之和。
- `success_rate`：`success_count / (success_count + failure_count)`。
- `average_duration_ms`：已完成任务平均耗时。
- `p95_duration_ms`：已完成任务耗时的第 95 百分位。

时间序列只汇总成功上传字节数，同时附带任务状态计数。幂等重放不会创建新任务，因此不会重复计入流量。

### 12.2 Provider 原生统计（ZOS v1）

ZOS 官方 SDK 提供的 Bucket 统计作为可选补充面板：

- `Get Bucket Statistics`：展示各存储类型的 `OpsRequested`、`BytesSent`、`BytesRetrieved`、`OpsRetrieved`、`BytesCross` 和 CDN 字节数。
- `Get Bucket Storage Info`：展示 Bucket 总大小、对象数量、分段碎片数量及各存储类型占用。
- 查询时间按 UTC 传入，单次范围最大 31 天。
- 结果缓存 5 分钟，降低 Dashboard 刷新对 ZOS 的压力。
- ZOS 原生统计不可用时，服务本地上传统计继续正常工作，页面显示最近一次成功获取时间和错误状态。

## 13. 日志设计

### 13.1 日志级别

定义自定义级别：

```text
NOTIFY = 25
```

它位于 `INFO` 与 `WARNING` 之间。Dashboard 持久化并显示 `NOTIFY` 及以上级别。

### 13.2 输出目标

每条日志同时输出到：

1. stdout/stderr：JSON Lines，供 Docker 或宿主机收集。
2. SQLite `service_logs`：只保存 `NOTIFY` 及以上级别，供 Dashboard 查询。

### 13.3 核心事件

至少记录：

- `service_started`
- `service_ready`
- `service_degraded`
- `recovery_started`
- `recovery_resolved`
- `recovery_pending`
- `upload_started`
- `upload_succeeded`
- `upload_failed`
- `upload_capacity_rejected`
- `idempotency_replayed`
- `database_error`
- `storage_probe_failed`
- `storage_metrics_failed`
- `storage_config_test_succeeded`
- `storage_config_test_failed`
- `storage_config_activated`
- `storage_config_revision_conflict`
- `maintenance_completed`

每个上传事件包含可用的：

- `request_id`
- `task_id`
- 原始文件名
- `content_type`
- `size_bytes`
- `object_key`
- `duration_ms`
- `error_code`
- `storage_provider`
- `storage_config_revision`

日志内容执行长度限制和控制字符清洗。AK、SK、请求文件内容和完整环境变量永远不进入日志。

## 14. Storage Provider 与 ZOS SDK 策略

### 14.1 Provider 边界

上传服务内部定义稳定的 Provider adapter：

- `provider_id` 与 `schema_version`：标识 adapter 及其设置结构。
- `get_settings_schema()`：返回 Dashboard 使用的 Provider preset、非敏感字段与凭证字段定义。
- `validate_config()`：校验 Provider 专属设置与凭证 envelope。
- `create_client()`：根据已解密凭证创建 Client。
- `test_connection()`：执行低副作用连通性检查。
- `upload_file()`：上传文件并返回 Provider 原始确认信息。
- `head_object()`：恢复任务和诊断对象状态。
- `get_metrics()`：获取可选 Provider 指标。
- `build_public_url()`：使用已保存的访问根地址构造 URL。

上传调用方只依赖本服务的 HTTP API。未来增加其他 SDK 或对象存储时，新增 adapter 和 provider preset，不改变 `/v1/uploads` 的请求及成功响应。

### 14.2 `ctyun_zos` 预设

天翼云官方资料要求 SDK 访问准备 AK/SK 和 Bucket 所在地域的 Endpoint。Python SDK 的 Client 由 `Session(access_key, secret_key)` 与 `endpoint_url` 创建。

第一版设置字段：

```text
provider=ctyun_zos
provider_schema_version=1
endpoint_url
bucket
public_base_url
access_key
secret_key
connect_timeout_seconds
read_timeout_seconds
max_attempts
verify_tls
enable_bucket_metrics
```

`public_base_url` 与 SDK Endpoint 分开保存。外网场景可以使用 `https://{bucket}.{endpoint-host}` 形式的 Bucket 外网访问域名；内网 Endpoint、CDN 或自定义域名场景由用户填写实际访问根地址。

### 14.3 核心上传路径

核心上传优先依赖标准 S3 能力：

- `upload_fileobj` / S3 Transfer Manager
- multipart upload
- `HeadObject`
- `HeadBucket`

上传时设置：

- active revision 固定的 Bucket。
- 生成后的对象 Key。
- 文件 Content-Type。
- 私有或 Bucket 既定对象权限；调用方无权覆盖。

### 14.4 超时、重试与连接池

建议默认值：

```text
connect_timeout_seconds=5
read_timeout_seconds=300
max_attempts=2  # 最大重试次数，不含首次请求
S3_MULTIPART_THRESHOLD_BYTES=16777216
S3_MULTIPART_CHUNK_BYTES=16777216
S3_TRANSFER_MAX_CONCURRENCY=2
```

连接池大小至少为：

```text
MAX_CONCURRENT_UPLOADS × S3_TRANSFER_MAX_CONCURRENCY + 4
```

应用层不对已经返回明确失败的上传执行自动整文件重试。SDK 的短连接重试只处理连接建立等瞬时错误。

### 14.5 ZOS 扩展统计

- 核心上传优先使用仍在维护的标准 `boto3`。
- `Get Bucket Statistics` 和 `Get Bucket Storage Info` 通过可选 ZOS 官方 SDK 扩展实现。
- active revision 的 `enable_bucket_metrics=false` 时完全跳过扩展统计调用。
- 扩展统计故障不会阻塞上传 API。

### 14.6 Bucket 生命周期要求

部署前确认 Bucket 已设置：

- 业务需要的对象过期规则。
- 未完成 multipart upload 自动清理规则。
- `AbortIncompleteMultipartUpload.DaysAfterInitiation=1` 作为默认建议。

部署验收脚本验证规则存在并记录结果；运行服务不主动修改 Bucket 生命周期配置。

## 15. 错误约定

所有错误响应采用统一结构：

```json
{
  "task_id": "optional-task-id",
  "error": {
    "code": "FILE_TOO_LARGE",
    "message": "文件不能超过 200 MiB",
    "request_id": "request-id"
  }
}
```

主要错误码：

| HTTP | `code` | 场景 |
|---:|---|---|
| 400 | `FILE_REQUIRED` | 缺少 `file` 字段 |
| 400 | `FILE_EMPTY` | 文件为空 |
| 400 | `BAD_REQUEST` | multipart 或查询参数错误 |
| 400 | `STORAGE_CONFIG_INVALID` | Provider、schema version、设置字段、URL、Bucket、凭证格式或参数不合法 |
| 400 | `STORAGE_CREDENTIALS_REQUIRED` | 首次配置或 Provider/Endpoint 变化时缺少完整 AK/SK |
| 404 | `TASK_NOT_FOUND` | 任务不存在 |
| 409 | `CONFIG_REVISION_CONFLICT` | `expected_revision` 与当前 active revision 不一致 |
| 409 | `UPLOAD_IN_PROGRESS` | 同一幂等键对应任务仍在处理或待确认 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 同一幂等键已对应失败任务 |
| 413 | `FILE_TOO_LARGE` | 文件超过上限 |
| 500 | `DATABASE_ERROR` | SQLite 异常 |
| 500 | `SETTINGS_STORAGE_ERROR` | 设置加密、写入或 Client 切换失败 |
| 500 | `INTERNAL_ERROR` | 未分类内部异常 |
| 502 | `UPLOAD_FAILED` | Storage Provider 明确拒绝或上传失败 |
| 502 | `STORAGE_TIMEOUT` | Storage Provider 请求超时 |
| 502 | `STORAGE_ENDPOINT_UNREACHABLE` | 设置测试无法连接 Endpoint |
| 502 | `STORAGE_CREDENTIALS_REJECTED` | 设置测试中的 AK/SK 被拒绝 |
| 502 | `STORAGE_BUCKET_UNAVAILABLE` | Bucket 不存在或当前凭证不可访问 |
| 503 | `UPLOAD_CAPACITY_EXCEEDED` | 上传并发槽位已满 |
| 503 | `STORAGE_NOT_CONFIGURED` | 尚未激活存储配置 |
| 503 | `NOT_READY` | 就绪检查失败 |
| 503 | `STORAGE_METRICS_UNAVAILABLE` | 可选 Storage Provider 原生指标暂时不可用 |

已创建任务的错误响应包含 `task_id`。错误消息用于人类阅读，程序逻辑判断 `code`。

## 16. 配置

### 16.1 部署级不可变配置

以下配置继续由容器环境或 Secret 注入，Dashboard 不可修改：

```text
# 凭证加密
SETTINGS_ENCRYPTION_KEY

# 首次启动导入
BOOTSTRAP_STORAGE_FROM_ENV=true

# 上传
MAX_UPLOAD_BYTES=209715200
MAX_REQUEST_BODY_BYTES=213909504
MAX_CONCURRENT_UPLOADS=4
UPLOAD_READ_CHUNK_BYTES=1048576
UPLOAD_SPOOL_THRESHOLD_BYTES=8388608
S3_MULTIPART_THRESHOLD_BYTES=16777216
S3_MULTIPART_CHUNK_BYTES=16777216
S3_TRANSFER_MAX_CONCURRENCY=2
TEMP_DIR=/data/tmp
TEMP_MIN_FREE_BYTES=1073741824

# 数据库与保留
DATABASE_PATH=/data/zos-upload.db
SQLITE_BUSY_TIMEOUT_MS=5000
TASK_RETENTION_DAYS=180
LOG_RETENTION_DAYS=30
LOG_MAX_ROWS=100000

# 服务
APP_TIMEZONE=Asia/Shanghai
REQUEST_TIMEOUT_SECONDS=600
STORAGE_PROBE_INTERVAL_SECONDS=30
STORAGE_PROBE_MAX_AGE_SECONDS=60
RECOVERY_RETRY_SECONDS=60
STALE_UPLOAD_SECONDS=900
DASHBOARD_ENABLED=true
```

`SETTINGS_ENCRYPTION_KEY` 是 URL-safe base64 Fernet key，由部署平台作为 Secret 提供。该 Key 缺失或无法解密现有配置时，服务启动失败并输出不含敏感内容的错误。

### 16.2 可选的首次启动 ZOS 导入

为了兼容最初的环境变量部署，以下变量只用于数据库中没有任何 storage config 时创建 revision 1：

```text
ZOS_ENDPOINT
ZOS_PUBLIC_BASE_URL
ZOS_BUCKET
ZOS_ACCESS_KEY
ZOS_SECRET_KEY
ZOS_CONNECT_TIMEOUT_SECONDS=5
ZOS_READ_TIMEOUT_SECONDS=300
ZOS_MAX_ATTEMPTS=2
ZOS_VERIFY_TLS=true
ENABLE_ZOS_BUCKET_METRICS=false
```

导入规则：

- `BOOTSTRAP_STORAGE_FROM_ENV=true` 且五个必要字段完整时，服务测试连接、加密凭证并创建 revision 1。
- 数据库已有 active 或 inactive revision 后，环境变量不再自动覆盖 Dashboard 设置。
- 导入完成后，active storage config 是上传流程的唯一运行时来源。
- 环境中的 AK/SK 仍不得进入日志。

### 16.3 未配置状态

数据库没有 active storage config 时：

- 服务进程和 Dashboard 正常启动。
- `/healthz` 返回 `200`。
- `/readyz` 返回 `503 STORAGE_NOT_CONFIGURED`。
- `POST /v1/uploads` 返回 `503 STORAGE_NOT_CONFIGURED`，不创建任务。
- `/dashboard/settings` 可用于完成首次配置。

### 16.4 Dashboard 管理的 ZOS 设置

Dashboard 管理：

```text
provider=ctyun_zos
provider_schema_version=1
endpoint_url
bucket
public_base_url
access_key
secret_key
connect_timeout_seconds
read_timeout_seconds
max_attempts
verify_tls
enable_bucket_metrics
```

- 非敏感 Provider 设置规范化后写入 `config_json`，AK/SK 作为单个加密凭证对象写入 `credentials_ciphertext`。
- GET 接口只返回 masked AK、AK 是否配置和 SK 是否配置。
- 同一 Provider、`provider_schema_version` 和 `endpoint_url` 下更新时，可以省略整个 `credentials` 对象或其中一个字段，表示沿用 active revision 的对应凭证；空字符串为非法值。
- Provider、`provider_schema_version` 或 `endpoint_url` 发生变化时必须同时提交新的 AK 和 SK。
- 每次保存都创建新 revision，不原地修改历史记录。
- 保存失败或连接测试失败时，旧 active revision 保持不变。
- ZOS 凭证至少具备执行 HeadBucket 检查、PutObject、multipart upload 和 HeadObject 所需的目标 Bucket 权限；启用 Bucket 指标时再增加对应统计读取权限。
- 优先使用专用 IAM 用户或服务账号的 AK/SK，并将权限范围限制在目标 Bucket；Dashboard 不管理 IAM、Bucket ACL 或 Bucket Policy。

## 17. 保留与维护

进程内维护协程在启动时和每 24 小时执行一次：

- 删除超过 `LOG_RETENTION_DAYS` 的日志。
- 超过 `LOG_MAX_ROWS` 时按最旧记录继续裁剪。
- 删除超过 `TASK_RETENTION_DAYS` 的 `succeeded` 和 `failed` 任务。
- 保留所有 `uploading` 和 `unknown` 任务。
- 保留被任何任务引用的 storage config revision。
- 只有 inactive revision 已无任务引用且超过 `TASK_RETENTION_DAYS` 时才允许删除。
- 执行 `PRAGMA optimize`。
- 记录 `maintenance_completed` 日志。

第一版不提供通过 HTTP 删除任务或日志的接口。

## 18. 部署

- 单 Docker 容器运行 FastAPI、Dashboard 和维护协程。
- 正式局域网部署在服务前使用内网 HTTPS 反向代理，或仅允许隔离管理网络访问设置页；反向代理、应用访问日志和 APM 均禁止记录设置请求体。该部署不增加应用层身份认证。
- SQLite 目录和 `TEMP_DIR` 分别挂载。
- `SETTINGS_ENCRYPTION_KEY` 通过容器 Secret 注入并单独备份。
- 容器端口只绑定局域网 IP 或内部 Docker 网络。
- Dashboard 静态资源打包进镜像，不依赖互联网。
- 使用容器健康检查调用 `/healthz`。
- 编排平台的就绪检查调用 `/readyz`。
- 关闭时停止接收新上传，等待 `SHUTDOWN_GRACE_SECONDS`；未完成任务由下次启动恢复。
- 定期使用 SQLite Online Backup API 或宿主机快照备份数据库。

## 19. 验证与验收

### 19.1 自动测试

- 缺少文件、空文件、超限文件和错误 multipart 正确返回。
- Content-Length 缺失或伪造时仍能执行 200 MiB 限制。
- 对象 Key、扩展名和日期目录符合约定。
- Content-Type 正确传递，缺失时使用默认值。
- 任务插入、更新和稳定排序正确。
- SQLite WAL、busy timeout 和多连接并发行为正确。
- 幂等键的首次请求、重放、处理中冲突和失败后冲突正确。
- 上传容量达到上限时返回 `503`，查询接口继续可用。
- 本地统计的数量、字节数、成功率、平均耗时和 P95 计算正确。
- `NOTIFY` 及以上日志入库，较低级别只输出到 stdout。
- 日志筛选、分页、保留和裁剪正确。
- Dashboard 对文件名和日志内容进行 HTML 转义。
- Provider preset、`provider_schema_version`、当前设置、测试连接和保存激活接口结构正确。
- 首次配置以及 Provider 或 Endpoint 变化时必须提交完整 AK/SK；同一 Provider 和 Endpoint 下更新时省略凭证可以保留旧值。
- GET 设置接口、HTML、日志和错误响应不包含 AK/SK 明文或密文。
- `expected_revision` 冲突返回 `409 CONFIG_REVISION_CONFLICT`。
- 新配置测试失败时 active revision 保持不变。
- 配置激活期间的在途上传继续使用旧 revision，新任务使用新 revision。
- 使用测试用第二 Provider adapter 验证 `/v1/uploads`、任务查询和错误结构无需变化。

### 19.2 故障注入测试

- 临时文件写入过程中客户端断开。
- ZOS 上传过程中进程被终止。
- ZOS 上传成功后、SQLite 更新前进程被终止。
- 重启后 `HEAD Object` 返回存在、404、超时和 5xx。
- SQLite 锁等待、磁盘写满和数据库只读。
- `TEMP_DIR` 空间不足。
- ZOS 连接超时、认证失败、Bucket 不存在和请求时间偏差。
- `SETTINGS_ENCRYPTION_KEY` 错误、凭证解密失败和设置事务失败。
- Provider 或 Endpoint 改变但未重新提交完整 AK/SK 时被拒绝。
- 设置 GET、错误响应、日志、数据库明文字段和浏览器存储均不出现 AK/SK。
- active revision 切换时终止进程，重启后保持单一 active revision。
- 未完成 multipart upload 被生命周期规则清理。

### 19.3 真实 ZOS 集成测试

- 上传 TXT、PDF、图片和接近 200 MiB 的文件。
- 上传超过 multipart 阈值的文件。
- 校验 ZOS 对象、Content-Type、大小、对象 Key、URL 和任务记录一致。
- 从第三方公网环境实际访问返回 URL。
- 验证 `HeadObject` 恢复逻辑。
- 开启扩展统计时验证 Bucket Statistics 和 Storage Info。
- 使用控制台中记录的 Endpoint、Bucket 外网访问域名、AK 和 SK 完成 Dashboard 首次配置。
- “测试连接”能够识别正确配置、错误 Endpoint、错误 AK/SK 和不可访问 Bucket。

### 19.4 Dashboard 验收

- 局域网浏览器无需登录即可打开 `/dashboard`。
- 24 小时、7 天和 30 天上传流量与 SQLite 任务数据一致。
- 成功率、状态数量、平均耗时和 P95 正确。
- 近期任务可以查看对象 Key、URL 和错误码。
- `NOTIFY`、`WARNING`、`ERROR`、`CRITICAL` 日志可查看和筛选。
- Provider 原生统计不可用时，本地统计、任务列表和日志保持可用。
- `/dashboard/settings` 能显示 masked 凭证、测试连接、保存并激活新 revision。
- 首次未配置时 Dashboard 可访问，上传接口返回 `STORAGE_NOT_CONFIGURED`。
- 设置修改写入 NOTIFY 日志，日志不包含凭证。
- 页面在服务持续上传时稳定轮询，无明显数据库锁冲突。

## 20. 完成标准

- 单次调用完成“创建任务、接收临时文件、上传 ZOS、持久化结果、返回 URL”。
- 服务不在请求结束后保留文件本体。
- 上传成功与数据库状态具有可恢复的一致性。
- 调用方可以通过幂等键避免网络超时后的重复对象。
- 任务列表和任务详情支持故障排查。
- Dashboard 显示上传流量、任务状态、服务状态和近期任务。
- Dashboard 显示并筛选 `NOTIFY` 及以上日志。
- Dashboard 可以使用天翼云 ZOS 预设测试、保存和激活 SDK Endpoint、Bucket、访问根地址、AK、SK 与连接参数。
- 上传 API 保持 Provider 无关，后续新增其他对象存储 adapter 时调用方契约不变。
- 配置切换使用不可变 revision，在途上传和恢复任务可以定位原配置。
- SQLite 在并发上传、Dashboard 轮询和设置切换下保持稳定。
- 服务、API 和 Dashboard 只在受控局域网暴露，无调用方认证。
- AK/SK 只以加密形式持久化，不暴露给调用方、日志、页面或 GET 响应。

## 21. 实施顺序

1. `[已完成]` 根据本文同步更新 `API.md`，冻结 v1 接口字段和错误码。
2. 验证标准 S3 Client 的 Client 创建、Head Bucket、上传、multipart、Content-Type、Head Object 和 URL 生成。
3. 实现 Storage Provider registry、`ctyun_zos` adapter 和 provider preset 描述。
4. 建立 Provider 通用的 `storage_configs` envelope、`upload_tasks`、`service_logs` schema、索引、WAL 参数和轻量 schema version。
5. 实现 `SETTINGS_ENCRYPTION_KEY`、凭证加解密、首次环境导入和未配置启动模式。
6. 实现 provider 列表、当前设置、测试连接、保存激活、revision 冲突和 Client 原子切换。
7. 实现请求体限制、临时文件、并发信号量和上传主流程，并让任务绑定 storage config revision。
8. 实现幂等键、任务列表、任务详情和统一错误响应。
9. 实现启动恢复、周期恢复、Provider 探测和 `/readyz`。
10. 实现 JSON 结构化日志、`NOTIFY` 级别、日志入库和保留清理。
11. 验证可选 ZOS Bucket Statistics、Storage Info 和 SDK 安装方式。
12. 实现 Dashboard 统计 API、可选 Provider 统计缓存、监控页面和设置页面。
13. 完成自动测试、故障注入和真实 ZOS 集成测试。
14. 以单容器部署到局域网，配置持久化目录、临时目录、加密主密钥、生命周期规则和数据库备份。

## 22. 已确认决策

1. 服务只在受控局域网运行，不设置调用方认证。
2. API 与 Dashboard 共用 FastAPI 服务和同一局域网端口。
3. Dashboard 监控区域只读，设置页面可以修改 active storage config。
4. 局域网内能够访问服务端口的客户端均可执行设置操作。
5. 上传调用方 API 保持 Provider 无关；第一版只实现 `ctyun_zos` adapter。
6. ZOS 设置至少包含 SDK Endpoint、Bucket、public base URL、AK 和 SK；Endpoint 与对象访问根地址分别保存。
7. 存储设置采用 Provider-neutral envelope 和通用 JSON 持久化，新增 Provider 不改变上传 API 或 `storage_configs` 表结构。
8. AK/SK 使用部署级主密钥加密后持久化，GET 接口和页面只显示 masked 状态。
9. 每次设置激活创建不可变 revision，上传任务绑定创建时的 revision。
10. 保存设置前执行 `HeadBucket` 连接测试，测试失败时保留旧 active revision。
11. 技术栈为 Python 3.11、FastAPI、S3 Client、SQLite、cryptography、Jinja2 和原生 JavaScript。
12. 单文件最大 200 MiB，接受所有文件类型。
13. 文件只在请求期间进入受控临时文件，请求结束后删除。
14. 对象按 `YYYY/MM/DD/{UUID}.{安全扩展名}` 命名。
15. 任务表持久化 `object_key`、URL、Content-Type、请求 ID、耗时和 storage config 引用。
16. 服务支持可选 `Idempotency-Key` 和单任务详情查询。
17. 服务重启后通过任务对应 Provider revision 恢复中断任务。
18. 上传并发通过进程内信号量限制，默认最多 4 个。
19. Dashboard 上传流量以 SQLite 任务台账为主数据源。
20. ZOS Bucket Statistics 和 Storage Info 为可选补充指标。
21. `NOTIFY=25`，Dashboard 持久化并显示 `NOTIFY` 及以上日志。
22. 日志默认保留 30 天或最多 100000 条，任务默认保留 180 天。
23. 服务不提供对象下载、删除、更新、列表或 Bucket 权限管理能力。

## 23. `API.md` 同步状态

以下接口契约已经同步写入 `API.md`：

- [x] 增加可选 `Idempotency-Key` 请求头及重放语义。
- [x] 增加任务状态 `unknown`。
- [x] 任务列表增加 `request_id`、`content_type`、`object_key`、`duration_ms`、`storage_provider` 和 `storage_config_revision`。
- [x] 增加状态和时间范围筛选。
- [x] 增加 `GET /v1/upload-tasks/{task_id}`。
- [x] 增加 `GET /readyz`。
- [x] 增加 Dashboard summary、traffic、logs 和 Provider 通用的可选 storage metrics 接口。
- [x] 增加 `/dashboard/settings` 页面和 storage provider 设置接口。
- [x] 增加 `ctyun_zos` provider preset、schema version、连接测试、masked 凭证和 revision 激活语义。
- [x] Provider 或 Endpoint 变化时要求重新提交完整 AK/SK。
- [x] 增加 `STORAGE_NOT_CONFIGURED`、`STORAGE_CONFIG_INVALID`、`STORAGE_CREDENTIALS_REQUIRED`、`CONFIG_REVISION_CONFLICT` 和设置连接错误码。
- [x] 增加 `UPLOAD_CAPACITY_EXCEEDED`、`UPLOAD_IN_PROGRESS`、`IDEMPOTENCY_KEY_REUSED`、`TASK_NOT_FOUND` 和 `NOT_READY` 等错误码。
- [x] 更新重启恢复和重试语义。
- [x] 保留现有上传成功响应中的 `task_id`、`key` 和 `url`。

## 24. 参考资料

- [API.md](API.md)
- [天翼云对象存储 ZOS：获取访问密钥（AK/SK）](https://www.ctyun.cn/document/10026735/10172656)
- [天翼云对象存储 ZOS：查询终端节点（Endpoint）](https://www.ctyun.cn/document/10026735/10172670)
- [天翼云对象存储 ZOS：SDK 参考](https://www.ctyun.cn/document/10026735/10110276)
- `ZOS对象存储Python_SDK使用手册.pdf`
  - Session、AK/SK、Endpoint 与连接超时
  - Head Bucket
  - 连接配置与超时重试
  - Put Object
  - Head Object
  - Get Bucket Statistics
  - Get Bucket Storage Info
  - Put Bucket Lifecycle Configuration
