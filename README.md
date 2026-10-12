# Pylon 网关

Pylon 负责机器人连接、本人鉴权、可靠投递、任务执行、systemd 接入与受限管理操作。业务在相邻普通目录 `pylon-tasks` 独立维护，不纳入此 Git 仓库。

## 开发

Python 3.12，日常开发与针对性检查在本机执行，不使用 Grok Cloud。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/python -m pip install --require-hashes -r ../pylon-tasks/requirements.lock
.venv/bin/python -m pip install pytest==9.1.1
.venv/bin/python -m pytest -q tests
.venv/bin/python -m pylon.module_cli check itunes
.venv/bin/python -m pylon.module_cli preview content
```

模块入口自动发现相邻工作区，也可用 `--source` 或 `PYLON_MODULES_DIR` 指定。预览只绑定回环地址并使用临时样本数据库，退出时删除样本，不读取生产凭据。

## 结构与接口

- `src/pylon/`：网关、受限管理与模块生命周期。
- `src/pylon/sdk/`：版本 1 的路径、事件、状态、锁、事务和模型请求接口。
- `jobs.json`：只保留受控核心任务。
- `ops/`：可信 systemd 模板、sudo 边界与服务绑定。
- 外置 `module.json`：业务任务、定时规则、命令、按钮前缀、依赖与执行入口。

网关校验整个候选注册表，拒绝重复标识、不兼容协议、越界命名空间和特权任务。业务只在普通用户独立进程执行，通过 JSON 交换请求与结果；网关与 root 管理入口不导入外置实现。受限 Cookie 控制所需 Playwright 由业务运行时锁文件安装，root 只导入受控校验代码。

任务启动时固定模块版本和依赖，进程租约保护这些源码，部署不会杀死正在等待模型的任务。保留原数据库位置、表名、任务 ID、去重键与游标；投递不确定记录不会自动重发。

扩展处理器通过 `pylon.sdk.serve(handle)` 接收 `{api_version: 1, action, profile, payload}`，返回 `{api_version: 1, value}`。处理器日志写到 stderr，stdout 只传 JSON。普通命令处理器有 150 秒边界；长任务由清单 `jobs` 交给 runner，保留各任务原有 `timeout`，其中 `null` 表示持续等待。

业务使用 `pylon.sdk.events` 报告结果和投递，使用 `Outbox.connect()` 管理事务，使用 `profile_root()` 与 `sdk.paths` 访问原状态目录。业务之间的依赖写入 `requires`；业务工具放在外置模块中。SDK 的权限校验、回调签名与状态语义变化才需要网关兼容验证。

## 模块更新

```sh
.venv/bin/python -m pylon.module_cli check software
.venv/bin/python -m pylon.module_cli ci content
.venv/bin/python -m pylon.module_cli deploy itunes --host tencent-cloud
.venv/bin/python -m pylon.module_cli rollback itunes --host tencent-cloud
```

`check` 自动包含反向依赖。普通业务变更不修改网关版本、不要求 Git 提交或正式 Release。源包只包含清单和源码，校验 SHA-256 后进入不可变版本目录；候选在隔离数据下以普通用户检查，注册表原子切换，失败保留旧版本。部署记录内容哈希，保留当前、上一版以及仍被运行任务引用的版本。

业务脚本更新不重启网关。服务绑定由网关 `ops/module-services.json` 控制，内容套件更新只重启相应网页和采集服务。任务描述变更拒绝影响仍在运行的任务；数据或协议变化需要专门迁移。

私有 `modules.yml` 接收临时源码包与网关提交 SHA，验证哈希后执行受影响测试及必要网页检查，结束后删除临时草稿和输入 artifact。`ci content --router` 额外验证 ARM64 镜像。网关 push 只触发核心测试；接口、权限、调度或投递语义变更还需提交外置兼容 CI。

## 生产

生产继续由腾讯云 systemd 运行。本机只是开发、预览和部署入口。`/srv/pylon/src`、虚拟环境和模块版本由 root 持有，运行用户只有业务状态写权限；不要将安装目录改归运行用户。

`ops/bootstrap.py` 安装受控入口与任务单元，不启用调度。首次结构迁移先暂停新触发，等待在途任务结束，保留服务启用状态和 outbox，再安装核心与外置版本、恢复原先服务。日常业务更新使用模块入口，无需重复 bootstrap 或数据库迁移。

现有三处网页地址、登录、反馈与 Grok 配置保持原协议。软件更新仅执行受控白名单动作，所有操作由本人鉴权；不提供外置脚本的 root 执行入口。
