# ctyun_ZOS 综合仓库审查与修复路线图

> 审查日期：2026-08-03  
> 审查基线：`master` @ `a60802a`  
> 审查范围：`app/`、`tests/`、`Dockerfile`、`compose.yaml`、`deploy.sh`、备份与验收脚本、GitHub Actions、README 与 `docs/`、仓库卫生  
> 输入来源：本轮架构与一致性审查 + `DS_review.md` 安全/运维审查  
> 当前测试基线：仓库与 WORKLOG 记录 65 项容器化自动测试通过

---

## 1. 执行结论

`ctyun_ZOS` 已达到**可信局域网内单机生产服务**的工程完成度，综合评级为 **B+，具备条件式生产就绪能力**。

当前架构与业务规模匹配：FastAPI 单进程、SQLite WAL、不可变 `StorageSnapshot`、按配置 revision 缓存 Provider、严格删除状态机、事务迁移、加密凭证、异地加密备份和真实 ZOS 验收共同形成了扎实的基础。现阶段继续使用单实例与 SQLite；多实例高可用、持续高写入并发或跨节点任务调度出现后，再评估 PostgreSQL、队列和容器编排。

下一轮开发的发布门槛集中在五项：

1. **管理控制面保护**：设置接口、Dashboard、日志和完整任务详情具备真实访问控制与网络隔离。
2. **远端副作用与本地状态一致性**：恢复成功的对象拥有明确的删除能力状态，失败任务拥有准确的对象状态。
3. **恢复过程有界**：启动恢复具备批次、并发、时间预算与可观察 backlog。
4. **部署回滚完整**：首次部署失败和同 schema 发布失败都有确定的容器与数据库恢复路径。
5. **运行时防护完善**：日志轮转、容器权限、配额、依赖锁定和 CI 安全检查进入默认基线。

---

## 2. 建议保留的核心设计

### 2.1 严格删除协议

当前删除流程具备较完整的 capability-based 安全语义：

- 每任务独立的 256-bit URL-safe `delete_token`；
- SQLite 只保存 SHA-256；
- 常量时间比较；
- 删除前校验任务原配置、对象大小、ETag 和可选 VersionId；
- 使用任务原 `storage_config_id` 执行精确删除；
- 删除后再次 HeadObject；
- `deleting`、`delete_unknown`、`deleted` 的恢复流程；
- `object_delete_*` 永久审计。

这套设计应继续作为领域核心。后续修改围绕异常窗口补全状态，不需要重写协议。

### 2.2 SQLite schema 与迁移策略

当前实现已经包含：

- WAL；
- `BEGIN IMMEDIATE`；
- busy timeout；
- 外键检查；
- schema v1→v2→v3 逐版本事务迁移；
- 升级前 SQLite Online Backup；
- `integrity_check`、`foreign_key_check` 和引用完整性验证；
- 历史任务继续绑定原 Storage Config Revision。

这些能力足以支撑当前单机规模。下一次状态模型修改建议升级为 schema v4，并沿用同一迁移纪律。

### 2.3 Provider 与配置 revision

`StorageProvider`、`ProviderRegistry`、独立 Storage Preset、配置 revision 和不可变 `StorageSnapshot` 形成了清晰的扩展边界。在途任务与历史恢复继续使用原 Provider revision，默认预设切换不会改变历史对象定位。

### 2.4 凭证、日志与备份

- AK/SK 使用 Fernet 加密；
- 设置响应只暴露 masked AK；
- 日志根据敏感字段名递归清洗；
- 删除 token 明文不进入 SQLite 与日志；
- 私有备份同时保存数据库与 `SETTINGS_ENCRYPTION_KEY`；
- 备份使用 PBKDF2-HMAC-SHA256 派生密钥与 Fernet 认证加密；
- 远端备份使用 `private` ACL，并支持下载、摘要、解密与 SQLite 完整性验证。

这部分继续保留，重点补充内存上限、独立灾难恢复入口和保留策略。

### 2.5 测试关注故障语义

现有测试覆盖并发上传、并发删除、幂等、配置 revision、迁移回滚、对象元数据变化、超时、重启恢复、数据库写入失败、凭证加密和备份篡改。后续新增测试应继续围绕“远端对象与本地台账是否可信”展开。

