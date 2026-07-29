# MVP 工作记录

## 当前状态

进行中。实现以 [PLAN.md](PLAN.md) v4 和 [API.md](API.md) v1 为准。

## 已完成

- 2026-07-29：确认用户整理后的 v4 计划和 v1 API 是当前权威规格。
- 2026-07-29：完整阅读计划、接口文档和 ZOS SDK 包结构。
- 2026-07-29：确定使用 Python 3.11 Docker 环境构建和测试，避免宿主机 Python 3.14 与无 pip 环境造成偏差。
- 2026-07-29：规范当前规格文件名为 `PLAN.md` 和 `API.md`，保留旧版到 `legacy/`。

## 进行中

- 建立最小项目结构、依赖、SQLite schema、Provider 边界和 FastAPI 基础接口。

## 尚未完成

- Storage Provider registry 与 ZOS adapter。
- 加密存储配置、连接测试、revision 激活和首次环境导入。
- 上传临时文件、大小/并发限制、S3 上传、幂等和任务恢复。
- 任务、统计、日志、健康与就绪 API。
- Dashboard 监控页和设置页。
- 容器部署文件、完整自动测试与故障注入测试。
- 真实 ZOS 集成测试；需要可用的 Endpoint、Bucket、AK 和 SK。

## 下一步

完成基础服务与 SQLite schema，运行第一批自动测试并提交阶段性 Git commit。
