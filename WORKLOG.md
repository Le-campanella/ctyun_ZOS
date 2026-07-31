# MVP 工作记录

## 当前状态

第一版 MVP 已完成本地构建与自动验收。当前 HTTP 路径仍为 `/v1`；仓库数据库已升级为 schema v3，上传对象元数据确认、一次性删除凭证、多预设 Runtime、管理 API 和显式上传路由已经实现，但 DELETE API 尚未发布。[docs/PLAN.md](docs/PLAN.md) v6 和 [docs/API.md](docs/API.md) v3 的其余能力仍是未发布目标。

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
- 2026-07-30：增加面向 `liyang@192.168.1.150:~/services/ctyun_ZOS` 的 SSH 部署脚本；本地测试、按 Git commit 构建镜像、传输、Compose 重建和健康检查由单条命令完成。
- 2026-07-30：Compose 镜像标签支持 `IMAGE_TAG`，服务器 `.env` 与持久卷不会被部署覆盖。
- 2026-07-30：完成首次局域网服务器部署；版本 `f38067ee0934` 容器健康，Dashboard 返回 200，数据库与临时文件持久卷已创建。
- 2026-07-30：在目标服务器安装 Portainer CE LTS 2.39.5，通过 `192.168.1.150:9443` 管理 Docker，配置独立持久卷和自动重启。
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

## 尚未完成

- 多预设 Dashboard。
- DELETE API、严格删除、删除恢复与审计。
- 完成接近 200 MiB、multipart、重启恢复等剩余真实 ZOS 压力与故障验收；基础 PDF 上传和公网访问已经通过。
- 在目标局域网部署内网 HTTPS 反向代理、网络访问控制、数据库备份和 Bucket 生命周期规则；这些属于部署环境工作。

## 下一步

进入下一提交：实现严格 DELETE API、对象身份校验、删除状态持久化和不确定结果恢复。
