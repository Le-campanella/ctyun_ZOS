# 局域网轻量文件上传服务实施计划（ZOS v3）

> 状态：v6 方案已确认，多服务存储预设、严格删除、删除恢复与审计、Dashboard v3、可靠性、基础生产部署与私有异地备份验收已完成。
>
> 完整调用方与 Dashboard 接口契约见 [API.md](API.md)。本文与 `API.md` 已完成同步。
>
> 实现进度（2026-08-03）：SQLite schema v5、管理员控制面认证、有界恢复、事务化部署回滚、独立灾备、调用方身份/配额，以及既有多预设、严格删除与 Dashboard 已完成。

## 1. 目标

建设一个仅在受控局域网运行的轻量 HTTP 服务，完成以下能力：

1. 接收局域网内其他服务上传的单个文件。
2. 通过可替换的存储 Provider 将文件同步上传到对象存储；内置天翼云 ZOS 和通用 S3 兼容 Provider。
3. 上传成功后返回任务 ID、对象 Key 和公网 URL；失败时返回稳定错误码。
4. 使用 SQLite 保存上传任务台账，支持任务列表和单任务详情查询。
5. 提供 Web Dashboard，显示上传流量、成功率、任务状态、服务状态和近期任务。
6. Dashboard 提供存储设置页，可测试、保存并激活 ZOS Endpoint（SDK 上传接口地址）、Bucket、对象访问根地址、AK、SK 和连接参数。
7. 保存并展示 `NOTIFY` 及以上级别的结构化运行日志。
8. 在服务异常重启后，通过存储 Provider 的对象元数据接口恢复可确认的上传结果。
9. 允许上传调用方凭对象级删除凭证严格删除该任务创建的 ZOS 对象，并永久保留删除审计记录。
10. 支持配置多个存储预设；调用方可选择一个固定绑定 Endpoint 与 Bucket 的预设，未指定时使用唯一默认预设。

## 2. 信任边界与范围

### 2.1 网络边界

- 服务仅发布到受控局域网地址或容器内部网络。
- API 与 Dashboard 共用同一个局域网端口。
- 上传数据面不要求统一调用方身份；Dashboard、设置、日志、OpenAPI 和完整任务查询要求管理员密钥。
- 对象删除是例外的破坏性操作，必须同时提供任务 ID 和上传成功时返回的对象级 `delete_token`；该 token 是持有者凭证，不代替未来的统一服务认证。
- 只有持有管理员密钥的客户端可以读取监控与完整任务信息或修改存储设置。
- 局域网、防火墙、交换机 VLAN 和部署平台的端口暴露规则构成访问边界。
- 部署配置禁止公网入口、端口转发和公有负载均衡器。
- CORS 默认关闭，Dashboard 通过同源接口读写设置。
- 设置写接口同时要求管理员认证、JSON、自定义 Header、同源检查和 revision 乐观锁。
- 设置请求会传输 AK/SK。管理员认证不替代内网 HTTPS 或隔离管理网络提供的传输保护。

### 2.2 服务管理范围

服务管理上传过程、任务记录、运行日志和统计视图。对象存储中的对象继续由对应 Bucket 配置管理。

服务包含：

- 上传文件。
- 删除本服务成功上传且可以严格确认身份的对象。
- 查询上传任务。
- 查询任务详情。
- 查询上传流量统计。
- 查询 `NOTIFY` 及以上日志。
- 查看 Dashboard 监控页面。
- 创建、查看、测试、修改、启用或禁用多个存储预设，并设置唯一默认预设。
- 健康检查、就绪检查和中断任务恢复。

服务范围之外：

- 持久化保存文件本体。
- 代理下载、更新、重命名或列出 ZOS 对象。
- 删除调用方任意指定的 Bucket、对象 Key、URL 或非本服务上传的对象。
- 由调用方直接提交 Endpoint、Bucket、对象 ACL 或任意对象 Key；调用方只能选择服务端已启用的预设。
- 文件去重、内容搜索、文件分类、版本管理和素材管理。
- Dashboard 内执行文件上传、任务重试、对象删除或 Bucket 权限管理。
- 通过 Dashboard 创建、删除或修改 ZOS Bucket、生命周期、ACL、策略、版本控制或 CORS。
- 消息队列、独立异步 Worker、ORM 和完整数据库迁移框架。

文件过期、非本服务创建的对象、历史版本清理和未完成分段上传清理由 ZOS Bucket 生命周期规则负责。

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
│  删除 API ─────────┤                                      │
│  查询 API ─────────┼──> SQLite                            │
│  Dashboard API ────┤    - storage_presets                 │
│  设置 API ─────────┤    - storage_configs                 │
│  日志模块 ─────────┘    - upload_tasks / service_logs     │
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

上传 API 与调用方契约保持 Provider 无关。每个预设固定绑定一个 Provider、Endpoint、Bucket 和公网访问根地址；同一 Endpoint 下的不同 Bucket 建立为不同预设。调用方只提交稳定的 `preset_key`，不能直接覆盖存储参数。

文件只在当前请求期间存在于受控临时文件中。请求完成、失败或客户端断开后立即关闭并删除临时文件。

## 5. 文件与对象约定

- 单文件最大 `200 MiB`，即 `209715200` 字节。
- 接受所有文件类型。
- 每次请求只允许一个 `file` 字段。
- 空文件返回 `FILE_EMPTY`。
- 原始文件名只用于记录和展示，不直接进入对象 Key。
- 原始文件名最大保存 255 个 Unicode 字符，超出部分截断。
- Content-Type 缺失或无效时使用 `application/octet-stream`。
- 上传对象固定使用 canned ACL `public-read`；调用方不能覆盖。
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

上传成功后额外通过对象元数据接口确认并保存 `ETag`、可选 `VersionId` 和对象大小。`ETag` 是 Provider 返回的不透明标识，multipart 场景下不得当作文件 MD5。上传响应只返回稳定的删除清单，不返回 Bucket、Endpoint、AK/SK、配置 ID、SDK 原始响应或签名信息。

`delete_token` 使用密码学安全随机数生成 256-bit URL-safe token。数据库只保存 token 的 SHA-256 哈希，并通过任务行把它绑定到配置 ID、对象 Key、ETag、VersionId 和大小；服务不保存 token 明文。token 只在首次 `201` 响应中返回一次，幂等重放不会补发。调用方必须把 token 当作密码保存，不得写入 URL、普通业务日志或任务查询缓存。

## 6. 请求接收、临时文件与并发控制