---

## 3. 两份审查的结论校正

| 审查意见 | 综合结论 | 进入路线图的处理 |
|---|---|---|
| 设置接口与 Dashboard 零认证 | 成立，P0 | 管理 API Key、网络隔离、管理面路由收敛、Endpoint allowlist |
| Docker 缺 `cap_drop: ALL`、只读 rootfs | 成立，P1 | Compose 默认加固并增加运行测试 |
| stdout 日志无轮转 | 成立，P1 | 使用 Docker `local` 驱动或有限制的 `json-file` |
| 200–204 MiB 文件返回 400 | 当前实现下缺少充分代码依据，按误报处理 | 保留一条边界测试验证 200 MiB、请求体上限和 413 语义，无需专门改 parser |
| `EventLogger.degraded` 未暴露 | 成立，P1 | 加入 `/readyz` 与恢复逻辑 |
| `request_timeout_seconds`、`dashboard_enabled` 为死配置 | 成立，P1 | 实现或移除，禁止保留虚假运维开关 |
| 按 `X-Request-ID` 前缀限流 | 目标成立，身份依据不可靠 | 使用来源 IP；引入认证后按 `client_id` 配额 |
| SDK ZIP 与 Zone.Identifier 被跟踪 | 成立，P2 | 从索引移除并补 ignore；仓库扩大前可选择清理历史 |
| `docs/API.md`、`docs/PLAN.md` 与实现漂移 | 成立，P2 | OpenAPI 作为当前契约，未来设计进入 RFC 目录 |
| CI 只有 pytest/build | 成立，P2 | Ruff、类型检查、ShellCheck、依赖与镜像扫描 |
| Chart.js 无许可证说明 | 当前 vendor 文件头已包含 MIT 声明 | 可选增加 `THIRD_PARTY_NOTICES.md`，不列为缺陷 |
| 备份必须调用 `get_object_acl` 验证 | 可选增强 | 主要依赖私有 Bucket Policy、Block Public Access 与匿名访问验收 |
| S3 Compatible 的 metrics UX 冲突 | 当前前端已按 capability 禁用开关 | 动态字段 schema 作为低优先级体验优化 |
| `deploy-backups/` 无清理 | 成立，P1 | 增加保留数量、总容量和受保护迁移备份策略 |

---

## 4. 风险与问题清单

## P0：发布阻断问题

### R-001 管理控制面缺少真实访问控制

**涉及位置**

- `/v1/settings/storage/*`
- `/dashboard`、`/dashboard/settings`
- `/v1/dashboard/logs`
- 任务列表与完整任务详情
- Storage Endpoint 连接测试

**当前风险**

`X-Settings-Request: true` 与 Origin 校验提供浏览器 CSRF 防护。局域网程序可以直接构造请求，修改 Endpoint、Bucket、Provider、AK/SK、默认预设和公网 URL。配置被定向到攻击者控制的 S3 Endpoint 后，后续上传会进入错误存储。任意 Endpoint 还会形成受限程度较低的服务端网络探测能力。

**目标状态**

- 上传数据面维持受控局域网调用方式；
- 管理控制面要求管理员凭证；
- 管理路由仅对管理员网络开放；
- Storage Endpoint 进入显式 allowlist；
- 设置写操作继续保留 CSRF 防护与 revision 乐观锁。

**推荐实现**

第一阶段增加 `ADMIN_API_KEY`：

```text
Authorization: Bearer <admin-key>
```

或：

```text
X-Admin-Key: <admin-key>
```

使用 SHA-256/HMAC 或常量时间比较验证；日志完整清洗该 Header。

目标部署结构：

| 平面 | 建议入口 | 暴露范围 |
|---|---:|---|
| 数据面 | `:8000` | 可信局域网；只开放上传、必要查询、health/ready |
| 管理面 | `127.0.0.1:8001` 或管理员 VLAN | Dashboard、设置、日志、完整任务详情、管理清理 |
| 应用内部端口 | loopback/Compose 内网 | 由反向代理做路径 allowlist 与 HTTPS |

可以先在同一 FastAPI 应用中加入管理员依赖，再通过反向代理和防火墙完成端口/路径隔离；后续再拆分 `public_router` 与 `admin_router`。

**Endpoint 安全策略**

