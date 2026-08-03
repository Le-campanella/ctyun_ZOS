# MVP 工作记录

## 当前状态

第一版 MVP 已完成本地构建与自动验收。当前 HTTP 路径仍为 `/v1`；仓库数据库已升级为 schema v3，上传对象元数据确认、一次性删除凭证、多预设 Runtime、管理 API、显式上传路由、严格 DELETE、删除恢复、永久审计和 Dashboard v3 已经实现。

## 已完成

- 2026-07-29：确认用户整理后的 v4 计划和 v1 API 是当前权威规格。
- 2026-07-29：完整阅读计划、接口文档和 ZOS SDK 包结构。
- 2026-07-29：确定使用 Python 3.11 Docker 环境构建和测试，避免宿主机 Python 3.14 与无 pip 环境造成偏差。
- 2026-07-29：规范当前规格文件名为 `PLAN.md` 和 `API.md`，保留旧版到 `legacy/`。
- 2026-07-29：锁定 FastAPI、Uvicorn、boto3、cryptography、Jinja2 和测试依赖。
- 2026-07-29：完成部署配置解析、Fernet 凭证加密、三张 SQLite 表、WAL 参数和配置 revision 事务。
- 2026-07-29：完成 Provider registry、`ctyun_zos` 配置校验、S3 Client、HeadBucket、上传、HeadObject 和 URL 构造。
- 2026-07-29：完成 JSON 结构化日志、敏感字段清洗和 `NOTIFY` 日志入库。
- 2026-07-29：基础层 10 个自动测试全部通过。
- 2026-07-29：完成 FastAPI 生命周期、请求 ID、统一错误、请求体上限和上传并发守卫。
- 2026-07-29：完成存储 Provider preset、候选连接测试、masked 凭证读取、revision 激活和凭证继承。
- 2026-07-29：完成临时文件、200 MiB 校验、S3 上传、日期 UUID Key、幂等重放和失败持久化。
- 2026-07-29：完成任务列表、任务详情、健康、就绪、概览、流量、日志和可选 Provider 指标 API。
- 2026-07-29：完成启动恢复和周期恢复底座。
- 2026-07-29：完成同源 Dashboard 监控页，展示健康、概览、本地 Chart.js 流量图、近期任务和日志。
- 2026-07-29：完成存储设置页，支持 masked 凭证、连接测试、revision 确认和保存激活。
- 2026-07-29：动态内容使用 DOM `textContent`，页面不加载远程资源且不使用浏览器存储保存凭证。
- 2026-07-29：增加并发容量、伪造请求长度、未知结果、探测过期、跨站设置写入、统计 P95、日志分页与裁剪等边界测试。
- 2026-07-29：优化 31 天流量聚合为单次任务扫描，避免 Dashboard 数据量增长后产生任务数乘时间桶数的开销。
- 2026-07-29：完成非 root 生产镜像、容器健康检查、Compose 持久卷、局域网绑定示例和中文运行说明。
- 2026-07-29：前端 JavaScript 语法、Compose 配置与 Git diff 校验通过。
- 2026-07-29：生产容器 smoke test 通过：`/healthz`、两个 Dashboard 页面正常；未配置状态下 `/readyz` 按约定返回 503。
- 2026-07-29：最终 28 个自动测试全部通过。
- 2026-07-29：增加 `/v1/uploads/validate` 和 Dashboard 局域网接收测试区；复用正式接收限制，但不上传 ZOS、不创建任务。
- 2026-07-29：接收测试区增加默认关闭的真实上传开关；开启后复用正式上传接口并显示其响应。
- 2026-07-29：真实 ZOS 诊断确认 botocore 默认 checksum 导致 `XAmzContentSHA256Mismatch`；Provider 永久改为 `when_required`，上传对象固定使用 `public-read` ACL。
- 2026-07-29：通过正式 `/v1/uploads` 上传 `k3_tech_report.pdf` 验证成功；任务状态为 `succeeded`，匿名公网 HEAD 返回 200，大小 `1795077` 且 Content-Type 为 `application/pdf`。
- 2026-07-30：增加面向局域网 Linux 服务器的 SSH 部署脚本；本地测试、按 Git commit 构建镜像、传输、Compose 重建和健康检查由单条命令完成。
- 2026-07-30：Compose 镜像标签支持 `IMAGE_TAG`，服务器 `.env` 与持久卷不会被部署覆盖。
- 2026-07-30：完成首次局域网服务器部署；版本 `f38067ee0934` 容器健康，Dashboard 返回 200，数据库与临时文件持久卷已创建。
- 2026-07-30：在目标服务器安装 Portainer CE LTS 2.39.5，通过局域网 HTTPS 管理 Docker，配置独立持久卷和自动重启。
- 2026-07-30：将本机 19 条历史上传任务幂等合并到远程数据库，保留远程配置和 2 条新任务；合并后共 21 条，SQLite 完整性与 ZOS 就绪检查通过。

