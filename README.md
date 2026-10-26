# Pylon

Rust 实现的个人 Telegram 网关。核心提供多机器人连接、本人鉴权、可靠投递、任务调度、进程管理和受限管理接口。业务代码、文案、规则、模型调用与特权 helper 在独立的普通目录维护，不属于这个仓库。

## v0.5.17 部署修复

运行时准备允许服务器已安装 CI 产物中的目标 Node 版本；升级和回退均选择与实际记录版本匹配的包。未知版本仍拒绝切换。保留 v0.5.16 的效率与可靠性改动、现有依赖版本及匹配的 v0.5.15 回退资料。

## v0.5.16 效率与可靠性

通知转交通过索引分别筛选尚未提交和仍需核对的记录，保留顺序、重试和幂等行为；常驻转交服务复用 HTTP 连接，每次请求仍读取当前凭据与超时。格式错误的 RPC 响应进入不可自动重试的核对状态。

状态页显示完整排队数量，任务列表仍保留 200 条窗口。Gmail 批次面板复用单次渲染的候选快照。业务 SQLite 调用显式关闭连接，附件在同一次流式复制中计算摘要，限制仍为 50 MiB，完成后原子落盘再入队。

移除不可达的交易编辑实现、重复 Chiphell 控制代码和退役日报实时钩子，保留停用提示、旧按钮鉴权、独立 root helper、归档初始化及首次发现时间。Broker 在启动时初始化 Store，阻塞检查进入既有执行层，每次调用继续实时核验凭据、授权及文件归属。浏览器采集与 KDocs 保持现有实现。

API v2 和业务记录保持兼容，仅新增通知查询索引；SDK 调用与转交可选传入复用客户端。升级沿用现有冻结、排空、切换与健康核验，保留匹配的 v0.5.15 运行时、模块和网关回退资料。

## 运行环境

专用运行时优先使用常规 GIL Python 3.14.10；仅当其兼容检查失败而相同依赖的 Python 3.12.16 全部通过时使用后者。系统 Python 保持发行版管理。Node 22.23.4、Playwright 1.64.1、certifi 2026.10.23、pip 26.2.2，保留必要依赖并排除 uvicorn、starlette。

外部模块 CI 从校验后的官方源码构建两种 Python，验证六个模块、SDK、特权辅助程序、网关契约和无账号的浏览器页面。发布同时附带独立的 `pylon-runtime.tar.gz` 与 `RUNTIME_SHA256SUMS`；运行时、模块及网关绑定同一提交。服务器仅离线安装 CI wheel，不编译 Python 或依赖。先准备新环境，冻结并排空后切换；回退记录同时保存原虚拟环境、浏览器、Node、模块和网关，支持恢复至 v0.5.14，失败恢复后保持冻结。

## v0.5.14 运行与升级

本版退役网页代理、网页登录与旧架构导入工具，保留 Telegram 鉴权和按钮、Linux.do 专用导入、HTML 归档、附件、Bark、调度及诊断。

- 网关仅提供 Unix socket API。旧网页路径由 HTTPS 服务直接返回已下线；不再转发到网关。升级事务移除旧网页令牌和会话表，保留业务、任务、回执、幂等占位与执行上下文。
- 摘要查询使用未完成与近期完成的两个部分索引，展示窗口仍为 200 条，完整队列统计独立计算；相同创建时间沿用原表扫描顺序。输出使用有界环形缓冲，任务与命令仍保留各 8 MiB，常驻服务只保留各 64 KiB 诊断尾部。
- 附件最多 50 MiB，打开后先检查大小，读取期间继续限制；摘要与上传使用同一份字节。符号链接、越界、多硬链接和内容校验失败均拒绝发送。
- 业务进程启动后的输入、读取、等待与响应错误均视为结果待核对，不自动重试。模块健康检查保留 60 秒／1 MiB、停止准备保留 30 秒／64 KiB 上限；失败回收检查进程组并保留原服务。
- 服务新凭据先写入已有私有缓存，再撤销经缓存与进程引用核对的旧哈希；撤销失败保留待办，下次安全启动续办。无法确认无人使用时保留。
- 请求共享不可变配置快照；热加载整体替换，旧请求继续原快照，无效配置仍保留上次有效版本。