- `ctyun_zos`：允许配置的天翼云 ZOS 域名后缀或明确主机列表；
- `s3_compatible`：使用环境变量提供允许的域名/CIDR；
- 默认拒绝 loopback、link-local、云 metadata 地址和未授权私网段；
- 对 DNS 解析结果执行相同校验，降低 DNS rebinding 风险；
- `http://` 仅在显式开发开关下允许。

**验收标准**

- 未携带管理员凭证访问管理写接口返回 `401/403`；
- 错误管理员凭证使用常量时间路径；
- 普通上传接口继续正常工作；
- 非 allowlist Endpoint 无法测试和保存；
- 管理凭证不进入响应、日志和 Dashboard DOM；
- 管理端口无法从普通业务 VLAN 访问。

---

### R-002 恢复成功的对象可能缺失删除能力

**当前异常窗口**

```text
远端上传成功
→ HeadObject 成功或稍后恢复确认成功
→ 进程中断 / 最终数据库更新失败 / 首次响应丢失
→ 本地恢复为 succeeded + present
→ delete_token_hash 为空或明文无法交付
```

该对象可以存在于公共 Bucket，任务状态也可以显示成功，但调用方无法提供合法 `X-Delete-Token`。当前状态模型没有表达“对象存在、删除凭证未交付”。

**最低成本修复**

1. 本地文件接收完成后、调用 Provider 前，先持久化 `size_bytes`；
2. schema v4 增加 `object_status='present_unclaimed'`；
3. 恢复发现远端对象存在且 `delete_token_hash IS NULL` 时进入 `present_unclaimed`；
4. API 返回 `delete_capability_available: false`；
5. 管理面增加强认证的 orphan 清理操作，继续执行任务原配置、元数据校验、精确删除和审计；
6. Dashboard 将该状态显示为高优先级告警。

**完整交付模型（后续可选）**

引入加密 token escrow：

```text
delete_token_hash
delete_token_ciphertext
delete_token_delivered_at
delete_token_acknowledged_at
```

上传前生成 token，hash 与 ciphertext 在远端副作用发生前持久化。调用方通过幂等请求领取，确认领取后清除 ciphertext。该方案提供更完整的一次性交付语义，也会增加接口和状态复杂度。

**验收标准**

- 数据库中不存在含义不明的 `succeeded + present + delete_token_hash IS NULL`；
- 此类任务稳定转换为 `present_unclaimed`；
- 管理清理路径继续执行元数据保护与永久审计；
- 模拟最终 `update_task()` disk full 后，重启可以识别并展示 orphan；
- 190 MiB 与 multipart 对象也具备相同语义。

---

### R-003 失败任务的 `object_status` 不一致

**当前问题**

本地空文件、文件超限和部分确定性 Provider 失败只更新：

```text
status = failed
error_code = ...
```

`object_status` 可能继续保持默认 `pending`。维护任务只清理 `absent` 或 `deleted` 的终态任务，因此部分失败记录会永久保留，也会在 Dashboard 中呈现“远端对象状态未确定”。

**目标状态不变量**

| 事件 | `status` | `object_status` |
|---|---|---|
| 本地空文件、大小校验失败、格式校验失败 | `failed` | `absent` |
| Provider 明确拒绝且可确认无远端副作用 | `failed` | `absent` |
| 网络中断、5xx、连接关闭、响应丢失等不确定结果 | `unknown` | `pending` |
| 恢复发现对象存在且删除能力完整 | `succeeded` | `present` |
| 恢复发现对象存在且删除能力未交付 | `succeeded` | `present_unclaimed` |
| 恢复确认对象不存在 | `failed` | `absent` |
| 删除确认完成 | `succeeded` | `deleted` |

**推荐实现**

将分散在 `main.py`、`runtime.py` 和 `database.py` 的字段更新收敛成领域方法：

```python
mark_local_failure(...)
mark_upload_unknown(...)
mark_upload_confirmed(...)
mark_recovered_unclaimed(...)
mark_object_absent(...)
mark_delete_started(...)
mark_delete_confirmed(...)
```

Provider 错误进一步区分：

```text
remote_effect = none | possible | confirmed
```

至少把 `ConnectionClosedError`、5xx、CompleteMultipartUpload 响应丢失等场景归入 `possible`。

