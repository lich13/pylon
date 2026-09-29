# pylon

独立 Python 任务与机器人服务，使用 systemd timers 调度、SQLite 保存执行记录和通知。无网页、Agent 或外部数据库。

保留 Gmail 清理、线报、V2EX、Linux.do、Chiphell、日港 iTunes、KDocs 报价、软件更新及数据清理；统一管理 SSH 北京 IP 段更新和可信登录 IP 同步。软件更新使用 DeepL 翻译和原端点的 Grok 4.7 润色。

## 管理

服务器命令：

- `pylon status`：执行结果、下次运行、待发及不确定回执。
- `pylon run <编号或任务ID>`：立即执行，报价主任务可在排程窗口外刷新。
- `pylon pause <编号或任务ID或all>` / `pylon resume <同上>`：暂停或恢复；Chiphell 同步控制抓取。
- `pylon logs <编号或任务ID>` / `pylon outbox`：日志和通知状态。
- `pylon ssh status` / `pylon ssh check <IP>`：SSH 维护状态、北京地址集及 Fail2ban 豁免情况。
- `pylon ssh sync` / `pylon ssh refresh`：提交可信 IP 同步或北京 IP 段更新。

Telegram 仅接受本人私聊：对应 `/status`、`/run`、`/pause`、`/resume`、`/logs` 和 `/ssh`；保留“增加监控 URL”“加监控 owner/repo”“移除监控 → 回复编号”。`/gmail list`、`/gmail accept 候选ID`、`/gmail ignore 候选ID` 管理未来规则，接受候选不会立即删除邮件。

所有时间均为北京时间。停机或暂停期间错过的日历排程恢复后合并补跑一次；同一任务互斥，报价主任务与重试共用锁。暂停不撤销防火墙规则、不丢弃待发通知。

## 投递与权限

正文和附件先持久化再推进业务去重状态。连接失败和限流只重试投递，已确认的长消息分段不重发；回执不确定的记录单独保留供核对。空输出、`[SILENT]` 和 `{"wakeAgent":false}` 静默。执行记录区分无变化、业务失败与待补发。

常驻服务为 `pylon-bot.service`，另保留 Chiphell 抓取服务。14 项任务使用独立 timer 和共享 service 模板。SSH 权限通过 root 持有的固定操作入口提供，不能执行任意命令、编辑任意规则或替换密钥。北京地址集继续使用现有 `codex_beijing_ipv4` 和 UFW 规则；更新前校验，异常保留旧版本。

## 部署与测试

安装目录 `/srv/pylon`，Python 3.12 依赖锁定在 `requirements.lock`。`ops/bootstrap.py` 安装原生单元和受限入口，不自动启用调度。切换时先停止旧调度并等待在途任务结束，保留当前业务数据和 outbox，再启用新 timers；禁止重新从旧 Hermes 数据覆盖运行状态。

运行数据、OAuth、Cookie 和凭据不进 Git。SSH 管理员指纹及静态 CIDR 配置首次安装到 `/etc/pylon/ssh.json`，后续升级保留本机配置。

测试组分开运行，避免同名 `lib` 包串用：`tasks/default/tests`、`tasks/webops/tests`、`tests`；设置独立的 `PYLON_HOME` 和 `PYLON_PROFILE_ROOT`。`ops/archive_history.py` 仅用于旧版历史保留，不参与运行。