- 升级脚本先校验 CI 产物，再冻结新准入，并等待所有在途任务、启动中的请求、命令、投递和完成缓冲排空，默认 300 秒。排空使用完整状态，不以任务摘要的 200 条展示窗口代替。
- 新版三个核心服务 active、网关诊断版本一致、executor／broker socket 可连接后才解冻，健康等待默认 60 秒。失败返回非零；开始停服务后的失败自动恢复上一版二进制与 REVISION，保持冻结，不恢复旧数据库、不自动重试切换。回退兼容检查受阻时保留冻结并报告 blocked。
- `scripts/deploy-gateway.sh` 支持 `--drain-timeout`、`--health-timeout`；独立升级成功后解冻，协调升级使用 `--keep-frozen` 留给外置部署入口核验受影响服务并解冻。`pylon drain-status` 是 root 只读检查，输出冻结状态及 tasks／starting／commands／deliveries／completions 全量计数；仅冻结且全部为零时退出 0。
- 锁回收仅处理 request／dispatch 锁。job、daemon、quota 及其它锁均保留；已有外部持有者不一定会复核文件身份。保留 completed 请求按锁龄回收和不安全文件软跳过行为。
- execute、resume 和后台派发共用恢复准入，启动登记前也受保护。恢复模式与完成时间保存在任务库的单行 recovery 表；新增表兼容旧版本，不改变请求字段。诊断任务 metrics 保留 recovery_active，并增加 recovery_completed_at；排空后恢复原主题配额。
- 两个既有维护任务每分钟检查冻结、恢复完成和 WAL 阈值，普通保留清理仍在启动 10 分钟后开始、每小时一轮。checkpoint 在维护路径执行；冻结时跳过，busy 后下轮重试，retention.enabled=false 不停止 WAL 维护。解冻后的锁回收在下次分钟检查执行。
- 冻结提醒从实际发送入队时间计算重复间隔，复用 freeze-alert:last；兼容旧记录，停止新增逐时间桶的 KV 键。原冻结提醒白名单保持不变。

`downgrade-check --target-version 0.5.13` 及目标 0.5.11、0.5.12 均接受其已支持的有效事件占位记录，仍拒绝损坏状态和未处理完成缓冲。API v2、数据库格式和执行幂等规则保持兼容。生产发布继续使用 main 提交对应的成功 CI 成品，标签不另行构建。

## 本机开发

本机与 CI 共用 `rust-toolchain.toml` 的固定工具链版本，最低支持版本在 `Cargo.toml` 中维护，edition 为 2024；依赖沿用 `Cargo.lock`，日常工作不依赖云端开发环境。

```sh
cargo build --locked
cargo fmt --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
.venv/bin/python -m pytest -q tests/rust_contract
```

Python 3.12 和测试依赖仅用于黑盒契约测试。正式网关是单一 Rust 可执行文件，不需要 Python 网关包。外置 Python 业务使用自己的锁文件及 `pylon_sdk`。

首次准备契约测试环境时运行 `.venv/bin/python -m pip install --require-hashes -r tests/rust_contract/requirements.txt`；使用 uv 管理的环境可运行 `uv pip install --python .venv/bin/python --require-hashes -r tests/rust_contract/requirements.txt`。CI 使用相同哈希锁文件，并用固定版本的 cargo-audit 检查 Cargo.lock。

GitHub Actions 执行完整网关检查与 Linux root 权限测试，产出 `pylon-linux-x86_64`、`SHA256SUMS` 和 `REVISION`。生产使用对应提交的 CI 成品。