**验收标准**

- 所有终态失败任务拥有明确 `object_status`；
- 保留期测试确认本地失败任务能够清理；
- Provider 不确定错误继续进入恢复队列；
- 状态迁移通过数据库条件更新保证并发安全；
- schema CHECK constraint 覆盖新增状态。

---

### R-004 启动恢复过程缺少批次和时间预算

**当前风险**

`Runtime.start()` 在 FastAPI lifespan 完成前顺序执行所有 pending upload 与 pending deletion 的恢复。历史任务较多、旧 Endpoint 不可达或 read timeout 很长时，服务启动会持续等待。

**目标状态**

- 启动执行一个有限恢复 pass；
- 剩余 backlog 由后台 reconciliation 处理；
- 每轮恢复具备批次、并发和独立 timeout；
- `/readyz` 显示 backlog、最近成功时间和最旧待恢复任务年龄；
- 恢复保持删除的保守语义，不主动重复未知删除。

**推荐配置**

```text
RECOVERY_INITIAL_BUDGET_SECONDS
RECOVERY_BATCH_SIZE
RECOVERY_MAX_CONCURRENCY
RECOVERY_CONNECT_TIMEOUT_SECONDS
RECOVERY_READ_TIMEOUT_SECONDS
RECOVERY_MAX_ATTEMPTS
```

**建议流程**

```text
初始化 schema
→ 加载 active snapshots
→ 在预算内完成一轮恢复
→ 启动 HTTP 服务
→ 后台 supervisor 持续清理 backlog
```

恢复线程中的 boto 调用使用独立短超时配置。全局 ASGI timeout 需要谨慎处理，取消 `to_thread` 等待不会停止正在执行的远端请求。Provider 层的连接/读取/重试预算应成为主要超时控制。

**验收标准**

- 构造 100 个绑定不可达 Endpoint 的 pending task，启动过程仍能在有限 pass 后完成；
- `/readyz` 与 Dashboard 可观察 backlog；
- 后台任务失败后自动重试并降级 readiness；
- 恢复不会再次调用未知删除的 `DeleteObject`；
- 存储恢复超时不会阻塞 health endpoint。

---

### R-005 部署回滚覆盖不完整

**当前缺口**

1. 首次部署没有 previous image，健康检查失败后可能留下不健康容器；
2. schema v3→v3 发布失败时可以恢复旧镜像，部署前数据库快照不会恢复；
3. 部署期间缺少明确的流量冻结与并发部署锁；
4. `deploy-backups/` 缺少保留策略。

**推荐发布模型**

- 使用远程 `flock` 保证单一部署；
- 部署进入 maintenance/drain 状态，阻止新上传；
- 停止旧服务后创建一致性备份；
- 新版本先在 loopback 或内部端口完成 health/ready；
- 验收成功后切换 LAN 入口；
- 任一失败路径恢复旧镜像与部署前数据库；
- 首次部署失败执行 `docker compose down`；
- 迁移备份与普通发布备份采用独立保留策略。

**验收标准**

- 首次部署故意失败后无残留不健康服务；
- v3→v3 部署故意写入后失败，维护窗口内可恢复发布前状态；
- v3→v4 迁移失败恢复旧 schema 与旧镜像；
- 两次并发部署只有一个获得锁；
- 保留策略不会删除最近一次可用迁移备份；
- mock SSH/Docker 测试覆盖主要 rollback 分支。

---

## P1：高价值运行时与安全加固

### R-006 容器日志轮转

推荐使用 Docker `local` 日志驱动：

```yaml
logging:
  driver: local
  options:
    max-size: "10m"
    max-file: "3"
```

有限制的 `json-file` 也可接受。验收包含长期写日志后宿主机日志文件保持在配置范围内。

### R-007 容器权限收缩

推荐 Compose 基线：

```yaml
security_opt:
  - no-new-privileges:true
cap_drop:
  - ALL
read_only: true
pids_limit: 256
tmpfs:
  - /tmp:size=64m,noexec,nosuid,nodev
```

数据库与业务临时文件继续写入 `/data/db`、`/data/tmp`。生产镜像运行测试需覆盖启动、上传、Dashboard、备份 create/verify/restore 和 graceful shutdown。

