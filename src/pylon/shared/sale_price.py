"""Identify price roles within one item; never choose the smallest number.

Callers establish item boundaries. This lexer excludes specifications and
metadata before resolving seller prices, reference prices and explicit revisions.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re
import unicodedata
from typing import Callable

NUMBER = re.compile(r"(?P<prefix>[¥￥]|人民币|RMB)?[ \t]*(?P<n>\d+(?:\.\d+)?)(?P<scale>[ \t]*[kKwW万千])?(?P<suffix>[ \t]*(?:元|块|RMB|人民币))?", re.I)
SELLER = re.compile(r"明盘|一口价|售价|卖价|价格|挂价|现价|现出|现卖|出价|自刀(?:到)?|再刀(?:到)?|刀至|降至|降到|现降|改价(?:为|到)?|现改|现在", re.I)
REVISION = re.compile(r"自刀|再刀|刀至|降至|降到|现降|改价|现改")
REFERENCE = re.compile(
    r"(?:官网|官方|京东|淘宝|天猫|拼多多|商城|专柜|旗舰店)[^\d\n。；;，,]{0,12}?"
    r"(?:现价|售价|价格|报价|到手价|到手|价)|原价|官网|官方价|参考价|市场价|全新价|面值|"
    r"购入|买入|入手|买时|到手价|购价|购买价|收购价|回收价|报价|预算", re.I)
EXPENSE = re.compile(r"定金|订金|押金|补贴|优惠券|省了|便宜了|立减|直降|降了|运费|邮费|维修|换屏|电池更换|首付|每月|分期", re.I)
METADATA = re.compile(r"微信|QQ|电话|联系|编号|信用|IMEI|序列号|浏览|查看|阅读|回复|楼层|下载次数|电池(?:健康|寿命|效率)?|循环|充电次数|频率|频宽|频段|主频|刷新率|分辨率|尺寸|重量|容量|功率|使用时间|剩余(?:容量|电量)|保修至", re.I)
UNIT = re.compile(r"^(?:%|％|GB|TB|MB|MHz|GHz|Hz|mAh|kWh|kW|Wh|W(?![a-z])|V(?![a-z])|mm|cm|kg|寸|英寸|毫米|厘米|公里|小时|分钟|秒|年|月|日|号|代|款|成新|新|次|个|台|部|件|只|张|套|核|线程|页|千米|天|个月|[GT](?![a-z]))", re.I)
MODEL_PREFIX = re.compile(r"(?:iPhone|iPad(?:\s*(?:Pro|Air|mini))?|苹果|Mate(?:Pad|Book)?|Pura|Galaxy\s*[AS]?|小米|红米|Redmi|荣耀|Honor|OPPO|vivo|iQOO|RTX|GTX|RX|DDR|Ryzen\s*[3579]?|Core\s*Ultra\s*[579]?|\bi[3579]|\bR[3579])\s*$", re.I)
DEVICE = re.compile(r"iPhone|iPad|MacBook|Mate|Pura|Galaxy|小米|红米|Redmi|荣耀|Honor|OPPO|vivo|iQOO|手机|平板|笔记本|电脑|内存|硬盘|SSD|NAS", re.I)
CAPACITIES = {8, 16, 32, 64, 128, 256, 512, 1024, 2048}
DATE = re.compile(r"(?<!\d)(?:20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?|\d{1,2}:\d{2}(?::\d{2})?)(?!\d)")
DELIVERY = re.compile(r"^\s*(?:元|块)?\s*(?:出(?!厂|生|现)|包邮|包顺丰|顺丰|到付|不刀|可(?:小)?刀|不议|不讲价|自提|面交)(?:\W|$|[\u4e00-\u9fff])")
POSTFIX_REFERENCE = re.compile(r"^\s*(?:元|块)?\s*(?:买的|买入|购入|入手|原价|官网|参考价|市场价|定金|订金|押金|运费|邮费)")


@dataclass(frozen=True)
class Candidate:
    amount: int | float
    role: str
    evidence: str
    position: int
    reason: str


@dataclass(frozen=True)
class PriceAnalysis:
    amount: int | float | None
    status: str
    reason: str
    evidence: str
    candidates: tuple[Candidate, ...]


def _normalise(text: str) -> str:
    text = unicodedata.normalize('NFKC', text or '')
    # Group separators belong to one amount, while ordinary commas delimit clauses.
    text = re.sub(r'(?<![\d,])\d{1,3}(?:,\d{3})+(?![\d,])', lambda m: m[0].replace(',', ''), text)
    return text


def _role(before: str) -> tuple[str, re.Match | None]:
    markers = [(m.end(), m.start(), role, m) for role, pattern in
               (('seller', SELLER), ('reference', REFERENCE), ('expense', EXPENSE), ('metadata', METADATA))
               for m in pattern.finditer(before)]
    if not markers:
        return '', None
    # The complete "官网现价" span beats the seller word nested inside it.
    _, _, role, match = max(markers, key=lambda value: (value[0], -value[1]))
    return role, match


def analyse_price(text: str, *, minimum: int = 1, maximum: int = 1000000,
                  allow_bare: bool = False,
                  subject: Callable[[str], bool] | None = None,
                  strict_title: Callable[[str], bool] | None = None,
                  capacity_context: bool | None = None) -> PriceAnalysis:
    text = _normalise(text)
    protected = [m.span() for pattern in (DATE, re.compile(r'https?://\S+'),
                 re.compile(r'~~[^\n]+?~~'), re.compile(r'\d+\s*\+\s*\d+\s*(?:G|GB)?', re.I))
                 for m in pattern.finditer(text)]
    candidates: list[Candidate] = []
    offset = 0
    device = bool(DEVICE.search(text)) if capacity_context is None else capacity_context
    for line in re.split(r'[\n。；;，,]', text):
        line_start = offset
        offset += len(line) + 1
        for match in NUMBER.finditer(line):
            start, end = match.span('n')
            position = line_start + start
            before, after = line[:start], line[match.end():]
            raw_after = line[end:]
            scale = (match['scale'] or '').strip().lower()
            amount = Decimal(match['n']) * (10000 if scale in {'w','万'} else 1000 if scale in {'k','千'} else 1)
            if not minimum <= amount <= maximum:
                continue
            value = int(amount) if amount == int(amount) else float(amount)
            marked = bool(match['prefix'] or match['suffix'])
            role, marker = _role(before)
            # A battery/cycle/count label owns its next number, not every later
            # number in a compact specification-and-price heading.
            if role in {'metadata','seller'} and marker is not None and re.search(r'\d', before[marker.end():]):
                role, marker = '', None
            postfix = POSTFIX_REFERENCE.match(after)
            forward_reference = bool(postfix and re.search(r'原价|官网|参考价|市场价', postfix[0])
                                     and re.search(r'\d', after[postfix.end():postfix.end()+16]))
            reason = ''
            if any(left <= position < right for left, right in protected):
                reason = 'protected_metadata'
            elif (not match['prefix'] and start and re.match(r'[A-Za-z0-9_.]', line[start-1])) or (MODEL_PREFIX.search(before) and not marked):
                reason = 'model_identifier'
            elif re.match(r'[A-Za-z0-9_]', raw_after) and not scale and not match['suffix']:
                reason = 'model_identifier'
            elif (UNIT.match(raw_after.lstrip()) and not match['suffix']
                  and not (scale == 'w' and role == 'seller')):
                reason = 'specification_unit'
            elif (scale == 'k' and value <= 8000 and amount % 1000 == 0
                  and re.search(r'分辨率|显示器|屏幕|显示屏|视频', line) and role != 'seller' and not marked):
                reason = 'display_resolution'
            elif role in {'reference','expense','metadata'}:
                reason = role
            elif postfix and not forward_reference:
                reason = 'reference_or_expense'
            elif (not marked and not scale and role != 'seller' and 2000 <= value <= 2035
                  and re.search(r'(?:MacBook|iMac|Mac\s*mini|Mac\s*Pro|ThinkPad|Surface|iPad)\s*(?:Air|Pro|mini)?\s*$', before, re.I)):
                reason = 'model_year'
            elif (not marked and not scale and role != 'seller' and 800 <= value <= 10000
                  and re.search(r'DDR\s*[345]', before, re.I)):
                reason = 'memory_frequency'
            elif (not marked and role != 'seller' and len(match['n']) > 1
                  and match['n'].startswith('0') and not match['n'].startswith('0.')):
                reason = 'identifier_with_leading_zero'
            elif not marked and not scale and device and value in CAPACITIES and role != 'seller':
                reason = 'implicit_capacity'
            if reason:
                candidates.append(Candidate(value, 'excluded', line.strip()[:160], position, reason))
                continue
            explicit = marked or bool(scale) or role == 'seller' or bool(DELIVERY.match(after))
            bare = False
            if not explicit:
                # A transaction's recognised subject gives meaning to a trailing
                # amount even without "出" or "元". Other numbers on the same
                # line have already been classified as model/specification data.
                tail = not after.strip().strip('!！~～')
                bound = (not before.strip() or before[-1:].isspace() or before.endswith((':','：'))
                         or bool(re.search(r'明盘|出售|转让|出个|(?:^|[【\[][^】\]]*[】\]])出', before)))
                bare = allow_bare and tail and bound and (not before.strip() or bool(subject and subject(line)))
                if not bare and tail and strict_title:
                    bare = strict_title(before)
            if explicit or bare:
                role = 'revision' if marker is not None and role == 'seller' and REVISION.search(marker[0]) else 'asking'
                candidates.append(Candidate(value, role, line.strip()[:160], position,
                                            'explicit_sale' if explicit else 'item_tail'))
            else:
                candidates.append(Candidate(value, 'unresolved', line.strip()[:160], position, 'unbound_number'))
    sales = [c for c in candidates if c.role in {'asking','revision'}]
    revisions = [c for c in sales if c.role == 'revision']
    if revisions:
        # A labelled revision supersedes earlier prices for this same item.
        # A different later price still makes the result ambiguous.
        sales = [c for c in sales if c.position >= revisions[-1].position]
    values = {c.amount for c in sales}
    if len(values) == 1:
        value = next(iter(values)); chosen = next(c for c in sales if c.amount == value)
        return PriceAnalysis(value, 'identified', chosen.reason, chosen.evidence, tuple(candidates))
    if len(values) > 1:
        return PriceAnalysis(None, 'ambiguous', 'multiple_sale_prices', '', tuple(candidates))
    reason = ('reference_only' if any(c.reason in {'reference','reference_or_expense'} for c in candidates)
              else 'no_bound_sale_price')
    return PriceAnalysis(None, 'missing', reason, '', tuple(candidates))