### 6.1 临时文件策略

上传只使用 Starlette multipart 解析器创建的单个 `UploadFile` spool。解析完成后读取框架维护的文件大小，复位同一文件指针并直接交给 S3 Transfer Manager，不再复制到第二个临时文件。

默认参数：

```text
MAX_UPLOAD_BYTES=209715200
```

spool 是否落盘由 Starlette multipart 实现决定；上传结束、校验失败和异常路径均关闭整个 `FormData`，由框架统一回收所有上传 part。

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
- 直接来源 IP 默认每分钟最多发起 `60` 次上传或接收验证，超过时返回 `429 UPLOAD_RATE_LIMITED`；不信任未经配置的代理转发头。
- 每个调用方默认最多保有 `10000` 个可能存在的对象、总计 `1 TiB`；检查和任务插入在同一 SQLite 写事务中完成，超过时返回 `429 CLIENT_QUOTA_EXCEEDED`。
- Dashboard、任务查询和健康检查不占用上传信号量。
- `TEMP_DIR` 可用空间至少满足：

```text
MAX_CONCURRENT_UPLOADS × MAX_UPLOAD_BYTES × 1.2
```

就绪检查会验证临时目录可写和剩余空间。

## 7. SQLite 数据模型

数据库包含四张业务表。使用 `PRAGMA user_version` 管理轻量级手写 schema 升级。

### 7.1 存储预设表

预设是调用方可选择的稳定路由标识。`preset_key` 创建后不可修改，格式为 1 至 64 个字符的小写 slug，只允许小写字母、数字和中划线，且首尾必须为字母或数字。

```sql
CREATE TABLE storage_presets (
    id              TEXT PRIMARY KEY,
    preset_key      TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    enabled         INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    is_default      INTEGER NOT NULL CHECK (is_default IN (0, 1)),
    state_revision  INTEGER NOT NULL CHECK (state_revision >= 1),
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX uq_storage_presets_default
ON storage_presets(is_default)
WHERE is_default = 1;
```

任意时刻最多一个默认预设；存在可上传预设时必须有且仅有一个启用的默认预设。第一个成功创建的预设自动成为默认项。当前默认预设不能直接禁用，必须先把另一个启用预设设为默认。预设不提供硬删除接口，以免历史任务失去可解释的路由关系。

### 7.2 存储配置表

每个预设每次成功保存设置都会创建一条不可变配置 revision。表结构使用 Provider 通用 envelope，ZOS 专属字段保存在 `config_json`，凭证作为一个整体加密保存。

```sql
CREATE TABLE storage_configs (
    id                       TEXT PRIMARY KEY,
    preset_id                TEXT NOT NULL REFERENCES storage_presets(id),
    revision                 INTEGER NOT NULL CHECK (revision >= 1),
    provider                 TEXT NOT NULL,
    provider_schema_version  INTEGER NOT NULL CHECK (provider_schema_version >= 1),
    config_json              TEXT NOT NULL,
    credentials_ciphertext   BLOB NOT NULL,
    status                   TEXT NOT NULL CHECK (status IN ('active', 'inactive')),
    created_at               TEXT NOT NULL,
    activated_at             TEXT NOT NULL,
    last_tested_at           TEXT NOT NULL,
    last_test_latency_ms     INTEGER,
    UNIQUE (preset_id, revision)
);

CREATE UNIQUE INDEX uq_storage_configs_active
ON storage_configs(preset_id)
WHERE status = 'active';

CREATE INDEX idx_storage_configs_revision
ON storage_configs(preset_id, revision DESC);

CREATE INDEX idx_storage_configs_provider_revision
ON storage_configs(provider, revision DESC);
```

字段约定：

| 字段 | 说明 |
|---|---|
| `id` | 配置 UUID，供任务引用 |
| `preset_id` | 所属存储预设 |
| `revision` | 在同一预设内从 1 开始递增的可见版本号 |
| `provider` | Provider ID；第一版为 `ctyun_zos` |
| `provider_schema_version` | 该 Provider 设置结构的版本；`ctyun_zos` 第一版为 `1` |
| `config_json` | Provider 专属的非敏感配置；ZOS 包含 Endpoint、Bucket、`public_base_url`、超时、重试、TLS 和指标开关 |
| `credentials_ciphertext` | Provider credential envelope 的认证密文；ZOS 包含 AK 和 SK |
| `status` | `active` 或 `inactive`，每个预设任意时刻最多一个 active revision |
| `last_tested_at` | 激活前最后一次连接测试时间 |
| `last_test_latency_ms` | Provider 连接测试耗时 |

Provider registry 负责按照 `provider + provider_schema_version` 校验 `config_json` 和解密后的 credential envelope。未知 Provider、未知 schema version、缺失字段或非法字段均拒绝加载。历史 revision 保持不可变，任务恢复始终使用任务创建时绑定的配置。

`config_json` 使用 UTF-8 canonical JSON，禁止包含 Provider preset 标记为 secret 的字段。`credentials_ciphertext` 解密后的对象只在创建 Provider Client 时短暂存在于进程内存中。AK、SK 使用 `SETTINGS_ENCRYPTION_KEY` 和 `cryptography.fernet.Fernet` 进行认证加密，数据库、页面、API 和日志均不保存或返回明文凭证。

Provider ID 不使用数据库枚举约束，新增 adapter 和 preset 即可引入新 Provider。仍有任务引用历史 revision 时，对应 Provider adapter 和 schema 解析器必须继续保留。

### 7.3 上传任务表

```sql
CREATE TABLE upload_tasks (
    id                 TEXT PRIMARY KEY,
    request_id         TEXT NOT NULL,
    client_id          TEXT NOT NULL,
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
    etag               TEXT,
    version_id         TEXT,
    delete_token_hash  BLOB,
    object_status      TEXT NOT NULL CHECK (
        object_status IN (
            'pending', 'present', 'present_unclaimed', 'absent', 'legacy_unverified',
            'deleting', 'deleted', 'delete_unknown'
        )
    ),
    delete_request_id  TEXT,
    delete_error_code  TEXT,
    delete_started_at  TEXT,
    deleted_at         TEXT,
    error_code         TEXT,
    created_at         TEXT NOT NULL,
    finished_at        TEXT,
    duration_ms        INTEGER
);

CREATE UNIQUE INDEX uq_upload_tasks_idempotency_key
ON upload_tasks(client_id, idempotency_key)
WHERE idempotency_key IS NOT NULL;

CREATE INDEX idx_upload_tasks_created_at_id
ON upload_tasks(created_at DESC, id DESC);

CREATE INDEX idx_upload_tasks_status_created_at
ON upload_tasks(status, created_at DESC);

CREATE INDEX idx_upload_tasks_request_id
ON upload_tasks(request_id);

CREATE INDEX idx_upload_tasks_storage_config_id
ON upload_tasks(storage_config_id);

CREATE INDEX idx_upload_tasks_object_status
ON upload_tasks(object_status, created_at DESC);
```

