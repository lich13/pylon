# pylon

## 每日审核反馈维护

接续用户反馈时，先运行 `python -m pylon.tasks.daily_review batches --unresolved`，再用 `batch --id <批次>` 读取冻结的原文、原判断和预期结果。草稿和提交本身不会修改生产筛选，也不会执行 Agent 或重发历史帖子。

处理完成用 `reconcile --id <批次> --status processing|fixed|needs_info --result-file <结果文件>` 回填。`comparisons` 中每项必须指明 `feedback_id`；已修复项还须提供 `commit`、`before`、`after` 和 `evidence`。可以逐条回填，全部反馈都有修复证据后才能将整批标为 `fixed`。判断正确的反馈也应关联防回归验证。

审核主页优先展示已送达、手动取回及每天固定的最多 50 条优先核对记录；其余内容折叠收纳，仍可全局搜索。各列表滚动续载，浏览顺序固定到用户刷新。每日名单和取回状态存入 SQLite，审核完成不补位；取回与撤销只影响展示，不产生反馈、通知或策略变更。

自动内容分析沿用 Grok API 和现有推理强度，本地不限制模型等待时长。领取归属由进程文件锁判断，心跳仅用于显示；不要按耗时回收仍在运行的请求。人工 Agent 与 Gmail 执行时限独立保留。

Python 机器人服务、SQLite 待发队列和 systemd timers。维护 Gmail、线报、V2EX、Linux.do、Chiphell、日港 iTunes、KDocs 报价、软件更新、数据清理、SSH 和软件维护任务；四平台统一交易处理任务在关注策略明确启用后工作。

## 情报与交易分流

Linux.do 只消费 Helper 通知、摘要、编辑与链接；不主动访问原帖、JSON、回复、附件或摘要里的外链。`b7a84d1d9f21` 采集，`linuxdo-review` 将公开内容持久化到情报队列。私信、回复、提及等私人通知保持独立直达，不进入公共模型。旧频道配置不能恢复原站补抓。

`intelligence-review` 分别推进 Linux.do、V2EX 非交易与线报队列，统一发往 WebOps；线报保留既有 Bark 分级。每条摘要的具体资源、收益、方法或能力变化有原文依据即可作为线索，缺少步骤或资格细则会注明。未经核验的优惠和经验只表述为原帖声称。纯提问、闲聊、引流、重复、过期与范围外内容仍过滤。

情报策略在 SQLite 中版本化保存，首次迁入原 Linux.do 关注范围，V2EX 和线报保留各自规则及地域例外。`/intel` 打开可展开的分类、来源、关键词筛选页，草稿自动保存，预览后确认应用，可恢复上一版本。交易的 `/market` 继续独立管理出售与求购，两个网页共用一次性登录与最长七天会话。更改情报策略只重评近24小时未投递候选；历史回放不会发送。

Linux.do Helper 摘要是独立证据范围：其中明确的商品、价格和地区可参与普通交易判断，但不等于完整原帖，不用于实体重点比价。AI 价格条件继续按确认的规则核对，不补猜期限、人数或总支出。服务使用地区与卖家所在地分开；卡券可邮寄不能证明服务全国可用。

采集、分析、投递分离，内容指纹与模型缓存持久化；同一摘要资料不足后等待 Helper 更新或策略变更，不反复调用模型。普通失败与恢复静默；有积压且组件持续60分钟无进展时只告警一次，恢复静默。记录失败阶段和具体校验原因，单帖资料不足不是服务故障。

授权入口是 `python -m pylon.tasks.linuxdo helper-auth`，必须在运行用户的交互终端使用。自己的 Telegram API 凭据、登录码和两步验证密码均隐藏输入；凭据与会话位于 WebOps profile 的 `private/linuxdo-helper`，目录权限 0700、文件权限 0600，不提交或输出内容。首次部署需预置该目录并归运行用户所有。`helper-check` 只读最近消息，输出识别计数和读取前后的已读标记是否相同。会话为 Telegram 账号级授权，程序仅访问首次授权固定的 Helper 身份；认证失效提示重新授权，限流按服务端时间延后重试。