## 依赖维护

Dependabot 每周一 09:00（Asia/Taipei）检查 Cargo、Python 契约测试和 GitHub Actions，普通更新冷却 7 天，只接受 minor／patch，每类最多一个分组 PR；安全更新独立分组，不受普通更新类型和冷却限制。Cargo 同时覆盖直接、开发和间接依赖，采用 `lockfile-only`，合并前核对锁文件仍满足清单约束。所有 PR 均由人工合并；合并后删除分支。CI 在 main push 和 PR 上运行，机器人分支 push 不重复执行。

Rust 工具链每月由 Dependabot 检查；更新后同时核对最低支持版本。每月人工检查 Python、Node、cargo-audit，以及外置模块检查工作流中的独立工具版本。Actions 继续固定提交 SHA，同一 Action 使用同一引用。外置业务模块及其依赖由各自目录维护，不进入核心依赖更新范围。

依赖更新须通过哈希安装、Cargo 审计及既有 CI。`serde_json/float_roundtrip` 必须保留，以维持持久化完成记录的校验一致性。reqwest、rusqlite、sha2／hmac 跨兼容线升级单独评估，有安全必要时优先处理。

## 扩展边界

`module.json` v2 注册命令、回调、生命周期钩子、任务、服务、通知主题与权限申请。注册表冲突、依赖循环或配置不兼容时保留上次有效版本。协议见 [扩展协议](docs/extensions.md)。

业务通过 Unix socket 上的 JSON API 调用投递、回执、任务与本人鉴权能力，不能连接网关数据库。业务结果及本地待投递记录在同一业务事务中提交，再按事件 ID 幂等转交；结果不确定时只核对，不自动重放。

```sh
target/debug/pylon modules check example --directory ../pylon-tasks
target/debug/pylon modules deploy example --directory ../pylon-tasks --host production-alias --artifacts ./external-dist
target/debug/pylon modules ci example --directory ../pylon-tasks --gateway .
```

`modules ci all` 在既有私有矩阵全部通过后生成匹配提交的 SDK、模块与必要 helper 部署成品。单模块 CI 只做针对性检查；部署入口使用已验证的成品目录，不在本机重新打包。协调升级保留匹配的 SDK、模块、helper 和配置用于回退，旧版所需运行依赖在回退窗口内保留。

服务器上 `pylon modules rollback example --activate` 回到上一模块版本。普通部署只重启该模块的服务；网关进程和其他模块保持运行。在途任务固定启动时的源码版本。停止准备钩子会等待本模块在途工作；数据版本变化还要求冻结新触发。

外置源码需要 CI 时使用私有仓库的临时哈希校验包，运行后删除输入包。既有检查通过后生成 SDK、模块包及校验清单，部署直接使用 CI 字节，不在本机重新打包；不把业务源码提交回网关仓库。

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

机器人各自保留 Telegram 游标、退避及队列。主题路由变更只影响新事件，旧消息和待发记录仍属于原机器人。机器人归属与业务数据工作区相互独立。

## 运行与升级

`pylon modules render <目录>` 生成通用 systemd 模板。网关使用专用普通用户；Rust executor 以受控监督进程运行，所有扩展子进程切换到模块各自的普通 UID。root broker 只执行管理员明确授权、root 持有的固定 helper，不接收任意 root 命令。

旧 `/pylon/` 入口直接返回已下线。升级前冻结并排空，失败保留数据库与冻结状态；回退同步恢复匹配的 SDK 与模块版本，不自动重试切换。数据库内部初始化随网关启动完成，不再提供旧架构导入命令。

只读诊断使用 `pylon diagnose`。普通扩展通过 `diagnostics:read` 能力查看运行摘要；任务拥塞、结果续交、停止准备与兼容契约见扩展协议。


## 故障隔离与诊断