### R-008 调用方配额与滥用控制

`MAX_CONCURRENT_UPLOADS` 只限制同时请求数。建议加入：

- 反向代理按来源 IP 的请求速率限制；
- 单 IP/调用方的每日对象数；
- 单 IP/调用方的每日上传字节数；
- 全局 Bucket 日预算告警；
- `Retry-After` 与稳定错误码。

`X-Request-ID` 继续承担追踪用途。调用方身份建议使用认证后的 `client_id`；过渡期可以使用来源 IP。

### R-009 `EventLogger.degraded` 进入 readiness

增加 `event_log` 检查：

```json
{
  "event_log": {
    "status": "ok|degraded",
    "last_failure_at": "...",
    "last_success_at": "..."
  }
}
```

成功持久化后清除 degraded 状态。删除审计写入失败继续触发 CRITICAL 与 `not_ready`。

### R-010 运维配置必须真实生效

- `DASHBOARD_ENABLED=false` 时不注册 Dashboard 页面和管理静态资源；
- `REQUEST_TIMEOUT_SECONDS` 选择实现或移除；
- 若保留，名称与语义应对应真实的 ingress/provider deadline；
- `.env.example`、README 与运行代码保持一致；
- 增加配置加载测试，检查所有公开环境变量都被使用。

### R-011 数据库默认路径统一

`Settings` 默认路径应统一为：

```text
/data/db/zos-upload.db
```

该路径与 Dockerfile、Compose volume 和 `.env.example` 保持一致，避免漏配环境变量时数据库写入容器可写层。

### R-012 部署备份保留策略

建议：

- 保留最近 N 份普通部署备份；
- 保留最近 N 份跨 schema 迁移备份；
- 任何清理动作保留最后一份已验证可恢复备份；
- 以文件数量与总容量双重限制；
- 清理结果进入结构化日志。

---

## P2：性能、维护性与工程基线

### R-013 上传文件存在二次 spool

Starlette 已将 multipart 文件写入 `UploadFile.file`，业务层又复制到第二个 `SpooledTemporaryFile`。大文件并发时会增加临时空间和磁盘 I/O。

优化方向：

- 使用 `UploadFile.size` 与请求体限制进行大小校验；
- rewind 原始 `source.file` 后直接交给 Provider；
- 在所有错误路径统一关闭 FormData 与 UploadFile；
- 保留 temp free-space 检查；
- 通过 4×200 MiB 压力测试确认临时空间下降。

### R-014 同步 SQLite 可能阻塞事件循环

普通查询较快，维护事务与 busy timeout 可能让事件循环等待。建议引入单独 DB executor 或 repository worker，集中执行同步 SQLite 操作。当前规模允许把这项放在状态修复之后。

### R-015 Provider cache 无界增长

历史配置 revision 的 Provider 与解密凭证会保留到进程退出。建议使用有界 LRU/弱引用缓存：

- active snapshot 和在途任务持有强引用；
- cache miss 时从加密配置重新构造；
- 设定最大历史 Provider 数；
- 不影响历史删除与恢复。

### R-016 幂等键缺少调用方命名空间

当前幂等键全局唯一，不同局域网服务使用同一业务编号时会冲突。引入 `client_id` 后，将唯一约束升级为：

```text
(client_id, idempotency_key)
```

兼容策略可以让未提供 client identity 的旧调用方进入 `legacy` 命名空间。该接口变化适合配合 `/v2` 或明确的兼容窗口。

### R-017 备份全量驻留内存

当前备份过程会同时持有数据库 bytes、gzip tar、明文归档与 Fernet 密文。建议：

- 短期增加数据库大小上限与可用内存预检；
- 中期设计分块流式加密格式；
- manifest 记录格式版本、分块摘要与总摘要；
- 保持恢复工具向后兼容 v1 备份。

### R-018 灾难恢复脚本依赖运行容器

`verify` 与 `restore` 应支持新机器和服务容器缺失的场景：

```text
BACKUP_IMAGE=<已验证镜像>
```

或自动从当前 commit 构建工具镜像。`create` 可以继续依赖运行数据库卷；`verify/restore` 使用独立镜像和备份凭证即可完成。

### R-019 依赖闭包与基础镜像缺少完全锁定

