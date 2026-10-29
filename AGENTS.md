# Pylon 开发约定

- 本项目日常开发、检查与预览在本机执行，不使用 Grok Cloud/devhost-local。Rust 使用工具链文件和 Cargo.lock；本机黑盒测试复用 `.venv`；GitHub CI 只构建和测试网关本身。
- 仓库只维护通用 Rust 网关、扩展协议、root broker 及模块管理。业务、独立 `pylon_sdk`、具体 root helper 和迁移部署配置位于相邻的 `pylon-tasks` 外置目录，不初始化业务 Git 仓库。
- 业务改动使用 `pylon modules check <模块>`、`deploy`、`rollback`；只检查受影响模块和接口契约，全部在本机完成，不使用 CI，不把模块源码上传到任何公开位置。离线运行时只在 Python 版本或依赖锁变化时用 `scripts/module-runtime.py` 本机重建，否则复用缓存。
- 生产数据、凭据、运行状态与代码分开；不把生产配置或数据打入模块包。沿用任务 ID、投递幂等键和现有状态，不重放不确定操作。
- 修改网关/SDK 后验证受影响业务兼容；普通业务部署不提交或重启网关。保护仍在运行的旧版本和已验证的上一部署版本。