轮询、命令、Telegram 投递、Bark 投递和菜单同步分别运行；Bark 等待不阻塞 Telegram。
SQLite 操作在有界阻塞执行层处理，已确认的远端回执只重试本地保存，不重复发送。
请求已发出但结果无法确认时保留 `uncertain`，包括返回 HTML 的 HTTP 5xx。

`config/bots.json` 可选 `telegram_ip_family`（`ipv4`、`ipv6` 或 `auto`，默认 `ipv4`）。无效重载保留整个旧配置。
诊断提供队列等待时间、worker 故障次数、投递耗时及配置拒绝摘要，不包含消息或凭据。

网关系统恢复提醒按机器人合并，系统提醒有效期为 10 分钟；业务消息仅遵循其显式有效期。
新增投递不确定记录每天最多向个人运维主题汇总一次；已存在旧水位的历史记录不补发。
进程超时保留最多 64 KiB 输出尾部的字节数、指纹及已知异常类型；不可信正文全部脱敏，
诊断文件仅保存在 supervisor 私有目录。任务输入和输出收尾均受原有限时约束；无限模型等待不受此限制。


## v0.5.5 运行边界

Telegram 轮询、命令、投递和 Bark 使用独立异步循环；慢 RPC 使用独立的 64 个许可，等待准入超过 1 秒返回可重试的 unavailable。健康检查不占用慢请求许可。配置快照不会跨网络等待持锁，RPC 请求正文读取上限为 30 秒。

SQLite 单次写冲突最多等待 5 秒；已执行命令的结果、回复和上下文撤销一起提交。投递回执及命令完成结果先原子写入 `gateway/completions/` 私有缓冲（目录 0700、文件 0600，写入及目录变更均 fsync），前台补存数据库最多等待 60 秒，并响应停止信号。后台和重启恢复只补交结果，不再次发送或执行命令；记录核对原事件、机器人、内容和尝试编号，提交后才移除。完整有效回执可恢复为 delivered，部分投递或未知结果保持 uncertain。Telegram 成功投递必须有有效消息编号和正确的目标会话；无效回执、HTML 5xx 和发送后中断继续保持 uncertain，不自动重发。业务消息沿用自行声明的有效期，不新增统一过期或重试次数上限。

executor 每 2 秒独立刷新任务摘要，转交结果的网络等待不会阻塞刷新。诊断包含完整排队计数、等待时间、当前进程的 SQLite 占用次数和 executor 错误计数。现有 `diagnostics` RPC 的 `metrics.telegram_proxy_invalid` 统计 HTTP 502/503/504 的无效 Telegram 写响应，`metrics.telegram_response_invalid` 统计其他无效写响应；这两个计数随网关进程重启清零，不改变投递分类，也不写入 executor 的任务摘要。机器人恢复后结束重试状态；累计次数保留到进程退出。

新增 uncertain 采用版本化 KV 计数和事务内触发器维护，不改变 events/updates 列或事件 ID。初始化幂等保留现有计数；缺失计数时按旧水位计算未汇总记录，没有旧水位则统计现存非摘要 uncertain。触发器可重建意外缺失的计数；已明确退役的计数除外。每天最多一条摘要，当天后续新增项留到次日首次汇总；已有当日摘要不会导致待汇总数被清零。摘要自身不递归统计。无效配置继续使用上一有效版本，并向原 personal 路由发送按配置指纹去重、10 分钟有效的提示。

计划升级仍须等待在途任务及投递结束。网关 SIGTERM 使用全局退出预算（异步 worker 最多 5 秒、运行时收尾最多 2 秒），空闲轮询可立即停止；无法及时补存的结果保留在私有缓冲，启动时先核对缓冲再处理未知的中断状态。取消网关等待不停止独立 executor 或模型任务。回退保持当前数据库；退回不认识完成缓冲的旧版前，必须先由新版处理完缓冲，不直接删除记录。若退回旧计数实现，在冻结且网关已停止的状态下，用同一事务将旧 uncertain 水位对齐当前记录，并把 `uncertain:counter:v2` 移到 `uncertain:counter:v2:retired` 保留键，再启动旧版。触发器保留，但旧版运行期间不累积新计数；重新升级从旧水位重新建立计数，不还原数据快照或重复汇总已报告记录。任务、派发和模块租约使用 Rust 标准库文件锁，保留继承描述符的 dev/ino 校验和 CLOEXEC 保护。

