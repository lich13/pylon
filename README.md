# pylon

Python 机器人服务、SQLite 待发队列和 systemd timers。维护 17 项任务：Gmail、线报、V2EX、Linux.do、Chiphell、日港 iTunes、KDocs 报价、软件更新抓取与翻译提炼、数据清理、SSH 白名单、每周维护汇总和系统小包更新审查。

```sh
pylon status
pylon run 任务编号
pylon pause 任务编号
pylon resume 任务编号
pylon logs 任务编号
pylon outbox
pylon updates
pylon updates --all
pylon update codex
pylon update grok
pylon agent run codex '核对 sub2api 实际运行版本，只检查不更新'
pylon agent run grok '检查 pylon-bot.service 最近错误'
pylon agent status
pylon agent reply 任务编号 '补充要求或回答'
pylon agent cancel 任务编号
pylon agent logs 任务编号
pylon ssh status|sync|refresh
pylon ssh check IP
pylon chiphell status|test|baseline|pause|resume|import-cookies
pylon bot-menu sync
pylon bot-menu check
```

两个 Telegram 机器人提供相同的 36 项中文菜单。`/help` 查看全部入口，`/help 命令` 查看参数、示例和操作影响。菜单包含任务管理、通知队列、软件监控、软件更新、按需 Agent、Gmail 候选审查、Chiphell 和 SSH 维护；仅接受配置的本人私聊。

Gmail 的 `/gmail_candidates` 只展示尚未确认的新类型；已确认的内置和自定义类型继续自动清理。对候选使用 `/gmail_candidate 候选ID` 查看元数据，再用 `/gmail_feedback 候选ID 意见` 交给 Grok 生成规则草案。草案通过 `/gmail_rule_preview 提案ID` 预览、`/gmail_rule_apply 提案ID` 确认后写入运行时覆盖层，下一次排程才会按新规则清理；反馈、预览和应用命令都不会删除当前邮件。`/gmail_accept` 和 `/gmail_ignore` 保留为兼容入口。

`/agent_codex 任务描述`、`/agent_grok 任务描述` 收集相关信息后以 sudo 启动指定 CLI，沿用现有登录和模型配置；修改类要求会实际执行。`/agent_status [编号]`、`/agent_logs 编号` 查询进度与结果，`/agent_reply 编号 内容` 或直接回复任务消息可继续同一会话，`/agent_cancel 编号` 停止任务。运行中的补充内容在本轮结束后处理；完成后线程不能恢复，须发起新任务。CLI 任务默认向 WebOps 回报。

Agent 由 `pylon-agent.service` 按需执行，无常驻 Agent。全局串行，与 Codex/Grok 更新器共用安装锁；每轮退出后回收专属后台及工具进程。默认累计执行 60 分钟、等待回答 24 小时，root 可在 `/etc/pylon/agents.json` 调整。完成必须通过独立版本、服务状态或文件哈希复核；无独立检查时显示结果待核验。结果先进入持久化 outbox，随后按原生会话 ID 删除线程和临时文件；清理失败独立提示并重试，发送失败不会重做业务操作。取消不撤销已有修改，中断不自动重跑；脱敏结果保留 30 天，原始输入和线程在清理时删除。

`/update_codex` 和 `/update_grok` 通过固定 sudo 入口运行原生更新，只使用 stable 版本；对应会话进程占用时不执行。更新由独立 systemd 服务执行，最长 15 分钟，不阻塞机器人。重复请求合并，完成后向请求所在机器人回报实装版本和结果；发送失败只重试通知，执行结果不确定时不自动重复安装。CLI 发起的更新向 WebOps 回报。

软件更新监控先抓取并保存原始条目，再由独立 worker 调用现有 Grok 一次完成英文翻译和价值提炼；高价值、普通更新和低价值精简更新均保留通知。Grok 不使用工具且不保存请求；失败时依次回退到配置的 LibreTranslate 兼容公共端点和原文，不影响源站抓取。订阅源网络抖动只记录到状态和日志，连续 3 次失败后才告警；只有已经告警的持续故障恢复时才发送恢复通知。`/status` 与 `/logs` 显示待处理、Grok 成功、降级翻译和原文回退状态。

