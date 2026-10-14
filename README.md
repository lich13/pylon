# Pylon

Rust 实现的个人 Telegram 网关。核心提供多机器人连接、本人鉴权、可靠投递、共享网页登录、任务调度、进程管理和受限管理接口。业务代码、文案、规则、模型调用与特权 helper 在独立的普通目录维护，不属于这个仓库。

## 本机开发

使用 `rust-toolchain.toml` 和 `Cargo.lock` 固定工具链与依赖；日常工作不依赖云端开发环境。

```sh
cargo build --locked
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
.venv/bin/python -m pytest -q tests/rust_contract
```

Python 3.12 和测试依赖仅用于黑盒契约测试。正式网关是单一 Rust 可执行文件，不需要 Python 网关包。外置 Python 业务使用自己的锁文件及 `pylon_sdk`。

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

服务器上 `pylon modules rollback example --activate` 回到上一模块版本。普通部署只重启该模块的服务；网关进程和其他模块保持运行。在途任务固定启动时的源码版本。数据版本变化要求冻结新触发并等待相关任务结束。

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
