# 扩展协议 v2

模块目录由 root 或本机开发者维护。部署后源码不可被扩展运行用户写入。扩展业务使用自己的数据目录、数据库和依赖；核心不导入业务源码。

## 清单

```json
{
  "api_version": 2,
  "data_version": 1,
  "id": "example",
  "workspace": "workspace_a",
  "handler": ["python", "example.extension"],
  "requires": [],
  "commands": [{"name": "example", "description": "打开工作台", "topic": "topic_a", "menu": true}],
  "callbacks": ["example|"],
  "hooks": [],
  "jobs": {
    "example-poll": {
      "name": "检查新结果", "entry": ["python", "example.poll"],
      "workspace": "workspace_a", "topic": "topic_a",
      "schedule": "*/10 * * * *", "timeout": 300,
      "model": false, "deliver": false, "failure_notify": true
    }
  },
  "services": {
    "web": {"entry": ["python", "example.web"], "workspace": "workspace_a", "topic": "topic_a", "passive": true},
    "worker": {"entry": ["python", "example.worker"], "workspace": "workspace_a", "topic": "topic_a"}
  },
  "web": {"example": {"service": "web", "upstream": "http://127.0.0.1:8797", "title": "工作台"}},
  "notifications": {"default": "topic_a"},
  "capabilities": [],
  "cli": {"inspect": ["python", "example.inspect"]}
}
```

`workspace_a` 与 `topic_a` 必须事先在部署配置中注册。命令名、任务 ID、网页路径与回调前缀不能冲突。模块 ID、别名和任务 ID 只使用小写字母、数字、连字符或下划线。`requires` 声明业务依赖；模块哈希涵盖源码和可执行位。

入口支持 `python`、`bash`、`node` 或 `native`。Python 入口使用模块名；其余入口使用模块内相对文件。命令 handler 接收一行 JSON，返回 `{"api_version":2,"value":...}`。健康检查的 action 为 `health`，成功值为 `{"status":"ok"}`，必须仅使用隔离样本状态，不读取生产凭据或发起外部副作用。

`timeout: null` 表示不按时间强杀任务，适用于仍在等待模型的任务；运行锁和持久化调用标记防止重新提交。`model` 使用所在主题的独立模型配额。`lock` 可显式沿用已有锁名。`passive` 仅用于无后台业务动作的网页进程，迁移冻结期可提前启动；worker 保持等待。

## 运行上下文与 API

监督进程设置 `PYLON_MODULE_ID`、`PYLON_MODULE_VERSION`、`PYLON_WORKSPACE_ID`、`PYLON_BOT_ID`、`PYLON_CONTEXT`、`PYLON_MODULE_SNAPSHOT` 和 `PYLON_GATEWAY_SOCKET`。运行上下文不能自行创建或转授。普通扩展只继承清理后的业务环境，机器人 token 不进入扩展环境。

向 Unix socket 的 `/rpc` 发送：

```json
{"api_version":2,"method":"delivery.submit","context":"<运行上下文>","params":{"id":"stable-event-id","job_id":"example-poll","topic":"topic_a","kind":"text","payload":{"text":"结果"}}}
```

响应为 `{"ok":true,"value":...}` 或 `{"ok":false,"error":"..."}`。使用独立 SDK 包装调用，不访问核心 SQLite。

| 接口 | 作用 |
|---|---|
| `delivery.submit/get/lookup/cancel` | 幂等提交、核对回执、查询或取消自有事件 |
| `delivery.clear_buttons` | 清理已确认消息上的历史按钮 |
| `auth.owner/sign/callback` | 本人信息、历史回调签名与消息归属校验 |
| `auth.url/issue/redeem/session/logout` | 一次性登录、共享会话与退出 |
| `task.run/status` | 运行本模块已注册任务、读取执行状态 |
| `extension.invoke/hook` | 调用声明依赖与生命周期钩子 |
| `capability.invoke` | 向 root broker 提交结构化受限请求 |

业务先在自己的事务中记录结果与待投递事件，再转交核心。同一事件 ID 的所有权和投递目标固定。发送失败可以退避重试；发送中重启、网络回执丢失或部分投递不确定时保留证据，不自动重发。取消接口对尚未出现的事件保留取消标记，防止迟到转交重新入队。

共享网页登录必须校验会话、来源和 CSRF。邮件等不可信正文只以纯文本展示。网页与 worker 无权调用任意 shell 或自行授予权限。

## 权限、部署与回退

manifest 的 `capabilities` 只是申请。管理员还须在独立 root 配置中限定模块、来源类型、固定 helper 参数、受信任文件路径与时限。broker 同时验证上下文及文件归属；业务更新不能替换 root helper 或读取机器人凭据。

`modules install <包> --sha256 <哈希> --activate` 校验安全路径、完整清单、运行配置与普通 UID 下的健康检查，再发布新注册表并更新该模块服务。失败恢复旧注册表与服务。运行中的任务使用不可变源码快照，版本回收保留当前、上一版以及在途任务引用的版本。

`data_version` 变化需要冻结新触发并等待相关任务结束，禁止用普通热部署跨越不兼容数据变化。切换流量后回退应用版本必须保留新业务状态，不恢复旧数据库快照。
