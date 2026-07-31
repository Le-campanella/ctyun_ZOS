# ctyun_ZOS 代码审查与 v3 实施建议

> 文档用途：交给编码 Agent，作为后续修改仓库的执行依据。  
> 审查对象：`Le-campanella/ctyun_ZOS`，`master` 分支。  
> 审查基线：提交 `fd3f65b6bd15f9a0c1060fcba8226903501e79a1`。  
> 目标规格：`PLAN.md` v6、`API.md` v3。  
> 当前实现基线：`WORKLOG.md` 记录的 `PLAN.md` v4、`API.md` v1。  
> 审查方式：静态代码审查；仓库记录显示已有自动测试和真实 ZOS 上传验证，本次审查未在独立环境中重新运行测试。

---

## 0. Agent 执行要求

请先完整阅读以下文件，再修改代码：

```text
docs/PLAN.md
docs/API.md
README.md
WORKLOG.md
app/config.py
app/database.py
app/providers.py
app/runtime.py
app/main.py
app/eventlog.py
app/security.py
tests/test_core.py
tests/test_api.py
Dockerfile
compose.yaml
deploy.sh
```

执行过程中遵守以下规则：

1. **先冻结当前可用契约，再实施 v3。**
2. **先完成数据库迁移并在真实数据库副本上验证，再开发多预设和删除能力。**
3. **每个阶段独立提交，避免把 schema 迁移、API 重构、Dashboard 重写和删除能力混在一个提交中。**
4. **任何时候都不能在日志、API、HTML、JavaScript、数据库非加密字段、异常信息或测试输出中暴露 AK、SK、`SETTINGS_ENCRYPTION_KEY`、`delete_token` 或其哈希。**
5. **保持单容器、单进程、单实例、SQLite 的既定架构，除非 `PLAN.md` 明确更新。**
6. **保持局域网无统一认证的边界；不要自行加入用户、登录、角色、JWT、OAuth 或 API Key 系统。**
7. **保持上传 Provider 无关；调用方不能直接提交 Endpoint、Bucket、Object Key、ACL 或任意 URL。**
8. **保持历史配置 revision 不可变，历史任务恢复与删除必须使用任务绑定的原 `storage_config_id`。**
9. **任何迁移都要可重复测试、可检测失败，并保留升级前数据库备份。**
10. **修改完成后同步更新 `README.md`、`WORKLOG.md`、OpenAPI/接口说明和测试。**

---

## 1. 当前实现判断

当前仓库已经形成一个可工作的单存储配置 MVP，主要能力包括：

- FastAPI 文件上传 API。
- 单一 active Storage Config。
- 天翼云 ZOS / S3 Provider adapter。
- AK/SK Fernet 加密保存。
- SQLite 上传任务台账。
- `Idempotency-Key`。
- 临时文件和 200 MiB 上限。
- 上传并发限制。
- 上传状态恢复。
- Dashboard、统计和结构化日志。
- 非 root Docker 镜像。
- Docker Compose 持久卷。
- SSH 部署脚本。
- 真实 ZOS PDF 上传和公网访问验证。

当前 Python 代码仍然实现的是单一 active Storage Config 模型。`PLAN.md v6` 和 `API.md v3` 已经设计了多预设、对象元数据、删除凭证和严格删除能力，这些属于下一版目标。

因此，当前最重要的工作是消除以下三者之间的版本漂移：

```text
README.md            当前 MVP 使用说明
当前 Python 代码      当前实际行为
PLAN.md / API.md      下一版目标行为
```

在 v3 完成前，调用方不能把 `API.md v3` 当作已经上线的生产契约。

---

## 2. 必须保持的架构约束

后续实现应保持以下不变量：

### 2.1 网络和访问边界

- 服务只运行在受控局域网或内部容器网络。
- API 和 Dashboard 共用一个局域网端口。
- 不建立统一调用方认证。
- CORS 默认关闭。
- 设置写请求继续要求：
  - `Content-Type: application/json`
  - `X-Settings-Request: true`
  - 浏览器请求必须同源。
- 正式部署使用内网 HTTPS，或将设置页限制在隔离管理 VLAN / 管理主机。

### 2.2 文件生命周期

- 服务不长期保存上传文件本体。
- 文件只在当前请求中存在于受控临时文件。
- 请求成功、失败、客户端断开或异常后都必须清理临时文件。
- 单文件上限继续为 `209715200` 字节。
- 请求体上限继续独立限制。
- 对象 Key 继续由服务生成，调用方不能指定。

### 2.3 Provider 边界

- 上传 API、任务 API 和删除 API保持 Provider 无关。
- 第一版 Provider 为 `ctyun_zos`。
- Provider 通过 `provider + provider_schema_version` 选择。
- 历史配置 revision 对应的 adapter 和 schema parser 必须继续保留。
- 多预设只提供稳定路由选择，不提供权重、轮询、负载均衡、自动故障转移或失败回退。

### 2.4 对象和配置一致性

- 新任务创建时固定绑定一个 `storage_config_id`。
- 设置更新不改变在途任务和历史任务。
- 默认预设切换只影响之后未显式选择预设的新任务。
- 幂等重放必须返回原任务和原对象。
- 删除目标只能由任务记录和原配置 revision 决定。

