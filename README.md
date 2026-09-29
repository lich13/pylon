# pylon

Python 机器人服务、SQLite 待发队列和 systemd timers。维护 14 项任务：Gmail、线报、V2EX、Linux.do、Chiphell、日港 iTunes、KDocs 报价、软件更新、数据清理与 SSH 白名单。

```sh
pylon status
pylon run 任务编号
pylon pause 任务编号
pylon resume 任务编号
pylon logs 任务编号
pylon outbox
pylon ssh status|check|sync|refresh
pylon chiphell status|test|baseline|pause|resume|import-cookies
```

Telegram 支持 `/status`、`/run`、`/pause`、`/resume`、`/logs`、`/ssh`、`/chiphell`、`/gmail list|accept|ignore`，以及“增加监控 URL”“加监控 owner/repo”“移除监控→回复编号”。仅接受配置的本人私聊。Gmail 接受候选只修改后续清理规则。

任务按北京时间执行，同任务禁止重叠；报价刷新与重试共用锁。手动运行报价绕过时段限制。通知先入队再推进状态，投递失败只补发；回执不确定不会自动标为成功或盲目重发。

## 目录

- `src/pylon/tasks/`：按业务分类的 Python 模块。
- `src/pylon/shared/`：路径、Bark、锁和状态读写。
- `src/pylon/admin/`：固定的 root 特权操作。
- `jobs.json`：任务编号、模块、时间和超时。
- `profiles/`：生产配置、凭据和业务状态，不存放 Python 代码，不提交 Git。
- `tools/chiphell/`：本机 Cookie 同步与 FRP 维护入口。

## 安装与升级

生产目录 `/srv/pylon`，Python 3.12，代码和虚拟环境由 root 持有，业务目录归 pylon。安装依赖与包：

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