字段约定：

| 字段 | 说明 |
|---|---|
| `id` | 任务 UUID，同时用于生成对象 Key |
| `request_id` | 请求追踪 ID |
| `client_id` | 认证调用方 ID；兼容模式和 v5 前历史任务为 `legacy` |
| `idempotency_key` | 调用方可选幂等键；在同一 `client_id` 内唯一 |
| `storage_config_id` | 创建任务时使用的存储配置 revision |
| `filename` | 经过长度限制的原始文件名 |
| `content_type` | 上传到对象存储的 Content-Type |
| `object_key` | 稳定的对象 Key，上传开始前写入 |
| `public_url` | 成功后返回的完整 URL |
| `status` | `uploading`、`unknown`、`succeeded`、`failed` |
| `size_bytes` | 已确认的文件大小；无法确认时为空 |
| `etag` | 上传后 `HeadObject` 返回的不透明 ETag；不得假设为 MD5 |
| `version_id` | Provider 返回的对象版本 ID；未启用或不支持版本控制时为空 |
| `delete_token_hash` | 对象级删除凭证的 SHA-256 哈希；明文 token 不持久化 |
| `object_status` | `pending`、`present`、`present_unclaimed`、`absent`、`legacy_unverified`、`deleting`、`deleted` 或 `delete_unknown` |
| `delete_request_id` | 最近一次删除请求的追踪 ID |
| `delete_error_code` | 最近一次删除失败或不确定结果的稳定错误码 |
| `delete_started_at` | 最近一次删除开始时间 |
| `deleted_at` | 服务确认目标对象或精确版本不存在的时间 |
| `error_code` | 当前错误或恢复状态码 |
| `created_at` | UTC ISO 8601 创建时间 |
| `finished_at` | UTC ISO 8601 完成时间 |
| `duration_ms` | 完整请求处理耗时 |

schema v1 升级到 v2 时不伪造历史元数据：旧的成功任务设置为 `legacy_unverified`，其他旧任务按可确认结果设置为 `pending` 或 `absent`。历史任务不会自动获得可对外使用的删除凭证。

schema v2 升级到 v3 时创建 `preset_key=default`、显示名为“默认 ZOS”的启用默认预设，并把现有全部 `storage_configs` 关联到该预设；配置 revision 与任务的 `storage_config_id` 保持不变。

### 7.4 运行日志表

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

### 7.5 SQLite 运行参数

每个请求或工作线程使用独立连接，并设置：