## v0.5.6 变更

`diagnostics` 增加 `completions_pending`：私有完成缓冲中的文件数，包含临时文件和无法解析的记录。目录缺失返回零；异常文件类型、链接、归属或权限返回检查错误，不伪装为空。检查不读取结果正文、不创建目录、不补交结果。无扩展上下文的 `pylon diagnose` 仍需 root 权限。

## v0.5.7 性能与占用

自动热加载约每 5 秒检查配置、registry、模块完整源码清单、凭据元数据及路径边界。未变化时跳过模块内容哈希和凭据读取；直接修改 live 源码仍会触发完整验证。无效或持续变化的候选配置保留上一完整快照。`admin.reload` 始终强制完整加载，成功仍返回 `{"ok":true}`；Telegram 菜单沿用独立同步周期。

完成缓冲仅在确认空目录时按 1、2、5、10 秒退避；进程内持久写入和写入方释放所有权会唤醒协调任务，外部写入最多 10 秒兜底发现。Telegram／Bark 空 claim 按 0.5、1、2 秒退避，数据库提交后按机器人和车道通知；未来到期事件提前结束空闲等待，通知不绕过限流、重试或 require_start。冻结实时检查、五车道隔离、完成缓冲 fsync 和 SQLite FULL 保持不变。

`diagnostics.metrics.optimization` 提供 `reload_full`、`reload_skipped`、`completion_scans`、`delivery_empty_claims` 四项进程内累计计数，重启清零，不写数据库，不含凭据或路径。完整加载计数包含加载尝试；扫描计数包含启动恢复、退役协调和后台扫描。评估空闲频率时应在同一进程中、配置和流量稳定后比较增量。

移除 reqwest SOCKS 和 chrono-tz，Tokio 使用明确的必要 features；UTC+8 日键仍为 `%Y-%m-%d`，保留历史水位和每日摘要去重。release 仍使用 thin LTO 和 `strip = "debuginfo"`。

替换为旧版之前，用当前新版二进制执行只读检查（v0.5.8 的目标为 0.5.7）：

```sh
sudo pylon --home /srv/pylon downgrade-check --target-version 0.5.7
```

标准输出为 JSON，包含 `ok`、`target_version`、`completions_pending` 和固定短码 `reason`；通过退出 0，阻断或无法确认退出 1。命令直接检查本地状态，无需网关或联网；不初始化数据库、不转换 counter、不处理完成缓冲，也不创建缺失的数据库辅助文件。已有 WAL 必须正常读取；只有取得排他数据库锁且确认没有未归并日志时，才按静态文件读取。检查遇到竞争或需恢复的数据库时拒绝通过。

| 目标 | 必要条件 |
| --- | --- |
| 0.5.7 | 完成缓冲为空、状态有效；无有效事件占位记录 |
| 0.5.6 | 完成缓冲为空、状态有效；保留正常 v2 counter |
| 0.5.5 | 完成缓冲为空、状态有效；保留正常 v2 counter |
| 0.5.4 | 完成缓冲为空、状态有效；该版本已支持 v2 counter，不执行退役转换 |
| 0.5.3 | 缓冲为空，无活动 v2 counter，合法旧水位已覆盖现有 uncertain 事件；存在 v3 触发器时还须保留有效 retired counter，转换另行执行 |
| 其他版本 | 尚未核实，拒绝通过 |