### 2.5 ZOS 已验证兼容行为

以下行为已用于解决真实 ZOS 兼容问题，后续重构需要保留：

```python
request_checksum_calculation="when_required"
response_checksum_validation="when_required"
```

正式上传继续设置：

```python
ACL="public-read"
```

是否能匿名访问仍由 Bucket Policy、账号权限和 ZOS 侧策略共同决定。

---

## 3. P0：必须优先解决的问题

## 3.1 P0-1：冻结当前契约，标明 v3 尚未发布

### 当前问题

`API.md v3` 已经描述：

- `X-Storage-Preset`
- 多存储预设 API
- `storage_preset`
- ETag
- VersionId
- `delete_token`
- 对象状态
- 删除接口

当前代码仍然返回旧版上传响应：

```json
{
  "task_id": "...",
  "key": "...",
  "url": "..."
}
```

### 修改要求

建议采用以下文档结构之一：

#### 方案 A：当前契约和目标契约分离

```text
API.md                    当前已实现契约
docs/API-v3-draft.md      v3 目标契约
PLAN.md                   v3 实施方案
```

#### 方案 B：保留现有文件名并增加状态头

在 `API.md` 顶部增加：

```yaml
status: unreleased
target_version: v3
implementation_baseline: API v1
implementation_status: partial
```

同时在 `README.md` 明确当前运行版本和目标版本。

### 代码要求

- 为所有公开 JSON 接口增加 Pydantic request / response model。
- 使用 `response_model` 生成 OpenAPI。
- 增加契约测试，校验真实响应包含必填字段并排除敏感字段。
- v3 新增字段尽量采用向后兼容的“新增字段”方式。
- 旧字段 `task_id`、`key`、`url` 在 v3 中继续保留。

### 验收标准

- README、API 文档和运行代码对“当前已经实现的功能”描述一致。
- OpenAPI 能准确展示当前响应。
- 调用方可以明确区分已发布接口和目标接口。
- CI 中存在至少一组 response schema 测试。

---

## 3.2 P0-2：完成 SQLite schema v1 → v2 → v3 迁移

### 当前问题

当前 `SCHEMA_VERSION = 1`，业务表为：

```text
storage_configs
upload_tasks
service_logs
```

当前模型只允许全局唯一 active config，无法表达多个预设。`upload_tasks` 也缺少删除所需的对象元数据和状态。

### 目标模型

业务表至少包括：

```text
storage_presets
storage_configs
upload_tasks
service_logs
```

建议额外增加：

```text
object_delete_events
```

用于 append-only 删除审计。

### v2 迁移要求：对象元数据与删除状态

为 `upload_tasks` 增加：

```text
etag
version_id
delete_token_hash
object_status
delete_request_id
delete_error_code
delete_started_at
deleted_at
```

历史数据处理：

- 历史 `succeeded` 任务：
  - `object_status='legacy_unverified'`
  - `delete_token_hash=NULL`
  - 不自动生成删除凭证
  - 默认禁止通过删除 API 删除
- 历史 `uploading` / `unknown`：
  - 根据恢复逻辑保持 `pending`
- 历史 `failed`：
  - 根据是否能够确认对象存在设置 `absent` 或保守状态

### v3 迁移要求：多存储预设

新增 `storage_presets`。

创建初始预设：

```text
preset_key=default
display_name=默认 ZOS
enabled=true
is_default=true
state_revision=1
```

然后：

- 给现有所有 `storage_configs` 增加并填充同一个 `preset_id`。
- 保持已有 `storage_configs.id` 不变。
- 保持已有 revision 数字不变。
- 保持所有 `upload_tasks.storage_config_id` 不变。
- 将 revision 唯一约束调整为 `(preset_id, revision)`。
- active 唯一约束调整为“每个 preset 最多一个 active config”。

### 迁移实现要求

在 `app/database.py` 中：

- 将 schema 初始化拆分为明确版本迁移函数。
- 使用 `PRAGMA user_version`。
- 每次迁移在事务中执行。
- 迁移失败时事务回滚。
- 迁移完成后执行完整性检查。
- 禁止通过删除旧表后无校验地重建数据。
- 对真实数据库迁移前创建备份。

建议结构：

```python
def initialize(self) -> None:
    version = self._read_user_version()
    if version == 0:
        self._create_schema_v3()
    elif version == 1:
        self._migrate_v1_to_v2()
        self._migrate_v2_to_v3()
    elif version == 2:
        self._migrate_v2_to_v3()
    elif version == 3:
        self._verify_schema_v3()
    else:
        raise RuntimeError(...)
```

### 数据库备份要求

在部署升级前执行 SQLite Online Backup 或受控文件备份，至少保留：

```text
zos-upload.db.pre-v3-<timestamp>
```

备份需要包括 WAL 中已提交数据。优先使用 SQLite Backup API。

### 完整性检查

迁移后检查：

- `PRAGMA integrity_check` 返回 `ok`。
- 恰好一个启用的默认预设。
- 每个 preset 最多一个 active config。
- 每个 storage config 都关联有效 preset。
- 每个 task 都关联有效 storage config。
- 历史 task ID、config ID 和 revision 不变。
- 历史成功任务没有删除 token。
- 历史成功任务为 `legacy_unverified`。
- 旧数据查询结果数量一致。

