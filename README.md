# pylon

Python 机器人服务、SQLite 待发队列和 systemd timers。维护 Gmail、线报、V2EX、Linux.do、Chiphell、日港 iTunes、KDocs 报价、软件更新、数据清理、SSH 和软件维护任务；新增三平台二手交易处理任务在关注策略明确启用后工作。

## Linux.do 聚合

`b7a84d1d9f21` 每分钟采集，`linuxdo-review` 独立补证和调用现有 Grok，投递由 outbox 完成。Helper 模式通过专用 Telegram 用户会话读取 `linux_do_helper_bot` 私聊；板块帖子按主题聚合并判断价值，关注者发帖/回复、私信和个人互动提醒直接转发。个人提醒正文独立保存，不进入模型证据；采集不标记已读、不代为回复或点赞。板块、标签和关注用户由使用者在论坛维护。

旧版来源配置继续采集 LinuxDoNew、linux_do_channel、linuxdo_tg，直到显式切换。Helper 分页检查点随消息在同一事务内保存，重启续采，首次接入只建立基线。新消息优先，有限回查处理编辑；未知通知保留并合并告警。个人提醒按通知身份去重，同主题不同回复各自保留。同一帖子同时命中板块订阅和个人提醒时，未发送的板块通知让位于个人提醒，已确认或回执不确定的消息不重复发送。

WebOps profile 的 `config/linuxdo_policy.json` 是独立、版本化的关注目录，缺省采用已确认策略。支持 `groups`、`categories` 和优先级更高的 `excluded_categories`；类别见 `pylon.tasks.linuxdo.policy.CATEGORIES`，未知类别拒绝。默认关注顶级 AI 资源，Codex、Claude Code、Grok Build、MCP/Skills、API 工具，开发自动化、自托管、代理稳定性、登录支付方法，以及全国/北京实用福利。DNS/CDN/证书、安全故障资讯、普通效率软件与硬件类关闭。纯国产模型、影音/PT、游戏素材、论文、邮箱账号、非北京限定和 WPS 福利继续排除；涉及这些产品的可复用技术方法独立判断。

主机促销仅腾讯云、阿里云，地域包含大陆、香港和海外；域名和转售排除。配置、地域、实付价格、周期和参与资格须有匹配的官方原文依据，续费未说明时明确标注。`config/linuxdo_sources.json` 可设置来源列表、分页数、请求超时和单来源预算；默认值见 `pipeline.source_config`。

授权入口是 `python -m pylon.tasks.linuxdo helper-auth`，必须在运行用户的交互终端使用。自己的 Telegram API 凭据、登录码和两步验证密码均隐藏输入；凭据与会话位于 WebOps profile 的 `private/linuxdo-helper`，目录权限 0700、文件权限 0600，不提交或输出内容。首次部署需预置该目录并归运行用户所有。`helper-check` 只读最近消息，输出识别计数和读取前后的已读标记是否相同。会话为 Telegram 账号级授权，程序仅访问首次授权固定的 Helper 身份；认证失效提示重新授权，限流按服务端时间延后重试。

Helper 的“某人 发布了”同时用于板块新帖和关注者发帖。使用本机已登录浏览器导出完整关注名单，以运行用户执行 `helper-import-following --input <private-following.json> --apply`，原子写入上述私有目录的 `following.json`。输入须为 0600 文件，包含 `version: 1`、`complete: true`、带时区的 `imported_at` 和从用户资料链接提取的 `usernames` 数组。名单内作者的发帖与回复直通，其余这类通知只有明确指向首帖时才进入价值判断；名单缺失、无效或消息无法确认时保留待解析。私信和明确个人互动提醒不依赖名单。浏览器导入是快照，后续增删关注需重新导入；不另行授权论坛账号或自动修改订阅。

切换前停止两个 Linux.do timer，等待在途任务结束，执行 `helper-baseline --apply`。在来源配置原有字段基础上设置 `version: 2`、`mode: "helper"`、`page_size: 100`、`recheck_limit: 100`，然后恢复 timer。只有新 Helper 板块消息进入判断，旧源独有候选停止消费；旧证据、投递记录和待发消息保留。再次启动保持已有基线，不重置游标。需要回到三源时停两个 timer 并等待结束，将 `mode` 改为 `"channels"` 后恢复；Helper 暂时失败不会自动启用三源。

Grok 只处理合并后的证据并返回结构化判断，不执行操作。关注范围、证据引用、链接、关键条件和重复事实由本地校验。缺证据或暂时失败的候选退避重试，普通提问可在补充明确答案后重新判断。相同证据按策略、模型和提示词版本缓存；候选最长保留七天待处理，明确过期则关闭。通知只发 Telegram，无日报和新增机器人菜单。通知与判断在同一事务中保存；入队不等于送达，失败/不确定回执沿用 outbox。

从早期脚本首次升级到持久聚合时，采集入口会原子导入旧游标、已投主题和旧待发队列。新旧入口不得同时运行。运维入口为 `python -m pylon.tasks.linuxdo status`；隔离回放使用 `replay --store <analysis.sqlite3> --input <observations.json> --limit 4`，输入是 `sources.Observation` 字段数组，回放数据库不可设为正式 outbox，且不连接发送器。仅当需要退回早期脚本时，先停两个新 timer、等待任务结束，再执行 `python -m pylon.tasks.linuxdo export-legacy --apply`，最后恢复旧模块入口与原 timer；正式 outbox 保持原样继续投递。

## 三平台二手交易

`config/marketplace.json` 是统一关注策略，必须显式设置 `version`、`enabled` 和 `categories`。`groups` 关注整个大类，`types` 关注小类及其细分类，`categories` 只关注指定细分类；`blocked_groups` 优先级最高，屏蔽该大类全部现有和未来子类，子类关注不能绕过。未选类别不推送，品牌、型号和规格仅作属性。类别使用 `pylon.tasks.marketplace.taxonomy` 中的稳定标识；缺失策略时保留旧流程，新 NGA 任务静默。启用后，V2EX 频道的二手交易分流、CHH 抓取结果和 NGA 新帖统一进入持久化处理队列，其他 V2EX 业务不变。

关注策略只匹配仍在出售的独立商品，混合帖至少一件命中才通知。整机配置和赠品不作为独立商品；北京自提和可邮寄规则适用于三个来源。正文读取失败会退避重试，最终仅以明确标题生成带缺失说明的普通通知，禁止重点比价。重点比价与普通通知共用关注清单，KDocs 无效或过期只暂停比价。

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

“增加监控 URL”“加监控 owner/repo”“移除监控→回复编号”继续可用。`/monitors` 只查看列表，`/remove_monitor` 开启编号移除。旧 CHH 分类按钮只返回入口已移除，不修改策略。库库干的 `/market` 将出售筛选、求购目标、新品种确认、待核对和帖子原因检查集中在一个入口；闷头干不新增此菜单。

交易筛选先保存草稿，支持大类批量关注或屏蔽，以及细类内的品牌、型号、价格、成色和规格组合。字段内多选满足其一，字段之间同时满足，排除条件优先；缺少受限属性时由 Grok 从原文补判，仍不明确则暂缓。预览并确认后才修改生产版本，可以恢复上一版本。新品种建议每天北京时间 21:00 汇总一次，仅提醒新增内容；新品牌、新型号不会单独创建类别。分类和是否关注分别确认，历史样本不自动补发。

复杂交易分析由 `marketplace-analyze` 独立任务处理，不占用三平台抓取。来源原类目、商品范围、属性证据和每阶段决定均保留；`/market 原帖链接` 可查看未推送原因。历史回放只能在独立数据库中运行，不产生通知。

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