所有旧目标还必须没有有效的事件占位记录；存在时返回 `retained_events_require_newer_gateway`。占位状态损坏或有效期不明同样拒绝，命中计数为零不能绕过。空缓冲但 counter 损坏、权限异常或无法读取时仍拒绝通过。先冻结新触发并等在途任务、命令和投递结束，再检查；停止核心服务后、替换旧二进制前再检查一次。此命令是部署前置门槛，不能阻止 root 绕过检查替换文件。缓冲无法清空或已有有效占位时保留新版和冻结状态，优先前向修复；不删除记录或恢复旧数据库快照。

版本以运行进程的二进制哈希、版本目录 `REVISION` 和诊断版本交叉核对；切换后同步根目录 `REVISION`。仅分支提交触发网关 CI，版本 tag 不重复构建。发布成品须来自对应提交的成功 CI，并核对 `REVISION` 和 `SHA256SUMS`；校验和中的路径带 `dist/` 前缀，下载至临时根下的 `dist/` 后，从临时根运行 `sha256sum -c dist/SHA256SUMS`。CI artifact 保留 14 天，tag 本身不长期保存二进制。



## v0.5.11 锁回收：软跳过与 completed 按锁龄回收

个别 candidate 锁文件未通过安全检查（非空、模式不符等）时改为软跳过并计入 `skipped`，不再把整轮标记为 `lock_safety_check_failed`；`daemon.lock`／`quota-*` 仍永不删除。对已 `completed`（或请求已不存在）的 `request-`／`dispatch-` 锁，在 flock 成功且文件安全的前提下，若锁文件自身 mtime／ctime 均满 24 小时即可回收，即使 reconcile 把 `updated_at` 刷成较新值。未完成或仍持锁的请求保持保护。

替换为旧版之前，用当前新版二进制执行只读检查（v0.5.11 的目标为 0.5.10）：

```sh
sudo pylon --home /srv/pylon downgrade-check --target-version 0.5.10
```

| 目标 | 必要条件 |
| --- | --- |
| 0.5.10 | 完成缓冲为空、状态有效；无有效事件占位记录 |
| 0.5.9 | 同 v0.5.10 |

## v0.5.10 WAL 压缩与锁回收收紧

恢复排空结束后，以及 `admission.wal_checkpoint_bytes`（默认 32 MiB）阈值触发时，网关与 executor 各自对 `gateway/state.sqlite3`、`supervisor/task-state.sqlite3` 做一次 fail-fast WAL checkpoint（先 PASSIVE 再 TRUNCATE）。冻结期跳过；SQLite busy 时放弃本轮，不阻塞调度车道。`diagnostics.metrics.storage.wal_checkpoint`（executor 侧在 `tasks.metrics.storage`）记录最近一次结果与压缩前 WAL 字节数。

锁回收在原有 `request-`／`dispatch-` 规则之外，纳入 `run/` 下闲置超过 24 小时的 **job 锁**（`{job_id|job.lock}.lock`）。**故意不删**：`daemon.lock`（进程单例）、`quota-*`（主题配额槽位文件）。误删二者会打乱并发准入。

`config/bots.json` 可选：

```json
{"admission":{"freeze_alert_after_seconds":900,"freeze_alert_repeat_seconds":1800,"recovery_concurrency":4,"wal_checkpoint_bytes":33554432}}
```

黑盒合约中，会预置 Telegram 命令更新的 fixture 须先起 executor 再起 gateway（`RuntimeCase.start_for_commands`），避免 command 车道在 `executor.sock` 未就绪时把更新标成 uncertain。

替换为旧版之前，用当前新版二进制执行只读检查（v0.5.10 的目标为 0.5.9）：

```sh
sudo pylon --home /srv/pylon downgrade-check --target-version 0.5.9
```

| 目标 | 必要条件 |
| --- | --- |
| 0.5.9 | 完成缓冲为空、状态有效；无有效事件占位记录 |
| 0.5.8 | 同 v0.5.9 |