直接依赖已固定版本，传递依赖仍会在构建时重新解析。建议：

- 使用 uv lock、pip-tools 或等价 lock 文件；
- 生产安装启用 hash 验证；
- Python base image 固定 digest；
- 生成 SBOM；
- 依赖升级通过独立 PR 与测试完成。

### R-020 CI 工程基线

建议流水线包含：

1. `ruff check`；
2. `ruff format --check`；
3. Pyright 或 mypy；
4. pytest 容器测试；
5. ShellCheck；
6. `pip-audit`；
7. Trivy 镜像扫描；
8. OpenAPI snapshot diff；
9. runtime image build；
10. GitHub Action 固定 commit SHA。

真实 ZOS 验收继续保留为手动、受保护或定期 workflow，避免在普通 PR 暴露生产凭证。

### R-021 文档契约收敛

建议目录：

```text
docs/
  current/
    API.md
    OPERATIONS.md
    BACKUP_AND_RESTORE.md
  rfcs/
    v3-strict-delete.md
    v4-state-integrity.md
  releases/
    2026-07-v3.md
```

规则：

- `/openapi.json` 是机器契约；
- `docs/current/API.md` 与当前实现同步；
- 未来目标进入 `docs/rfcs/`；
- WORKLOG 按 release 归档；
- CI 生成或校验 OpenAPI snapshot。

### R-022 仓库卫生

执行：

```bash
git rm --cached sdk_python3.X.zip
git rm 'sdk_python3.X.zip:Zone.Identifier'
printf '\n*.zip\n*:Zone.Identifier\n' >> .gitignore
```

仓库尚未广泛协作时，可以评估 `git filter-repo` 清理历史大文件；执行历史重写前需要冻结推送并通知所有 clone 使用者。

Chart.js 文件头已经包含 MIT 许可声明。可选增加 `THIRD_PARTY_NOTICES.md` 统一记录 vendor 依赖。

### R-023 大文件模块拆分

行为稳定后，将 `main.py`、`database.py` 和 `runtime.py` 拆分：

```text
app/
  api/
    uploads.py
    tasks.py
    settings.py
    dashboard.py
  domain/
    upload_states.py
    deletion_states.py
    storage_presets.py
  services/
    upload_service.py
    deletion_service.py
    recovery_service.py
  repositories/
    tasks.py
    storage.py
    logs.py
  providers/
    base.py
    s3.py
    ctyun_zos.py
  main.py
```

拆分目标：状态迁移进入 domain/service，HTTP 路由负责输入输出，repository 负责事务与条件更新。

---

## 5. 修复 Phase Roadmap

## Phase 0：基线冻结与即时防护

**目标**：在 schema 与 API 修改前建立可靠的回归基线，并立即降低运维风险。

**修改内容**

- 为以下场景先增加失败测试：
  - 最终数据库写入失败后重启；
  - 恢复成功且无删除 token；
  - 本地失败任务 retention；
  - 首次部署失败；
  - v3→v3 回滚；
  - 100 个不可达恢复任务；
- 保存当前 OpenAPI snapshot；
- Compose 增加日志轮转、`cap_drop: ALL`、`read_only`、`pids_limit` 与 `/tmp` tmpfs；
- 通过防火墙或反向代理临时限制 `/v1/settings/*` 与 Dashboard 只允许管理员 IP；
- 删除 SDK ZIP 与 Zone.Identifier；
- 修正数据库默认路径。

**建议 PR**

```text
PR-00 test: freeze failure semantics and runtime baseline
PR-01 chore: harden compose and clean repository artifacts
```

**完成标准**

- 原 65 项测试继续通过；
- 新增问题复现测试稳定失败；
- 加固后的生产镜像可完成 smoke test；
- 普通业务网段无法访问设置写接口。

---

## Phase 1：上传状态完整性与 schema v4

**目标**：封闭远端对象成功、本地台账或删除能力缺失的异常窗口。

**修改内容**

- schema v4 增加 `present_unclaimed`；
- 在远端上传前持久化已接收 `size_bytes`；
- 定义上传与对象状态不变量；
- 本地失败进入 `failed + absent`；
- Provider 错误分类为确定无副作用/可能有副作用；
- 恢复对象缺少删除能力时进入 `present_unclaimed`；
- 任务响应增加 `delete_capability_available`；
- 管理面预留 orphan 清理 service；
- 迁移、回滚和 retention 测试覆盖 v1/v2/v3→v4。