## 进行中

- 2026-07-31：根据外部 review 开始 v3 第一阶段；先冻结当前契约，再实施 schema v1→v2→v3 事务迁移。
- 2026-07-31：为当前公开 JSON 接口增加 Pydantic request/response model 和 OpenAPI 契约基线。
- 2026-07-31：完成 schema v3、逐版本事务迁移、SQLite Online Backup、完整性验证和默认预设兼容层。
- 2026-07-31：完成空库、v1、v2、重复启动、数据保持、迁移故障回滚和安全保留策略测试；共 34 个自动测试通过。
- 2026-07-31：使用远程生产 schema v1 的 SQLite Online Backup 副本完成离线演练；1 个配置、21 个任务、7 条日志全部保留，config/task ID 与 revision 保持，`integrity_check=ok` 且无外键问题。远程生产数据库未修改。
- 2026-07-31：完成上传后 HeadObject 确认；远端大小一致后才保存 ETag、可选 VersionId、`object_status=present` 并返回原 API v1 成功响应。
- 2026-07-31：恢复流程复用相同大小校验；404 标记对象不存在，超时保持待恢复，大小不一致保持 `unknown`。
- 2026-07-31：新增 HeadObject 成功、404、超时、大小不一致、元数据持久化和恢复测试；共 38 个自动测试通过。
- 2026-07-31：完成 256-bit URL-safe `delete_token` 签发；明文仅在首次 `201` 返回，SQLite 只保存 SHA-256，日志按敏感字段清洗，幂等重放固定返回 `null`。
- 2026-07-31：上传响应增加稳定对象元数据并使用 `Cache-Control: no-store`；Dashboard 接收测试不展示 token。
- 2026-07-31：增加 token 随机性、哈希持久化、重放和日志清洗测试；共 40 个自动测试通过。
- 2026-07-31：完成存储预设 repository：支持独立配置 revision、显示名与启停状态 revision、原子默认切换，并保持预设 key 不可修改。
- 2026-07-31：Runtime 改为不可变 `StorageSnapshot` 注册表，Provider 按 `storage_config_id` 缓存；设置更新原子替换目标预设快照，在途任务和历史恢复仍使用原配置。
- 2026-07-31：验证两个预设隔离、默认切换、旧 Provider 快照保持和 HTTP 预设 API 尚未暴露；共 42 个自动测试通过。
- 2026-07-31：开放预设列表、创建、详情、配置更新、显示名/启停和默认切换 API；写操作复用同源检查与 `X-Settings-Request`，响应统一 `Cache-Control: no-store`。
- 2026-07-31：默认设置兼容接口增加预设状态字段；候选连接测试可通过 `preset_key` 安全复用目标预设已保存的凭证。
- 2026-07-31：完成格式、唯一性、配置与状态 revision 冲突、禁用默认项、禁用项设默认、敏感字段清洗和完整管理生命周期测试；共 43 个自动测试通过。
- 2026-07-31：上传接口支持可选 `X-Storage-Preset`；未传时使用默认项，显式预设不存在、禁用或未配置时返回稳定错误且不回退。
- 2026-07-31：幂等键绑定首次任务的预设；默认切换或原预设禁用后仍可重放，显式改用其他预设返回 `IDEMPOTENCY_SCOPE_MISMATCH`。
- 2026-07-31：任务列表与详情返回 `storage_preset`；上传开始和成功日志记录预设 key。
- 2026-07-31：完成预设路由、失败不回退、幂等范围、禁用后重放和在途配置 revision 冻结测试；共 45 个自动测试通过。
- 2026-07-31：开放 `DELETE /v1/upload-tasks/{task_id}/object`；删除凭证使用 SHA-256 和常量时间比较，缺失、篡改及跨任务 token 均拒绝。
- 2026-07-31：删除通过 SQLite 条件更新抢占 `present → deleting`，使用任务原 `storage_config_id`，删除前后执行 HeadObject，并校验大小、ETag 和可选 VersionId。
- 2026-07-31：支持精确版本删除、已删除幂等响应、删除前已不存在、Provider 明确失败和 `202 DELETE_PENDING`；任务查询增加删除状态字段。
- 2026-07-31：完成 token、请求体、历史任务、元数据变化、旧配置 revision、并发删除、Provider 超时和数据库落盘失败测试；共 55 个自动测试通过。
- 2026-07-31：启动和周期恢复增加 `delete_unknown` 与陈旧 `deleting`；使用任务原 Provider revision 执行精确 HeadObject，不会再次主动调用删除。
- 2026-07-31：恢复确认不存在时转为 `deleted`，原对象仍一致时转回 `present + DELETE_FAILED`，元数据变化时转为 `present + OBJECT_CHANGED`，仍无法确认时保持 `delete_unknown`。
- 2026-07-31：删除开始、结果和恢复迁移写入不含 token 的 `object_delete_*` 持久审计；维护任务永久跳过这些事件。
- 2026-07-31：增加 `STALE_DELETE_SECONDS`（默认 900 秒），完成重启恢复、陈旧删除、保守恢复、审计内容和审计保留测试；共 60 个自动测试通过。
- 2026-07-31：Dashboard 设置页支持预设列表、创建、连接测试、配置 revision、显示名称、启停和默认切换，全程复用既有设置 API。
- 2026-07-31：Dashboard 真实上传测试可选择已启用预设；服务状态显示默认预设，任务列表显示预设、ETag、VersionId、对象状态与删除状态。
- 2026-07-31：Dashboard 保持删除状态只读，不保存凭证、不展示删除 token、不增加高风险删除操作；前端继续只用本地原生 JavaScript 和 DOM `textContent`。
- 2026-07-31：Dashboard v3 前端语法与静态安全检查通过，完整容器测试仍为 60 项全部通过，生产镜像 `ctyun-zos-upload:commit10` 构建成功。
- 2026-07-31：真实环境验收前确认远程仍运行 `f38067ee0934`、schema v1、21 条任务；健康和 ZOS 就绪状态正常，因此先建立跨 schema 安全部署链路。
- 2026-07-31：后台探测、恢复和维护循环增加统一 supervisor；异常写入 CRITICAL、就绪状态降级并自动重试，取消时干净退出。
- 2026-07-31：部署配置移入被忽略的 `.deploy.env`；部署增加完整测试、运行镜像构建、远程 SQLite Online Backup、schema 识别、health/ready 检查及跨 schema 失败回滚。
- 2026-07-31：增加最小 GitHub Actions 容器 CI 和真实 ZOS 并发上传、公网 HEAD、严格删除验收脚本；共 62 个自动测试通过。
- 2026-07-31：提交 `e3f7067` 后执行安全部署；远程 schema v1 已在线备份并迁移到 v3，原 21 条任务完整保留，`integrity_check=ok`。
- 2026-07-31：真实 ZOS 验收通过 1 MiB 单任务、4 个并发 20 MiB multipart、190 MiB 近上限文件及显式 `default` 预设上传；7 个对象均完成公网 HEAD 和严格删除，无验收对象遗留。
- 2026-07-31：不存在 Bucket 的候选连接测试返回 `502 STORAGE_BUCKET_UNAVAILABLE`，当前 active revision 保持为 1。
- 2026-07-31：受控重启后容器恢复 healthy，`/readyz` 显示 schema、恢复、存储探测和三个后台任务全部正常，`restart=unless-stopped`。
- 2026-07-31：为独立私有备份 Bucket 增加 SQLite Online Backup、数据库与 `SETTINGS_ENCRYPTION_KEY` 归档、PBKDF2 + Fernet 认证加密和 `private` ACL 上传。
- 2026-07-31：增加远端 HeadObject 大小确认、下载摘要、解密、SQLite `integrity_check`、非覆盖式恢复导出和每日 crontab 安装入口；错误密码、篡改、不安全 Endpoint 和文件权限测试通过。
- 2026-07-31：自动测试增加到 64 项；备份代码和生产镜像构建通过，提交 `5cd1e65` 并部署到远程服务器。
- 2026-07-31：首次真实私有备份成功，保存 schema v3、28 条任务、1 个预设及 `SETTINGS_ENCRYPTION_KEY`；加密对象大小 18598 bytes。
- 2026-07-31：从 ZOS 下载备份后完成摘要、解密和 `integrity_check`；恢复导出目录为 `700`、数据库和密钥文件为 `600`，任务数量与运行库一致，临时明文副本验证后已删除。
- 2026-07-31：备份对象匿名 HEAD 返回 403；每日 02:17 的用户 crontab 已安装且保持单一任务，Bucket 的版本控制和 30 天合规保留由 ZOS 执行。
- 2026-07-31：存储设置页从“天翼云 Bucket 列表”调整为“独立对象存储服务预设”；卡片展示 Provider、Endpoint 和 Bucket，新建与更新均从服务端 Provider schema 动态选择类型。
- 2026-07-31：新增 `s3_compatible` Provider，复用已验证的 S3 上传、HeadObject、严格删除和公网 URL 流程；保留 `ctyun_zos` 的扩展 Bucket 指标能力。
- 2026-07-31：通用 Provider 明确要求 S3 API、HeadObject、DeleteObject 和 `public-read` ACL，不假定兼容厂商私有协议；前端会按 Provider 能力启停扩展指标。
- 2026-07-31：多服务预设 UI、Provider schema 和生产镜像验证通过，自动测试增加到 65 项。
- 2026-07-31：提交并部署 `01dc272`；远程 Provider API 与设置页已显示天翼云和通用 S3 两种服务类型，原默认预设、私有备份配置和每日备份任务保持不变。