`/updates [all]` 查询软件、系统包候选和最近更新结果，最多缓存 10 分钟。默认机器人只对 `libegl-mesa0`、`libgbm1`、`libgl1-mesa-dri`、`libglx-mesa0`、`mesa-libgallium`、`sosreport`、`thermald` 逐包显示确认按钮；点击前不会安装，库库干不收到确认卡片。FRP、PostgreSQL、Redis、Sub2API、npm 主版本、pylon 依赖和 PyMuPDF/lxml 仍只展示检查结果。sub2api 读取容器内程序自报版本，并核对其与运行进程一致；easy_proxies 无版本接口，仅在确认程序未被替换或挂载覆盖时按镜像标签推定，并标明来源。版本读取失败不会退回旧镜像标签。APT 候选采用系统自动维护的软件源索引，显示索引时间；Docker 只按 Ubuntu 软件源候选判断更新。每 6 小时审查一次候选；没有新候选时静默。安装经过单包模拟升级和白名单校验，不执行 autoremove，不自动重启主机；thermald 和需要重启的结果会明确回报。

关键服务监控涵盖 FRP、OpenList、sing-box、easy_proxies、Certbot、PostgreSQL 18、Redis 8.6，各源每小时拉取一次。首次登记只建立基线，不回放历史消息。首次部署时在暂停软件监控且等待在途执行结束后，以 webops profile 环境运行 `python -m pylon.tasks.software.infrastructure`；后续部署不重新登记已移除的订阅。

原 `/ssh`、`/chiphell`、`/gmail list|accept|ignore` 及“增加监控 URL”“加监控 owner/repo”“移除监控→回复编号”继续可用。`/monitors` 只查看列表，`/remove_monitor` 开启编号移除。Gmail 接受候选只修改后续清理规则。

机器人启动时后台核对默认、所有私聊和本人会话的默认/中文/英文菜单，统一使用中文，自动替换旧命令并设置菜单按钮。网络失败或限流按退避策略重试，不阻塞消息收发。`bot-menu check` 只读核对，不一致时退出码为 1；`bot-menu sync` 仅更新差异并读回确认。命令定义统一维护在 `src/pylon/command_catalog.py`。

任务按北京时间执行，同任务禁止重叠；报价刷新与重试共用锁。手动运行报价绕过时段限制。通知先入队再推进状态，投递失败只补发；回执不确定不会自动标为成功或盲目重发。

## 目录

- `src/pylon/tasks/`：按业务分类的 Python 模块。
- `src/pylon/shared/`：路径、Bark、锁和状态读写。
- `src/pylon/admin/`：固定的 root 特权操作。
- `jobs.json`：任务编号、模块、时间和超时。
- `profiles/`：生产配置、凭据和业务状态，不存放 Python 代码，不提交 Git。
- `tools/chiphell/`：本机 Cookie 同步与 FRP 维护入口。

## 安装与升级

生产目录 `/srv/pylon`，Python 3.12，代码和虚拟环境由 root 持有，业务目录归 pylon。源码及虚拟环境不允许组或其他用户写入；从归档部署时使用 `tar --no-same-permissions`。安装依赖与包：

```sh
python3 -m venv /srv/pylon/venv
/srv/pylon/venv/bin/pip install --require-hashes -r requirements.lock
/srv/pylon/venv/bin/pip install --no-deps -e .
PLAYWRIGHT_BROWSERS_PATH=/srv/pylon/playwright-browsers /srv/pylon/venv/bin/playwright install --only-shell chromium
sudo python3 ops/bootstrap.py
```

`bootstrap.py` 安装单元与受限入口，不启用调度。升级时停止 timers、机器人与 Chiphell 抓取，等待在途任务结束，保留最新业务状态和 outbox；安装后以 pylon 用户执行 `python -m pylon.migrations`，恢复原先启用的 timers。线报任务 ID 升级为 `xianbao-poll`，旧 timer 的持久化时间戳须沿用。首次安装需配置两个 profile 的 `.env`、业务凭据及 SSH `/etc/pylon/ssh.json`。

源码统一由可编辑安装加载；root 入口使用隔离 Python，安装器拒绝可被普通用户修改的源码或虚拟环境。不要将生产安装目录改归运行用户。

## 测试

```sh
python -m pytest -q tests
npm test --prefix tools/chiphell
```

业务报告保留原周期，运行记录清理保留最新结果、失败及未完成投递；消息去重和待发记录独立保留。Cookie、OAuth、报价数据库和订阅不属于缓存清理范围。
