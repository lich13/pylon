"""Owner-only Telegram controls for the fixed SSH travel operation."""
import hmac
import time

from . import scheduler
from .inline import owns_callback
from .outbox import Outbox
from .inline import signature as _signature


def callback_data(profile, action):
    return f'st1|{action}|' + _signature(profile, 'ssh_travel', action)


def card(profile, result):
    if not result.get('consistent'):
        text = 'SSH 旅行模式状态不一致，请检查受管规则。'
    elif result['enabled']:
        text = 'SSH 旅行模式：已开启\n所有外部 IPv4 可访问 TCP/22，持续生效，需手动关闭。\n密钥认证和 Fail2ban 继续生效。'
    else:
        text = 'SSH 旅行模式：已关闭\n已恢复原有来源限制。'
    action = 'close' if result.get('enabled') or result.get('rule_present') else 'open'
    return {'text': text, 'reply_markup': {'inline_keyboard': [[{
        'text': '立即关闭' if action == 'close' else '开启全量放行', 'callback_data': callback_data(profile, action)}]]}}


def command(profile, argument=''):
    actions = {'': 'status', '开启': 'open', '关闭': 'close'}
    if argument not in actions:
        raise ValueError('用法：/ssh_travel [开启|关闭]')
    action = actions[argument]
    result = scheduler.ssh('travel', action)
    if action != 'status':
        Outbox().put('ssh-travel:last-operation', {'profile': profile, 'action': action, 'at': time.time()})
    return card(profile, result)


def callback(profile, payload):
    if not owns_callback(profile, payload):
        raise ValueError('按钮不属于本人会话或原始机器人消息')
    parts = str(payload.get('data') or '').split('|')
    if len(parts) != 3 or parts[0] != 'st1' or parts[1] not in {'open', 'close'} or not hmac.compare_digest(parts[2], _signature(profile, 'ssh_travel', parts[1])):
        raise ValueError('按钮已失效，请重新打开 /ssh_travel')
    box = Outbox()
    key = 'ssh-travel:callback:' + profile + ':' + str(payload.get('id') or '')
    previous = box.get(key)
    if previous:
        return card(profile, scheduler.ssh('travel', 'status'))
    # Actions set an absolute state. Crash recovery never toggles it back.
    result = command(profile, '开启' if parts[1] == 'open' else '关闭')
    box.put(key, {'action': parts[1], 'at': time.time()})
    return result