## 尚未完成

- 配置第二个真实 Bucket 和版本化 Bucket 后，验收跨 Bucket 路由、精确 VersionId 删除及外部替换对象保护。
- 完成上传/删除过程断网或杀进程等破坏性故障注入；当前重启恢复和模拟故障测试已通过。
- 观察下一次定时备份实际执行结果，并将独立备份密码持续保存在服务器之外。

## 下一步

确认下一次定时备份成功；之后准备第二个业务测试 Bucket，继续多预设与版本化对象验收。

## 2026-08-03 综合审查路线图

### 已完成：Phase 0 基线冻结与即时防护

- 纳入 `docs/review.md` 作为本轮 Phase 0–6 的执行路线图，并保存当前 `docs/openapi.json` 机器契约快照。
- 新增最终数据库写入失败、无删除能力对象、本地失败 retention、100 项恢复 backlog、首次部署失败和同 schema 回滚的严格 `xfail` 复现测试；对应 Phase 修复后必须转为普通通过测试。
- Compose 启用 Docker `local` 日志轮转、`cap_drop: ALL`、只读 rootfs、PID 上限和受限 `/tmp` tmpfs；加固生产容器 smoke test 通过，`/healthz` 正常且 schema v3 可写。
- 数据库默认路径统一为 `/data/db/zos-upload.db`；SDK ZIP 已从 Git 索引移除，Zone.Identifier 已删除并加入忽略规则。
- 完整容器测试结果：`67 passed, 5 xfailed`。5 项 xfail 是后续 Phase 1、3、4 的已知失败基线。
- 管理员 IP/VLAN 路径限制属于部署网络策略，当前没有可安全推断的管理员网段，因此未修改服务器防火墙；Phase 2 将以应用层管理员认证提供默认保护，网络隔离留作现场验收。