## v0.5.9 冻结告警与恢复限流

部署必须使用仓库内 `scripts/deploy-gateway.sh`（或等价地调用 `pylon freeze` / `pylon thaw`）：成功结束时删除 `run/freeze`；任意失败保留冻结并由网关在冻结超过可配置阈值后告警。网关在 `run/freeze` 存在超过 `admission.freeze_alert_after_seconds`（默认 900 秒）时，向 `personal` 路由投递一次系统告警，并按 `freeze_alert_repeat_seconds`（默认 1800 秒）去重重复提醒；冻结期间仍放行 `module=gateway` 且 `job_id=freeze-alert` 的运维告警。`diagnostics.freeze` 给出 `present`、`age_seconds`、阈值与最近告警摘要。

同一 job 在已有 `queued` 请求时只合并触发（过期轮次记入 `skipped_duplicate_triggers`，仍只补跑这一次，含 daily）。解冻或重启后若积压超过 `admission.recovery_concurrency`（默认 4），调度按该全局上限放行，排空后回到主题配额。锁回收在解冻后及未完成扫描时改为约每分钟继续，并允许 root 回收同组空锁。`config/bots.json` 可选：

```json
{"admission":{"freeze_alert_after_seconds":900,"freeze_alert_repeat_seconds":1800,"recovery_concurrency":4}}
```

非法值拒绝加载。保留策略默认未变；`no_change` 运行记录是否纳入 runs 清理需另行确认。

替换为旧版之前，用当前新版二进制执行只读检查（v0.5.9 的目标为 0.5.8）：

```sh
sudo pylon --home /srv/pylon downgrade-check --target-version 0.5.8
```

| 目标 | 必要条件 |
| --- | --- |
| 0.5.8 | 完成缓冲为空、状态有效；无有效事件占位记录 |
| 0.5.7 | 同 v0.5.8 文档对 0.5.7 的要求 |

## v0.5.8 容量治理

网关与 executor 各有一个串行维护任务：启动 10 分钟后首轮，此后每小时运行。`config/bots.json` 的可选 `retention` 配置默认如下，`enabled: false` 同时停止保留清理和请求锁回收；非法或零保留值拒绝加载。冻结或退出后不开始新批次，当前事务提交或回滚；数据库忙则结束本轮。

```json
{"retention":{"enabled":true,"updates_per_bot":5000,"runs_days":30,"events_days":7,"requests_days":14,"placeholders_days":90,"expired_days":1}}
```

| 对象 | 默认清理条件及保护边界 |
| --- | --- |
| updates | 每机器人最新 5,000 条窗口之外的已知终态；保留 pending、running、uncertain、未知状态和去重水位 |
| runs | 启动和可信完成时间均满 30 天的 ok／failed；保留每 job 最新启动时间的所有并列记录，关联 run_events、run-result 同事务删除 |
| 孤儿 run-result | 每天按键分批扫描，确认 run 不存在才删除 |
| events | 创建和更新时间均满 7 天的 delivered／failed／suppressed；保留 uncertain-summary、run_events 引用、部分回执、异常或未知结果 |
| requests／triggers | 创建和完成更新均满 14 天，结果已补交且没有恢复输入；有不确定或缺少可信最终结果证据的记录保留，triggers 随请求同事务删除 |
| contexts | 有明确到期时间且到期超过 1 天；`expires_at=0` 的长期上下文保留 |

每批扫描至多 500 个主记录，每轮每类至多删除 20,000 个、扫描 50,000 个候选；提交后让出 50 ms。游标跨轮保留，已保护的前缀不会阻塞后面的记录。单批关联记录上限为 5,000，过大的关联组保留并报短码。完成缓冲非空时，events／updates 清理暂缓，保留后台补交仍需核对的正文。进程重启从头扫描，不改变资格或重复执行任务。