```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

数据库写事务保持短小。更新某个预设时，在一个事务内将该预设的旧 revision 设为 `inactive` 并插入新 `active` revision；切换默认预设也在单个事务内完成。统计查询和 Dashboard 查询使用只读连接。

## 8. 上传状态流与一致性

### 8.1 正常流程

1. 接收请求头，确定最终 `request_id`。
2. 校验可选 `X-Storage-Preset` 的格式，并处理可选 `Idempotency-Key`；已有任务优先按原预设执行重放或冲突判断。
3. 新任务使用显式指定的启用预设；未指定时使用唯一默认预设。读取该预设的 active `storage_config` 并创建不可变快照。
4. 获取上传并发槽位。
5. 解析 `file`，生成任务 UUID、对象 Key 和公网 URL。
6. 在 SQLite 中原子写入 `uploading` 任务，同时记录 `storage_config_id`。
7. 写入临时文件，同时校验空文件、文件大小和客户端连接状态。
8. 通过该配置对应的 Provider adapter 上传文件。
9. 上传返回成功后执行 `HeadObject`，确认并保存大小、ETag、可选 VersionId，将上传状态更新为 `succeeded`、对象状态更新为 `present`。
10. 生成随机对象级 `delete_token`，在同一数据库事务中只保存哈希；恢复确认对象存在但没有哈希时进入 `present_unclaimed`。
11. 数据库更新成功后向调用方返回 `201` 和稳定删除清单。
12. 任一步骤失败时，将可定位任务更新为 `failed` 或 `unknown`，写入稳定错误码并清理临时文件。

对象 Key、配置 revision 和公网 URL 在上传前持久化。设置切换不会改变已经创建任务的目标位置。

### 8.2 配置激活与并发上传

保存某个预设的设置时执行以下流程：

1. 校验 Provider envelope、`provider_schema_version`、URL、Bucket 名称、超时和重试参数。
2. 合并当前已保存凭证与本次提交的凭证；首次配置必须提交 AK 和 SK。Provider、`provider_schema_version` 或 `endpoint_url` 发生变化时必须重新提交完整 AK/SK，禁止把旧凭证自动发送到新的设置结构或 Endpoint。
3. 创建候选 `ctyun_zos` Client，并调用 `HeadBucket` 测试 Endpoint、凭证和 Bucket 可访问性。
4. 测试成功后，将 Provider credential envelope（ZOS 为 AK/SK）整体加密。
5. 在单个 SQLite 事务内为该预设创建新 revision，并将其旧 revision 设为 `inactive`。
6. 原子替换以 `storage_config_id` 为键的 Provider 快照和 Client 缓存。
7. 写入 `storage_config_activated` NOTIFY 日志；日志只包含 Provider、revision、Endpoint 主机名、Bucket 和来源 IP。

正在执行的上传持有旧配置快照并继续完成；新任务使用目标预设的新 revision。默认预设切换只影响切换后的未指定预设的新任务。显式选择的预设失败时不回退到默认项，也不尝试其他预设；第一版不做负载均衡、权重、健康路由或故障转移。

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
3. 对象存在：更新为 `succeeded` 和 `present`，写入返回的对象大小、ETag、可选 VersionId、完成时间和既有 URL。
4. Provider 明确返回对象不存在：更新为 `failed`，错误码为 `SERVICE_RESTARTED_OBJECT_NOT_FOUND`。
5. 超时、网络异常、认证异常或服务端错误：更新为 `unknown`，错误码为 `RECOVERY_PENDING`。
6. 进程内周期任务继续扫描全部 `unknown` 任务，以及超过 `STALE_UPLOAD_SECONDS` 的 `uploading` 任务，默认每 60 秒重试。

旧 revision 的 AK/SK 已失效且同一预设的当前 active revision 指向相同 Provider、Endpoint 和 Bucket 时，恢复器可以使用该 revision 再尝试一次；不得使用默认预设或其他预设替代。

### 8.5 正常上传确认

正常请求在 Provider 上传方法成功返回后必须执行一次对象元数据读取。只有对象大小与本次接收大小一致，且 ETag 等稳定身份字段已经持久化时，才将对象状态设为 `present` 并返回删除凭证。

上传成功但元数据读取超时或结果不完整时，任务进入 `unknown`、对象状态保持 `pending`，不返回删除凭证。启动和周期恢复继续使用任务绑定的 Provider revision 确认对象；恢复成功后任务可转为 `succeeded`，但不会通过任务查询接口补发删除凭证。

### 8.6 对象删除状态流

删除只使用任务绑定的不可变 `storage_config_id` 和数据库中的 `object_key`。预设被禁用、默认项被切换或配置新增 revision 均不改变删除目标。

1. 校验任务 UUID 和 `X-Delete-Token` 格式，并使用常量时间比较验证签名。
2. 读取任务及其原 storage config；只允许 `status=succeeded` 且 `object_status=present` 的任务进入删除。
3. 在短事务内执行 `present → deleting` 条件更新，写入 `delete_request_id` 和 `delete_started_at`；更新行数为 0 时按当前状态返回幂等成功或冲突。
4. 通过原配置创建 Provider，执行 `HeadObject`；比较保存的大小、ETag 和可选 VersionId。
5. 元数据不一致时恢复为 `present`，记录 `OBJECT_CHANGED` 并返回 `409`，不调用删除。
6. 有 VersionId 时删除该精确版本；否则删除任务保存的对象 Key。
7. 再次查询相同 Key 或精确 VersionId，只有明确不存在时更新为 `deleted`、写入 `deleted_at` 并返回成功。
8. Provider 明确拒绝删除时恢复为 `present` 并记录稳定错误码；超时或连接中断时进入 `delete_unknown`，绝不谎报成功。
9. 周期恢复扫描 `delete_unknown` 和超时的 `deleting`：确认不存在则转为 `deleted`，确认仍为原对象则转回 `present`，无法确认则继续保持 `delete_unknown`。

重复删除已经为 `deleted` 的任务返回 `200` 和 `already_deleted=true`，不再次请求 ZOS。删除前对象已经明确不存在时，可以记为 `deleted` 并返回 `already_absent=true`，同时保留审计日志。删除只确认 ZOS 源对象或精确版本不存在，不保证 CDN、代理或第三方缓存立即失效。

## 9. 幂等语义

调用方可以传入：

```http
Idempotency-Key: opaque-key-up-to-128-chars
```

规则：

- Header 可选，最大 128 个字符。
- 第一次出现的幂等键创建新任务。
- 幂等键同时绑定首次新任务解析得到的 `preset_key`。重放请求未携带 `X-Storage-Preset` 时始终返回原任务，不受默认项变化影响。
- 重放请求显式指定了与原任务不同的预设时返回 `409 IDEMPOTENCY_SCOPE_MISMATCH`。
- 已有任务为 `succeeded` 时，返回同一个任务、Key 和 URL，状态码为 `200`，响应头包含 `Idempotency-Replayed: true`。
- 已有任务为 `uploading` 或 `unknown` 时，返回 `409 UPLOAD_IN_PROGRESS` 并包含 `task_id`。
- 已有任务为 `failed` 时，返回 `409 IDEMPOTENCY_KEY_REUSED`；新的上传尝试使用新的幂等键。
- 幂等键绑定第一次请求意图，服务不读取完整文件来比较重复请求的内容。
- 未传幂等键时，每次请求继续生成新的任务 UUID 和对象 Key。

幂等键的唯一性通过 SQLite 唯一索引和短事务保证。

成功上传的幂等重放返回原任务和对象元数据，但 `delete_token=null`，不会补发首次响应中的删除凭证。`Idempotency-Key` 不是认证凭证，不能用来恢复删除权限。首次 `201` 丢失时，在统一内部认证或受控管理恢复能力实现前，该对象不能通过公开删除 API 删除。删除接口本身按任务和对象状态幂等，不再引入第二套删除幂等键。

## 10. API

### 10.1 上传文件

```http
POST /v1/uploads
Content-Type: multipart/form-data
X-Request-ID: optional
Idempotency-Key: optional
X-Storage-Preset: optional