Helper 的“某人 发布了”同时用于板块新帖和关注者发帖。安装 `tools/linuxdo/linuxdo-following.user.js` 后，在自己的关注页点击“复制关注名单”，将得到的 `/linuxdo_following_import` 消息粘贴到 Pylon 私聊；超长名单自动下载 `linuxdo-following.json`，发送该文件即可。脚本校验当前登录账号，读取完整关注接口，不导出登录信息或交易正文。格式为 `version: 2`、`complete: true`、`owner`、带时区的 `imported_at` 及 `usernames` 数组。只有所有者私聊可以导入；完整快照原子替换名单，拒绝错误账号、旧快照和同时间冲突，重复发送幂等。空数组表示主动清空完整名单。

首次部署需以运行用户执行 `helper-import-following --input <private-following.json> --owner <forum-username> --apply` 绑定账号；输入文件权限为 0600。兼容已有 v1 快照，绑定不会更改其中的名单和时间。Telegram 不能重新绑定论坛身份。名单缺失、无效或通知无法确认时保留待解析；名单更新仅重新解析未解决消息，不批量重发历史通知。私信和明确个人互动提醒不依赖名单。名单是快照，后续增删关注需重新导入；不另行授权论坛账号或自动修改订阅。

AI 交易先由 Grok 提取逐项报价和原文依据，再以十进制金额计算。求购、纯网页交易、批量起购价、已售出/满车和超过三个月预付静默；缺期限、用途、计费倍率、必要费用或个人额度时待补。本人代充、成品独享、独立席位、共享登录、Codex 专用和 API 分别比较，未验证或功能异常的账号不能套用正常账号上限。报价活动截止与账号使用到期分别记录，临期只按实际剩余使用时间折算。API 按提供方、模型、额度单位/周期和倍率匹配，不把站内刀数视为等值官方余额，不自动换汇。

价格配置位于 WebOps 的 `config/linuxdo_prices.json`，仓库不保存个人价格表，缺失配置时所有交易档位保持观察。通过 `python -m pylon.tasks.linuxdo trade-config --input <private-prices.json> --apply` 安装已确认的 0600 配置。顶层字段是 `version: 1`、`revision`、`confirmed_at`、`rules`；每个规则包含 `id`、精确规格 `match`、`monthly_max`、`total_max`、`minimum_days`、`unit_max`、`car_monthly_reference`、`reference_until`，金额均为十进制字符串，不适用项为空字符串。`match` 必含产品、套餐、交付、使用方式、账号状态、币种；API 还须提供方、模型和额度单位/周期。未知或重叠档位不推送，市场挂牌价不能自动修改上限。

共享订阅必须明确包含车主的总人数、每席额度及全部价格档位，整车价逐档求和且不超过整号参考的 120%。`shared_price_mode: "equal_share"` 仅启用有明确同价同额度依据的车，同时将个人月价限制为参考价 × 120% ÷ 总人数；分档车待补对应确认价表。明确确认各席上限后可用 `fixed` 模式逐档计算整车价。独立计费的成员席位不走整车均摊。参考价到期关闭对应拼车推送并提醒复核。

同卖家、同规格重复开帖只通知一次，累计较上次通知降价至少 10%，或明确新增权益/补货/恢复/事实纠正，才可再通知。新版本保留旧个人提醒及其隐私边界，不把已通知历史正文转入模型。正文编辑会撤回尚未开始发送的旧公开报价，再独立判断；已送达和回执不确定状态仍按 outbox 处理。采集不等待价格判断或投递。

首次切换须停止相关采集、分析与投递任务并等待在途完成，再执行 `python -m pylon.tasks.intelligence.migrate --apply`。迁移保留 Helper 游标、关注范围、价格规则、活动去重、个人通知和 outbox；清除强制补抓标记，仅取消从未发送的旧短故障提示。近24小时未投递且未过期的公开候选重新判断，旧记录仅供回放。已有任务运行状态不能替代真实通知验收。

该迁移还为其他三论坛的近24小时未投递交易建立独立刷新断点。执行 `python -m pylon.tasks.marketplace.reconsider --intelligence-cutover` 重新确认这些原帖后再筛选；各来源保持原有限速，断点可继续。Linux.do 不进入此刷新流程，只等待 Helper 已提供内容。

## 四平台交易