### 验收标准

- 空数据库可直接创建 v3 schema。
- v1 数据库副本可以无数据丢失升级到 v3。
- v2 数据库副本可以升级到 v3。
- 升级中途注入异常时事务回滚。
- 重复启动不会重复迁移或破坏数据。
- 远程实际数据库副本迁移演练通过后才允许部署正式版本。

---

## 3.3 P0-3：升级上传成功语义

### 当前问题

当前成功路径为：

```text
provider.upload_file()
→ 使用本地读取大小
→ 数据库直接标记 succeeded
→ 返回 201
```

正常上传成功后没有执行 `HeadObject`，因此没有确认：

- 远端对象实际存在。
- 远端大小一致。
- ETag。
- VersionId。
- 远端 Content-Type。

### 目标流程

```text
1. 解析并固定 storage preset 和 storage config revision
2. 创建 uploading task
3. 接收文件到唯一临时文件
4. Provider 上传
5. HeadObject
6. 校验远端对象大小
7. 保存 ETag、VersionId、对象状态
8. 生成 delete token
9. 只保存 token SHA-256
10. 单个事务更新任务为 succeeded/present
11. 返回首次 201
```

### Provider 数据结构

建议增加：

```python
@dataclass(frozen=True)
class ObjectMetadata:
    size_bytes: int
    etag: str | None
    version_id: str | None
    content_type: str | None
    last_modified: str | None
```

Provider 接口：

```python
class StorageProvider(ABC):
    def upload_file(
        self,
        fileobj: BinaryIO,
        object_key: str,
        content_type: str,
    ) -> None: ...

    def head_object(
        self,
        object_key: str,
        version_id: str | None = None,
    ) -> ObjectMetadata | None: ...

    def delete_object(
        self,
        object_key: str,
        version_id: str | None = None,
    ) -> None: ...
```

### 上传响应

首次成功：

```json
{
  "task_id": "...",
  "storage_preset": "default",
  "key": "...",
  "url": "...",
  "size_bytes": 125678,
  "content_type": "application/pdf",
  "etag": "\"opaque-etag\"",
  "version_id": null,
  "delete_token": "opaque-secret"
}
```

幂等重放：

```json
{
  "task_id": "...",
  "storage_preset": "default",
  "key": "...",
  "url": "...",
  "size_bytes": 125678,
  "content_type": "application/pdf",
  "etag": "\"opaque-etag\"",
  "version_id": null,
  "delete_token": null
}
```

### delete token 设计

默认设计：

- 使用密码学安全随机数生成 256-bit URL-safe token。
- 数据库只保存 `SHA-256(token)`。
- 验证时使用常量时间比较。
- 只在首次 `201` 返回明文。
- 不进入任务详情、列表、Dashboard、日志、错误对象或 URL。

需要在文档中明确首次响应丢失的语义。推荐采用调用方生成 token 的增强方案：

```http
X-Delete-Token: caller-generated-256-bit-secret
```

服务只保存哈希。这样首次 `201` 丢失时，调用方仍然拥有原 token。若保持服务端生成方案，需要明确“首次响应丢失会永久失去 API 删除能力”。

### 验收标准

- `201` 前必定完成一次 HeadObject。
- 远端大小不一致时不能返回成功。
- ETag 和 VersionId 被持久化。
- delete token 只在首次成功响应出现。
- 幂等重放不补发 token。
- 任意日志和错误响应都不包含 token。
- 上传成功后数据库失败时任务保留为可恢复状态。
- 恢复成功后不会为历史任务补发 token。

---

## 3.4 P0-4：将 Runtime 重构为多预设快照模型

### 当前问题

当前 Runtime 只维护：

```python
self._active_record
self._active_provider
```

这无法支持：

- 多个 preset。
- 每个 preset 独立 active revision。
- 默认 preset。
- 显式 `X-Storage-Preset`。
- 历史配置恢复和删除。

### 目标运行时模型

建议使用：

```python
@dataclass(frozen=True)
class StorageSnapshot:
    preset_id: str
    preset_key: str
    storage_config_id: str
    revision: int
    provider_id: str
    provider_schema_version: int
    provider: StorageProvider

presets_by_key: dict[str, PresetSnapshot]
active_by_preset_id: dict[str, StorageSnapshot]
providers_by_config_id: dict[str, StorageProvider]
default_preset_key: str | None
```

### 上传解析顺序

顺序必须固定：

```text
1. 校验 Idempotency-Key
2. 查询已有幂等任务
3. 已有任务：
   - 未传 preset 或传入原 preset：按原任务重放/冲突
   - 显式传入其他 preset：IDEMPOTENCY_SCOPE_MISMATCH
4. 新任务：
   - 校验 X-Storage-Preset 格式
   - 显式指定：查找对应启用 preset
   - 未指定：查找唯一启用默认 preset
5. 固定该 preset 当前 active storage config
6. 创建任务
```

### 预设状态要求

- `preset_key` 创建后不可修改。
- 第一个 preset 自动成为默认。
- 存在可上传 preset 时必须有且仅有一个启用默认 preset。
- 默认 preset 不能直接禁用。
- 禁用只阻止新上传。
- 历史恢复和删除继续使用原配置。
- 设置某个 preset 的新 revision 不影响其他 preset。