file=<binary>
```

成功响应保持原字段并增加稳定对象元数据和删除凭证：

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "storage_preset": "zos-main",
  "key": "2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf",
  "url": "https://public-bucket.example.com/2026/07/28/550e8400-e29b-41d4-a716-446655440000.pdf",
  "size_bytes": 125678,
  "content_type": "application/pdf",
  "etag": "\"opaque-etag\"",
  "version_id": null,
  "delete_token": "opaque-object-delete-capability"
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
- 列表增加 `request_id`、`storage_preset`、`content_type`、`object_key`、`duration_ms`、`storage_provider`、`storage_config_revision`、`etag`、`version_id`、`object_status` 和删除结果字段。
- 列表和详情永远不返回 `delete_token`。
- Dashboard 的近期上传表直接复用该接口。

### 10.3 查询单个任务

```http
GET /v1/upload-tasks/{task_id}
```

返回完整任务字段。任务不存在时返回 `404 TASK_NOT_FOUND`。

### 10.4 删除已上传对象

```http
DELETE /v1/upload-tasks/{task_id}/object
X-Delete-Token: required
X-Request-ID: optional
```

调用方不提交 Bucket、对象 Key、URL 或 Provider 参数。服务从任务及其原 storage config 定位对象，执行元数据比对、精确版本删除和删除后确认。成功响应返回任务 ID、Key、对象状态、删除时间以及 `already_deleted` / `already_absent` 标志。

`delete_token` 只在首次上传成功的 `201` 中返回；成功上传的幂等重放返回 `delete_token=null`。任务列表、任务详情、Dashboard、日志和错误响应均不返回 token 或 token 哈希。历史 `legacy_unverified` 任务默认不具备可用删除凭证，也不能通过该接口删除。

### 10.5 健康检查

```http
GET /healthz
```

只表示进程可以响应，返回 `200`。

### 10.6 就绪检查

```http
GET /readyz
```

检查：

- 必要配置已加载。
- SQLite 可读写。
- 临时目录可写且剩余空间满足阈值。
- 启动 schema 初始化完成。
- 启动恢复扫描完成。
- 默认预设的 active Storage Provider 探测成功且未超过缓存有效期；非默认预设故障显示为 degraded，但不阻止默认上传就绪。

任一关键项失败时返回 `503` 和各依赖项状态。

### 10.7 Dashboard 数据接口

```http
GET /v1/dashboard/summary?from=...&to=...
GET /v1/dashboard/traffic?from=...&to=...&interval=hour|day
GET /v1/dashboard/logs?min_level=NOTIFY&limit=100&before_id=...
GET /v1/dashboard/storage?from=...&to=...
```

`/v1/dashboard/storage` 可通过 `preset_key` 查询指定预设，未传时使用默认预设；是否启用由该预设 active storage config 的 Provider 能力和 `enable_bucket_metrics` 决定。

### 10.8 存储设置接口

```http
GET  /v1/settings/storage/providers
GET  /v1/settings/storage/presets
POST /v1/settings/storage/presets
GET  /v1/settings/storage/presets/{preset_key}
PUT  /v1/settings/storage/presets/{preset_key}
PATCH /v1/settings/storage/presets/{preset_key}
PUT  /v1/settings/storage/default
GET  /v1/settings/storage
POST /v1/settings/storage/test
PUT  /v1/settings/storage
```

- Provider 列表接口描述支持的 Provider 类型；预设列表接口返回服务端可选目标。
- 创建预设同时保存 revision 1；第一个预设自动成为默认项。
- `PUT .../{preset_key}` 使用 `expected_revision` 更新不可变配置 revision。
- `PATCH .../{preset_key}` 使用 `expected_state_revision` 修改显示名或启用状态；`preset_key` 不可修改，预设不提供硬删除。
- `PUT .../default` 原子切换唯一默认预设；目标必须已启用。
- 原 `GET/PUT /v1/settings/storage` 保留为默认预设的兼容别名；数据库为空时，首次 PUT 创建 `default` 预设。
- 测试接口不持久化数据，可通过 `preset_key` 复用该预设已保存的凭证。
- `POST`、`PUT` 和 `PATCH` 必须使用 `application/json` 并携带 `X-Settings-Request: true`。
- 设置接口要求 `ADMIN_API_KEYS` 管理员认证，并继续保留同源与自定义 Header 防护。

所有响应时间字段使用 UTC ISO 8601。Dashboard 按 `APP_TIMEZONE` 显示。

### 10.9 局域网文件接收测试

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

Dashboard 与 API 同源，使用浏览器原生 HTTP Basic 管理员认证和本地静态资源。监控区域为只读，设置页面可以管理多个存储预设。

### 11.2 监控页面内容

监控页面包含六个区域：

1. **局域网文件接收测试**
   - 选择单个文件并提交到 `/v1/uploads/validate`。
   - 在一旁的只读文本框中显示成功或错误响应。
   - “真实上传到 ZOS”开关默认关闭；关闭时不上传、不写任务，开启时改用正式 `/v1/uploads` 并显示正式响应。

2. **服务状态**
   - 进程状态。
   - SQLite 状态。
   - 临时目录状态。
   - 默认预设、active Provider、配置 revision、存储连通性及最近探测时间。
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

设置页先列出全部存储预设，显示 `preset_key`、显示名、Provider、Endpoint 主机名、Bucket、配置 revision、启用状态、默认状态和最近测试结果，并支持新建、编辑、测试、设为默认以及启停非默认预设。选中预设后按 Provider 类型渲染表单；第一版提供 **天翼云对象存储 ZOS**：

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

- 当前 `preset_key`、预设状态 revision、Provider 和配置 revision。
- 当前 Endpoint、Bucket、public base URL 与连接参数。
- masked AK，例如 `****A1B2`。
- SK 是否已配置，永远不显示原值。
- 最近一次连接测试状态、时间和耗时。
- “测试连接”和“保存并激活新 revision”两个操作。
- 当 `public_base_url` 为空时，页面可根据 Bucket 与外网 Endpoint 建议 `https://{bucket}.{endpoint-host}`，用户仍可改为控制台显示的 Bucket 外网访问域名、CDN 或自定义域名。

保存前显示目标 Endpoint、Bucket 和新 revision 的确认信息。只有持有管理员密钥的客户端可以执行设置操作。

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

时间序列只汇总成功上传字节数，同时附带任务状态计数。幂等重放不会创建新任务，因此不会重复计入流量。对象后续删除不改变历史上传成功率或上传字节统计。

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
- `delete_started`
- `delete_succeeded`
- `delete_already_absent`
- `delete_rejected`
- `delete_pending`
- `delete_recovery_resolved`
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
- `storage_preset`
- `storage_provider`
- `storage_config_revision`

删除事件只记录任务 ID、请求 ID、Provider、配置 revision、对象 Key、对象状态和稳定错误码。日志内容执行长度限制和控制字符清洗。AK、SK、`delete_token`、请求文件内容和完整环境变量永远不进入日志。

## 14. Storage Provider 与 ZOS SDK 策略

### 14.1 Provider 边界

上传服务内部定义稳定的 Provider adapter：

- `provider_id` 与 `schema_version`：标识 adapter 及其设置结构。
- `get_settings_schema()`：返回 Dashboard 使用的 Provider 类型、非敏感字段与凭证字段定义。
- `validate_config()`：校验 Provider 专属设置与凭证 envelope。
- `create_client()`：根据已解密凭证创建 Client。
- `test_connection()`：执行低副作用连通性检查。
- `upload_file()`：上传文件。
- `head_object()`：返回大小、ETag 和可选 VersionId，用于上传确认、恢复和删除前后校验。
- `delete_object()`：按对象 Key 和可选 VersionId 删除精确目标。
- `get_metrics()`：获取可选 Provider 指标。
- `build_public_url()`：使用已保存的访问根地址构造 URL。

上传调用方只依赖本服务的 HTTP API。未来增加其他 SDK 或对象存储时，新增 adapter 和 Provider 类型，不改变 `/v1/uploads` 请求或删除接口的 Provider-neutral 语义。

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
- `DeleteObject`

上传时设置：

- active revision 固定的 Bucket。
- 生成后的对象 Key。
- 文件 Content-Type。
- canned ACL `public-read`；调用方无权覆盖。
- botocore 请求与响应 checksum 策略固定为 `when_required`，兼容 ZOS 对 `x-amz-content-sha256` 的校验。

