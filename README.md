# pylon

Dagu 2.18.0 调度现有 Python 任务，独立服务负责 Telegram 固定命令和持久化通知。生产入口：https://example.com/pylon/

## 运行

- `/srv/pylon/venv/bin/pylon status`：最近执行结果、待发及不确定回执。
- `pylon run <job_id>`：执行任务；相同任务互斥，报价主任务与重试共用锁。
- `pylon pause <job_id>` / `pylon resume <job_id>`：控制定时任务，Chiphell 同时控制抓取服务。
- `pylon outbox`：查看投递状态，不触发业务操作。

服务：`pylon-dagu.service`、`pylon-bot.service`、`chiphell-fid26-poller.service`。业务数据、OAuth、Cookie 和机器人凭据不进 Git。

Telegram 仅接受本人私聊：`/status`、`/pause 编号`、`/resume 编号`、`/gmail list`、`/gmail accept 候选ID`、`/gmail ignore 候选ID`，以及原有“增加监控 URL”“加监控 owner/repo”“移除监控 → 回复编号”。接受 Gmail 候选只添加未来规则。

## 投递语义

脚本先将正文或附件保存到 SQLite outbox，再推进业务去重状态。连接失败、限流由投递服务重试；已确认的长消息分段不重发。发送后未取得可靠回执的记录进入 `uncertain`，需核对实际会话，不能自动视为成功或盲目重发。

空输出、`[SILENT]`、`{"wakeAgent":false}` 不产生通知。面板日志区分 `no_change`、`failed`、`delivery_pending`；投递失败不重新触发 Gmail 删除。

## 部署与验证

Python 3.12 依赖锁定在 `requirements.lock`。`ops/bootstrap.py` 仅准备运行环境，不启用调度；`ops/sync_state.py` 仅复制保留的业务数据，要求机器人服务停止。切换前必须先停止旧调度并等待在途任务结束，再同步最新状态。

三个测试组分别执行，避免同名 `lib` 包串用：`tasks/default/tests`、`tasks/webops/tests`、`tests`。测试环境设置独立的 `PYLON_HOME` 和 `PYLON_PROFILE_ROOT`。