`market_active` 指向 SQLite 中的生效策略版本；`config/marketplace.json` 仅作首次导入，必须显式设置 `version`、`enabled` 和 `categories`。`groups` 关注整个大类，`types` 关注小类及其细分类，`categories` 只关注指定细分类；`blocked_groups` 优先级最高，屏蔽该大类全部现有和未来子类，子类关注不能绕过。未选类别不推送，品牌、型号和规格仅作属性。类别使用 `pylon.tasks.marketplace.taxonomy` 中的稳定标识；缺失策略时保留旧流程，新 NGA 任务静默。启用后，V2EX 各节点的明确交易与拼车、CHH、NGA 和 Linux.do 公共交易共用筛选与投递。Linux.do 私人互动保持独立，非交易内容进入情报模块。

关注策略只匹配仍在出售的独立商品，混合帖至少一件命中才通知。整机配置和赠品不作为独立商品；北京自提和可邮寄规则适用于四个来源。正文读取失败会退避重试，最终仅以明确标题生成带缺失说明的普通通知，禁止重点比价。重点比价与普通通知共用关注清单，KDocs 无效或过期只暂停比价。

求购使用独立的 `config/marketplace-wanted.json`，缺失时关闭。`targets` 必须逐项列出可提供的具体商品型号或地区服务权益及规格，不继承出售关注品类。仅匹配明确、未结束的求购，询价和求推荐不触发；无法确定型号或关键规格时保留内部诊断。求购出价单独标示，绝不参与出售比价。混售/混求帖合并成一条消息，只展示分别命中两套策略的条目。实时抓取按来源并行推进，每个来源复用连接或浏览器，并独立投递和记录健康状态。

离线历史分析使用 `pylon marketplace import-existing|history|refill|categories --store <analysis.sqlite3>`，`history` 另带 `--source v2ex|chiphell|nga`，默认回填 90 天。`refill` 在同一协调进程内分别推进三平台队列，来源各自限速和退避，复用 HTTP 连接或浏览器页面；NGA 保留 15 秒最低间隔，历史和实时进程共享请求锁、间隔及限流冷却状态。`--seconds` 限定本次预算，下一次从持久化队列继续，完整队列包含尚未到重试时间的条目。必须使用独立 SQLite，不允许传入正式 outbox。分页游标可恢复，历史记录永不入队；完整分类统计包含缺失正文和访问失败数量。此维护入口不加入 Telegram 菜单。

上线先等待在途采集结束，保留现有游标、CHH 已处理 ID 和待发队列，再安装同一提交并写入已确认策略。旧待发消息继续投递，新处理链只接收切换后的帖子；NGA 首次启动只建立当前列表基线。

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

两个 Telegram 机器人提供相同的 11 项中文菜单。`/help` 查看全部入口，`/help 命令` 查看参数、示例和操作影响。菜单只保留 Gmail、更新、监控、Agent 和 SSH 旅行模式。日常状态、任务运行、启停、日志、outbox 和 Chiphell 运维命令已从 Telegram 下线；CLI 与自动调度继续可用。

统一策略启用前，CHH 沿用 `src/pylon/tasks/chiphell/categories.py` 的既有处理和去重。启用后由统一关注清单替代，混合帖必须有明确命中的出售商品，未知类别仅内部留存，不再发送类别询问卡片。旧卡片和已入队消息保留投递记录。

`/gmail` 打开批量清理向导。一批最多选择 20 个样本，可来自不同发件人或主题族；分别标记“应删除”或“必须保留”并直接回复原因，整批一次交给 Grok。草案通过按钮查看逻辑、预览、应用、补充样本或拒绝；已应用提案可从“已生效规则”回滚。Grok 只生成受限逻辑 DSL，邮件全文不进入审查数据库。已确认规则继续自动清理，新逻辑规则只匹配生效之后收到的邮件。分析、预览和应用均不删除当前邮件。旧 Gmail 命令统一引导到 `/gmail`。

Gmail 审查、批次、原因、草案和规则覆盖层保存在 `review.sqlite3`。升级只导入一次旧 JSON；旧文件不会覆盖数据库。本次仅迁移 Gmail 时使用 `python -m pylon.migrations --gmail-only`；完成核对后可移除已导入的 JSON，候选与业务去重仍使用原有状态。规则与提案更新共用 SQLite 事务。

`/ssh_travel` 显示当前状态和开启/关闭按钮，也支持 `/ssh_travel 开启`、`/ssh_travel 关闭`。开启后持续放行所有外部 IPv4 的 TCP/22，直到手动关闭；不设置到期时间。保留 SSH 密钥认证、Fail2ban 和北京白名单。CLI 对应 `pylon ssh travel status|open|close`。固定 root 入口只操作 `pylon-ssh-travel` UFW 规则，修改失败自动回滚，启动服务按持久状态恢复。

