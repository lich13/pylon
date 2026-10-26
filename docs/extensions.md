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
    "worker": {"entry": ["python", "example.worker"], "workspace": "workspace_a", "topic": "topic_a"}
  },
  "notifications": {"default": "topic_a"},
  "capabilities": [],
  "cli": {"inspect": ["python", "example.inspect"]}
}
```

`workspace_a` 与 `topic_a` 必须事先在部署配置中注册。命令名、任务 ID 与回调前缀不能冲突。模块 ID、别名和任务 ID 只使用小写字母、数字、连字符或下划线。`requires` 声明业务依赖；模块哈希涵盖源码和可执行位。

入口支持 `python`、`bash`、`node` 或 `native`。Python 入口使用模块名；其余入口使用模块内相对文件。命令 handler 接收一行 JSON，返回 `{"api_version":2,"value":...}`。健康检查的 action 为 `health`，成功值为 `{"status":"ok"}`，必须仅使用隔离样本状态，不读取生产凭据或发起外部副作用。

`timeout: null` 表示不按时间强杀任务，适用于仍在等待模型的任务；运行锁和持久化调用标记防止重新提交。`model` 使用所在主题的独立模型配额。`lock` 可显式沿用已有锁名。`passive` 仅用于无业务副作用的诊断服务，迁移冻结期可提前启动；worker 保持等待。

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
| `task.run/status` | 运行本模块已注册任务、读取执行状态 |
| `extension.invoke/hook` | 调用声明依赖与生命周期钩子 |
| `capability.invoke` | 向 root broker 提交结构化受限请求 |

业务先在自己的事务中记录结果与待投递事件，再转交核心。同一事件 ID 的所有权和投递目标固定。发送失败可以退避重试；发送中重启、网络回执丢失或部分投递不确定时保留证据，不自动重发。取消接口对尚未出现的事件保留取消标记，防止迟到转交重新入队。

document 的 payload 沿用 path、sha256 和可选 caption。文件须位于 `data/attachments/`，入队和发送时均拒绝路径越界、符号链接及非普通或多硬链接文件；发送前及读取期间均限制为 50 MiB，再校验实际读取内容的摘要；上传复用同一份字节。路径或内容验证失败为明确失败终态，不自动重发。

v0.5.8 默认仅清理创建和最后更新均满 7 天且无安全保留理由的终态事件正文；正文实际删除时再保留 90 天最小占位记录。有效占位仍满足 submit／get／cancel／clear_buttons 的归属和幂等规则，返回原有字段结构及终态，但 receipt 仅含必要回执，非空 error 为安全短码 `retained_terminal`；不保证原 receipt／error 逐字复现。Bark 保留 code，Telegram 保留最后消息的 message_id／chat_id；文本分片早期回执不再保留。

`delivery.lookup` 仅查真实 events，不查占位。占位到期立即失效；原事件也不存在时，同一 ID 可重新提交。过期或无归属依据的 callback 由网关空应答后静默完成，不调用模块；有效 callback 继续模块的签名、业务和权限校验。占位只保证归属依据，不延长模块业务记录寿命。长期服务 `expires_at=0` 上下文不由保留任务回收。

网页登录接口及 SDK 登录封装已退役。仅可信 root 监督者可调用 `context.retire_service`，按 `module`、`service` 与明确的旧 `hashes` 操作；调用前须在服务启动锁内排除缓存与进程引用。

## 权限、部署与回退

manifest 的 `capabilities` 只是申请。管理员还须在独立 root 配置中限定模块、来源类型、固定 helper 参数、受信任文件路径与时限。broker 同时验证上下文及文件归属；业务更新不能替换 root helper 或读取机器人凭据。

`modules install <包> --sha256 <哈希> --activate` 校验安全路径、完整清单、运行配置与普通 UID 下的健康检查，再发布新注册表并更新该模块服务。失败恢复旧注册表与服务。运行中的任务使用不可变源码快照，版本回收保留当前、上一版以及在途任务引用的版本。

`data_version` 变化需要冻结新触发并等待相关任务结束，禁止用普通热部署跨越不兼容数据变化。切换流量后回退应用版本必须保留新业务状态，不恢复旧数据库快照。

## 可靠执行与兼容（v2 增量）

任务触发在配额竞争前持久化，状态为 queued、running、completing、reporting、completed。每个任务最多保留一轮 queued 触发；相同 invocation 标识始终映射同一请求。执行归属由文件锁确认，失去归属的已启动请求进入待核对，不能自动重放。有限 timeout 同时覆盖输入、执行和管道收尾；null 保留无限等待。

`runtime.complete` 返回 v2 envelope，value.status 必须为 ok、failed 或 uncertain；可附 execution_status、completion_status、notification_status。核心分别记录执行、结束处理、通知状态，失败不能被成功退出码覆盖。回报先保存在监督进程的持久信箱；断线只续交已完成结果，不再次执行完成钩子或任务。

RPC 错误保留字符串 error，新增 error_code 和 retryable，旧客户端仍可解析。永久单事件错误需要处理后明确解除阻塞；不把不确定事件重新提交为新事件。

executor／broker 连接前失败返回 retryable=true；发送后断连、超时、无效响应或远端明确报告结果不确定时，返回 `operation result uncertain; inspect retained state` 和 retryable=false。业务子进程启动后的管道、等待或无效输出同样不可自动重试。命令与扩展调用保留不确定执行的上下文，不据此撤销仍可能运行的业务或自动重放。特权 helper 的时限包含输入、输出和等待，输出上限 8 MiB；超时或超限后回收进程组，并按不确定结果处理。

`diagnostics` 提供脱敏的连接、投递计数、任务概要、最近结果和模块版本。扩展需要 diagnostics:read；无上下文的 CLI 仅允许可信 root 监督者。原始消息、凭据、上下文和业务数据不在响应中。

可选 `lifecycle` 为标准入口 argv，输出 `{"ready":true}` 或 `{"ready":false}`。部署设置只读 drain 标记后等待任务锁和旧模块停止准备钩子，再停止对应服务。旧 v2 清单省略字段仍兼容。准备失败时保留原注册表与运行服务；无限模型等待不得被部署时限强杀。业务服务对网关使用 Wants/After，显式网关重启不传播停止动作。