### 兼容接口

保留：

```text
GET /v1/settings/storage
POST /v1/settings/storage/test
PUT /v1/settings/storage
```

它们作为默认 preset 的兼容别名。

新增：

```text
GET  /v1/settings/storage/presets
POST /v1/settings/storage/presets
GET  /v1/settings/storage/presets/{preset_key}
PUT  /v1/settings/storage/presets/{preset_key}
PATCH /v1/settings/storage/presets/{preset_key}
PUT  /v1/settings/storage/default
```

### 验收标准

- 两个不同 Bucket 的 preset 可以分别上传。
- 未传 Header 时使用唯一默认 preset。
- 显式 preset 不存在、已禁用或未配置时返回准确错误码。
- 显式 preset 失败时不回退到默认 preset。
- 默认切换不影响在途任务和历史任务。
- 幂等重放始终返回原 preset。
- 对已有幂等任务显式传其他 preset 返回 `IDEMPOTENCY_SCOPE_MISMATCH`。

---

## 4. P1：高优先级可靠性问题

## 4.1 P1-1：消除双份临时文件

### 当前问题

当前流程先使用：

```python
await request.form(...)
```

Starlette 会将文件写入 `UploadFile.file`。随后代码又创建 `SpooledTemporaryFile` 并完整复制一次。

峰值磁盘占用可能接近：

```text
MAX_CONCURRENT_UPLOADS × MAX_UPLOAD_BYTES × 2
```

当前就绪空间阈值按约 `× 1.2` 估算，可能低估真实使用量。

### 推荐修改

短期：

- 直接使用 `UploadFile.file` 作为 Provider 上传源。
- 在该文件对象上获取大小、复位指针并上传。
- 删除第二份 spool。

长期：

- 使用受控流式 multipart 解析。
- 边接收边写入唯一临时文件。
- 同时执行大小计数、客户端断开检测和临时盘错误处理。

### 验收标准

- 一个请求生命周期内只保留一份完整临时文件。
- 4 个接近 200 MiB 并发上传时不会因为双份文件耗尽空间。
- 临时目录空间计算与真实峰值一致。
- 请求结束后没有残留临时文件。

---

## 4.2 P1-2：避免 SQLite 阻塞事件循环

### 当前问题

Provider 网络调用已经进入线程池，SQLite 方法仍直接在 async route 中执行。`BEGIN IMMEDIATE` 配合 5 秒 busy timeout 时，可能阻塞 FastAPI 事件循环。

### 推荐修改

优先采用：

```python
await anyio.to_thread.run_sync(database_method, ...)
```

覆盖：

- task create/update/query
- settings transaction
- preset transaction
- logs
- dashboard summary
- maintenance
- recovery updates

保持每次调用独立连接。

### PRAGMA 要求

每次 `connect()` 都执行：

```sql
PRAGMA busy_timeout=...
PRAGMA foreign_keys=ON
PRAGMA synchronous=NORMAL
```

`journal_mode=WAL` 可以在初始化时设置并验证。

### 验收标准

- SQLite 写锁等待时 `/healthz` 和静态 Dashboard 仍能快速响应。
- 多个上传和 Dashboard 轮询不会阻塞事件循环。
- 所有数据库调用均有一致 PRAGMA。
- 增加锁竞争测试。

---

## 4.3 P1-3：为后台任务增加 supervisor 和健康状态

### 当前问题

以下循环缺少循环级异常保护：

```text
_probe_loop
_recovery_loop
_maintenance_loop
```

一次未捕获异常可能导致对应后台任务永久退出。

### 修改要求

每个循环使用：

```python
while not stopped:
    try:
        await operation()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log CRITICAL
        mark task degraded
        await sleep(backoff)
```

Runtime 保存：

```python
background_status = {
    "probe": {...},
    "recovery": {...},
    "maintenance": {...},
}
```

`/readyz` 展示这些状态。

### 验收标准

- 注入一次数据库异常后后台任务会重试。
- 后台任务永久故障时 `/readyz` 返回 degraded / 503。
- 后台任务异常会写 CRITICAL 日志。
- 取消任务时能够干净退出。

---

## 4.4 P1-4：修正任务保留策略

### 当前问题

当前维护逻辑会删除过期的所有 `succeeded` 和 `failed` 任务。对象可能仍然存在于 ZOS 中，从而丢失唯一定位台账。

### 目标规则

只自动删除：

```text
终态 + object_status in ('absent', 'deleted')
```

长期保留：

```text
uploading
unknown
pending
present
legacy_unverified
deleting
delete_unknown
```

保留所有被任务引用的历史 storage config revision。

### 删除审计

推荐增加 append-only：

```sql
CREATE TABLE object_delete_events (...)
```

记录：

- task_id
- request_id
- from_status
- to_status
- provider_result
- error_code
- started_at
- finished_at
- metadata snapshot

需要明确“永久保留删除审计”与“清理 deleted task”之间的关系。推荐：

- `upload_tasks` 可以按长期策略清理。
- `object_delete_events` 保留更长时间或永久保留。

### 验收标准

- 仍存在对象的任务永远不会被自动清理。
- 历史未验证任务不会被清理。
- 删除审计在任务清理后仍可追溯。
- inactive config 只有无任务引用且超过期限时才删除。

