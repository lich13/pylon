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

    @property
    def usage(self) -> str:
        return '/' + self.name + (' ' + self.argument if self.argument else '')


COMMANDS = {item.name: item for item in (
    Command('help', '查看全部功能；使用 /help 命令查看详细用法与示例', 'help', argument='[命令]',
            example='/help run', detail='省略命令时列出全部入口；命令前的 / 可省略。'),
    Command('status', '查看 14 项任务的运行状态、下次执行时间及通知异常', 'status',
            detail='时间均为北京时间；已暂停、运行中、无变化、业务失败、待补发分别显示。任务编号也可用于运行、启停和日志查询。'),
    Command('run', '立即运行指定任务，支持任务编号或任务 ID', 'task', 'run', '任务编号或任务ID', '/run 8',
            '提交一次正式执行，可用 /status 和 /logs 核对结果。同一任务不重叠；暂停排程后仍可手动运行，且不会恢复排程。'
            '报价任务会绕过时段限制。Gmail 和数据清理任务会按既定规则实际删除邮件或过期数据。'),
    Command('pause', '暂停指定任务或全部排程；Chiphell 同时暂停抓取', 'task', 'pause', '任务编号或任务ID或all', '/pause 7',
            'all 表示全部 14 项任务。普通任务只暂停后续排程，不强杀在途执行；Chiphell 联动停止抓取和定时投递。已入队的通知仍会发送。'),
    Command('resume', '恢复指定任务或全部排程；Chiphell 同时恢复抓取', 'task', 'resume', '任务编号或任务ID或all', '/resume 7',
            'all 表示全部 14 项任务。恢复后可能合并补跑错过的排程；Chiphell 同时恢复抓取和投递。Cookie 或健康状态不满足要求时会提示失败。'),
    Command('logs', '查看指定任务的最近执行日志', 'task', 'logs', '任务编号或任务ID', '/logs 8',
            '显示最近的脱敏日志，内容过长时保留末尾；只查询，不运行任务。'),
    Command('outbox', '查看待发、失败和回执不确定的通知', 'outbox',
            detail='显示待发送、发送中、失败和回执不确定的数量及最近记录。只查询；网络失败按队列策略补发，回执不确定不会自动重发。'),
    Command('monitors', '查看当前软件更新监控列表', 'monitor', 'list',
            detail='只查看软件更新订阅，不开启编号移除。需要移除时发送 /remove_monitor，再回复该次列表中的编号。'),
    Command('add_monitor', '增加软件更新监控，支持网站 URL 或 GitHub owner/repo', 'monitor', 'add', 'URL或owner/repo',
            '/add_monitor openai/codex\n/add_monitor https://github.com/openai/codex',
            '识别支持的发布源，保存后校验配置；重复源不会重复添加。不支持的网站会明确提示。原“增加监控 URL”“加监控 owner/repo”仍可使用。'),
    Command('remove_monitor', '列出可移除的监控，回复对应编号完成移除', 'monitor', 'remove',
            detail='先发送此命令取得列表，再单独回复编号。编号对应此次列表中的项目；成功后结束本次选择。原“移除监控”仍可使用。'),
    Command('gmail_candidates', '查看待确认的新邮件类型候选', 'gmail', 'list',
            detail='只列出候选及候选 ID，不新增规则、不删除邮件。接受或忽略时使用列表中的完整 gmail-nt-… ID。'),
    Command('gmail_accept', '接受指定候选，加入后续清理规则，本次不删除邮件', 'gmail', 'accept', '候选ID',
            '/gmail_accept gmail-nt-abc123', '将 /gmail_candidates 中的完整候选 ID 替换示例 ID。接受只修改未来清理规则；已有非星标保护继续生效。'),
    Command('gmail_ignore', '忽略指定候选，保留现有清理规则', 'gmail', 'ignore', '候选ID',
            '/gmail_ignore gmail-nt-abc123', '将 /gmail_candidates 中的完整候选 ID 替换示例 ID。记录忽略选择，不新增清理规则、不删除邮件。'),
    Command('chiphell_status', '查看 Chiphell 抓取、待发队列和健康状态', 'chiphell', 'status',
            detail='显示抓取与投递排程、待发数量、最近健康结果。已暂停是运行状态，不等于抓取失败。'),
    Command('chiphell_test', '测试 Chiphell 实际页面抓取及登录、代理状态', 'chiphell', 'test',
            detail='通过现有家宽代理执行一次页面检查并更新健康结果。抓取锁被占用时提示忙碌，不会显示为测试通过；不会重置历史去重。'),
    Command('chiphell_pause', '暂停 Chiphell 抓取和定时投递', 'chiphell', 'pause',
            detail='联动停止抓取服务与投递排程，保留 Cookie、去重和待发记录。已进入公共通知队列的消息仍可发送。'),
    Command('chiphell_resume', '恢复 Chiphell 抓取和定时投递', 'chiphell', 'resume',
            detail='检查现有 Cookie、健康与去重状态后恢复抓取和投递；条件不满足时返回失败原因。'),
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
    return [{'command': item.name, 'description': item.description} for item in COMMANDS.values()]


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