`upload_fileobj` 本身不返回稳定对象元数据，因此成功返回后使用 `HeadObject` 获取 ETag、大小和可选 VersionId。删除时若存在 VersionId，必须把它传给 `DeleteObject`，并针对同一版本执行删除后确认；无 VersionId 时才按 Key 删除。

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
| 400 | `STORAGE_PRESET_INVALID` | 预设 key 格式错误 |
| 403 | `DELETE_TOKEN_INVALID` | 删除凭证缺失、格式错误或与任务不匹配 |
| 404 | `TASK_NOT_FOUND` | 任务不存在 |
| 404 | `STORAGE_PRESET_NOT_FOUND` | 指定预设不存在 |
| 409 | `CONFIG_REVISION_CONFLICT` | `expected_revision` 与当前 active revision 不一致 |
| 409 | `PRESET_STATE_CONFLICT` | 预设状态 revision 冲突或试图禁用默认项 |
| 409 | `DEFAULT_PRESET_CONFLICT` | 默认项切换并发冲突或目标未启用 |
| 409 | `STORAGE_PRESET_DISABLED` | 上传显式指定了已禁用预设 |
| 409 | `IDEMPOTENCY_SCOPE_MISMATCH` | 幂等键原任务绑定了其他预设 |
| 409 | `UPLOAD_IN_PROGRESS` | 同一幂等键对应任务仍在处理或待确认 |
| 409 | `IDEMPOTENCY_KEY_REUSED` | 同一幂等键已对应失败任务 |
| 409 | `OBJECT_NOT_DELETABLE` | 任务状态、对象状态或历史元数据不满足严格删除条件 |
| 409 | `OBJECT_CHANGED` | 删除前对象大小、ETag 或 VersionId 与上传记录不一致 |
| 409 | `DELETE_IN_PROGRESS` | 同一对象已有删除请求正在执行或等待确认 |
| 413 | `FILE_TOO_LARGE` | 文件超过上限 |
| 500 | `DATABASE_ERROR` | SQLite 异常 |
| 500 | `SETTINGS_STORAGE_ERROR` | 设置加密、写入或 Client 切换失败 |
| 500 | `INTERNAL_ERROR` | 未分类内部异常 |
| 502 | `UPLOAD_FAILED` | Storage Provider 明确拒绝或上传失败 |
| 502 | `STORAGE_TIMEOUT` | Storage Provider 请求超时 |
| 502 | `DELETE_FAILED` | Storage Provider 明确拒绝删除 |
| 502 | `STORAGE_ENDPOINT_UNREACHABLE` | 设置测试无法连接 Endpoint |
| 502 | `STORAGE_CREDENTIALS_REJECTED` | 设置测试中的 AK/SK 被拒绝 |
| 502 | `STORAGE_BUCKET_UNAVAILABLE` | Bucket 不存在或当前凭证不可访问 |
| 503 | `UPLOAD_CAPACITY_EXCEEDED` | 上传并发槽位已满 |
| 503 | `STORAGE_NOT_CONFIGURED` | 显式预设尚未激活配置 |
| 503 | `STORAGE_DEFAULT_NOT_CONFIGURED` | 没有可用默认预设 |
| 503 | `NOT_READY` | 就绪检查失败 |
| 503 | `STORAGE_METRICS_UNAVAILABLE` | 可选 Storage Provider 原生指标暂时不可用 |

已创建任务的错误响应包含 `task_id`。错误消息用于人类阅读，程序逻辑判断 `code`。

删除调用在 Provider 返回超时、连接中断或不确定响应时返回 `202`，对象状态为 `delete_unknown`，并由恢复器继续确认；不能把未知结果映射成删除成功。

## 16. 配置

### 16.1 部署级不可变配置

以下配置继续由容器环境或 Secret 注入，Dashboard 不可修改：

```text
# 凭证加密
SETTINGS_ENCRYPTION_KEY
ADMIN_API_KEYS

# 上传调用方；留空时兼容归入 legacy
CLIENT_API_KEYS=

# 首次启动导入
BOOTSTRAP_STORAGE_FROM_ENV=true

# 上传
MAX_UPLOAD_BYTES=209715200
MAX_REQUEST_BODY_BYTES=213909504
MAX_CONCURRENT_UPLOADS=4
UPLOAD_RATE_LIMIT_PER_MINUTE=60
CLIENT_MAX_OBJECTS=10000
CLIENT_MAX_BYTES=1099511627776
S3_MULTIPART_THRESHOLD_BYTES=16777216
S3_MULTIPART_CHUNK_BYTES=16777216
S3_TRANSFER_MAX_CONCURRENCY=2
TEMP_DIR=/data/tmp
TEMP_MIN_FREE_BYTES=1073741824

# 数据库与保留
DATABASE_PATH=/data/db/zos-upload.db
SQLITE_BUSY_TIMEOUT_MS=5000
TASK_RETENTION_DAYS=180
LOG_RETENTION_DAYS=30
LOG_MAX_ROWS=100000

# 服务
APP_TIMEZONE=Asia/Shanghai
STORAGE_PROBE_INTERVAL_SECONDS=30
STORAGE_PROBE_MAX_AGE_SECONDS=60
RECOVERY_RETRY_SECONDS=60
RECOVERY_INITIAL_BUDGET_SECONDS=5
RECOVERY_BATCH_SIZE=25
RECOVERY_MAX_CONCURRENCY=4
RECOVERY_CONNECT_TIMEOUT_SECONDS=3
RECOVERY_READ_TIMEOUT_SECONDS=10
RECOVERY_MAX_ATTEMPTS=1
STALE_UPLOAD_SECONDS=900
PROVIDER_CACHE_MAX_ENTRIES=128
DASHBOARD_ENABLED=true
```

`SETTINGS_ENCRYPTION_KEY` 是 URL-safe base64 Fernet key，由部署平台作为 Secret 提供。该 Key 缺失或无法解密现有配置时，服务启动失败并输出不含敏感内容的错误。

### 16.2 可选的首次启动 ZOS 导入

为了兼容最初的环境变量部署，以下变量只用于数据库中没有任何 storage preset 时创建 `preset_key=default` 及 revision 1：

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