---

## 4.5 P1-5：为设置接口增加请求体限制

### 当前问题

上传接口有请求体计数，设置接口直接 `await request.json()`，缺少独立大小上限。

### 修改要求

新增配置：

```text
MAX_SETTINGS_BODY_BYTES=65536
```

覆盖：

```text
POST /v1/settings/storage/test
PUT /v1/settings/storage
POST /v1/settings/storage/presets
PUT /v1/settings/storage/presets/{preset_key}
PATCH /v1/settings/storage/presets/{preset_key}
PUT /v1/settings/storage/default
```

### 验收标准

- 超过 64 KiB 返回 `413` 或稳定配置错误。
- 正常设置请求保持兼容。
- 限制发生在完整 JSON 解析前。
- 日志不记录请求体。

---

## 4.6 P1-6：清理无效配置项

当前定义但未完整生效的配置至少包括：

```text
REQUEST_TIMEOUT_SECONDS
DASHBOARD_ENABLED
```

### 修改要求

- `REQUEST_TIMEOUT_SECONDS`：
  - 应覆盖完整上传请求生命周期。
  - 超时后任务进入 `failed` 或 `unknown`，取决于是否已开始 Provider 调用。
- `DASHBOARD_ENABLED=false`：
  - 不挂载 Dashboard 页面。
  - 不挂载或不公开 Dashboard 专用静态资源。
  - API 是否保留按 `PLAN.md` 决定。

每个配置项必须有自动测试。暂未实现的配置从 `.env.example` 移除，或明确标记为 reserved。

---

## 4.7 P1-7：统一幂等状态判断

### 当前问题

预检查和 SQLite 唯一约束竞争后的错误处理逻辑不一致。并发情况下，已有 failed task 可能被错误映射为 `UPLOAD_IN_PROGRESS`。

### 修改要求

提取唯一函数：

```python
def resolve_idempotent_task(existing, requested_preset) -> ReplayOrError:
    ...
```

预检查和 `IntegrityError` 恢复都调用同一逻辑。

只有确认查到相同 idempotency task 时才能按幂等冲突处理。其他 `IntegrityError` 返回 `DATABASE_ERROR`。

### 验收标准

- succeeded → 200 replay
- failed → `IDEMPOTENCY_KEY_REUSED`
- uploading / unknown → `UPLOAD_IN_PROGRESS`
- 不同 preset → `IDEMPOTENCY_SCOPE_MISMATCH`
- 其他数据库约束错误 → `DATABASE_ERROR`
- 并发测试结果稳定。

---

## 4.8 P1-8：补齐客户端断开和临时盘异常处理

### 必须覆盖的异常

- 客户端断开。
- 临时文件创建失败。
- 临时文件写满。
- UploadFile 读取失败。
- Provider 调用前进程取消。
- Provider 调用中超时。
- 上传成功后数据库提交失败。
- 响应发送前连接关闭。

### 状态原则

- 可以明确确认远端未创建对象：`failed`
- 远端结果可能已经发生：`unknown`
- 已成功上传但数据库失败：保持可恢复状态并写 CRITICAL
- 客户端断开：
  - Provider 调用前：`failed / CLIENT_DISCONNECTED`
  - Provider 调用后或调用中：根据不确定性进入 `unknown`

### 验收标准

- 所有异常路径都清理临时文件。
- 所有已创建 task 最终有明确状态或可恢复状态。
- 不出现永久 `uploading` 且无恢复信息的任务。
- 日志包含稳定 error code。

---

## 4.9 P1-9：实现真实 ZOS 原生指标 Client

当前生产 Client 是标准 boto3 S3 Client。标准 S3 Client通常不包含：

```text
get_bucket_statistics
get_bucket_storage_info
```

### 修改要求

- 核心上传、Head、Delete 保持使用 boto3 S3 Client。
- `enable_bucket_metrics=true` 时创建独立的 ZOS 官方 SDK Client。
- 增加 5 分钟缓存。
- 指标失败只影响 `/v1/dashboard/storage`，不影响上传就绪。
- 指标响应做字段白名单和敏感信息清洗。

### 验收标准

- 指标关闭时无额外 SDK 请求。
- 指标开启且 SDK 可用时返回真实数据。
- 指标失败时本地 Dashboard 统计继续工作。
- 缓存减少重复请求。

---

## 5. 严格删除能力实施要求

## 5.1 API

```http
DELETE /v1/upload-tasks/{task_id}/object
X-Delete-Token: required
X-Request-ID: optional
```

请求不得接受：

```text
Bucket
Object Key
URL
Endpoint
Provider
VersionId
```

### 允许删除的任务

```text
upload status = succeeded
object_status = present
delete token matches
metadata verified
```

历史 `legacy_unverified` 默认禁止删除。

---

## 5.2 状态机

```text
present
  └── deleting
        ├── deleted
        ├── present
        └── delete_unknown
```

其他状态：

```text
pending
absent
legacy_unverified
```

### 并发控制

使用 SQLite 条件更新：

```sql
UPDATE upload_tasks
SET object_status='deleting', ...
WHERE id=? AND object_status='present'
```

只有一个请求能成功进入 Provider 删除。

---

## 5.3 删除流程