`/agent_codex 任务描述`、`/agent_grok 任务描述` 收集相关信息后以 sudo 启动指定 CLI，沿用现有登录和模型配置；修改类要求会实际执行。`/agent_status [编号]`、`/agent_logs 编号` 查询进度与结果，`/agent_reply 编号 内容` 或直接回复任务消息可继续同一会话，`/agent_cancel 编号` 停止任务。运行中的补充内容在本轮结束后处理；完成后线程不能恢复，须发起新任务。CLI 任务默认向 WebOps 回报。

Agent 由 `pylon-agent.service` 按需执行，无常驻 Agent。全局串行，与 Codex/Grok 更新器共用安装锁；每轮退出后回收专属后台及工具进程。默认累计执行 60 分钟、等待回答 24 小时，root 可在 `/etc/pylon/agents.json` 调整。完成必须通过独立版本、服务状态或文件哈希复核；无独立检查时显示结果待核验。结果先进入持久化 outbox，随后按原生会话 ID 删除线程和临时文件；清理失败独立提示并重试，发送失败不会重做业务操作。取消不撤销已有修改，中断不自动重跑；脱敏结果保留 30 天，原始输入和线程在清理时删除。

`/update_codex` 和 `/update_grok` 通过固定 sudo 入口运行原生更新，只使用 stable 版本；对应会话进程占用时不执行。更新由独立 systemd 服务执行，最长 15 分钟，不阻塞机器人。重复请求合并，完成后向请求所在机器人回报实装版本和结果；发送失败只重试通知，执行结果不确定时不自动重复安装。CLI 发起的更新向 WebOps 回报。

软件更新监控先抓取并保存原始条目，再由独立 worker 调用现有 Grok 一次完成英文翻译和价值提炼；高价值、普通更新和低价值精简更新均保留通知。Grok 不使用工具且不保存请求；失败时依次回退到配置的 LibreTranslate 兼容公共端点和原文，不影响源站抓取。订阅源网络抖动只记录到状态和日志，连续 3 次失败后才告警；只有已经告警的持续故障恢复时才发送恢复通知。`pylon status` 与 `pylon logs` 显示待处理、Grok 成功、降级翻译和原文回退状态。

`/updates [all]` 查询软件、系统包候选和最近更新结果，最多缓存 10 分钟。默认机器人只对 `libegl-mesa0`、`libgbm1`、`libgl1-mesa-dri`、`libglx-mesa0`、`mesa-libgallium`、`sosreport`、`thermald` 提供多选与批量确认按钮；最终确认前不会安装，库库干不收到确认卡片。FRP、PostgreSQL、Redis、Sub2API、npm 主版本、pylon 依赖和 PyMuPDF/lxml 仍只展示检查结果。sub2api 读取容器内程序自报版本，并核对其与运行进程一致；easy_proxies 无版本接口，仅在确认程序未被替换或挂载覆盖时按镜像标签推定，并标明来源。版本读取失败不会退回旧镜像标签。APT 候选采用系统自动维护的软件源索引，显示索引时间；Docker 只按 Ubuntu 软件源候选判断更新。每 6 小时审查一次候选；没有新候选时静默。安装经过所选包精确事务模拟和白名单校验，不执行 autoremove，不自动重启主机；thermald 和需要重启的结果会明确回报。

关键服务监控涵盖 FRP、OpenList、sing-box、easy_proxies、Certbot、PostgreSQL 18、Redis 8.6，各源每小时拉取一次。首次登记只建立基线，不回放历史消息。首次部署时在暂停软件监控且等待在途执行结束后，以 webops profile 环境运行 `python -m pylon.tasks.software.infrastructure`；后续部署不重新登记已移除的订阅。

“增加监控 URL”“加监控 owner/repo”“移除监控→回复编号”继续可用。`/monitors` 只查看列表，`/remove_monitor` 开启编号移除。旧 CHH 分类按钮只返回入口已移除，不修改策略。库库干的 `/market` 只发出网页一次性登录链接；闷头干不提供入口。旧 `mk1`、`ch1/ch2` 配置按钮无法修改策略。