**建议 PR**

```text
PR-02 feat: add schema v4 upload object-state invariants
PR-03 feat: reconcile and expose unclaimed remote objects
```

**完成标准**

- 数据库中没有含义不明的状态组合；
- disk full、进程终止和超时场景都能恢复到明确状态；
- 所有可安全清理的失败任务进入 retention；
- 实际 ZOS 小文件、multipart 和近上限文件验收通过。

---

## Phase 2：管理控制面安全

**目标**：让无登录上传数据面与受保护管理能力拥有清晰边界。

**修改内容**

- 增加管理员认证依赖；
- 管理凭证轮换机制与启动校验；
- `public_router` / `admin_router` 分组；
- 反向代理路径 allowlist 或独立管理端口；
- Endpoint 域名/CIDR allowlist；
- 默认拒绝 metadata、loopback 与未授权私网目标；
- 管理面提供 `present_unclaimed` 清理；
- 普通任务查询最小化敏感字段；
- Dashboard 与日志归入管理面；
- CSRF、Origin 与 revision 锁继续保留。

**建议 PR**

```text
PR-04 feat: authenticate administrative routes
PR-05 feat: isolate control plane and restrict storage endpoints
```

**完成标准**

- 无管理员凭证无法修改配置、查看日志或执行管理清理；
- 普通上传调用方式保持兼容；
- allowlist 与 DNS 解析测试通过；
- 管理凭证不会出现在日志、响应和前端存储；
- Dashboard 通过内网 HTTPS 访问。

---

## Phase 3：有界恢复、超时与 readiness

**目标**：让服务启动时间、恢复负载和依赖健康状态可预测。

**修改内容**

- 初始恢复时间预算；
- batch size 与最大并发；
- 恢复专用 Provider timeout；
- backlog 指标与最旧任务年龄；
- `EventLogger.degraded` 进入 readiness；
- `dashboard_enabled` 生效；
- 明确处理或删除 `request_timeout_seconds`；
- 后台 supervisor 继续自动重试；
- Provider 连接关闭与 5xx 错误使用保守语义。

**建议 PR**

```text
PR-06 fix: bound startup reconciliation and expose backlog health
PR-07 fix: make runtime configuration and event-log readiness effective
```

**完成标准**

- 大量不可达历史任务不会无限阻塞启动；
- health 始终快速响应；
- readiness 准确表达数据库、存储、恢复、日志和后台任务状态；
- 断网、kill 进程和响应丢失故障注入通过。

---

## Phase 4：部署、回滚与灾难恢复

**目标**：任何发布和恢复演练都具备确定、可重复的操作路径。

**修改内容**

- 远程部署 `flock`；
- maintenance/drain；
- 停服后一致性备份；
- 内部端口验收后再切流；
- 首次部署失败清理；
- 同 schema 与跨 schema 完整回滚；
- `deploy-backups/` 保留策略；
- `verify/restore` 支持独立工具镜像；
- 备份大小与内存预检；
- 私有 Bucket Policy 和匿名访问验收写入 runbook。

**建议 PR**

```text
PR-08 fix: make deployment rollback transactional under maintenance lock
PR-09 feat: make backup verification and restore independent of a running service
```

**完成标准**

- 首次部署、同 schema 发布、跨 schema 发布的失败演练全部恢复；
- 恢复工具可在空白 Linux 主机运行；
- 定时备份完成后自动执行可控验证；
- 保留策略不会无限占用数据库卷。

---

## Phase 5：容量、性能与调用方边界

**目标**：降低大文件临时 I/O、避免调用方互相干扰，并为业务增长建立配额。

**修改内容**

- 移除二次 spool；
- 统一 UploadFile/FormData 清理；
- DB executor/repository worker；
- Provider 有界缓存；
- 来源 IP 限流；
- 认证后引入 `client_id`；
- 幂等唯一约束升级为 `(client_id, idempotency_key)`；
- 每调用方对象数与字节配额；
- Dashboard 展示容量与配额告警。

**建议 PR**

