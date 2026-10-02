"""One source for Telegram menus, command routing and Chinese help."""
from __future__ import annotations

from dataclasses import dataclass

from .config import jobs


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    route: str
    action: str = ''
    argument: str = ''
    example: str = ''
    detail: str = ''
    menu: bool = True

    @property
    def usage(self) -> str:
        return '/' + self.name + (' ' + self.argument if self.argument else '')


COMMANDS = {item.name: item for item in (
    Command('help', '查看全部功能；使用 /help 命令查看详细用法与示例', 'help', argument='[命令]',
            example='/help run', detail='省略命令时列出全部入口；命令前的 / 可省略。'),
    Command('status', '查看全部任务的运行状态、下次执行时间及通知异常', 'status',
            detail='时间均为北京时间；已暂停、运行中、无变化、业务失败、待补发分别显示。任务编号也可用于运行、启停和日志查询。'),
    Command('run', '立即运行指定任务，支持任务编号或任务 ID', 'task', 'run', '任务编号或任务ID', '/run 8',
            '提交一次正式执行，可用 /status 和 /logs 核对结果。同一任务不重叠；暂停排程后仍可手动运行，且不会恢复排程。'
            '报价任务会绕过时段限制。Gmail 和数据清理任务会按既定规则实际删除邮件或过期数据。'),
    Command('pause', '暂停指定任务或全部排程；Chiphell 同时暂停抓取', 'task', 'pause', '任务编号或任务ID或all', '/pause 7',
            'all 表示全部定时任务。普通任务只暂停后续排程，不强杀在途执行；Chiphell 联动停止抓取和定时投递。已入队的通知仍会发送。'),
    Command('resume', '恢复指定任务或全部排程；Chiphell 同时恢复抓取', 'task', 'resume', '任务编号或任务ID或all', '/resume 7',
            'all 表示全部定时任务。恢复后可能合并补跑错过的排程；Chiphell 同时恢复抓取和投递。Cookie 或健康状态不满足要求时会提示失败。'),
    Command('logs', '查看指定任务的最近执行日志', 'task', 'logs', '任务编号或任务ID', '/logs 8',
            '显示最近的脱敏日志，内容过长时保留末尾；只查询，不运行任务。'),
    Command('outbox', '查看待发、失败和回执不确定的通知', 'outbox',
            detail='显示待发送、发送中、失败和回执不确定的数量及最近记录。只查询；网络失败按队列策略补发，回执不确定不会自动重发。'),
    Command('updates', '查看腾讯云可更新软件、系统包和最近升级结果', 'updates', argument='[all]', example='/updates\n/updates all',
            detail='省略参数查看摘要；all 展示全部软件及系统包候选。默认机器人会为允许点选的系统小包显示更新和跳过按钮。检查结果最多缓存 10 分钟并注明时间，APT 使用系统自动刷新的软件源索引。检查失败显示未确认，不会当作最新版。查询不安装软件。每周一北京时间 09:00 向库库干汇总维护事项，无更新且无异常时静默。'),
    Command('update_codex', '使用 sudo 更新腾讯云 Codex CLI，完成后自动回报', 'software_update', 'codex',
            detail='执行 sudo codex update，使用官方 stable 版本；无需参数。发现对应会话进程占用时不更新，退出会话后可再次点击。后台执行不阻塞机器人，最长 15 分钟；重复请求合并。回报更新前后版本、耗时及失败原因，保留登录、配置与会话。'),
    Command('update_grok', '使用 sudo 更新腾讯云 Grok Build，完成后自动回报', 'software_update', 'grok',
            detail='执行 sudo grok update --stable；无需参数。Grok 原生更新可能重启旧后台，因此发现会话进程占用时不更新，退出会话后可再次点击。后台执行最长 15 分钟；重复请求合并。完成后回报版本和结果，保留登录、配置与会话。'),
    Command('agent_codex', '收集相关信息，使用 sudo Codex 执行任务并自动回报', 'agent', 'codex', '任务描述',
            '/agent_codex 核对 sub2api 实际运行版本及官方最新版，只检查不更新',
            '仅在本人明确发起后执行，按描述完成腾讯云本机任务；修改类要求会实际执行。全程使用 Codex，可回复任务消息补充要求。默认累计执行上限 60 分钟，等待回答 24 小时；结束后自动清理本次线程。'),
    Command('agent_grok', '收集相关信息，使用 sudo Grok 执行任务并自动回报', 'agent', 'grok', '任务描述',
            '/agent_grok 检查 pylon-bot.service 最近错误并说明原因',
            '仅在本人明确发起后执行，按描述完成腾讯云本机任务；修改类要求会实际执行。全程使用 Grok，不切换到 Codex。支持同一会话续接；默认累计执行 60 分钟、等待回答 24 小时，结束后自动清理线程。'),
    Command('agent_status', '查看 Agent 任务、等待回答、结果及线程清理状态', 'agent', 'status', '[任务编号]',
            '/agent_status\n/agent_status 0123456789ab',
            '省略编号查看进行中的任务及最近记录；指定编号查看结果。任务结果、通知投递和线程清理分别记录；没有独立验收条件时显示结果待核验。已结束任务的脱敏结果保留 30 天。'),
    Command('agent_reply', '补充要求或回答问题，继续原来的 Agent 会话', 'agent', 'reply', '任务编号 内容',
            '/agent_reply 0123456789ab 只检查，不执行更新',
            '也可直接回复对应的机器人任务消息。执行中的补充内容在本轮结束后处理；等待回答时恢复同一会话。不会切换 Agent；已结束并清理的线程不能恢复，请发起新任务。'),
    Command('agent_cancel', '停止指定 Agent 任务，并清理本次线程与临时文件', 'agent', 'cancel', '任务编号',
            '/agent_cancel 0123456789ab',
            '取消排队任务或请求停止正在执行的任务；仅停止本任务进程。取消不会撤销已经完成的修改。结果先进入可靠通知队列，再删除本次会话；清理失败会明确提示并重试。'),
    Command('agent_logs', '查看 Agent 当前脱敏进度、修改和验证结果', 'agent', 'logs', '任务编号',
            '/agent_logs 0123456789ab',
            '只查询，不触发执行。显示当前阶段及最近结果，区分 Agent 验证与独立复核；不展示凭据或完整工具输出。线程清理后仍可查询保留期内的脱敏结果。'),
    Command('monitors', '查看当前软件更新监控列表', 'monitor', 'list',
            detail='只查看软件更新订阅，不开启编号移除。需要移除时发送 /remove_monitor，再回复该次列表中的编号。'),
    Command('add_monitor', '增加软件更新监控，支持网站 URL 或 GitHub owner/repo', 'monitor', 'add', 'URL或owner/repo',
            '/add_monitor openai/codex\n/add_monitor https://github.com/openai/codex',
            '识别支持的发布源，保存后校验配置；重复源不会重复添加。不支持的网站会明确提示。原“增加监控 URL”“加监控 owner/repo”仍可使用。'),
    Command('remove_monitor', '列出可移除的监控，回复对应编号完成移除', 'monitor', 'remove',
            detail='先发送此命令取得列表，再单独回复编号。编号对应此次列表中的项目；成功后结束本次选择。原“移除监控”仍可使用。'),
    Command('gmail_candidates', '查看待确认的新邮件类型候选', 'gmail', 'list',
            detail='兼容入口：只列出待确认候选及元数据。日常操作请使用 /gmail_review。', menu=False),
    Command('gmail_review', '打开 Gmail 清理向导（按钮操作）', 'gmail', 'review',
            detail='按卡片选择样本，标记应删除或必须保留，回复原因后交给 Grok 生成受限逻辑规则。预览和分析不会删除邮件；应用按钮只影响下一次排程。'),
    Command('gmail_rules', '查看已生效 Gmail 规则及版本', 'gmail', 'rules',
            detail='查看内置规则、已确认规则和运行时逻辑覆盖层版本；只读，不删除邮件。'),
    Command('gmail_candidate', '查看单条新邮件类型的元数据和摘要', 'gmail', 'candidate', '候选ID', '/gmail_candidate gmail-nt-abc123',
            '兼容入口：显示发件人、主题族、日期、摘要、标签和样本数。向导中可直接查看完整文本样本。', menu=False),
    Command('gmail_feedback', '提交一条意见并交给 Grok 生成规则草案', 'gmail', 'feedback', '候选ID 意见', '/gmail_feedback gmail-nt-abc123 这是登录提醒，保留安全告警，删除普通通知',
            '兼容入口：每个候选独立处理；重复消息不会重复启动任务。Grok 只生成草案，不删除邮件。', menu=False),
    Command('gmail_review_status', '查看 Gmail 候选的 Grok 审查和草案状态', 'gmail', 'review_status', '[候选ID]', '/gmail_review_status gmail-nt-abc123',
            '兼容入口：显示意见、Grok 任务、提案编号和失败原因。向导入口请用 /gmail_review。', menu=False),
    Command('gmail_rule_preview', '预览 Gmail 规则草案的当前影响', 'gmail', 'rule_preview', '提案ID', '/gmail_rule_preview gmail-rp-abc123456789',
            '兼容入口：重新读取 Gmail，展示预计匹配数量和样本；只读，不删除邮件。', menu=False),
    Command('gmail_rule_apply', '确认应用 Gmail 规则草案', 'gmail', 'rule_apply', '提案ID', '/gmail_rule_apply gmail-rp-abc123456789',
            '兼容入口：再次校验规则并写入覆盖层；只影响下一次排程，当前不会删除邮件。', menu=False),
    Command('gmail_rule_reject', '拒绝 Gmail 规则草案并保留候选', 'gmail', 'rule_reject', '提案ID', '/gmail_rule_reject gmail-rp-abc123456789',
            '兼容入口：拒绝后不修改规则、不删除邮件，候选回到待确认状态。', menu=False),
    Command('gmail_rule_rollback', '回滚已应用的 Gmail 规则提案', 'gmail', 'rule_rollback', '提案ID', '/gmail_rule_rollback gmail-rp-abc123456789',
            '兼容维护入口：移除指定提案写入的运行时覆盖层；不会删除邮件。', menu=False),
    Command('gmail_accept', '接受指定候选，加入后续清理规则，本次不删除邮件', 'gmail', 'accept', '候选ID',
            '/gmail_accept gmail-nt-abc123', '兼容旧的简单接受快捷方式，不经过 Grok；只修改未来清理规则。', menu=False),
    Command('gmail_ignore', '忽略指定候选，保留现有清理规则', 'gmail', 'ignore', '候选ID',
            '/gmail_ignore gmail-nt-abc123', '兼容旧入口：记录忽略选择，不新增清理规则、不删除邮件。', menu=False),
    Command('chiphell_status', '查看 Chiphell 抓取、待发队列和健康状态', 'chiphell', 'status',
            detail='显示抓取与投递排程、待发数量、最近健康结果。已暂停是运行状态，不等于抓取失败。'),
    Command('chiphell_test', '测试 Chiphell 实际页面抓取及登录、代理状态', 'chiphell', 'test',
            detail='通过现有家宽代理执行一次页面检查并更新健康结果。抓取锁被占用时提示忙碌，不会显示为测试通过；不会重置历史去重。'),
    Command('chiphell_pause', '暂停 Chiphell 抓取和定时投递', 'chiphell', 'pause',
            detail='联动停止抓取服务与投递排程，保留 Cookie、去重和待发记录。已进入公共通知队列的消息仍可发送。'),
    Command('chiphell_resume', '恢复 Chiphell 抓取和定时投递', 'chiphell', 'resume',
            detail='检查现有 Cookie、健康与去重状态后恢复抓取和投递；条件不满足时返回失败原因。'),
    Command('chiphell_categories', '查看 CHH 历史商品分类和当前静默策略', 'chiphell', 'categories',
            detail='只读分析现有投递队列；一帖多类会重复计数，混合和未归类帖子始终保留。重点比价不受类别策略影响。'),
    Command('chiphell_silence', '静默指定 CHH 商品类别', 'chiphell', 'silence', '[分类]', '/chiphell_silence 生活·服饰鞋包',
            '只影响尚未入队的普通新帖。混合和未归类帖子仍保留，重点比价不受影响；重复设置不会改变结果。'),
    Command('chiphell_unsilence', '恢复指定 CHH 商品类别推送', 'chiphell', 'unsilence', '[分类]', '/chiphell_unsilence 生活·服饰鞋包',
            '恢复后只对新的普通帖子生效，历史消息不重发；重点比价始终独立运行。'),
    Command('ssh_status', '查看 SSH 白名单及维护状态', 'ssh', 'status',
            detail='只读查看北京网段数量、内核生效数量、可信登录来源及是否存在待恢复事务。'),
    Command('ssh_check', '检查指定 IPv4 是否符合当前 SSH 白名单规则', 'ssh', 'check', 'IPv4', '/ssh_check 111.201.1.2',
            '分别查询北京网段命中与 fail2ban 豁免，不修改规则。查询结果不代替实际 SSH 连接测试。'),
    Command('ssh_sync', '同步现用管理员专用密钥对应的可信登录 IP', 'ssh', 'sync',
            detail='提交一次可信登录 IP 同步，仅识别配置的管理员用户与专用密钥指纹。结果通过 /status 或该任务日志查看。'),
    Command('ssh_refresh', '更新北京 IP 段；校验成功后替换，失败保留原白名单', 'ssh', 'refresh',
            detail='提交一次下载与校验，检查网段格式、数量和当前管理来源后原子替换；失败保留旧数据。结果通过 /status 或该任务日志查看。'),
)}


def menu_commands() -> list[dict[str, str]]:
    return [{'command': item.name, 'description': item.description} for item in COMMANDS.values() if item.menu]


def task_list() -> str:
    return '任务编号：\n' + '\n'.join(f"{i}. {job['name']}（{key}）" for i, (key, job) in enumerate(jobs().items(), 1))


def help_text(name: str = '') -> str:
    name = name.strip().removeprefix('/').split('@', 1)[0]
    if not name:
        return 'pylon 命令\n\n' + '\n'.join(f'/{c.name} — {c.description}' for c in COMMANDS.values()) + '\n\n详细用法：/help 命令，例如 /help run'
    item = COMMANDS.get(name)
    if item is None:
        return '没有这个命令。发送 /help 查看可用命令。'
    lines = [item.description, '', '用法：' + item.usage,
             '示例：' + (item.example or '/' + item.name)]
    if item.detail:
        lines.extend(['', item.detail])
    if item.route == 'task':
        lines.extend(['', task_list()])
    return '\n'.join(lines)
