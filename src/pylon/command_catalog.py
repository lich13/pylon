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
            example='/help gmail', detail='省略命令时列出全部入口；命令前的 / 可省略。'),
    Command('updates', '查看腾讯云可更新软件、系统包和最近升级结果', 'updates', argument='[all]', example='/updates\n/updates all',
            detail='省略参数查看摘要；all 展示全部软件及系统包候选。默认机器人支持勾选多个系统小包、全选、清空和跳过此版本；最后点击“批量更新已选”才会提交安装，关联包必须一起选择。选择会保存，重启后可继续。分阶段发布暂缓时不强行安装，失败或回执不确定不自动重试。检查结果最多缓存 10 分钟并注明时间，APT 使用系统自动刷新的软件源索引。检查失败显示未确认，不会当作最新版。查询不安装软件。每周一北京时间 09:00 向库库干汇总维护事项，无更新且无异常时静默。'),
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
    Command('intel', '打开情报筛选页，选择分类、来源和排除条件', 'intel',
            detail='仅库库干提供。Linux.do Helper、V2EX 非交易与线报统一筛选；展开类别、多选来源和排除词，保存草稿、预览后确认应用。Linux.do只使用Helper摘要，不补抓原帖；缺少条件会注明。私人通知保持独立。可恢复上一版本。'),
    Command('market', '打开四平台交易筛选页，批量选择分类、品牌与型号', 'market',
            detail='仅库库干提供。打开一次性登录链接（10分钟有效），在网页展开分类，批量关注或排除类型、品牌、系列、型号及关键词。修改保存为草稿，预览有效清单并确认后生效；可恢复上一版本。出售与求购独立设置。网页可查看待核对、新品种及原帖处理原因；旧 Telegram 配置按钮已停用。登录最长保持7天，可退出撤销。历史样本不会自动补发。'),
    Command('gmail', '批量选择邮件样本、生成逻辑规则并预览应用', 'gmail', 'review',
            detail='一批最多 20 个样本，可跨多个发件人；点击应删除或必须保留后直接发送原因，选好后一次提交 Grok。预览和应用不删除当前邮件，新规则只用于后续收到的邮件；已确认规则照常自动清理。'),
    Command('ssh_travel', '查看或切换外地 SSH 放行状态', 'ssh_travel', argument='[开启|关闭]',
            example='/ssh_travel\n/ssh_travel 开启\n/ssh_travel 关闭',
            detail='开启后持续允许所有外部 IPv4 访问 SSH TCP/22，不自动关闭；使用关闭按钮或命令撤销。保留密钥认证、Fail2ban 和北京白名单，不开放其他端口。'),
    Command('ssh_status', '查看 SSH 白名单及维护状态', 'ssh', 'status',
            detail='只读查看北京网段数量、内核生效数量、可信登录来源及是否存在待恢复事务。'),
    Command('ssh_check', '检查指定 IPv4 是否符合当前 SSH 白名单规则', 'ssh', 'check', 'IPv4', '/ssh_check 111.201.1.2',
            '分别查询北京网段命中与 fail2ban 豁免，不修改规则。查询结果不代替实际 SSH 连接测试。'),
    Command('ssh_sync', '同步现用管理员专用密钥对应的可信登录 IP', 'ssh', 'sync',
            detail='提交一次可信登录 IP 同步，仅识别配置的管理员用户与专用密钥指纹。结果通过 /status 或该任务日志查看。'),
    Command('ssh_refresh', '更新北京 IP 段；校验成功后替换，失败保留原白名单', 'ssh', 'refresh',
            detail='提交一次下载与校验，检查网段格式、数量和当前管理来源后原子替换；失败保留旧数据。结果通过 /status 或该任务日志查看。'),
)}

MENU = ('gmail', 'updates', 'monitors', 'add_monitor', 'remove_monitor', 'ssh_travel',
        'agent_codex', 'agent_grok', 'update_codex', 'update_grok', 'help')
from dataclasses import replace
COMMANDS = {name: replace(COMMANDS[name], menu=name in MENU)
            for name in (*MENU, *(key for key in COMMANDS if key not in MENU))}
RETIRED = {'status', 'run', 'pause', 'resume', 'logs', 'outbox', 'chiphell_status',
           'chiphell_test', 'chiphell_pause', 'chiphell_resume'}
RETIRED_MESSAGE = '该入口已下线，请使用 /help'


def menu_commands(profile: str = 'default') -> list[dict[str, str]]:
    return [{'command': item.name, 'description': item.description} for item in COMMANDS.values() if item.menu or (profile=='webops' and item.name in {'market','intel'})]


def task_list() -> str:
    return '任务编号：\n' + '\n'.join(f"{i}. {job['name']}（{key}）" for i, (key, job) in enumerate(jobs().items(), 1))


def help_text(name: str = '', profile: str = 'default') -> str:
    name = name.strip().removeprefix('/').split('@', 1)[0]
    if not name:
        return 'pylon 命令\n\n' + '\n'.join(f'/{c.name} — {c.description}' for c in COMMANDS.values() if c.menu or (profile=='webops' and c.name in {'market','intel'})) + '\n\n详细用法：/help 命令，例如 /help gmail'
    if name in RETIRED:
        return RETIRED_MESSAGE
    if name.startswith('gmail_'):
        return 'Gmail 操作已合并，请使用 /gmail'
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
