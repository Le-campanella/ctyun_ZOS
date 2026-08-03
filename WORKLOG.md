# 当前工作记录

历史 MVP 至 Phase 5 记录已归档到 [docs/releases/mvp-v1.md](docs/releases/mvp-v1.md)。

## 进行中：Phase 6

- 固化带 SHA256 哈希的生产与开发依赖锁文件，并固定 Python 基础镜像 digest。
- 增加 Ruff、mypy、ShellCheck、pip-audit、Trivy、SBOM 与 OpenAPI 快照门禁。
- 升级存在已知漏洞的 Python 依赖，并从 runtime 镜像移除不需要的包管理/构建工具。
- 收敛 `docs/current`、`docs/rfcs`、`docs/releases`，区分现行契约、未来提案与历史记录。
- 移除工作区中的 Windows Zone.Identifier 元数据。Git 对象库当前约 12.39 MiB，最大受控文件约 1.2 MiB，无需承担协作中断风险去重写历史。
- 暂不增加 `THIRD_PARTY_NOTICES.md`：本地 Chart.js 文件已保留 MIT 许可头，CI 生成的 SBOM 覆盖 Python 依赖；有独立法务交付要求时再集中生成 notices。

## 下一步

- 完成 API、数据库和运行时的最小职责拆分，保持行为测试不变。
- 完整回归并分阶段提交 Phase 6。