1. 校验 task UUID。
2. 校验 Header 格式。
3. 查询任务。
4. 常量时间比较 token hash。
5. 条件更新 `present → deleting`。
6. 使用任务原 `storage_config_id` 创建 Provider。
7. `HeadObject`。
8. 比较：
   - size
   - ETag
   - VersionId
   - 可选 LastModified
9. 元数据不一致：
   - 恢复 `present`
   - 返回 `409 OBJECT_CHANGED`
10. 元数据一致：
    - 有 VersionId：删除精确版本
    - 无 VersionId：删除 Key
11. 删除后再次 Head。
12. 明确不存在：
    - 更新 `deleted`
    - 写删除审计
    - 返回 200
13. 调用超时或结果不确定：
    - 更新 `delete_unknown`
    - 返回 202
14. Provider 明确拒绝：
    - 恢复 `present`
    - 返回 `DELETE_FAILED`

---

## 5.4 删除恢复

恢复器同时处理：

```text
unknown upload
stale uploading
deleting
delete_unknown
```

对于删除：

- 确认对象不存在 → `deleted`
- 确认原对象仍存在且元数据一致 → `present`
- 对象存在但元数据变化 → 保守记录并人工处理
- 仍无法确认 → `delete_unknown`

---

## 5.5 日志清洗

当前敏感字段正则需要增加：

```text
delete_token
delete-token
x-delete-token
token_hash
delete_token_hash
```

同时确保：

- Uvicorn access log 不输出 Header。
- 反向代理不记录 `X-Delete-Token`。
- APM 不采集该 Header。
- Dashboard 不展示 token 或 hash。

---

## 5.6 删除测试

必须覆盖：

- token 缺失。
- token 格式错误。
- token 篡改。
- 跨任务使用 token。
- 常量时间比较。
- 历史任务禁止删除。
- 对象大小变化。
- ETag 变化。
- VersionId 变化。
- 对象已经不存在。
- 并发删除。
- 重复删除。
- Provider 超时。
- Provider 明确拒绝。
- 删除成功后数据库失败。
- 重启恢复 `delete_unknown`。
- stdout、SQLite、API、Dashboard、HTML、JS 中均无 token。

---

## 6. 文件级修改建议

## 6.1 `app/config.py`

新增或落实：

```text
MAX_SETTINGS_BODY_BYTES
SHUTDOWN_GRACE_SECONDS
REQUEST_TIMEOUT_SECONDS
DASHBOARD_ENABLED
```

增加跨字段校验：

- request body > upload bytes。
- temp minimum 与并发、单文件上限合理。
- multipart chunk 符合 S3 最小限制。
- timeout 值合理。
- Dashboard 开关真实生效。

为所有配置增加测试。

---

## 6.2 `app/database.py`

主要工作：

- `SCHEMA_VERSION = 3`
- v1 → v2 → v3 migration
- `storage_presets`
- 每 preset active config
- task 对象元数据
- 删除状态
- token hash
- 删除审计
- preset CRUD transaction
- default switch transaction
- idempotency + preset 查询
- 条件状态更新
- 修正 retention
- schema integrity verification
- SQLite backup helper
- 所有动态 SQL filter 字段使用固定白名单

避免继续把所有数据库职责堆在单个超大类中。可以按领域拆分内部 repository，但无需引入 ORM。

---

## 6.3 `app/providers.py`

主要工作：

- `ObjectMetadata`
- `head_object()` 返回完整元数据
- `delete_object()`
- VersionId 支持
- ZOS 错误分类
- 上传确认错误
- 删除不确定错误
- 官方 ZOS metrics client
- 保留 checksum 与 `public-read`

Provider 抛出的错误应包含：

```python
ProviderError(
    code=...,
    message=...,
    uncertain=...,
    operation=...,
)
```

---

## 6.4 `app/runtime.py`

主要工作：

- 多 preset snapshot registry
- 按 config ID 缓存 Provider
- default preset
- per-preset probe
- background supervisor
- upload recovery
- delete recovery
- preset activation
- preset state update
- default switch
- environment bootstrap 创建 `default` preset
- ready checks 只以默认 preset 为关键上传依赖
- 非默认 preset 故障显示 degraded，但不阻止默认上传就绪

Runtime 应逐步拆分：

```text
StoragePresetService
UploadRecoveryService
DeletionService
ProbeService
MaintenanceService
```

保留单进程架构。

---

## 6.5 `app/main.py`

当前文件职责过多，建议拆分路由：

```text
app/api/uploads.py
app/api/tasks.py
app/api/settings.py
app/api/dashboard.py
app/api/health.py
app/errors.py
app/models.py
app/middleware.py
```

主要修改：

- Pydantic models
- 统一错误模型
- 设置请求体限制
- `X-Storage-Preset`
- 多 preset API
- 删除 API
- 幂等 scope
- 客户端断开
- 请求总超时
- 数据库线程化
- Dashboard 开关
- 兼容旧接口

---

## 6.6 `app/eventlog.py`

增加敏感键：

```text
delete_token
token_hash
x-delete-token
settings_encryption_key
```

增加核心事件：