### 下一步：Phase 1

- 升级 schema v4，引入 `present_unclaimed`，在远端副作用前持久化文件大小，并统一失败、未知与恢复状态不变量。

### 已完成：Phase 1 上传状态完整性与 schema v4

- schema v4 新增 `present_unclaimed`，并用数据库 CHECK 约束 `status × object_status`：本地或确定失败为 `failed + absent`，不确定上传为 `unknown + pending`，成功对象进入明确的存在、无凭证或删除状态。
- 空库直接创建 v4；v1/v2 依次迁移，v3 事务迁移到 v4。升级前创建 `pre-v4` SQLite Online Backup，迁移失败保持原 schema 与表结构。
- 文件接收完成后、调用 Provider 前先持久化 `size_bytes`；该写入失败时不会产生远端副作用。
- ConnectionClosed、连接/读取超时、未知 SDK 异常和 Provider 5xx 使用保守的不确定语义；确定 4xx 拒绝可进入 `absent` 与 retention。
- 恢复确认对象存在但 `delete_token_hash` 缺失时进入 `present_unclaimed`；任务 API 增加 `delete_capability_available`，Dashboard 使用高优先级告警且公开严格删除继续拒绝该状态。
- 部署脚本从新镜像读取 `SCHEMA_VERSION`，跨 v3→v4 回滚不再依赖硬编码版本；README、PLAN、API 与 OpenAPI 快照已同步。
- 完整容器测试结果：`74 passed, 3 xfailed`；JavaScript 语法、Shell 语法和 Git diff 校验通过。
- 真实 ZOS 小文件、multipart 与近上限文件将在发布候选部署时使用现有验收脚本执行，避免开发阶段额外写入生产 Bucket。