```text
PR-10 perf: remove duplicate upload spooling and isolate blocking database work
PR-11 feat: add caller identity, scoped idempotency and upload quotas
```

**完成标准**

- 4×200 MiB 并发时临时磁盘使用可预测；
- 查询接口在上传和维护事务期间保持可响应；
- 两个调用方使用同一幂等键互不冲突；
- 超过配额返回稳定错误与 `Retry-After`。

---

## Phase 6：CI、依赖、文档与代码结构

**目标**：把当前工程质量固化成长期维护基线。

**修改内容**

- 依赖 lock + hash；
- base image digest；
- Ruff、类型检查、ShellCheck、pip-audit、Trivy、SBOM；
- OpenAPI snapshot；
- `docs/current`、`docs/rfcs`、`docs/releases`；
- WORKLOG 分版本归档；
- `main.py`、`database.py`、`runtime.py` 模块化；
- 可选 `THIRD_PARTY_NOTICES.md`；
- 评估 Git 历史大文件清理。

**建议 PR**

```text
PR-12 chore: lock dependencies and expand CI quality gates
PR-13 docs: establish current contract, RFC and release documentation
PR-14 refactor: separate API, domain, services and repositories
```

**完成标准**

- CI 对代码格式、类型、Shell、依赖与镜像漏洞进行门禁；
- 当前 API 文档与 OpenAPI 一致；
- 未来设计不会混入当前契约；
- 路由层不直接编排复杂状态迁移；
- 行为测试在重构前后保持一致。

---

## 6. 推荐执行顺序

```text
Phase 0  基线冻结与即时防护
   ↓
Phase 1  状态完整性与 schema v4
   ↓
Phase 2  管理控制面安全
   ↓
Phase 3  有界恢复与 readiness
   ↓
Phase 4  部署与灾难恢复
   ↓
Phase 5  性能、配额与调用方身份
   ↓
Phase 6  CI、文档与结构重构
```

Phase 1 与 Phase 2 都属于 P0。执行顺序优先处理状态完整性，因为 schema 与领域不变量会决定管理清理接口；生产环境同时使用 Phase 0 的网络限制保护现有设置接口。

---

## 7. 全局 Definition of Done

全部 Phase 完成后，仓库应满足：

### API 与状态

- OpenAPI 是当前机器契约；
- 所有 `status × object_status` 组合都有领域含义；
- 所有远端对象都具有明确的删除能力状态；
- 幂等语义按调用方隔离；
- 管理写接口具备认证、CSRF 与 revision 三层保护。

### 故障恢复

- 上传前、上传中、上传成功后、HeadObject 后、数据库写入前后 kill 进程均有确定恢复结果；
- 删除请求响应丢失不会触发重复主动删除；
- 大 backlog 不会无限阻塞启动；
- 日志持久化失败进入 readiness；
- Provider 不确定错误保持保守状态。

### 部署与备份

- 首次部署、同 schema 发布、跨 schema 发布均完成故障演练；
- 数据库与 `SETTINGS_ENCRYPTION_KEY` 有可验证异地备份；
- 空白主机可以执行 verify/restore；
- 备份和部署快照都有保留策略；
- 管理凭证与备份密码保存在服务器之外的受控位置。

### 安全与运维

- 管理面只对管理员网络开放；
- Storage Endpoint 受 allowlist 约束；
- 容器运行于非 root、无 capabilities、只读 rootfs；
- stdout 日志有轮转；
- 调用方具备速率与容量配额；
- secret、token、credential 不进入日志和前端存储。

### 质量门禁

- 原有测试全部通过；
- schema v1/v2/v3→v4 迁移与回滚测试通过；
- 真实 ZOS 小文件、并发 multipart、190 MiB、版本化对象精确删除与公网访问验收通过；
- Ruff、类型检查、ShellCheck、依赖扫描和镜像扫描通过；
- 文档契约与 OpenAPI snapshot 一致。

---

## 8. 当前阶段的明确建议

立即执行 Phase 0，并将 **Phase 1：状态完整性与 schema v4** 作为下一项核心开发。该阶段直接解决远端对象已经产生、调用方删除能力缺失和失败任务无法正确清理的问题。Phase 1 完成后实施管理面认证与隔离，形成安全、可恢复、可运维的局域网上传服务基线。