符合条件的 event 正文实际删除时，在同一事务写入 KV 占位记录，并从这时起再保护 90 天，通常总保护期至少约 97 天。占位记录只留模块与机器人归属、终态、必要时间、最小回执以及完整 SHA-256 摘要，不留正文、按钮文字、附件路径或任意错误字符串。后续修改配置不会缩短已写到期时间。

`delivery.submit/get/cancel/clear_buttons` 识别有效占位：重复提交仍返回原终态，不再次投递；返回字段结构不变，但 receipt 为最小回执，非空 error 为 `retained_terminal`。取消墓碑仍阻止迟到提交。`delivery.lookup` 只查真实 events，需要正文的受保护历史记录仍可能返回。占位到期立即失效，不等待物理清理；原 event 也不存在时同一 ID 才能成为新事件。损坏占位拒绝操作。

过期或失去归属依据的按钮只收到不带文字的 Telegram 空应答，以停止客户端转圈；不调用模块、不发送提示或回复。有效按钮仍须通过原有模块签名及业务校验，90 天占位不保证业务记录一直有效。

请求／派发锁回收 `dispatch-<64位小写十六进制>.lock` 和 `request-<64位小写十六进制>.lock`：已知请求为 `completed` 且（`updated_at` 满 24 小时，或锁文件 mtime／ctime 均满 24 小时）才具备资格；请求已不存在的孤儿锁须 mtime、ctime 都满 24 小时。**不回收** job 锁、`daemon.lock` 与 `quota-*`。未通过安全检查的 candidate 软跳过，不将整轮标为扫描失败。每批最多检查 1,000 个目录项、让出 500 ms，每轮先收集最多 50,000 个目录项，再流式读取任务库，仅保留候选锁对应的记录，跨轮继续扫描。普通文件、属主、权限、长度、链接数、flock 及取得锁后的路径身份均须通过；被持有或不安全的文件保留。socket、`run/invocations/` 不参与回收。

`diagnostics.metrics.storage` 中 database／wal／run_dir 各自给出 bytes 和错误短码，并附最近一次 `wal_checkpoint` 结果；目录字节数是目录自身 stat 大小，目录项数来自低频回收扫描，附扫描起止时间，扣除本次实际删除项，扫描期间的外部变化不构成原子快照。未采样或读取失败为 null／短码。`metrics.retention` 提供清理状态、各对象删除数、占位写入数、批次数、最慢批次耗时、安全短码及占位命中数。executor 的对应结果在 `tasks.metrics.storage`，文件大小至多每分钟采样；诊断和两秒摘要不遍历目录。进程内计数重启清零。

Command 空 claim 同样采用 0.5、1、2 秒退避。进程内 pending update 提交按 bot／lane 唤醒，纯 offset 或重复记录不唤醒；外部写入由最多 2 秒兜底发现，另计数据库等待与执行时间。`metrics.optimization` 增加 `command_empty_claims`、`command_wakeups`，后者仅计通知实际结束空闲等待。

原有查询索引包括：executor 的 `requests_state_created`、`requests_created`、`triggers_request`，网关的 `updates_pending`、`events_receipt_last`、`events_receipt_one`。查找表达式与索引一致，保留归属限制和 latest runs 并列结果；损坏 JSON 等错误不会通过删除数据来规避。

删行形成可复用页，回收锁文件降低 inode 占用，两者都不保证文件或目录立即缩小。维护窗口中的 VACUUM 须在排空、停核心服务、私有一致性备份及索引创建后，由各数据库属主执行；每库至多一次、120 秒超时，锁竞争或空间不足则跳过，失败后核查完整性和 sidecar 权限，禁止手删 WAL／SHM。首次自动清理仍在新版启动 10 分钟后，因此此前 VACUUM 不会回收尚未删除的数据。锁数量、清理用时及容量预测只作估计；不自动 VACUUM 或重建运行目录。