### 下一步：Phase 2

- 增加管理员认证、Endpoint allowlist、管理路由保护和 `present_unclaimed` 管理清理接口。

### 已完成：Phase 2 管理控制面安全

- 新增必填 `ADMIN_API_KEYS`，每个 key 至少 32 字符并支持逗号分隔的新旧 key 并行轮换；常量时间验证 Bearer、`X-Admin-Key` 和浏览器原生 HTTP Basic。
- Dashboard、静态资源、设置、日志、OpenAPI、任务列表与完整详情统一进入管理控制面；普通上传、接收验证、health/ready 和持有 token 的严格删除保持原调用方式。
- OpenAPI 为管理操作声明 Bearer、Basic 与 Header key 三种可选 security scheme；管理员凭证字段进入日志递归清洗规则，不进入响应或页面 DOM。
- 新增 `STORAGE_ENDPOINT_ALLOWLIST` 与 HTTPS 默认策略；Provider 创建前校验主机/域名后缀/CIDR，并对 DNS 解析后的 loopback、link-local、metadata 与未授权私网地址再次拒绝。
- 新增 `DELETE /v1/admin/upload-tasks/{task_id}/object`，只清理 `present_unclaimed`，复用任务原配置、对象元数据、精确版本删除、删除恢复和永久审计。
- 完整容器测试结果：`79 passed, 3 xfailed`；加固生产容器 smoke test 验证 health 匿名 200、Dashboard 匿名 401、HTTP Basic 200。
- 独立管理端口、管理员 VLAN 与内网 HTTPS 属于部署网络策略；此前已明确本环境不启用内网 HTTPS/防火墙限制，因此当前以默认应用层认证作为实际边界，不擅自修改服务器网络。