- `BOOTSTRAP_STORAGE_FROM_ENV=true` 且五个必要字段完整时，服务测试连接、加密凭证并创建启用的默认预设及 revision 1。
- 数据库已有任意预设后，环境变量不再自动覆盖 Dashboard 设置。
- 导入完成后，该预设的 active storage config 是默认上传的运行时来源。
- 环境中的 AK/SK 仍不得进入日志。

### 16.3 未配置状态

数据库没有启用的默认预设或默认预设没有 active storage config 时：

- 服务进程和 Dashboard 正常启动。
- `/healthz` 返回 `200`。
- `/readyz` 返回 `503 STORAGE_DEFAULT_NOT_CONFIGURED`。
- 未指定预设的 `POST /v1/uploads` 返回 `503 STORAGE_DEFAULT_NOT_CONFIGURED`，不创建任务。
- `/dashboard/settings` 可用于完成首次配置。

### 16.4 Dashboard 管理的存储预设与 ZOS 设置

Dashboard 可以管理多个预设。每个预设固定包含：

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
- 每个预设独立维护 revision；每次保存都创建新 revision，不原地修改历史记录。
- 保存失败或连接测试失败时，旧 active revision 保持不变。
- `preset_key` 创建后不可修改；禁用只阻止新上传，历史任务的恢复和删除继续使用原配置。
- 不实现自动选路、轮询、权重、负载均衡或失败回退。
- ZOS 凭证至少具备执行 HeadBucket、PutObject、设置 `public-read` 对象 ACL、multipart upload、HeadObject 和 DeleteObject 所需的目标 Bucket 权限；Bucket 启用版本控制时还必须具备读取和删除精确版本所需权限。启用 Bucket 指标时再增加对应统计读取权限。
- 优先使用专用 IAM 用户或服务账号的 AK/SK，并将权限范围限制在目标 Bucket；Dashboard 不管理 IAM、Bucket ACL 或 Bucket Policy。

## 17. 保留与维护

进程内维护协程在启动时和每 24 小时执行一次：

- 删除超过 `LOG_RETENTION_DAYS` 的普通日志；`object_delete_*` 删除审计永久保留。
- 普通日志超过 `LOG_MAX_ROWS` 时按最旧记录继续裁剪；删除审计不计入该上限。
- 只删除超过 `TASK_RETENTION_DAYS` 且对象状态为 `absent` 或 `deleted` 的终态任务。
- 无论年龄多久，保留所有 `uploading`、`unknown`、`pending`、`present`、`legacy_unverified`、`deleting` 和 `delete_unknown` 任务，避免产生无法通过服务定位的孤儿对象。
- 保留被任何任务引用的 storage config revision。
- 存储预设不通过 HTTP 硬删除。
- 只有 inactive revision 已无任务引用且超过 `TASK_RETENTION_DAYS` 时才允许删除。
- 执行 `PRAGMA optimize`。
- 记录 `maintenance_completed` 日志。

服务仍不提供通过 HTTP 删除任务台账或日志的接口；对象删除只更新审计状态。

## 18. 部署

- 单 Docker 容器运行 FastAPI、Dashboard 和维护协程。
- 正式局域网部署继续使用内网 HTTPS 反向代理或隔离管理网络；应用层管理员认证默认启用，代理、访问日志和 APM 均禁止记录凭证与设置请求体。
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
- 默认项切换后，未指定预设的幂等重放仍返回原任务；显式改用其他预设返回 `IDEMPOTENCY_SCOPE_MISMATCH`。
- 上传容量达到上限时返回 `503`，查询接口继续可用。
- 本地统计的数量、字节数、成功率、平均耗时和 P95 计算正确。
- `NOTIFY` 及以上日志入库，较低级别只输出到 stdout。
- 日志筛选、分页、保留和裁剪正确。
- Dashboard 对文件名和日志内容进行 HTML 转义。
- Provider preset、`provider_schema_version`、当前设置、测试连接和保存激活接口结构正确。
- 多预设的创建、查询、配置更新、启停和默认项原子切换正确。
- `preset_key` 格式、唯一性、不可修改性以及默认预设不可直接禁用的约束正确。
- 显式预设、默认预设、未知预设、禁用预设和未配置默认预设的上传路由正确。
- 显式预设失败时不回退到默认项或其他预设。
- 首次配置以及 Provider 或 Endpoint 变化时必须提交完整 AK/SK；同一 Provider 和 Endpoint 下更新时省略凭证可以保留旧值。
- GET 设置接口、HTML、日志和错误响应不包含 AK/SK 明文或密文。
- `expected_revision` 冲突返回 `409 CONFIG_REVISION_CONFLICT`。
- 新配置测试失败时 active revision 保持不变。
- 配置激活期间的在途上传继续使用旧 revision，新任务使用新 revision。
- 使用测试用第二 Provider adapter 验证 `/v1/uploads`、任务查询和错误结构无需变化。
- 上传成功后保存大小、ETag 和可选 VersionId，并仅在首次 `201` 返回对象级 `delete_token`。
- 删除 token 缺失、篡改、跨任务使用和日志泄露测试。
- 删除前元数据一致、大小不一致、ETag 不一致和 VersionId 不一致测试。
- 并发删除只能有一个请求进入 Provider；重复删除返回幂等成功。
- 历史 `legacy_unverified` 任务默认禁止 API 删除。

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
- 每个预设的 active revision 切换时终止进程，重启后每个预设保持单一 active revision。
- 默认项切换事务失败时仍保持唯一、启用的旧默认预设。
- 未完成 multipart upload 被生命周期规则清理。
- 删除请求在 Provider 调用前、调用中、调用后及数据库提交前终止。
- 删除超时进入 `delete_unknown`，重启后能够恢复为 `deleted` 或 `present`。

### 19.3 真实 ZOS 集成测试

- 上传 TXT、PDF、图片和接近 200 MiB 的文件。
- 配置两个不同 Bucket 的预设，分别显式上传并验证任务绑定到正确预设和对象。
- 上传超过 multipart 阈值的文件。
- 校验 ZOS 对象、Content-Type、大小、对象 Key、URL 和任务记录一致。
- 从第三方公网环境实际访问返回 URL。
- 验证 `HeadObject` 恢复逻辑。
- 验证非版本化 Bucket 的 Key 删除和删除后 404。
- Bucket 启用版本控制时，验证只删除上传时记录的精确 VersionId，不误删更新版本。
- 删除前外部替换对象时返回 `OBJECT_CHANGED`。
- 开启扩展统计时验证 Bucket Statistics 和 Storage Info。
- 使用控制台中记录的 Endpoint、Bucket 外网访问域名、AK 和 SK 完成 Dashboard 首次配置。
- “测试连接”能够识别正确配置、错误 Endpoint、错误 AK/SK 和不可访问 Bucket。