```text
service_ready
service_degraded
recovery_started
recovery_resolved
recovery_pending
upload_failed
upload_capacity_rejected
idempotency_replayed
preset_created
preset_updated
preset_enabled
preset_disabled
default_preset_changed
delete_started
delete_succeeded
delete_already_absent
delete_failed
delete_pending
maintenance_completed
background_task_failed
```

日志 details 使用白名单字段，避免把完整 payload 交给清洗器后再依赖正则。

---

## 6.7 Dashboard

设置页：

- preset 列表。
- 创建 preset。
- 编辑 preset。
- 测试连接。
- 保存新 revision。
- 设置默认项。
- 启停非默认项。
- 显示 state revision 和 config revision。

监控页：

- 显示默认 preset。
- 接收测试真实上传时可选择启用 preset。
- 任务列表增加：
  - `storage_preset`
  - ETag
  - VersionId
  - object status
  - delete status
- 删除能力按当前 `PLAN.md` 保持不在 Dashboard 执行，只读展示状态。
- 动态内容继续只用 `textContent`。
- 不使用 `innerHTML`。
- 不保存凭证和 token。

---

## 6.8 测试文件

建议拆分：

```text
tests/test_config.py
tests/test_database_schema.py
tests/test_database_migrations.py
tests/test_provider_zos.py
tests/test_upload_api.py
tests/test_idempotency.py
tests/test_presets_api.py
tests/test_delete_api.py
tests/test_recovery.py
tests/test_dashboard.py
tests/test_security.py
tests/test_deployment.py
```

保留 Fake Provider，同时增加 Fake Versioned Provider，用于 VersionId 和严格删除测试。

---

## 6.9 `README.md`、`WORKLOG.md`

README 必须只描述已上线行为。

WORKLOG 每阶段记录：

- 规格基线。
- schema version。
- 已完成内容。
- 测试数量。
- 真实 ZOS 验证。
- 尚未完成项。
- 部署版本和迁移状态。

---

## 6.10 `deploy.sh` 和 `compose.yaml`

### 公开仓库信息

当前公开仓库中出现了真实内网地址、用户名和 Portainer 地址。修改为变量：

```text
DEPLOY_TARGET
DEPLOY_REMOTE_DIR
DEPLOY_HEALTH_URL
DEPLOY_READY_URL
DEPLOY_SSH_KEY
```

实际值放入被 `.gitignore` 排除的：

```text
.deploy.env
```

脚本缺少参数时退出，不提供真实默认内网地址。

### 部署前检查

- 工作树干净。
- 单元测试通过。
- migration 测试通过。
- runtime image build 通过。
- 远程数据库已备份。
- 当前远程 schema version 已识别。

### 部署后检查

先检查：

```text
/healthz
```

再检查：

```text
/readyz
```

### 自动回滚

记录升级前镜像 tag。若 health 或 ready 失败：

- 恢复旧 IMAGE_TAG。
- 重建旧容器。
- 输出失败原因。
- 保留升级数据库备份。
- schema 已升级时，回滚策略必须明确；不能盲目使用旧代码打开新 schema。

因此数据库迁移和应用部署需要采用兼容窗口：

1. 新代码先兼容旧 schema 并执行迁移。
2. 迁移完成后新代码运行。
3. 旧镜像回滚仅在 schema 兼容时允许。
4. 跨 schema 回滚需要数据库备份恢复。

---

## 7. CI 建议

当前仓库应增加 GitHub Actions，至少包括：

```text
pytest
ruff check
ruff format --check
pyright 或 mypy
docker build --target test
docker build --target runtime
pip-audit
镜像漏洞扫描
```

迁移测试使用预制 v1 SQLite fixture。

建议工作流：

```yaml
on:
  pull_request:
  push:
    branches: [master]
```

分支保护要求 CI 全部通过后才能合并。

---

## 8. 自动测试验收矩阵

## 8.1 当前行为回归

- 未配置时 health 和 Dashboard 可用。
- 上传成功。
- 上传失败。
- 上传结果不确定。
- 空文件。
- 超限文件。
- 多 file 字段。
- Content-Length 缺失/伪造。
- filename 清洗。
- Content-Type 默认值。
- 幂等成功重放。
- failed key 冲突。
- 并发容量。
- Dashboard XSS 防护。
- AK/SK 加密和清洗。
- ZOS checksum policy。
- `public-read` ACL。

## 8.2 数据库迁移

- 空库 → v3。
- v1 → v3。
- v2 → v3。
- 迁移异常回滚。
- 重复启动。
- ID 保持。
- revision 保持。
- task 数量保持。
- 历史成功任务 `legacy_unverified`。
- 唯一默认 preset。
- 每 preset 唯一 active config。
- foreign key 完整。

## 8.3 多预设

- 创建第一个 preset 自动默认。
- 创建第二 preset。
- preset key 格式。
- 重复 key。
- 更新配置 revision。
- state revision 冲突。
- 默认 preset 不能禁用。
- 默认切换。
- 未传 Header 使用默认。
- 显式 preset。
- 不存在 preset。
- disabled preset。
- 未配置 preset。
- 显式失败不回退。
- 默认切换不影响在途任务。
- 两个 Bucket 上传隔离。

## 8.4 上传确认

- HeadObject 成功。
- HeadObject 404。
- HeadObject 超时。
- 大小不一致。
- ETag 保存。
- VersionId 保存。
- Content-Type 保存。
- 首次 token。
- replay token null。
- 数据库提交失败后恢复。