### 下一步：Phase 3

- 为启动恢复增加预算、批次与并发，暴露 backlog；让 EventLogger 与公开运行配置真实进入 readiness。

### 已完成：Phase 3 有界恢复、超时与 readiness

- 启动恢复使用时间预算；每轮最多处理固定 batch，并按最大并发分组执行，剩余 backlog 交给后台 supervisor 后续轮次，不再顺序扫描全部历史任务。
- 恢复使用独立 Provider 实例与短 connect/read/retry 配置；普通上传 Provider 的长超时不再决定启动恢复上限。
- SQLite 提供 backlog 聚合：待恢复上传、待恢复删除、总数、最旧时间与年龄；`/readyz` 同时返回最近恢复成功时间。
- `EventLogger` 记录最近持久化成功/失败时间，成功写入会清除 degraded；日志持久化失败使 readiness 降级。
- `DASHBOARD_ENABLED=false` 时 Dashboard 页面、静态资源和 Dashboard API 返回 404；未实际生效的 `REQUEST_TIMEOUT_SECONDS` 已删除。
- 100 个不可达恢复任务测试确认单轮只发出 25 个 HeadObject，HTTP 服务完成启动并可观察剩余 backlog；Phase 0 对应 xfail 已转为通过。
- 完整容器测试目标现为 83 项通过、仅保留 Phase 4 的 2 项部署回滚 xfail；最终全量结果在本 Phase 提交前再次确认。

### 下一步：Phase 4

- 为部署增加远程锁、maintenance/drain、所有失败路径数据库回滚、首次失败清理和备份保留；让 verify/restore 不依赖运行容器。

### 已完成：Phase 4 部署、回滚与灾难恢复

- 发布产物先写入本次唯一 staging 目录，远程事务由 `flock` 串行化；锁冲突使用独立退出码，不会与发布失败混淆。
- 已有服务在发布前优雅停服，随后创建并执行 SQLite `integrity_check`；新镜像先以不映射端口的候选容器检查 health/ready，再重建正式局域网入口。
- 首次发布失败执行 Compose 清理；已有版本任意失败均恢复发布前数据库和旧镜像，同 schema 与跨 schema 使用同一路径，自动回滚失败会输出严重错误而不伪报成功。
- `deploy-backups/` 分别保留普通发布与跨 schema 快照，同时受总容量限制，并保护最新快照和最近跨 schema 快照。
- 私有异地备份增加数据库/备份对象大小和可用内存预检；每日任务改为上传后立即下载、解密、摘要及 SQLite 完整性校验。
- `verify/restore` 支持固定 `BACKUP_IMAGE`，没有运行服务时也可执行；未指定镜像时可从当前 checkout 构建独立 runtime 工具镜像。
- README 补充私有 Bucket Policy、Block Public Access、匿名 HEAD 验收以及空白 Linux 主机恢复步骤。
- mock Docker 覆盖首次发布失败、同 schema 回滚、跨 schema 回滚和无运行服务的固定工具镜像验证；完整容器测试 `91 passed`，生产镜像与两个灾备 CLI smoke test 通过。
- 未直接在当前远程服务器执行破坏性发布故障演练；真实首次/同 schema/跨 schema 演练留到发布验收窗口，避免未经确认停服或回滚生产数据。

### 下一步：Phase 5

- 移除上传二次 spool 并统一请求资源清理；隔离阻塞数据库工作并限制 Provider cache。
- 在明确兼容策略后引入调用方身份、作用域幂等键、限流与配额。