交易筛选先保存草稿，支持大类批量关注或屏蔽，以及细类内的品牌、型号、价格、成色和规格组合。字段内多选满足其一，字段之间同时满足，排除条件优先；缺少受限属性时由 Grok 从原文补判，仍不明确则暂缓。预览并确认后才修改生产版本，可以恢复上一版本。新品种建议每天北京时间 21:00 汇总一次，仅提醒新增内容；新品牌、新型号不会单独创建类别。分类和是否关注分别确认，历史样本不自动补发。

复杂交易分析由 `marketplace-analyze` 独立任务处理，不占用四平台采集。来源原类目、商品范围、属性证据和每阶段决定均保留；网页“检查帖子”可查看未推送原因。历史回放只能在独立数据库中运行，不产生通知。

网页使用 `/pylon/market/` 与 `/pylon/intel/`，由 `pylon-market-web.service` 在回环地址单 worker 运行。`config/market-web.json` 的 `public_url` 设为实际 HTTPS 页面地址；反向代理仅转发这两个路径并设置 `Host`、`X-Forwarded-Proto`，不开放整个 `/pylon/`。服务没有 sudo 提权能力。一次性登录凭据仅保留哈希，10分钟过期；Cookie 为 HttpOnly、Secure、SameSite=Strict，最长7天，退出会撤销会话。写操作校验 CSRF 与来源。客户端只改草稿；预览固定当时的数据范围，新增帖子不使确认失效，并发策略版本变化会拒绝旧草稿。

四平台 AI 交易共用 `market_active.content.ai_prices` 中已确认的价格规则，独立于本地商品分类器：包括实付总额、期限、每30天折合价、API额度及含车主的拼车分摊。卖家报价去重继承 Linux.do 旧通知记录。执行 `python -m pylon.tasks.marketplace.migrate_v12 --apply` 会一次性迁入现用价格规则、停用旧配置交互，并加入已批准的 Apple Music、88VIP及其明确来源权益、电话/儿童手表排除；不会改动求购目标或重发历史。近24小时漏推单独经 `reconsider --stage` 与新鲜读取后处理。只读审计入口为 `python -m pylon.tasks.marketplace.audit 数据库... --before 旧策略.json --after 新策略.json`，不导入发送器。

机器人启动时后台核对默认、所有私聊和本人会话的默认/中文/英文菜单，统一使用中文，自动替换旧命令并设置菜单按钮。网络失败或限流按退避策略重试，不阻塞消息收发。`bot-menu check` 只读核对，不一致时退出码为 1；`bot-menu sync` 仅更新差异并读回确认。命令定义统一维护在 `src/pylon/command_catalog.py`。

Chartoo 日报在发行日次日北京时间 21:30 先发送已经结算的市场；另一市场未完成时，消息和 HTML 会标明缺项，继续收集迟到歌曲，数据到齐后补发一次完整报告。正常齐备时仍只发一份；重启和重复排程不重发已入队的同一阶段报告。

任务按北京时间执行，同任务禁止重叠；报价刷新与重试共用锁。手动运行报价绕过时段限制。通知先入队再推进状态，投递失败只补发；回执不确定不会自动标为成功或盲目重发。

## 目录

- `src/pylon/tasks/`：按业务分类的 Python 模块。
- `src/pylon/shared/`：路径、Bark、锁和状态读写。
- `src/pylon/admin/`：固定的 root 特权操作。
- `jobs.json`：任务编号、模块、时间和超时。
- `profiles/`：生产配置、凭据和业务状态，不存放 Python 代码，不提交 Git。
- `tools/chiphell/`：本机 Cookie 同步与 FRP 维护入口。

系统小包可在默认机器人的 /updates 中勾选多个包，或全选当前候选，再点击“批量更新已选”。选择会保存，服务重启后仍可继续；已跳过版本不会被全选自动加入。旧单包卡片继续可用。Mesa 等关联包必须一并勾选；依赖不完整时明确列出需要补选的包。

批次仅升级明确选择的白名单包，安装前核对版本、系统分阶段放行和精确 APT 事务；系统暂缓时整批不安装。安装后检查实际版本、dpkg 状态和相关服务。结果统一回报，失败或回执不确定不会自动重新安装；重试需重新打开 /updates 并再次确认。不执行 autoremove，不自动重启主机。

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