## 8.5 删除

见第 5.6 节。

## 8.6 并发和故障

- SQLite 锁等待。
- 临时盘写满。
- 客户端断开。
- 进程在上传前终止。
- 进程在上传中终止。
- 上传成功后数据库提交前终止。
- 删除调用前终止。
- 删除调用中终止。
- 删除成功后数据库提交前终止。
- 后台任务异常后继续运行。

## 8.7 真实 ZOS

- TXT。
- PDF。
- 图片。
- 超过 16 MiB multipart。
- 接近 200 MiB。
- 两个不同 Bucket preset。
- 公网 URL。
- HeadObject 元数据。
- 非版本化删除。
- 版本化精确版本删除。
- 外部覆盖后 `OBJECT_CHANGED`。
- 重启恢复。

---

## 9. 建议提交顺序

禁止一次性完成所有功能。推荐提交顺序：

### Commit 1：冻结契约

```text
docs: mark API v3 as unreleased and document current baseline
```

内容：

- 文档状态。
- README 当前行为。
- Pydantic model 基础。
- 现有响应契约测试。

### Commit 2：数据库迁移框架

```text
feat: add transactional schema v1-v3 migrations
```

内容：

- schema v3。
- migration。
- fixture。
- 完整性测试。
- 暂不改路由行为。

### Commit 3：对象元数据确认

```text
feat: confirm uploaded objects and persist metadata
```

内容：

- ObjectMetadata。
- HeadObject。
- ETag / VersionId。
- object status。
- 保持旧响应字段。

### Commit 4：删除凭证基础

```text
feat: issue one-object deletion capabilities
```

内容：

- token hash。
- 首次响应。
- 日志清洗。
- 暂不开放 DELETE 或只在后续提交开放。

### Commit 5：多预设数据库与服务层

```text
feat: add storage preset domain model
```

内容：

- preset repository。
- Runtime snapshots。
- default preset。

### Commit 6：多预设 API

```text
feat: add storage preset management APIs
```

### Commit 7：上传预设路由

```text
feat: route uploads by storage preset
```

### Commit 8：严格删除

```text
feat: add verified object deletion workflow
```

### Commit 9：删除恢复和审计

```text
feat: recover uncertain deletions and persist audit events
```

### Commit 10：Dashboard v3

```text
feat: manage storage presets in dashboard
```

### Commit 11：可靠性和部署

```text
chore: harden background tasks, CI, deployment and rollback
```

---

## 10. 明确排除的工作

本轮不要引入：

- Kubernetes。
- 消息队列。
- 独立异步 Worker。
- Celery。
- ORM。
- 完整第三方 migration framework。
- PostgreSQL。
- 多实例写入。
- 用户系统。
- 登录、角色、权限。
- API Gateway。
- WebSocket 或 SSE。
- 文件搜索。
- 文件内容管理。
- 资产版本管理。
- 自动跨 Provider 失败回退。
- Dashboard 直接删除对象。
- Dashboard 管理 Bucket、ACL、Policy、生命周期或 CORS。

---

## 11. Definition of Done

v3 可以正式作为调用方契约，必须同时满足：

1. `API.md` 与 OpenAPI、代码和测试一致。
2. 远程 v1 数据库副本成功迁移到 v3。
3. 历史 task、config 和 revision 无丢失。
4. 多 preset API 全部实现。
5. `X-Storage-Preset` 行为稳定。
6. 默认 preset 事务切换正确。
7. 正常上传后执行 HeadObject。
8. ETag、VersionId 和 object status 持久化。
9. delete token 只在允许的位置出现。
10. 严格删除状态机实现。
11. 删除前元数据比对实现。
12. VersionId 精确删除实现。
13. `delete_unknown` 可恢复。
14. 保留策略不会删除仍存在对象的台账。
15. 后台任务异常可恢复并可观测。
16. SQLite 操作不会长时间阻塞事件循环。
17. 临时文件只有一份完整副本。
18. 设置接口有请求体限制。
19. 所有配置项真实生效并有测试。
20. 自动测试、静态检查和容器构建全部通过。
21. 两个不同 ZOS Bucket 的真实 preset 上传通过。
22. multipart、接近 200 MiB、重启恢复和删除故障测试通过。
23. 部署使用 `/readyz` 门禁。
24. 数据库备份和回滚流程有明确记录。
25. README 只描述已经上线的能力。

---

## 12. Agent 当前第一阶段任务

当前先完成以下范围，完成后停止并提交审查：

```text
Phase 0：冻结当前契约
Phase 1：实现并测试 schema v1 → v2 → v3 迁移
```

本阶段暂不实现：

```text
X-Storage-Preset 路由
Dashboard 多预设页面
DELETE API
delete_token
Provider DeleteObject
```

第一阶段交付物：

- 当前接口与目标接口状态明确。
- `SCHEMA_VERSION = 3`。
- v1、v2、v3 初始化与迁移测试。
- 真实数据库副本迁移演练说明。
- 历史数据保持证明。
- README、WORKLOG 更新。
- 所有现有测试继续通过。
- 新增迁移测试全部通过。

完成第一阶段后，再进入上传对象元数据确认和多预设 Runtime 重构。
