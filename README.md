# Pylon

Rust 实现的个人 Telegram 网关。核心提供多机器人连接、本人鉴权、可靠投递、共享网页登录、任务调度、进程管理和受限管理接口。业务代码、文案、规则、模型调用与特权 helper 在独立的普通目录维护，不属于这个仓库。

## 本机开发

本机 `rust-toolchain.toml`、最低支持版本与 CI 均固定为 Rust 1.99.0，edition 为 2024；依赖沿用 `Cargo.lock`，日常工作不依赖云端开发环境。

```sh
cargo build --locked
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
.venv/bin/python -m pytest -q tests/rust_contract
```

Python 3.12 和测试依赖仅用于黑盒契约测试。正式网关是单一 Rust 可执行文件，不需要 Python 网关包。外置 Python 业务使用自己的锁文件及 `pylon_sdk`。

首次准备契约测试环境时运行 `.venv/bin/python -m pip install --require-hashes -r tests/rust_contract/requirements.lock`。CI 使用相同锁文件，并用固定版本的 cargo-audit 检查 Cargo.lock。

GitHub Actions 执行完整网关检查与 Linux root 权限测试，产出 `pylon-linux-x86_64`、`SHA256SUMS` 和 `REVISION`。生产使用对应提交的 CI 成品。

## 扩展边界

`module.json` v2 注册命令、回调、生命周期钩子、任务、服务、网页路径、通知主题与权限申请。注册表冲突、依赖循环或配置不兼容时保留上次有效版本。协议见 [扩展协议](docs/extensions.md)。

业务通过 Unix socket 上的 JSON API 调用投递、回执、任务与登录能力，不能连接网关数据库。业务结果及本地待投递记录在同一业务事务中提交，再按事件 ID 幂等转交；结果不确定时只核对，不自动重放。

```sh
target/debug/pylon modules check example --directory ../pylon-tasks
target/debug/pylon modules preview example --directory ../pylon-tasks --port 8795
target/debug/pylon modules deploy example --directory ../pylon-tasks --host production-alias
target/debug/pylon modules ci example --directory ../pylon-tasks --gateway .
```

服务器上 `pylon modules rollback example --activate` 回到上一模块版本。普通部署只重启该模块的服务；网关进程和其他模块保持运行。在途任务固定启动时的源码版本。停止准备钩子会等待本模块在途工作；数据版本变化还要求冻结新触发。

外置源码需要 CI 时使用私有仓库的临时哈希校验包，运行后删除输入包，不把业务源码提交回网关仓库。

## 机器人与路由

`config/bots.json` 保存机器人标识、本人 ID、凭据引用、数据工作区、主题路由和资源配额。真实 token 单独保存为 `gateway/secrets/` 下的 0600 文件，仅网关运行用户可读。

```sh
pylon bots add example --owner 123456 --username example_bot
pylon bots check --online
pylon routes topic_example example
pylon bots reload
pylon bots disable example
```

`bots add` 从隐藏的终端输入读取 token，也支持受控标准输入；不要把 token 放入命令参数。重复凭据拒绝注册。配置约五秒内生效，无需编译或重启。新增机器人等待本人先发起私聊后才投递，待发事件保持在队列。

机器人各自保留 Telegram 游标、退避及队列。主题路由变更只影响新事件，旧消息和待发记录仍属于原机器人。网页登录按同一本人共享；机器人归属与业务数据工作区相互独立。

## 运行与迁移

`pylon modules render <目录>` 生成通用 systemd 模板。网关使用专用普通用户；Rust executor 以受控监督进程运行，所有扩展子进程切换到模块各自的普通 UID。root broker 只执行管理员明确授权、root 持有的固定 helper，不接收任意 root 命令。

HTTPS 代理统一转发 `/pylon/` 到回环网关，网页路径由注册表决定；模块网页仅监听回环地址。共享登录、CSRF 和回调签名通过 SDK 访问。

升级前保留生产状态并冻结新触发。一次性 `pylon migrate --source <业务数据库> --mapping <归属映射>` 要求目标为空及在途任务已结束，保留事件 ID、回执、不确定状态、游标和登录会话。业务表留在业务数据库。收到新流量后不能直接恢复旧数据库快照，必须先保留并合并新增状态。

只读诊断使用 `pylon diagnose`。普通扩展通过 `diagnostics:read` 能力查看运行摘要；任务拥塞、结果续交、停止准备与兼容契约见扩展协议。


## 故障隔离与诊断

轮询、命令、Telegram 投递、Bark 投递和菜单同步分别运行；Bark 等待不阻塞 Telegram。
SQLite 操作在有界阻塞执行层处理，已确认的远端回执只重试本地保存，不重复发送。
请求已发出但结果无法确认时保留 `uncertain`，包括返回 HTML 的 HTTP 5xx。

`config/bots.json` 可选 `web_port`（默认 8793，仅回环监听，修改后重启）和
`telegram_ip_family`（`ipv4`、`ipv6` 或 `auto`，默认 `ipv4`）。无效重载保留整个旧配置。
诊断提供队列等待时间、worker 故障次数、投递耗时及配置拒绝摘要，不包含消息或凭据。

网关系统恢复提醒按机器人合并，系统提醒有效期为 10 分钟；业务消息仅遵循其显式有效期。
新增投递不确定记录每天最多向个人运维主题汇总一次，上线前记录只展示、不补发。
进程超时保留最多 64 KiB 输出尾部的字节数、指纹及已知异常类型；不可信正文全部脱敏，
诊断文件仅保存在 supervisor 私有目录。任务输入和输出收尾均受原有限时约束；无限模型等待不受此限制。


## v0.5.4 运行边界

Telegram 轮询、命令、投递和 Bark 使用独立异步循环；慢 RPC 使用独立的 64 个许可，等待准入超过 1 秒返回可重试的 unavailable。健康检查不占用慢请求许可。配置快照不会跨网络等待持锁，网页请求正文读取上限为 30 秒。

SQLite 写冲突最多等待 5 秒；已执行命令的结果、回复和上下文撤销一起提交。回执已收到而本地暂时无法保存时，只补存回执。Telegram 成功投递必须有有效消息编号和正确的目标会话；无效回执、HTML 5xx 和发送后中断继续保持 uncertain，不自动重发。业务消息沿用自行声明的有效期，不新增统一过期或重试次数上限。

executor 每 2 秒独立刷新任务摘要，转交结果的网络等待不会阻塞刷新。诊断包含完整排队计数、等待时间、当前进程的 SQLite 占用次数和 executor 错误计数。机器人恢复后结束重试状态；累计次数保留到进程退出。

新增 uncertain 采用版本化 KV 计数和事务内触发器维护，不改变 events/updates 列或事件 ID。旧水位只转换一次，已有历史不补发；每天最多一条摘要，当天后续新增项留到次日首次汇总。摘要自身不递归统计。无效配置继续使用上一有效版本，并向原 personal 路由发送按配置指纹去重、10 分钟有效的提示。

升级须等待在途任务及投递结束。停止空闲轮询可以直接取消只读请求；已执行命令、发送和结果补存等待完成。回退保持当前数据库；若退回旧计数实现，在冻结且网关已停止的状态下，用同一事务将旧 uncertain 水位对齐当前记录，并把 `uncertain:counter:v2` 移到 `uncertain:counter:v2:retired` 保留键，再启动旧版。触发器保留，但旧版运行期间不累积新计数；重新升级从旧水位重新建立计数，不还原数据快照或重复汇总已报告记录。