### 19.4 Dashboard 验收

- 局域网浏览器使用管理员 HTTP Basic 凭证打开 `/dashboard`；匿名访问返回 `401`。
- 24 小时、7 天和 30 天上传流量与 SQLite 任务数据一致。
- 成功率、状态数量、平均耗时和 P95 正确。
- 近期任务可以查看对象 Key、URL 和错误码。
- `NOTIFY`、`WARNING`、`ERROR`、`CRITICAL` 日志可查看和筛选。
- Provider 原生统计不可用时，本地统计、任务列表和日志保持可用。
- `/dashboard/settings` 能列出多个预设，显示 masked 凭证，测试连接、保存新 revision、切换默认项和启停非默认项。
- 首次未配置时 Dashboard 可访问，未指定预设的上传接口返回 `STORAGE_DEFAULT_NOT_CONFIGURED`。
- 设置修改写入 NOTIFY 日志，日志不包含凭证。
- 页面在服务持续上传时稳定轮询，无明显数据库锁冲突。

## 20. 完成标准

- 单次调用完成“创建任务、接收临时文件、上传 ZOS、持久化结果、返回 URL”。
- 调用方可显式选择启用预设；未指定时稳定使用唯一默认预设。
- 服务不在请求结束后保留文件本体。
- 上传成功与数据库状态具有可恢复的一致性。
- 调用方可以通过幂等键避免网络超时后的重复对象。
- 新上传对象可以凭任务级删除凭证严格、幂等地删除，并保留数据库审计记录。
- 删除目标只能由任务及原 storage config 定位，调用方不能指定 Bucket、Key 或 VersionId。
- 任务列表和任务详情支持故障排查。
- Dashboard 显示上传流量、任务状态、服务状态和近期任务。
- Dashboard 显示并筛选 `NOTIFY` 及以上日志。
- Dashboard 可以使用天翼云 ZOS 预设测试、保存和激活 SDK Endpoint、Bucket、访问根地址、AK、SK 与连接参数。
- Dashboard 可以配置多个固定绑定 Endpoint 与 Bucket 的存储预设，并管理唯一默认项。
- 上传 API 保持 Provider 无关，后续新增其他对象存储 adapter 时调用方契约不变。
- 配置切换使用不可变 revision，在途上传和恢复任务可以定位原配置。
- SQLite 在并发上传、Dashboard 轮询和设置切换下保持稳定。
- 服务只在受控局域网暴露；上传数据面保持兼容，管理控制面要求管理员认证。
- AK/SK 只以加密形式持久化，不暴露给调用方、日志、页面或 GET 响应。

## 21. 实施顺序

1. `[已完成]` 根据本文同步更新 `API.md` v3 删除与多预设接口、字段和错误码。
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
15. 升级 schema v2，扩展对象元数据和删除状态，实现签名删除凭证、Provider 删除、恢复与严格测试。
16. 升级 schema v3，迁移现有配置到 `default` 预设，实现多预设设置 API、上传路由、Dashboard 和测试。

## 22. 已确认决策

1. 服务只在受控局域网运行；上传数据面不要求调用方认证，管理控制面要求管理员密钥。
2. API 与 Dashboard 共用 FastAPI 服务和同一局域网端口。
3. Dashboard 监控区域只读，设置页面可以管理多个 storage preset。
4. 只有持有管理员密钥的客户端可以执行设置、日志、Dashboard 和完整任务查询操作。
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
22. 普通日志默认保留 30 天或最多 100000 条，`object_delete_*` 删除审计永久保留，任务默认保留 180 天。
23. 服务不提供对象下载、更新、列表或 Bucket 权限管理能力。
24. 删除只面向本服务新上传且元数据完整的对象；目标由任务数据库定位，调用方不能传入任意 Bucket、Key 或 VersionId。
25. 删除必须同时提供任务 ID 和对象级 `delete_token`；明文 token 只返回一次，数据库只存哈希，token 不进入查询接口、Dashboard、日志或 URL。
26. 上传成功后通过 `HeadObject` 保存大小、ETag 和可选 VersionId；版本化 Bucket 删除精确版本。
27. 删除不移除任务记录；状态、错误和删除时间永久留在 SQLite 中。
28. v1 历史成功任务迁移为 `legacy_unverified`，默认不开放 API 删除。
29. 每个存储预设固定绑定一个 Provider、Endpoint、Bucket 和公网访问根地址；同一 Endpoint 的不同 Bucket 使用不同预设。
30. 调用方通过可选 `X-Storage-Preset` 选择预设，未指定时使用唯一默认项；不能提交原始 Endpoint 或 Bucket。
31. 第一版不实现负载均衡、权重、健康路由、自动故障转移或失败回退。
32. 显式选择的预设失败时直接报错，不回退到默认项。
33. 预设可以启用或禁用但不硬删除；禁用只影响新上传，历史任务恢复与删除仍使用原 `storage_config_id`。
34. 现有单配置迁移为启用的 `default` 预设，任务引用和配置 revision 保持不变。

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
- [x] 首次上传成功响应增加大小、Content-Type、ETag、可选 VersionId 和一次性返回的对象级 `delete_token`。
- [x] 增加 `DELETE /v1/upload-tasks/{task_id}/object`，禁止调用方指定删除目标。
- [x] 增加对象删除状态、严格元数据校验、精确版本删除、幂等和不确定结果恢复语义。
- [x] 增加 `DELETE_TOKEN_INVALID`、`OBJECT_NOT_DELETABLE`、`OBJECT_CHANGED`、`DELETE_IN_PROGRESS` 和 `DELETE_FAILED`。
- [x] 明确历史 `legacy_unverified` 任务默认不可删除。
- [x] 增加可选 `X-Storage-Preset`、默认预设解析和稳定 `storage_preset` 响应字段。
- [x] 增加多预设列表、创建、详情、配置更新、状态更新和默认项切换接口。
- [x] 明确预设禁用、默认切换、幂等重放、任务恢复和删除之间的隔离语义。
- [x] 增加 `STORAGE_PRESET_INVALID`、`STORAGE_PRESET_NOT_FOUND`、`STORAGE_PRESET_DISABLED`、`PRESET_STATE_CONFLICT`、`DEFAULT_PRESET_CONFLICT` 和 `IDEMPOTENCY_SCOPE_MISMATCH`。

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
