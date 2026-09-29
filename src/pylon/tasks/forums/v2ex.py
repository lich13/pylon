#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pylon import events
import copy
import html
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from pylon.shared import locks, output, paths, state_json
from pylon.tasks.quotes.arbitrage import collect_arbitrage_messages, format_contact_note, is_excluded_device_deal

CHANNEL = "v2ex_notice"
BASE_URL = f"https://t.me/s/{CHANNEL}"
V2EX_JOB_ID = "93e1c037c069"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
MAX_PAGES = 8
BOOTSTRAP_LIMIT = 40
TELEGRAM_REQUEST_TIMEOUT = 8
TELEGRAM_FETCH_MAX_ATTEMPTS = 4
TOPIC_REQUEST_TIMEOUT = 5
TOPIC_FETCH_MAX_ATTEMPTS = 4
FETCH_RETRY_DELAY_SECONDS = 1.0
RUN_BUDGET_SECONDS = 45
TOPIC_ENRICH_MIN_REMAINING_SECONDS = 6
# Keep candidate payloads small; V2EX cron runs every minute and body_excerpt
# only needs enough context for high-signal filtering.
TOPIC_EXCERPT_MAX_CHARS = 260
# Keep price/quota/coupon details even when the first-post excerpt is truncated;
# these are often below intro paragraphs in V2EX promotions.
OFFER_DETAIL_NOTE_MAX_CHARS = 360

PRIVATE_DIR = paths.PROFILE_ROOT
STATE_DIR = paths.CRON_STATE_DIR
STATE_FILE = STATE_DIR / f"{CHANNEL}.json"
PENDING_ALERTS_STATE_KEY = "pending_instant_alerts"
AGENT_BATCH_STATE_KEY = "agent_delivery_batch"
RULE_VERSION = "deterministic-v1"

MARKER = f'<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{CHANNEL}/'

# Strip invisible direction and control characters from external content.
INVISIBLE_PROMPT_CHARS_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e]")


def strip_invisible_prompt_chars(text: str) -> str:
    return INVISIBLE_PROMPT_CHARS_RE.sub("", str(text or ""))


def sanitize_for_prompt(value):
    if isinstance(value, str):
        return strip_invisible_prompt_chars(value)
    if isinstance(value, list):
        return [sanitize_for_prompt(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_prompt(item) for item in value)
    if isinstance(value, dict):
        return {sanitize_for_prompt(key): sanitize_for_prompt(val) for key, val in value.items()}
    return value


def deadline_after(seconds: int) -> float:
    return time.monotonic() + max(1, seconds)


def deadline_remaining(deadline: float | None) -> float:
    if deadline is None:
        return 999999.0
    return max(0.0, deadline - time.monotonic())


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Post:
    message_id: int
    telegram_url: str
    v2ex_url: str
    posted_at: str
    title: str
    tags: List[str]
    core_title: str
    rough_type: str
    body_excerpt: str


@dataclass
class SecondaryMarketListingFacts:
    transaction_role: str = ""
    listing_maturity: str = ""
    device_evidence: list[str] = field(default_factory=list)
    money_evidence: list[str] = field(default_factory=list)
    capability_terms: list[str] = field(default_factory=list)
    has_listing_frame: bool = False
    has_telecom_card_primary: bool = False


@dataclass
class OfferFacts:
    intent: str = "ambiguous"
    benefit_evidence: list[str] = field(default_factory=list)
    weak_terms: list[str] = field(default_factory=list)
    discussion_markers: list[str] = field(default_factory=list)
    has_direct_benefit: bool = False


@dataclass
class LotteryFacts:
    is_lottery: bool = False
    is_high_value: bool = False
    has_actionable_entry: bool = False
    prize_evidence: list[str] = field(default_factory=list)
    slot_evidence: list[str] = field(default_factory=list)
    draw_evidence: list[str] = field(default_factory=list)
    entry_evidence: list[str] = field(default_factory=list)
    tool_evidence: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)


@dataclass
class LocalityFacts:
    transaction_scope: str = "not_local"
    locality_names: list[str] = field(default_factory=list)
    local_only_evidence: list[str] = field(default_factory=list)
    optional_pickup_evidence: list[str] = field(default_factory=list)
    remote_trade_evidence: list[str] = field(default_factory=list)


@dataclass
class PostFacts:
    blob: str
    title_blob: str
    is_secondary_market: bool
    has_sale_offer: bool
    has_buy_request: bool
    has_price_or_quota: bool
    price_evidence_kind: str
    has_high_value_hardware: bool
    has_technical_resource: bool
    has_positive_offer_signal: bool
    has_ai_context: bool
    has_ai_economic_value: bool
    has_economic_value: bool
    economic_value_types: list[str]
    marketplace_verticals: list[str]
    primary_marketplace_categories: list[str]
    addon_marketplace_categories: list[str]
    is_digital_game_commerce_offer: bool
    is_photography_camera_gear_offer: bool
    is_local_service_offer: bool
    is_local_only_offer: bool
    has_beijing_locality: bool
    has_non_beijing_locality: bool
    locality_names: list[str]
    secondary_listing: SecondaryMarketListingFacts = field(default_factory=SecondaryMarketListingFacts)
    offer_facts: OfferFacts = field(default_factory=OfferFacts)
    lottery_facts: LotteryFacts = field(default_factory=LotteryFacts)
    locality: LocalityFacts = field(default_factory=LocalityFacts)


@dataclass
class RouteDecision:
    route: str
    reason: str = ""
    matched_categories: list[str] = field(default_factory=list)
    ignored_low_value_categories: list[str] = field(default_factory=list)


@dataclass
class PreferenceDecision:
    negative_categories: list[str] = field(default_factory=list)
    relaxation_categories: list[str] = field(default_factory=list)
    primary_blocking_categories: list[str] = field(default_factory=list)
    ignored_addon_categories: list[str] = field(default_factory=list)
    hard_exclude_categories: list[str] = field(default_factory=list)


def _is_retryable_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, (http.client.IncompleteRead, TimeoutError)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 425, 429, 500, 502, 503, 504}
    return isinstance(exc, urllib.error.URLError)


def fetch(
    url: str,
    *,
    timeout: int = TELEGRAM_REQUEST_TIMEOUT,
    attempts: int = TELEGRAM_FETCH_MAX_ATTEMPTS,
    phase: str = "fetch",
) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:
            elapsed = time.monotonic() - started
            print(
                f"v2ex_fetch_error phase={phase} attempt={attempt}/{attempts} "
                f"elapsed={elapsed:.2f}s error={type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            if not _is_retryable_fetch_error(exc) or attempt >= attempts:
                raise
            last_error = exc
            time.sleep(FETCH_RETRY_DELAY_SECONDS * attempt)

    if last_error is not None:
        raise last_error
    raise RuntimeError("fetch() exited without a result or captured error")


def strip_tags(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return strip_invisible_prompt_chars(html.unescape(" ".join(text.split())))


OFFER_DETAIL_TOKEN_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?\s*(?:元|块|刀|美元|USD|usd|折)|[$¥]\s*[0-9]+|"
    r"兑换码|邀请码|激活码|卡密|首充包|新用户|V\s*站用户|V\s*友|周卡|月卡|Plus\s*月卡|"
    r"福利|免费领取|免费领|注册即领|注册即可|注册送|平台余额|会员权益|倍率|额度)",
    re.I,
)


def format_offer_detail_note(text: str) -> str:
    """Return compact price/quota/coupon context from the full topic body."""
    normalized = strip_invisible_prompt_chars(" ".join(text.split()))
    snippets: list[str] = []

    def add_snippet(snippet: str) -> None:
        snippet = snippet.strip(" ，。；;:：")
        if len(snippet) < 8:
            return
        if any(snippet in old or old in snippet for old in snippets):
            return
        snippets.append(snippet)

    # Prefer semantically useful blocks over repeated contexts around each price token.
    for pattern, before, after in [
        (r"目前价格", 0, 240),
        (r"V\s*友福利", 0, 190),
        (r"V\s*(?:友|站用户).{0,24}(?:福利|免费|领取|注册送)", 0, 260),
        (r"(?:新人|新用户|专属|全站限时)?福利(?:活动)?", 0, 260),
        (r"免费(?:领取|领|赠送|获得)|注册(?:即领|即可|送)", 80, 180),
        (r"兑换码", 80, 130),
        (r"首充包", 60, 110),
        (r"倍率", 60, 110),
    ]:
        match = re.search(pattern, normalized, flags=re.I)
        if not match:
            continue
        add_snippet(normalized[max(0, match.start() - before) : min(len(normalized), match.end() + after)])

    for match in OFFER_DETAIL_TOKEN_RE.finditer(normalized):
        if len(snippets) >= 3:
            break
        start = max(0, match.start() - 42)
        end = min(len(normalized), match.end() + 95)
        add_snippet(normalized[start:end])

    if not snippets:
        return ""
    note = "优惠信息：" + "；".join(snippets[:3])
    if len(note) > OFFER_DETAIL_NOTE_MAX_CHARS:
        note = note[: OFFER_DETAIL_NOTE_MAX_CHARS - 1].rstrip(" ，。；;:：") + "…"
    return note


def extract_topic_excerpt(page_html: str) -> str:
    match = re.search(r'<div class="topic_content">(.*?)</div>', page_html, flags=re.S)
    if not match:
        return ""
    full_excerpt = strip_tags(match.group(1))
    contact_note = format_contact_note(full_excerpt)
    offer_note = format_offer_detail_note(full_excerpt)
    if len(full_excerpt) <= TOPIC_EXCERPT_MAX_CHARS:
        return full_excerpt
    excerpt = full_excerpt[: TOPIC_EXCERPT_MAX_CHARS - 1].rstrip() + "…"
    if contact_note and contact_note not in excerpt:
        excerpt = f"{excerpt} {contact_note}"
    if offer_note and offer_note not in excerpt:
        excerpt = f"{excerpt} {offer_note}"
    return excerpt


def topic_excerpt_enrichment_candidate(post: Post) -> bool:
    if post.rough_type in {"secondary_market", "promotion_or_offer", "share_creation"}:
        return True
    if is_cursor_related_post(post):
        return True
    if is_low_value_info_post(post):
        return True
    if is_question_or_discussion(post):
        return True
    return is_instant_candidate(post)


def enrich_posts_with_topic_excerpt(posts: List[Post], *, deadline: float | None = None) -> None:
    cache: dict[str, str] = {}
    for post in posts:
        if not topic_excerpt_enrichment_candidate(post):
            continue
        if deadline_remaining(deadline) < TOPIC_ENRICH_MIN_REMAINING_SECONDS:
            print("v2ex_topic_enrich_skipped reason=budget_exhausted", file=sys.stderr)
            break
        if post.v2ex_url not in cache:
            try:
                cache[post.v2ex_url] = extract_topic_excerpt(
                    fetch(
                        post.v2ex_url,
                        timeout=TOPIC_REQUEST_TIMEOUT,
                        attempts=TOPIC_FETCH_MAX_ATTEMPTS,
                        phase="v2ex_topic",
                    )
                )
            except Exception as exc:
                print(f"v2ex_topic_enrich_failed id={post.message_id} error={type(exc).__name__}", file=sys.stderr)
                cache[post.v2ex_url] = ""
        post.body_excerpt = cache[post.v2ex_url]


def extract_tags_and_core(title: str) -> tuple[List[str], str]:
    title = strip_invisible_prompt_chars(title)
    tags: List[str] = []
    s = title.strip()
    while s:
        if s.startswith("【") and "】" in s:
            end = s.find("】")
            tags.append(s[1:end].strip())
            s = s[end + 1 :].lstrip()
            continue
        if s.startswith("[") and "]" in s:
            end = s.find("]")
            tags.append(s[1:end].strip())
            s = s[end + 1 :].lstrip()
            continue
        break
    return tags, s.strip() or title.strip()


def classify(tags: List[str], title: str, core_title: str) -> str:
    blob = " ".join(tags + [title, core_title])

    if any(word in blob for word in ["二手交易", "拼车", "转让", "出售", "闲置", "收台", "出 ", "出个", "甩卖"]):
        return "secondary_market"

    if any(word in blob for word in ["推广", "公益", "免费", "额度", "赠送", "注册送", "白嫖", "试用", "福利"]):
        return "promotion_or_offer"

    if any(word in blob for word in ["分享创造", "做了个", "做了一个", "写了个", "写了一个", "发布", "开源", "上线", "自托管"]):
        return "share_creation"

    if any(tag in tags for tag in ["问与答", "生活", "程序员", "分享发现", "Cursor", "VPS", "推广"]):
        return "general_or_low_signal"

    return "unclassified"


QUESTION_DISCUSSION_TAGS = {"问与答", "生活", "职场话题", "自言自语", "情感"}
LOW_VALUE_INFO_TAGS = {"宽带症候群"}
QUESTION_DISCUSSION_RE = re.compile(
    r"(问与答|求助|咨询|请教|讨论|大家怎么看|想问|求推荐|求.*推荐|有没有|有无|怎么|如何|为什么|哪[个款里种]|靠谱吗|是否|吗\b|？|\?)",
    re.I,
)
LOW_VALUE_NETWORK_RE = re.compile(
    r"(宽带症候群|宽带|专线|电信|联通|移动|上行|下行|固定\s*IP|公网\s*IP|城域网|IPLC|IEPL|家宽|光猫|路由|测速|延迟|丢包)",
    re.I,
)
LOW_VALUE_NETWORK_DETAIL_RE = re.compile(
    r"(套餐|资费|上行|下行|固定\s*IP|公网\s*IP|城域网|VPS|线路|体验有感|使用情况)",
    re.I,
)
LOW_VALUE_DEVICE_REFRESH_DISCUSSION_RE = re.compile(
    r"(多久.{0,12}(?:换|升级|置换)|(?:换|升级|置换).{0,12}(?:一次|吗|么|多久)|"
    r"要不要.{0,8}(?:买|换|升级)|值不值得.{0,8}(?:买|换|升级)|值得.{0,8}(?:买|换|升级)|"
    r"残值|换机|换电脑|体验.{0,8}(?:飞跃|提升)|配置.{0,8}(?:推荐|选择))",
    re.I,
)
SECONDARY_MARKET_INTENT_RE = re.compile(
    r"(二手交易|出售|出个|出一|收一|收个|求购|转让|明盘|预算|可刀|包邮|自提|闲鱼|鱼链接)",
    re.I,
)
SECONDARY_MARKET_BUY_REQUEST_RE = re.compile(
    r"(^|[【\[\s，,。；;:：])(?:[0-9]{1,5}\s*收|求购|收购|想收|有偿收|收个|收台|收一|蹲个|蹲一个|求\s*收|收\s+[A-Za-z0-9\u4e00-\u9fff])",
    re.I,
)
SECONDARY_MARKET_BUY_DETAIL_RE = re.compile(
    r"(预算|型号|容量|成色|保修|电池|屏幕|配置|内存|硬盘|自提|面交|邮寄|闲鱼|走鱼|"
    r"[0-9]+\s*(?:G|GB|T|TB)|[¥￥]\s*[0-9]+|[0-9]{3,5}\s*(?:元|块))",
    re.I,
)
SECONDARY_MARKET_SPECIFIC_HARDWARE_BUY_RE = re.compile(
    r"(iPhone\s*[0-9A-Za-z ]+|iPad\s*[0-9A-Za-z ]+|MacBook|MBP|MBA|Mac\s*mini|Mac\s*Studio|"
    r"Mac\s*[A-Za-z0-9 ]*(?:M[1-9]|mini|Studio)|M[1-9]\s*(?:Pro|Max|Ultra)?|"
    r"NAS|CPE|5G\s*CPE|路由器|软路由|光猫|机械硬盘|SSD|显卡|内存|显示器|"
    r"[0-9]+\s*(?:G|GB|T|TB))",
    re.I,
)
SECONDARY_MARKET_SALE_OFFER_RE = re.compile(
    r"(^|[【\[\s，,。；;:：])(?:出售|继续出|打包出|试出|低价出|出个|出一|出两|出[0-9]+|出\s*[A-Za-z0-9\u4e00-\u9fff]|卖|转让|甩卖|闲置|明盘)",
    re.I,
)
PRICE_OR_QUOTA_RE = re.compile(
    r"([¥￥$]\s*[0-9]+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?\s*(?:元|块|刀|美元|USD|usd|折|%|G|GB|T|TB|W|w))",
    re.I,
)
SECONDARY_MARKET_BARE_PRICE_RE = re.compile(
    r"(?:明盘|价格|售价|卖|出|转让|预算|打包|自提|包邮|闲鱼|走鱼|可刀|低价|跑路价|专属)\D{0,12}([1-9][0-9]{2,5})(?!\s*(?:年|天|次|%|折))",
    re.I,
)
SECONDARY_MARKET_DEVICE_BARE_PRICE_RE = re.compile(
    r"(?:iPhone|iPad|MacBook|MBP|MBA|Mac\s*mini|Mac\s*Studio|红米|Redmi|Samsung|三星|Galaxy|"
    r"大疆|大僵|DJI|Nano|内存|DDR[0-9]?|SSD|硬盘|NAS|显示器|键盘|鼠标|路由器|CPE|标准套餐|单卖一根)"
    r"[^，。；;\n]{0,42}?([1-9][0-9]{2,5})(?![0-9]*\s*(?:年|天|次|%|折|G|GB|T|TB|Mhz|MHz|mhz|新))",
    re.I,
)
SECONDARY_MARKET_CONDITION_NUMBER_RE = re.compile(
    r"(?<![0-9])(?:9{1,3}|8{1,2}|7{1,2})\s*新(?![0-9])",
    re.I,
)
DEVICE_OR_DEV_RESOURCE_RE = re.compile(
    r"(iPhone|iPad|MacBook|MBP|MBA|Mac\s*mini|Mac|手机|平板|电脑|笔记本|硬盘|SSD|NAS|内存|键盘|鼠标|"
    r"CPE|5G\s*CPE|路由器|软路由|光猫|OpenAI|ChatGPT|Codex|API|激活码|兑换码|邀请码|卡密)",
    re.I,
)
HIGH_VALUE_HARDWARE_RE = re.compile(
    r"(iPhone|iPad|MacBook|MBP|MBA|Mac\s*mini|Mac\s*Studio|Mac|平板|电脑|笔记本|"
    r"硬盘|机械硬盘|SSD|NAS|内存|显卡|主板|CPU|键盘|鼠标|显示器|"
    r"CPE|5G\s*CPE|路由器|软路由|光猫|Samsung|三星|Galaxy|S[0-9]{2}\s*Ultra)",
    re.I,
)
SECONDARY_MARKET_DEVICE_EVIDENCE_RE = re.compile(
    r"(iPhone\s*[0-9A-Za-z ]*|iPad\s*(?:mini\s*)?[0-9A-Za-z ]*|Apple\s*Watch\s*Ultra\s*[0-9A-Za-z]*|"
    r"Apple\s*Watch|MacBook|MBP|MBA|Mac\s*mini|Mac\s*Studio|红米\s*Note\s*[0-9A-Za-z]*|"
    r"Redmi\s*Note\s*[0-9A-Za-z]*|Samsung|三星|Galaxy|S[0-9]{2}\s*Ultra|手机|平板|电脑|笔记本|"
    r"机械硬盘|硬盘|SSD|NAS|内存|显卡|主板|CPU|键盘|鼠标|显示器|CPE|5G\s*CPE|路由器|软路由|光猫)",
    re.I,
)
SECONDARY_MARKET_MONEY_WITH_UNIT_RE = re.compile(
    r"([¥￥$]\s*[0-9]+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?\s*(?:元|块|刀|美元|USD|usd))",
    re.I,
)
SECONDARY_MARKET_APPRAISAL_PRICE_RE = re.compile(
    r"(?:估价|回收价|爱回收|折抵|抵扣|可以抵|可抵|抵)\D{0,18}([1-9][0-9]{2,5})(?!\s*(?:年|天|次|%|折|G|GB|T|TB))",
    re.I,
)
SECONDARY_MARKET_DISCOUNT_OR_BALANCE_RE = re.compile(
    r"((?:余额|面值|卡内|储值|剩余)\D{0,12}[1-9][0-9]{1,5}|(?<![0-9])(?:[1-9](?:\.[0-9])?|[1-9][0-9])\s*折)",
    re.I,
)
SECONDARY_MARKET_LISTING_FRAME_RE = re.compile(
    r"(出售|继续出|打包出|试出|低价出|出个|出一|出两|出[0-9]+|出\s*[A-Za-z0-9\u4e00-\u9fff]|"
    r"想出|先询价|询价|不知什么价合适|售价|明盘|可刀|包邮|自提|闲鱼|走鱼|有需要|自用设备|买来后|"
    r"成色|电池|无磕碰|包装盒|保护套|实拍|参数详情|估价|回收价|可以抵|抵扣|可抵)",
    re.I,
)
TELECOM_CARD_STRONG_CONTEXT_RE = re.compile(
    r"(电话卡|手机卡|流量卡|号码卡|上网卡|物联卡|保号卡|接码卡|接码|保号套餐|PayGo|PAYG|"
    r"预付费|空卡|手机号|日本手机号|日本号码|Rakuten|楽天|giff\s*gaff|GiffGaff|"
    r"可收短信|能接短信|接短信|收短信|套餐|注册\s*(?:claude|paypay)|PayPay)",
    re.I,
)
SIM_CAPABILITY_CONTEXT_RE = re.compile(
    r"((?:iPhone|iPad|Apple\s*Watch|Watch|蜂窝版|国际版|美版|国行|单实体|双实体|卡槽).{0,48}"
    r"(?<![A-Za-z])(?:SIM|e[-\s]?SIM)(?![A-Za-z])|"
    r"(?<![A-Za-z])(?:SIM|e[-\s]?SIM)(?![A-Za-z]).{0,48}"
    r"(?:iPhone|iPad|Apple\s*Watch|Watch|蜂窝版|国际版|美版|国行|单实体|双实体|卡槽))",
    re.I,
)
BEIJING_LOCALITY_TERMS = [
    "北京",
    "东城",
    "西城",
    "朝阳",
    "丰台",
    "石景山",
    "海淀",
    "门头沟",
    "房山",
    "通州",
    "顺义",
    "昌平",
    "大兴",
    "怀柔",
    "平谷",
    "密云",
    "延庆",
    "亦庄",
]
NON_BEIJING_LOCALITY_TERMS = [
    "上海",
    "成都",
    "深圳",
    "广州",
    "杭州",
    "南京",
    "苏州",
    "武汉",
    "西安",
    "重庆",
    "天津",
    "郑州",
    "长沙",
    "合肥",
    "青岛",
    "济南",
    "厦门",
    "福州",
    "宁波",
    "无锡",
    "常州",
    "佛山",
    "东莞",
    "珠海",
    "中山",
    "惠州",
    "昆明",
    "贵阳",
    "南宁",
    "南昌",
    "长春",
    "沈阳",
    "大连",
    "哈尔滨",
    "太原",
    "石家庄",
    "兰州",
    "乌鲁木齐",
    "呼和浩特",
    "银川",
    "西宁",
    "拉萨",
    "香港",
    "澳门",
    "台湾",
    "四川",
    "广东",
    "浙江",
    "江苏",
    "湖北",
    "陕西",
    "湖南",
    "安徽",
    "山东",
    "福建",
    "云南",
    "贵州",
    "广西",
    "江西",
    "吉林",
    "辽宁",
    "黑龙江",
    "山西",
    "河北",
    "甘肃",
    "新疆",
    "内蒙古",
    "宁夏",
    "青海",
]
LOCAL_BEIJING_RE = re.compile("|".join(map(re.escape, BEIJING_LOCALITY_TERMS)))
NON_BEIJING_LOCALITY_RE = re.compile("|".join(map(re.escape, NON_BEIJING_LOCALITY_TERMS)))
LOCAL_ONLY_CONTEXT_RE = re.compile(
    r"((?:本地|同城|当地|附近).{0,12}(?:自提|面交|面取|到店|门店|上门|核销|服务)|"
    r"到店|门店|上门|核销|自提|面交|面取|线下|"
    r"仅限.{0,8}(?:本地|同城|自提|面交)|只(?:限|接受).{0,8}(?:本地|同城|自提|面交)|"
    r"(?:限|仅|只).{0,8}(?:自提|面交|面取|到店|上门|核销))",
    re.I,
)
LOCAL_SERVICE_OFFER_RE = re.compile(
    r"(家政|保洁|清洁|钟点工|京东\s*家政|上门.{0,12}(?:服务|维修|安装|清洗|保洁|家政)|"
    r"(?:洗衣|洗鞋|干洗|衣物护理).{0,20}(?:服务|券|卡|权益|核销|到店|门店)|"
    r"(?:维修|安装|搬家|跑腿|代办|陪诊|开锁|疏通|家电清洗|空气净化|除甲醛).{0,20}(?:服务|券|卡|权益|核销|上门|到店|门店)|"
    r"(?:到店|门店|上门|本地).{0,20}(?:服务|核销|洗衣|洗鞋|维修|安装|搬家|跑腿|代办|陪诊|保洁|家政)|"
    r"(?:服务券|服务卡|核销券|到店券|上门券))",
    re.I,
)
LOCAL_ONLY_STRONG_CONTEXT_RE = re.compile(
    r"(仅限.{0,12}(?:本地|同城|自提|面交|面取)|"
    r"(?:只|仅)(?:限|接受|支持).{0,12}(?:本地|同城|自提|面交|面取)|"
    r"(?:限|限定).{0,12}(?:本地|同城|自提|面交|面取)|"
    r"(?:(?:不支持|不接受|不走|不能|无法|不发).{0,8}(?:邮寄|快递|闲鱼|走鱼|发货)|不(?:邮寄|快递|走鱼|发货))|"
    r"(?:本地|同城|当地).{0,12}(?:面交|面取|自提)|"
    r"(?:面交|面取|自提).{0,12}(?:优先|限定|仅限|只限|只接受|不邮寄|不发货))",
    re.I,
)
LOCAL_OPTIONAL_PICKUP_RE = re.compile(
    r"((?:支持|可以|可|接受|欢迎).{0,10}(?:自提|面交|面取)|"
    r"(?:自提|面交|面取).{0,10}(?:可|可以|优惠|减|少|便宜)|"
    r"(?:支持|可以|可).{0,8}(?:本地|同城).{0,8}(?:自提|面交|面取))",
    re.I,
)
REMOTE_TRADE_CONTEXT_RE = re.compile(
    r"(闲鱼|咸鱼|走鱼|鱼链接|m\.tb\.cn|2\.taobao\.com|包邮|邮寄|快递|发货|到付|顺丰|"
    r"拍下|拍了|链接直接打开|点击链接直接打开|走平台|平台交易)",
    re.I,
)
# Hard signals that make a question/discussion worth sending to the model for
# semantic judgment. Keep this strict: plain numbers/specs in a question are not
# enough, otherwise low-value Q&A still wakes the cron agent.
ACTIONABLE_SIGNAL_RE = re.compile(
    r"(二手交易|出售|出个|出一|收一|收个|求购|转让|明盘|预算|可刀|包邮|自提|闲鱼|鱼链接|"
    r"兑换码|邀请码|激活码|卡密|API[_ -]?Key|sk-[A-Za-z0-9]|github\.com|gitlab\.com|开源|repo|repository|demo|官网|下载|App Store|Chrome Web Store|一键部署|Docker|自托管|"
    r"倍率|[0-9]+\s*(元|块|刀|美元|rmb|RMB|USD|usd|%|折|w|W)|[$¥]\s*[0-9])",
    re.I,
)
TECH_RESOURCE_RE = re.compile(
    r"(github\.com|gitlab\.com|开源|源码|repo|repository|Docker|docker\s*compose|自托管|本地部署|一键部署|"
    r"SDK|CLI|benchmark|合规扫描|管理平台|分页库|安全研究|离线解析|demo|演示)",
    re.I,
)
TECH_LOW_ECONOMIC_VALUE_RE = re.compile(
    r"(VS\s*Code|Visual\s*Studio\s*Code|BYOK|Codex.{0,32}(?:模型|不支持|支持变化|账户登录|ChatGPT\s*账户)|"
    r"GPT-5\.[0-9](?:-codex)?|Kubernetes|k8s|vLLM|SOCI|lazy\s*loading|Pod\s*Ready|containerd|overlayfs|"
    r"HermesPolicy|镜像.{0,24}(?:拉取|构建|缓存|启动|Ready)|开源项目|工程实测|技术实测|使用说明|发布说明|"
    r"分页库|合规扫描|管理平台|benchmark|自托管|本地部署|Docker|docker\s*compose|SDK|CLI|repo|repository|github\.com|gitlab\.com)",
    re.I,
)
STRONG_OFFER_SIGNAL_RE = re.compile(
    r"(免费(?:额度|余额|试用|体验|领取|领|赠送|获得)|注册(?:即领|即可|送)|注册送|赠送|白嫖|"
    r"兑换码|优惠码|折扣码|福利码|卡密|credit|平台余额|首充包|V\s*(?:友|站用户).{0,80}(?:免费|送|领取|额度|余额|福利|credit)|"
    r"(?:(?:注册|新用户|回帖|评论|留邮箱|V\s*(?:友|站用户)).{0,24}(?:即送|再送|送|赠送|领取)|(?:即送|再送|赠送|免费领取|福利)).{0,32}(?:[$]\s*[0-9]+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金|credit|credits))|"
    r"(?:免费|赠送|领取|福利).{0,50}[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金)|"
    r"[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金).{0,40}(?:免费|额度|余额|赠送|领取|福利|credit)|"
    r"教育优惠|优惠券|[0-9]+(?:\.[0-9]+)?\s*券|今天截止|今晚截止|限时截止|截止|"
    r"0\.[0-9]+\s*(?:元|块)\s*/?\s*(?:刀|美元|USD))",
    re.I,
)
NEGATED_OFFER_SIGNAL_RE = re.compile(
    r"(无|没有|不含|不送|不可|不能|没)(?:任何|明确)?(?:免费额度|免费余额|免费领取|兑换码|优惠码|折扣码|福利|V\s*友福利|平台余额|credit|credits)",
    re.I,
)
NEGATED_BENEFIT_CONTEXT_RE = re.compile(
    r"(无|没有|不含|不送|不可|不能|没)(?:任何|明确)?[^。；;，,\n]{0,18}"
    r"(免费额度|免费余额|免费领取|兑换码|邀请码|优惠码|折扣码|福利|金额|价格|平台余额|credit|credits|注册领取|领取方式)",
    re.I,
)
IMAGE_GEN_SITE_RE = re.compile(
    r"(GPT\s*-?\s*Image|Midjourney|\bMJ\b|Stable\s*Diffusion|DALL[·\- ]?E|Ideogram|Leonardo|Flux|Nano\s*Banana|"
    r"AI\s*(生图|绘画|画图|作图|图像生成)|文生图|图生图|生图|图片生成|图像生成)",
    re.I,
)
IMAGE_GEN_PROMO_RE = re.compile(
    r"(生图网站|生图站|AI\s*生图|提示词|prompt|案例|复制|直接生成|注册|注册送|免费|试用|额度|次数|生图机会|生图次数|生成图片)",
    re.I,
)
IMAGE_GEN_DEV_VALUE_RE = re.compile(
    r"(github\.com|gitlab\.com|开源|源码|repo|repository|自托管|本地部署|Docker|API|SDK|CLI|模型|权重|LoRA|checkpoint|ComfyUI|workflow|工作流)",
    re.I,
)
AI_API_RELAY_SITE_RE = re.compile(
    r"((?:OpenAI|ChatGPT|Claude|Gemini|GPT|LLM|Codex|大模型|模型).{0,24}(?:API|中转|转发|Base\s*URL|订阅|额度|用量|倍率|号池)|"
    r"(?:API|Base\s*URL|中转|号池).{0,24}(?:OpenAI|ChatGPT|Claude|Gemini|GPT|Codex|大模型)|中转站|API\s*中转|relay|Codex\s*(?:站|中转|号池))",
    re.I,
)
PAID_AI_RELAY_PROMO_RE = re.compile(
    r"(订阅套餐|订阅组|套餐|充值|余额|付费|价格|当前在售|限时特惠|[¥￥$]\s*[0-9]|"
    r"[0-9]+(?:\.[0-9]+)?\s*(?:元|块)\s*/\s*(?:月|30\s*天|天)|每日\s*[0-9]+\s*(?:刀|美元|USD).{0,20}用量|"
    r"倍率\s*(?:为|=|：|:)?\s*[0-9]+(?:\.[0-9]+)?\s*x?|[0-9]+(?:\.[0-9]+)?\s*(?:倍率|折)|"
    r"(?:rate|ratio)\s*(?:为|=|：|:)?\s*[0-9]+(?:\.[0-9]+)?)",
    re.I,
)
AI_RELAY_CONCRETE_BENEFIT_RE = re.compile(
    r"(免费(?:额度|余额|试用|体验|领取|领|赠送|会员|权益)|体验额度|注册(?:即领|即可|送)|赠送|白嫖|"
    r"兑换码|优惠码|折扣码|福利码|卡密|首充包|平台余额|会员权益|V\s*友福利|"
    r"V\s*(?:友|站用户).{0,80}(?:免费|领取|赠送|额度|余额|福利)|"
    r"(?:(?:注册|新用户|回帖|评论|留邮箱|V\s*(?:友|站用户)).{0,24}(?:即送|再送|送|赠送|领取)|(?:即送|再送|赠送|免费领取|福利)).{0,32}(?:[$]\s*[0-9]+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金|credit|credits))|"
    r"(?:免费|赠送|领取|福利).{0,50}[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金)|"
    r"[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金).{0,40}(?:免费|额度|余额|赠送|领取|福利))",
    re.I,
)
AI_RELAY_LOW_ABSOLUTE_PRICE_RE = re.compile(
    r"(0\.[0-9]+\s*(?:元|块)\s*/?\s*(?:刀|美元|USD)|(?<![0-9])[1-2]?[0-9]\s*(?:元|块)\s*/\s*(?:月|30\s*天))",
    re.I,
)
AI_RELAY_RATE_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(?:倍率|折|x\b)|(?:倍率|rate|ratio)\s*(?:为|=|：|:)?\s*([0-9]+(?:\.[0-9]+)?)\s*x?",
    re.I,
)
AI_SUBSCRIPTION_PRICE_LIMIT_RMB = 75.0
OPENAI_PRO_SUBSCRIPTION_PRICE_LIMIT_RMB = 1000.0
OPENAI_PRO20_PRODUCT_RE = re.compile(r"(?<![A-Za-z])(?:ChatGPT\s*|GPT\s*)?Pro\s*20\s*(?:[xX]|×|倍)(?![A-Za-z])", re.I)
OPENAI_SUBSCRIPTION_CONTEXT_RE = re.compile(
    r"(OpenAI|ChatGPT|Claude|Codex|GPT|卡网|代充|充值|订阅|月卡|年卡|号池|共享|Pro\s*20\s*(?:[xX]|×|倍))",
    re.I,
)
OPENAI_PLUS_PRODUCT_RE = re.compile(r"(?<![A-Za-z])(?:ChatGPT\s*)?(?:GPT\s*)?Plus(?![A-Za-z])", re.I)
OPENAI_PRO_PRODUCT_RE = re.compile(r"(?<![A-Za-z])(?:ChatGPT\s*|GPT\s*)Pro(?![A-Za-z])", re.I)
CLAUDE_PRO_PRODUCT_RE = re.compile(r"(?<![A-Za-z])Claude\s*Pro(?![A-Za-z])", re.I)
OPENAI_ANY_SUBSCRIPTION_PRODUCT_RE = re.compile(
    r"(?<![A-Za-z])(?:(?:ChatGPT\s*)?(?:GPT\s*)?Plus|(?:ChatGPT\s*|GPT\s*)Pro|(?:ChatGPT\s*|GPT\s*)?Pro\s*20\s*(?:[xX]|×|倍)|Claude\s*Pro)(?![A-Za-z])",
    re.I,
)
AI_SUBSCRIPTION_PRODUCT_PATTERNS = (
    ("chatgpt_pro", OPENAI_PRO20_PRODUCT_RE),
    ("chatgpt_plus", OPENAI_PLUS_PRODUCT_RE),
    ("chatgpt_pro", OPENAI_PRO_PRODUCT_RE),
    ("claude_pro", CLAUDE_PRO_PRODUCT_RE),
)
AI_SUBSCRIPTION_TOPUP_CONTEXT_RE = re.compile(
    r"(第三方|代充|充值|兑换(?!码)|开通|续费|卡网|账号|共享|拼车|即买即发|数字产品|"
    r"月卡|年卡|订阅|会员|当前在售|售价|标价|价格|只要|仅需|出售|转让|卖|挂出来卖|闲鱼|咸鱼|优惠码|官方退)",
    re.I,
)
AI_SUBSCRIPTION_CODE_RESOURCE_RE = re.compile(r"(激活码|兑换码|优惠码|折扣码|邀请码|卡密|API[_ -]?Key|credit|credits|额度)", re.I)
OPENAI_PRICE_WITH_UNIT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:[¥￥]\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:元|块|RMB|CNY)(?![A-Za-z])",
    re.I,
)
OPENAI_PRICE_WITH_PREFIX_RE = re.compile(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]+)?)")
OPENAI_BARE_PRICE_AFTER_CUE_RE = re.compile(
    r"(?:只要|仅需|标价|标个|售价|价格|价|卖|出|代充|充值|开通|续费|=|:|：)\s*(?:[¥￥]\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.I,
)
OPENAI_BARE_PRODUCT_TAIL_PRICE_RE = re.compile(
    r"^\s*(?:会员|月卡|年卡|代充|充值|开通|续费)?\s*(?:[¥￥]\s*)?([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.I,
)
OPENAI_K_PRICE_RE = re.compile(r"(?<![A-Za-z0-9])([1-9](?:\.[0-9]+)?)\s*[kKＫ](?![A-Za-z])")
OPENAI_NON_PRICE_SUFFIX_RE = re.compile(r"\s*(?:个?月|年|天|次|%|折|刀|美元|USD|usd|x\b|X\b)")
OPENAI_NON_PRICE_TIME_SUFFIX_RE = re.compile(r"\s*(?:[:：+\-][0-9]{2}|T[0-9]{2})")
OPENAI_CONTACT_NUMBER_CONTEXT_RE = re.compile(r"(QQ|Q\s*群|QQ群|群号|交流群|微信|VX|WeChat|联系|客服|进群)", re.I)
OPENAI_USAGE_POOL_CONTEXT_RE = re.compile(
    r"((?:Codex|OpenAI|ChatGPT|GPT).{0,40}(?:号池|站|全家桶|订阅分组|按量付费)|"
    r"(?:号池|订阅分组|按量付费|全家桶).{0,40}(?:Codex|OpenAI|ChatGPT|GPT|Plus))",
    re.I,
)
NETWORK_PROXY_CORE_SERVICE_RE = re.compile(
    r"(住宅\s*代理|海外\s*代理|网络\s*代理|动态\s*(?:住宅\s*)?IP|静态\s*(?:住宅\s*)?IP|"
    r"(?:动态|静态)\s*住宅(?:\s*(?:流量|套餐|代理|节点|服务|IP))?|住宅\s*(?:IP|流量|套餐|代理|节点)|"
    r"代理\s*IP|IP\s*代理|IP\s*池|"
    r"(?:HTTP|HTTPS|SOCKS|Socks|S5|s5)\s*/?\s*(?:代理|协议)|SOCKS\s*5|"
    r"(?:爬虫|跨境电商|SEO|风控|养号|注册).{0,24}(?:代理|IP\s*池|住宅\s*IP)|"
    r"(?:代理|住宅\s*IP).{0,24}(?:流量|兑换码|CDK|联盟计划|Affiliate|分佣)|"
    r"VPN|机场|科学上网|节点\s*(?:订阅|套餐|流量)|翻墙(?:服务|工具|软件|节点|线路|套餐)|Novproxy|JibaoProxy|jibaoproxy)",
    re.I,
)
NETWORK_PROXY_SERVICE_RE = NETWORK_PROXY_CORE_SERVICE_RE
NETWORK_PROXY_BRAND_OR_DOMAIN_RE = re.compile(r"(?:proxy|代理|住宅|jibaoproxy|novproxy)", re.I)
NETWORK_PROXY_COMMERCE_RE = re.compile(
    r"(注册(?:即送|送)?|注册送|首(?:次)?充|充值|套餐|流量|刀起|[$¥￥]\s*[0-9]|"
    r"兑换码|CDK|联盟计划|Affiliate|分佣|注册链接|member\.)",
    re.I,
)
NETWORK_PROXY_USE_CASE_RE = re.compile(r"(跨境电商|SEO|爬虫|风控|养号|注册|解锁|批量)", re.I)
CLOUD_SERVER_HOSTING_RE = re.compile(
    r"(?<![A-Za-z])(?:VPS|VDS)(?![A-Za-z])|"
    r"(?:云|轻量\s*(?:应用\s*)?|弹性|虚拟)\s*(?:服务器|主机)|"
    r"(?:阿里云|腾讯云|华为云|老李云|雨云|搬瓦工|RackNerd|HostHatch|Vultr|Linode|DigitalOcean|AWS|EC2).{0,18}(?:服务器|主机|套餐)|"
    r"(?:国际\s*)?(?:阿里云|腾讯云|华为云|AWS|EC2|GCP|Google\s*Cloud|谷歌云|Azure|Oracle\s*Cloud|甲骨文云|UCloud|青云|火山引擎|京东云|百度智能云|天翼云|Cloudflare).{0,28}(?:账号|账户|充值|代充|代付|折扣|余额|优惠券|代金券|充值券|云服务)|"
    r"(?:云服务|云厂商|云账号|云账户).{0,24}(?:账号|账户|充值|代充|代付|折扣|余额|优惠券|代金券|充值券)|"
    r"云平台.{0,24}(?:充值|代充|代付|折扣|优惠券|代金券|充值券)|"
    r"(?:账号|账户|充值|代充|代付|优惠券|代金券|充值券).{0,20}(?:AWS|EC2|GCP|Google\s*Cloud|谷歌云|阿里云|腾讯云|华为云|Azure|甲骨文云|UCloud|青云|火山引擎|京东云|百度智能云|天翼云|Cloudflare)|"
    r"\bSM\s*账号\b.{0,24}(?:云|AWS|GCP|谷歌云|阿里云|腾讯云|充值|代充)|"
    r"(?:服务器|云主机|VPS|VDS|虚拟主机).{0,24}(?:套餐|月付|年付|续费|剩余|机房|线路|带宽|流量|IPv?4|IPv?6)|"
    r"服务器.{0,24}(?:[0-9]+\s*C|[0-9]+\s*核).{0,24}(?:[0-9]+\s*M|[0-9]+\s*G|带宽|流量)|"
    r"虚拟主机|主机空间|建站主机|独立服务器|云服务商|云厂商|web\s*hosting|hosting",
    re.I,
)
WORK_RELATED_TAGS = {"酷工作", "职场话题", "外包", "招聘", "求职", "远程工作", "Jobs", "jobs"}
WORK_RELATED_RE = re.compile(
    r"(招聘|招人|招募|内推|求职|找工作|工作机会|外包|私活|接单|派遣|劳务|猎头|HR|"
    r"岗位|职位|全职|兼职|实习|校招|社招|面试|简历|薪资|薪水|工资|到岗|入职|候选人|"
    r"Job\s*Type|Full[-\s]?Time|Part[-\s]?Time|internship|hiring|recruit(?:ing|er|ment)?|job\s+opening|open\s+role|position\s+[一二三四五六七八九十0-9])",
    re.I,
)
BLACKLISTED_MERCHANT_RE = re.compile(r"(摩集电商|NewMoorj)", re.I)
# Keep card exclusions narrow: user only rejected JD/Jingdong E-cards;
# non-JD niche shopping cards should still reach semantic scoring.
EXCLUDED_V2EX_MISC_RE = re.compile(
    r"(京东\s*[eEＥｅ]?[-\s]*卡|京东礼品卡|京东购物卡|JD\s*(?:E[-\s]*)?卡|JD\s*(?:e[-\s]?card|gift\s*card)|"
    r"\bPT\s*(?:站|邀请|邀请码|账号)|Private\s*Tracker|HDarea|M[-\s]?Team|馒头|猫站|聆音|"
    r"租房|合租|整租|转租|求租|找室友|室友|房源|公寓|卖房|买房|房产|租售|"
    r"出租\s*(?:房|车|车位)|车位\s*(?:出租|出售|转让)|卖车|出车|二手车|租车|汽车|电车|新能源车|油车|房车)",
    re.I,
)
FINANCIAL_ACCOUNT_OPENING_RE = re.compile(
    r"((?:银行|港卡|证券|券商|股票|基金(?!会)|美股|港股|A\s*股).{0,36}(?:开户|开通|邀请|邀请码|返现|返利|入金|兑换券|股票券|开户链接)|"
    r"(?:英伟达|NVIDIA).{0,24}(?:股票|美股|入金|开户|股票券|股票兑换券|兑换券)|"
    r"(?:开户|开通|入金).{0,36}(?:银行|港卡|证券|券商|股票|基金|美股|港股)|"
    r"(?:开户奖励|开户礼|新户礼|首存|首入金|入金任务|邀请返现|邀请奖励|股票兑换券|转仓奖励|证券账户|银行账户)|"
    r"(?:众安银行|ZA\s*Bank|富途|老虎证券|长桥|盈透|IBKR|微牛|Webull).{0,40}(?:开户|邀请|入金|返现|兑换券|股票券))",
    re.I,
)
TELECOM_CARD_RE = re.compile(
    r"(电话卡|手机卡|流量卡|号码卡|上网卡|物联卡|保号卡|接码卡|接码|实体卡|保号套餐|PayGo|PAYG|"
    r"(?:实体\s*)?(?<![A-Za-z])(?:SIM|e[-\s]?SIM)(?![A-Za-z])\s*(?:卡|套餐|保号|激活|未激活)|空卡|"
    r"(?:预付费|海外|实体|空|保号|接码|号码|手机号|电话卡|手机卡|流量卡|上网卡|物联卡).{0,24}(?<![A-Za-z])(?:SIM|e[-\s]?SIM)(?![A-Za-z])|"
    r"(?<![A-Za-z])(?:SIM|e[-\s]?SIM)(?![A-Za-z]).{0,24}(?:套餐|保号|激活|未激活|预付费|接码|收短信|接短信|号码|手机号|实体|空卡)|"
    r"(?:日本手机号|日本号码)|"
    r"(?:T[-\s]?Mobile|VOXI|Vodafone|Google\s*Fi|Ultra\s*Mobile|Mint\s*Mobile|giff\s*gaff|GiffGaff|日本乐天|乐天移动|Rakuten\s*Mobile|楽天モバイル).{0,36}(?:卡|SIM|e[-\s]?SIM|套餐|预付费|激活|保号|接码|收短信|接短信|手机号|号码|空卡)|"
    r"(?<![A-Za-z0-9])gg(?![A-Za-z0-9])\s*(?:卡|SIM|e[-\s]?SIM|保号卡|接码卡|空卡)|"
    r"(?:SIM|e[-\s]?SIM|电话卡|手机卡|手机号|号码).{0,24}(?:可|能)?(?:收|接)短信|"
    r"(?:可|能)?(?:收|接)短信.{0,24}(?:SIM|e[-\s]?SIM|电话卡|手机卡|手机号|号码)|"
    r"(?:套餐|购买|自己).{0,16}(?:激活|保号).{0,16}(?:SIM|电话卡|手机卡|实体卡|空卡))",
    re.I,
)
GAMING_DEVICE_RE = re.compile(
    r"((?:PS5|PS4|PlayStation|Xbox|Switch|Nintendo|任天堂|NS2?).{0,36}(?:手柄|控制器|无线控制器|主机|卡带|光盘|配件|周边|底座|充电座)|"
    r"(?:手柄|控制器|无线控制器|Joy[-\s]?Con|JoyCon|DualSense|Pro\s*Controller|卡带|游戏卡|游戏光盘|底座|充电座).{0,24}(?:PS5|PS4|PlayStation|Xbox|Switch|Nintendo|任天堂|NS2?)|"
    r"(?:出|卖|转让|出售|低价出|试出)\s*游戏(?:主机|机|手柄|周边|配件|卡带|卡|光盘)|"
    r"游戏(?:手柄|周边|配件|卡带|光盘))",
    re.I,
)
DIGITAL_GAME_DOMAIN_RE = re.compile(
    r"(Steam|Epic(?:\s*Games?)?|GOG|Ubisoft|Uplay|Battle\.?net|Blizzard|EA\s*(?:App|Play)|Origin|"
    r"PlayStation\s*Store|PSN|Nintendo\s*eShop|Xbox\s*(?:Store|Game\s*Pass)|Game\s*Pass|"
    r"ChinaPlay|游戏平台|游戏商店|游戏商城|游戏已发售|游戏(?:上架|发售|首发)|"
    r"游戏(?:激活码|兑换码|优惠码|折扣码|卡密|CDK|key|Key|数字版|DLC|季票|账号|点卡|点券|充值|皮肤|道具)|"
    r"(?:激活码|兑换码|优惠码|折扣码|卡密|CDK|key|Key|数字版|DLC|季票|点卡|点券).{0,24}游戏)",
    re.I,
)
DIGITAL_GAME_COMMERCE_RE = re.compile(
    r"(激活码|兑换码|优惠码|折扣码|卡密|CDK|key|Key|数字版|DLC|季票|游戏账号|点卡|点券|充值|皮肤|道具|"
    r"发售|上架|首发|购买|买|出|出售|转让|低价|折扣|比\s*Steam\s*低|只要|价格|价|[¥￥$]\s*[0-9]|[0-9]+(?:\.[0-9]+)?\s*(?:元|块|折))",
    re.I,
)
PHOTOGRAPHY_GEAR_DOMAIN_RE = re.compile(
    r"(摄影器材|摄像设备|相机|微单|单反|单电|镜头|机身|快门|卡口|摄影机|摄像机|运动相机|云台|稳定器|"
    r"(?<![A-Za-z])(?:Sony|Canon|Nikon|Fujifilm|Fuji|Leica|Lumix|Olympus|Sigma|Tamron|GoPro|Insta360|DJI)(?![A-Za-z]).{0,32}"
    r"(?:相机|微单|单反|镜头|机身|卡口|运动相机|云台|Pocket|Osmo)|"
    r"(?:索尼|佳能|尼康|富士|徕卡|松下|奥林巴斯|适马|腾龙|大疆).{0,32}"
    r"(?:相机|微单|单反|镜头|机身|卡口|运动相机|云台|Pocket|Osmo)|"
    r"\bA7C\s*2\b|\bA7C2\b|\bA7M[0-9]\b|\bA7R[0-9]\b|\bA7S[0-9]\b|\bX100V?I?\b|\bX-T[0-9]\b|\bZf\b|"
    r"\bR[35678]\b.{0,12}(?:机身|镜头|相机)|RF\s*[0-9]{2}|EF\s*[0-9]{2}|FE\s*[0-9]{2}|Z\s*[0-9]{2})",
    re.I,
)
PHOTOGRAPHY_GEAR_COMMERCE_RE = re.compile(
    r"(二手交易|出售|出个|出一|出\s*[A-Za-z0-9\u4e00-\u9fff]|卖|转让|试出|低价出|明盘|闲鱼|走鱼|可刀|小刀|包邮|自提|"
    r"价格|售价|[¥￥$]\s*[0-9]|[0-9]+(?:\.[0-9]+)?\s*(?:元|块|刀|w|W))",
    re.I,
)
SECONDARY_MARKET_WARNING_RE = re.compile(
    r"(^|[【\[\s，,。；;:：])(?:避坑|投诉|维权|挂人|黑名单|交易纠纷|不发货|拒不发货|骗子|诈骗|被骗|拉黑)|"
    r"(^|[【\[\s，,。；;:：])曝光.{0,16}(?:卖家|商家|店铺|交易|闲鱼|咸鱼|骗子|跑路|避坑|维权|不发货|AI\s*拼车|拼车)|"
    r"(?:AI\s*拼车|拼车).{0,16}(?:跑路|避坑|投诉|维权)|"
    r"(?:卖家|商家|店铺|对方).{0,8}跑路|跑路(?!价)",
    re.I,
)
MEMBERSHIP_ENTITLEMENT_RE = re.compile(
    r"((?:88\s*VIP|88VIP|淘宝\s*88|阿里\s*88|阿里巴巴\s*88).{0,50}(?:权益|会员|转让|出|卖|刚开|拆分|分权益|权益包)|"
    r"(?:出|卖|转让).{0,16}(?:88\s*VIP|88VIP|淘宝\s*88|阿里\s*88)|"
    r"(?:网易云|优酷|芒果|高德|飞猪|希尔顿|万豪|饿了么|盒马|夸克|天猫超市).{0,80}(?:88\s*VIP|88VIP|淘宝\s*88|阿里\s*88)|"
    r"(?:88\s*VIP|88VIP).{0,100}(?:网易云|优酷|芒果|高德|飞猪|希尔顿|万豪|饿了么|盒马|夸克|天猫超市))",
    re.I,
)
SAMS_CLUB_CARD_RE = re.compile(
    r"(山姆\s*(?:会员)?卡|"
    r"(?:山姆(?:会员店|超市|店)?|(?<![A-Za-z])Sam(?:['’]?s)?\s*Club(?![A-Za-z])).{0,24}"
    r"(?:会员卡|副卡|亲友卡|家庭卡|购物卡|礼品卡|储值卡|预付卡|实体卡|电子卡|卡券|开卡|办卡|续卡|卡密|权益|名额|"
    r"membership|member(?:ship)?\s*card|gift\s*card|add[-\s]?on\s*card|household\s*card)|"
    r"(?:会员卡|副卡|亲友卡|家庭卡|购物卡|礼品卡|储值卡|预付卡|实体卡|电子卡|卡券|权益|名额).{0,24}"
    r"(?:山姆(?:会员店|超市|店)?|(?<![A-Za-z])Sam(?:['’]?s)?\s*Club(?![A-Za-z])))",
    re.I,
)
AUTOMOTIVE_SERVICE_RE = re.compile(
    r"(自助\s*洗车\s*店?|洗车\s*(?:店|门店|服务|活动|体验)|车服\s*(?:门店|服务|推广|活动)|"
    r"汽车美容|汽车保养|保养门店|贴膜|补胎|本地车店|车店(?:开业|推广|引流)|"
    r"(?:充电桩|停车场).{0,24}(?:活动|推广|优惠|领券|扫码|到店|门店|引流)|"
    r"(?:本地|附近).{0,12}(?:车服|洗车|汽车美容|补胎|贴膜|保养|充电桩|停车场))",
    re.I,
)
# User feedback: do not alert on liquor/baijiu/alcohol marketplace items.
ALCOHOL_LIQUOR_RE = re.compile(
    r"(白酒|酒类|酒水|品鉴酒|茅台|五粮液|汾酒|泸州老窖|洋河|郎酒|剑南春|水井坊|古井贡|习酒|国窖|"
    r"(?:[0-9]{2}\s*度.{0,16}(?:酒|茅台|五粮液|汾酒|泸州老窖|洋河|郎酒|剑南春))|"
    r"(?:茅台|五粮液|汾酒|泸州老窖|洋河|郎酒|剑南春).{0,24}(?:旅游资格|品鉴|转让|出售|出|卖))",
    re.I,
)
# Generic software license / authorization transfers are low-value for this user
# (e.g. Surge-like proxy-app licenses or SaaS product licenses). Keep this
# separate from OpenAI/ChatGPT/Codex activation-code offers, which use 激活码/兑换码.
SOFTWARE_LICENSE_TRANSFER_RE = re.compile(
    r"(Surge|Quantumult|\bQX\b|Shadowrocket|Loon|Stash|Clash|"
    r"软件\s*(?:授权|许可|license)|(?:授权|许可|license|licence)\s*(?:转让|转售|出售|出)|"
    r"(?:转|出|卖).{0,12}(?:授权|许可|license|licence)|SaaS\s*版\s*授权|saas\s*版\s*授权|"
    r"版\s*授权|授权码|注册码|许可证|永久授权|终身授权|软件\s*正版)",
    re.I,
)
TOOL_SHARE_TAGS = {"分享创造", "分享发现"}
# Low-urgency tool launches remain silent regardless of the forum node.
TOOL_SHARE_CONTEXT_RE = re.compile(
    r"(分享(?:一个|一下|下|款)?|做了(?:个|一个)|写了(?:个|一个)|自己做(?:了)?(?:个|一个)?|"
    r"(?:发布|上线|上架|内测|公测).{0,24}(?:App|APP|应用|工具|小工具|软件|插件|扩展|网站|服务)|"
    r"(?:App|APP|应用|工具|小工具|软件|插件|扩展|网站|服务).{0,24}(?:发布|上线|上架|内测|公测)|"
    r"(?:限时免费|免费试用|免费).{0,36}(?:App|APP|应用|工具|小工具|软件|插件|扩展|网站|服务)|"
    r"(?:App|APP|应用|工具|小工具|软件|插件|扩展|网站|服务).{0,36}(?:限时免费|免费试用|免费)|"
    r"Mac\s*App\s*Store|App\s*Store|Chrome\s*Web\s*Store)",
    re.I,
)
TOOL_SHARE_SIGNAL_RE = re.compile(
    r"(做了个|做了一个|写了个|写了一个|自己做(?:了)?(?:个|一个)?|发布|上线|上架|开源|源码|github\.com|gitlab\.com|repo|repository|"
    r"自托管|本地部署|一键部署|Docker|demo|演示|下载|Chrome Web Store|App Store|Mac\s*App|插件|扩展|"
    r"工具|小工具|状态栏|菜单栏|翻译\s*(?:App|APP|应用|工具)?|OCR|剪贴板|工作台|工作流|workflow|MCP|多模型|上下文|记忆|API|SDK|CLI|飞书|Notion|自动化|效率)",
    re.I,
)
TOOL_DIGEST_SKIP_PROMO_RE = re.compile(
    r"(倍率|兑换码|邀请码|激活码|卡密|注册送|白嫖|福利|额度|[0-9]+\s*(元|块|刀|美元|rmb|RMB|USD|usd)|[$¥]\s*[0-9])",
    re.I,
)
ACTIONABLE_OFFER_FOR_QUESTION_RE = re.compile(
    r"(二手交易|出售|出个|出一|转让|明盘|挂出来卖|闲鱼|咸鱼|走鱼|包邮|"
    r"注册(?:即送|即领|即可领取|送)|注册送|兑换码|优惠码|折扣码|福利码|卡密|"
    r"(?:回帖|评论|留邮箱|V\s*(?:友|站用户)).{0,32}(?:送|赠送|领|领取|福利)|"
    r"官网|Base\s*URL|注册链接|下载|TestFlight|github\.com|gitlab\.com|demo|演示|开源)",
    re.I,
)
AI_SUPPORT_QUESTION_CONTEXT_RE = re.compile(
    r"(OpenAI|ChatGPT|Claude|Codex|GPT|Plus|Pro\s*20|Codex\s*Go|memorySearch|embedding|CPA|"
    r"额度(?:耗尽|不够|不足|免费额度)|功能不正常|配置|反代|订阅|开通|充值|账号|账户|方案|推荐|哪个|绑定)",
    re.I,
)
APP_POLICY_SUPPORT_CONTEXT_RE = re.compile(
    r"(隐私政策|用户协议|App\s*Store|Google\s*Play|Play\s*Store|审核|等待审核|被拒|拒审|上架|"
    r"国区|付款账号|支付账号|账号绑定|账户绑定|绑定关系|"
    r"(?:Google\s*Play|Play\s*Store|App\s*Store|ChatGPT).{0,24}充值|充值.{0,24}(?:Google\s*Play|Play\s*Store|App\s*Store|ChatGPT))",
    re.I,
)
APP_POLICY_DISCUSSION_MARKER_RE = re.compile(
    r"(问|想问|求助|怎么|如何|为什么|吗\b|？|\?|不让上架|被拒|拒审|等待审核|卡一周|绑定关系|充值关系)",
    re.I,
)
APP_RELEASE_OR_RESOURCE_RE = re.compile(
    r"((?:我|自己).{0,12}(?:做了|写了|开发了|发布|上线|上架|内测|公测).{0,48}"
    r"(?:下载|TestFlight|github\.com|gitlab\.com|官网|demo|演示|开源|体验|工具|App|应用)|"
    r"(?:下载|TestFlight|github\.com|gitlab\.com|Chrome\s*Web\s*Store|官网|demo|演示|开源|源码|注册链接))",
    re.I,
)
CONSUMER_APP_SURFACE_RE = re.compile(r"(?:手机|iOS|Android|App|APP|应用|App\s*Store|Play\s*Store)", re.I)
CONSUMER_APP_PERK_RE = re.compile(
    r"(永久会员|终身会员|lifetime|life\s*time|会员|内购|高级版|Pro\s*版|兑换码|优惠码|送码|邀请码|激活码)",
    re.I,
)
CONSUMER_APP_LOW_VALUE_CONTEXT_RE = re.compile(
    r"(壁纸|锁屏|头像|表情|贴纸|滤镜|美颜|主题|铃声|桌面小组件|贺卡|星座|短剧|娱乐|穿搭)",
    re.I,
)
PRODUCTIVITY_OR_DEVELOPER_TOOL_RE = re.compile(
    r"(API|SDK|CLI|开发者|程序员|调试|自动化|工作流|workflow|MCP|OCR|翻译|转文本|字幕|剪贴板|"
    r"菜单栏|状态栏|文档|文件|笔记|日程|效率|Notion|飞书|浏览器插件|Chrome\s*Web\s*Store|"
    r"GitHub|开源|Docker|自托管|本地部署|监控|日志|数据库|终端|命令行|模型|LLM)",
    re.I,
)
SECONDARY_MARKET_READY_TRADE_RE = re.compile(
    r"(闲鱼|咸鱼|走鱼|鱼链接|m\.tb\.cn|2\.taobao\.com|包邮|邮寄|快递|走平台|平台交易|"
    r"联系方式|微信|VX|WeChat|绿泡泡|私信|加我|实拍|图见|照片|链接)",
    re.I,
)
AI_RELAY_REVIEW_ACCESS_RE = re.compile(
    r"(https?://[^\s，。；;]+|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:/[^\s，。；;]*)?|官网|站点|域名)",
    re.I,
)
AI_RELAY_REVIEW_MODEL_RE = re.compile(
    r"(GPT[-\s]?[0-9A-Za-z.]+|Claude|Gemini|OpenAI|ChatGPT|Codex|LLM|大模型|模型覆盖|模型列表|"
    r"plus\s*池|pro\s*池|号池|全家桶)",
    re.I,
)
AI_RELAY_REVIEW_PRICING_RE = re.compile(
    r"(倍率|[0-9]+(?:\.[0-9]+)?\s*[xX]|套餐|价格|充值|余额|按需充值|小额使用|"
    r"[¥￥$]\s*[0-9]|[0-9]+(?:\.[0-9]+)?\s*(?:元|块|刀|美元|USD|usd))",
    re.I,
)
AI_RELAY_REVIEW_INTEGRATION_RE = re.compile(r"(Base\s*URL|/v1|兼容接口|API\s*接入|转发|中转|relay)", re.I)
AI_RELAY_PUBLIC_MODEL_RE = re.compile(
    r"(OpenAI|ChatGPT|Claude(?:\s*Code)?|Gemini|DeepSeek|Codex|GLM|GPT[-\s]?[0-9A-Za-z.]*|LLM|大模型)",
    re.I,
)
AI_RELAY_PUBLIC_SERVICE_RE = re.compile(
    r"(API\s*(?:中转|网关|接入|转发)|中转站|Base\s*URL|/v1|兼容接口|统一接口|一个\s*key|"
    r"一键接入|直连|国内直连|不用翻墙|无需翻墙|免翻墙|满血|模型(?:分组|覆盖|列表)|"
    r"分组|号池|全部上线|上线|欢迎.{0,10}(?:尝试|试用)|relay)",
    re.I,
)
AI_RELAY_PUBLIC_FACT_RE = re.compile(
    r"(Base\s*URL|/v1|兼容接口|API\s*接入|统一接口|一个\s*key|一键接入|直连|国内直连|"
    r"不用翻墙|无需翻墙|免翻墙|学校企业可开票|可开票|倍率|分组|plus|pro|满血|模型|号池|上线|尝试|试用)",
    re.I,
)
NEGATED_AI_RELAY_VALUE_RE = re.compile(
    r"(?:没有|无|暂无|不含|不支持|不提供|不需要|不能|无法|非)[^。；;\n]{0,20}"
    r"(?:API|Base\s*URL|/v1|中转|relay|接口|接入|模型|号池|额度)",
    re.I,
)
# User feedback: Cursor-related posts are not useful in either instant alerts or
# the daily tool/share digest. Keep this as a global deterministic exclusion so
# current and future Cursor promotions/licenses/workflows/rules/plugins stay out.
CURSOR_RELATED_RE = re.compile(r"(?<![A-Za-z])cursor(?![A-Za-z])|Cursor|Ｃｕｒｓｏｒ", re.I)


def is_cursor_related_post(post: Post) -> bool:
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    return bool(CURSOR_RELATED_RE.search(blob))


def _post_blob(post: Post) -> str:
    return " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])


def _post_title_blob(post: Post) -> str:
    return " ".join([post.title, post.core_title])


def _has_negated_ai_relay_context(blob: str, start: int, end: int) -> bool:
    context = blob[max(0, start - 24) : min(len(blob), end + 18)]
    return bool(NEGATED_AI_RELAY_VALUE_RE.search(context))


def _has_unnegated_ai_relay_match(pattern: re.Pattern[str], blob: str) -> bool:
    for match in pattern.finditer(blob):
        if not _has_negated_ai_relay_context(blob, match.start(), match.end()):
            return True
    return False


def _ai_relay_model_families(blob: str) -> set[str]:
    families: set[str] = set()
    for match in AI_RELAY_PUBLIC_MODEL_RE.finditer(blob):
        token = match.group(0).lower().replace(" ", "").replace("-", "")
        if token.startswith(("openai", "chatgpt", "gpt")):
            families.add("openai")
        elif token.startswith("claude"):
            families.add("claude")
        elif token.startswith("gemini"):
            families.add("gemini")
        elif token.startswith("deepseek"):
            families.add("deepseek")
        elif token.startswith("codex"):
            families.add("codex")
        elif token.startswith("glm"):
            families.add("glm")
        elif token in {"llm", "大模型"}:
            families.add(token)
    return families


def _is_network_proxy_core_service_blob(blob: str) -> bool:
    return bool(NETWORK_PROXY_CORE_SERVICE_RE.search(blob))


def _has_ai_relay_public_subject(blob: str) -> bool:
    if _has_unnegated_ai_relay_match(AI_API_RELAY_SITE_RE, blob):
        return True
    if _has_unnegated_ai_relay_match(OPENAI_USAGE_POOL_CONTEXT_RE, blob):
        return True
    return len(_ai_relay_model_families(blob)) >= 2 and _has_unnegated_ai_relay_match(AI_RELAY_PUBLIC_SERVICE_RE, blob)


OFFER_METHOD_OR_DISCUSSION_RE = re.compile(
    r"(方法|渠道|教程|攻略|姿势|套路|经验|汇总|整理|分享|讨论|求助|咨询|请教|"
    r"怎么|如何|为什么|有没有|有无|哪[个款里种]|吗\b|？|\?)",
    re.I,
)
WEAK_OFFER_TERM_RE = re.compile(r"(白嫖|免费|福利|薅羊毛|羊毛)", re.I)
DIRECT_BENEFIT_EVIDENCE_RE = re.compile(
    r"(免费(?:额度|余额|试用|体验|领取|领|赠送|获得)|注册(?:即领|即可|送)|注册送|"
    r"兑换码|优惠码|折扣码|福利码|卡密|首充包|平台余额|V\s*友福利|"
    r"V\s*(?:友|站用户).{0,80}(?:免费|领取|赠送|额度|余额|福利|credit)|"
    r"(?:(?:注册|新用户|回帖|评论|留邮箱|V\s*(?:友|站用户)).{0,24}(?:即送|再送|送|赠送|领取)|"
    r"(?:即送|再送|赠送|免费领取|福利)).{0,32}(?:[$]\s*[0-9]+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金|credit|credits))|"
    r"(?:免费|赠送|领取|福利).{0,50}[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金)|"
    r"[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金).{0,40}(?:免费|额度|余额|赠送|领取|福利|credit)|"
    r"教育优惠|优惠券|[0-9]+(?:\.[0-9]+)?\s*券|今天截止|今晚截止|限时截止|截止|"
    r"0\.[0-9]+\s*(?:元|块)\s*/?\s*(?:刀|美元|USD))",
    re.I,
)
AI_RELAY_REGISTRATION_BENEFIT_RE = re.compile(
    r"(?:(?:注册|新用户|V\s*(?:友|站用户)|回帖|评论|留邮箱)[^。；;\n]{0,24}"
    r"(?:即领|即可|即送|就送|送|赠送|领取|可领|免费领取|免费领|免费送|分|给|获得)|"
    r"(?:即领|即送|就送|赠送|免费领取|免费领|免费送)[^。；;\n]{0,12})"
    r"[^。；;\n]{0,40}"
    r"(?:[0-9]+(?:\.[0-9]+)?\s*(?:余额|日卡|天|token|tokens?|次|额度|credit|credits|会员权益|体验)|"
    r"[一二两三四五六七八九十百千万亿]+\s*(?:到|至|-|~)?\s*[一二两三四五六七八九十百千万亿]*\s*(?:亿|万)?\s*(?:token|tokens?)|"
    r"余额日卡|日卡体验|体验额度|平台余额|免费额度|免费余额|会员权益)",
    re.I,
)
AI_RELAY_TOKEN_QUOTA_RE = re.compile(
    r"(?:(?:注册|新用户|免费|体验|赠送|领取|送|福利|分组|日卡|余额)[^。；;\n]{0,48}"
    r"(?:[0-9]+(?:\s*(?:到|至|-|~)\s*[0-9]+)?\s*(?:亿|万)?\s*(?:token|tokens?)|"
    r"[一二两三四五六七八九十百千万亿]+(?:\s*(?:到|至|-|~)\s*[一二两三四五六七八九十百千万亿]+)?\s*(?:亿|万)?\s*(?:token|tokens?))|"
    r"(?:[0-9]+(?:\s*(?:到|至|-|~)\s*[0-9]+)?\s*(?:亿|万)?\s*(?:token|tokens?)|"
    r"[一二两三四五六七八九十百千万亿]+(?:\s*(?:到|至|-|~)\s*[一二两三四五六七八九十百千万亿]+)?\s*(?:亿|万)?\s*(?:token|tokens?))"
    r"[^。；;\n]{0,48}(?:注册|新用户|免费|体验|赠送|领取|送|福利|分组|日卡|余额))",
    re.I,
)
AI_RELAY_ACTION_BENEFIT_OBJECT_RE = re.compile(
    r"((?:注册|新用户|回帖|回复|评论|留邮箱|进群|加群|入群|添加(?:官方)?(?:客服|群)?|官网|站点|地址)"
    r"[^。；;\n]{0,28}"
    r"(?:即领|即可|即送|就送|马上送|送|赠送|领取|可领|免费领取|免费领|免费送|试用|体验)"
    r"[^。；;\n]{0,36}"
    r"(?:余额|额度|平台余额|免费额度|免费余额|试用额度|体验额度|日卡|token|tokens?|credit|credits|会员权益|"
    r"[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金|credit|credits|余额|额度|张|次)))",
    re.I,
)
AI_RELAY_BENEFIT_ACTION_OBJECT_RE = re.compile(
    r"((?:余额|额度|平台余额|免费额度|免费余额|试用额度|体验额度|日卡|token|tokens?|credit|credits|会员权益)"
    r"[^。；;\n]{0,24}"
    r"(?:注册|新用户|回帖|回复|评论|留邮箱|进群|加群|领取|可领|试用|体验))",
    re.I,
)
LOTTERY_CONTEXT_RE = re.compile(r"(抽奖|开奖|抽\s*[0-9一二两三四五六七八九十百千万]+|中[奖签]|中奖|奖品|送出|赠送|抽取|lottery|raffle)", re.I)
LOTTERY_HIGH_VALUE_PRIZE_RE = re.compile(
    r"(GPT\s*PLUS|GPTPLUS|ChatGPT\s*Plus|GPT\s*Plus|ChatGPT\s*Pro|GPT\s*Pro|Claude\s*Pro|"
    r"Codex|OpenAI|API\s*(?:额度|账号|Key|密钥)|[0-9]+(?:\.[0-9]+)?\s*(?:刀|美元|USD|usd|美金|credit|credits|额度|平台余额)|"
    r"(?:iPhone|iPad|MacBook|Mac\s*mini|Apple\s*Watch|硬盘|SSD|NAS|显示器|路由器|CPE|键盘|鼠标))",
    re.I,
)
LOTTERY_SLOT_RE = re.compile(r"((?:抽|送|赠送|送出)?\s*[0-9一二两三四五六七八九十百千万]+\s*(?:个|名|份|位|台|张|套))", re.I)
LOTTERY_DRAW_RE = re.compile(r"((?:开奖时间|开奖|抽奖时间|截止|抽取时间)[^。；;\n]{0,48})", re.I)
LOTTERY_ENTRY_RE = re.compile(
    r"((?:在)?(?:本帖|评论区|回复|回帖|留言|留邮箱|注册|添加|进群|加入|关注)[^。；;\n]{0,40}(?:参与|抽奖|即可|可参与|报名|留下|回复|回帖)|"
    r"(?:回复|回帖|留言|留邮箱|评论)[^。；;\n]{0,24})",
    re.I,
)
LOTTERY_TOOL_RE = re.compile(r"((?:抽奖工具|开源项目|v2ex-lottery|lottery)[^。；;\n]{0,64})", re.I)
LOTTERY_RISK_RE = re.compile(r"(成品号|共享|渠道|代开|代充|号池|稳定渠道|封号|来源)", re.I)


def _unique_regex_matches(pattern: re.Pattern[str], blob: str, *, limit: int = 6) -> list[str]:
    values: list[str] = []
    for match in pattern.finditer(blob):
        text = strip_invisible_prompt_chars(match.group(0)).strip()
        if text and text not in values:
            values.append(text)
            if len(values) >= limit:
                break
    return values


def _has_negated_benefit_context(blob: str, start: int, end: int) -> bool:
    context = blob[max(0, start - 32) : min(len(blob), end + 16)]
    return bool(NEGATED_BENEFIT_CONTEXT_RE.search(context))


def _has_negated_lottery_context(blob: str, start: int, end: int) -> bool:
    context = blob[max(0, start - 24) : min(len(blob), end + 16)]
    return bool(re.search(r"(没有|无|暂无|未|不含|没)(?:明确|具体)?[^。；;\n]{0,18}(奖品|名额|开奖|参与|抽奖)", context, re.I))


def _direct_benefit_evidence(blob: str) -> list[str]:
    values: list[str] = []
    for match in DIRECT_BENEFIT_EVIDENCE_RE.finditer(blob):
        if _has_negated_benefit_context(blob, match.start(), match.end()):
            continue
        text = strip_invisible_prompt_chars(match.group(0)).strip()
        if text and text not in values:
            values.append(text)
            if len(values) >= 6:
                break
    return values


def _ai_relay_direct_benefit_evidence(blob: str) -> list[str]:
    if not (AI_API_RELAY_SITE_RE.search(blob) or OPENAI_USAGE_POOL_CONTEXT_RE.search(blob)):
        return []
    values: list[str] = []
    for pattern in [
        AI_RELAY_REGISTRATION_BENEFIT_RE,
        AI_RELAY_TOKEN_QUOTA_RE,
        AI_RELAY_ACTION_BENEFIT_OBJECT_RE,
        AI_RELAY_BENEFIT_ACTION_OBJECT_RE,
    ]:
        for match in pattern.finditer(blob):
            if _has_negated_benefit_context(blob, match.start(), match.end()):
                continue
            text = strip_invisible_prompt_chars(" ".join(match.group(0).split())).strip(" ，。；;:：")
            if text and text not in values:
                values.append(text)
                if len(values) >= 6:
                    return values
    return values


def build_lottery_facts(blob: str) -> LotteryFacts:
    is_lottery = bool(LOTTERY_CONTEXT_RE.search(blob))
    prize_evidence = []
    for match in LOTTERY_HIGH_VALUE_PRIZE_RE.finditer(blob):
        if not _has_negated_lottery_context(blob, match.start(), match.end()):
            prize_evidence.append(match.group(0))
    slot_evidence = []
    for match in LOTTERY_SLOT_RE.finditer(blob):
        if not _has_negated_lottery_context(blob, match.start(), match.end()):
            slot_evidence.append(match.group(0))
    draw_evidence = []
    for match in LOTTERY_DRAW_RE.finditer(blob):
        if not _has_negated_lottery_context(blob, match.start(), match.end()):
            draw_evidence.append(match.group(0))
    entry_evidence = []
    for match in LOTTERY_ENTRY_RE.finditer(blob):
        if not _has_negated_lottery_context(blob, match.start(), match.end()):
            entry_evidence.append(match.group(0))
    tool_evidence = _unique_regex_matches(LOTTERY_TOOL_RE, blob)
    risk_flags = _unique_regex_matches(LOTTERY_RISK_RE, blob)
    prize_evidence = _unique_clean(prize_evidence)[:6]
    slot_evidence = _unique_clean(slot_evidence)[:6]
    draw_evidence = _unique_clean(draw_evidence)[:6]
    entry_evidence = _unique_clean(entry_evidence)[:6]
    is_method_or_question = bool(OFFER_METHOD_OR_DISCUSSION_RE.search(blob) or QUESTION_DISCUSSION_RE.search(blob))
    has_actionable_entry = bool(entry_evidence or draw_evidence) and not bool(is_method_or_question and not draw_evidence)
    is_high_value = bool(is_lottery and prize_evidence and has_actionable_entry)
    return LotteryFacts(
        is_lottery=is_lottery,
        is_high_value=is_high_value,
        has_actionable_entry=has_actionable_entry,
        prize_evidence=prize_evidence,
        slot_evidence=slot_evidence,
        draw_evidence=draw_evidence,
        entry_evidence=entry_evidence,
        tool_evidence=tool_evidence,
        risk_flags=risk_flags,
    )


def build_offer_facts(blob: str) -> OfferFacts:
    weak_terms = _unique_regex_matches(WEAK_OFFER_TERM_RE, blob)
    discussion_markers = _unique_regex_matches(OFFER_METHOD_OR_DISCUSSION_RE, blob)
    benefit_evidence = _direct_benefit_evidence(blob)
    benefit_evidence.extend(_ai_relay_direct_benefit_evidence(blob))
    low_rate_values = [value for value in _ai_relay_rate_values(blob) if value <= 0.1]
    if low_rate_values:
        benefit_evidence.append(f"low_rate<={min(low_rate_values):g}")
    if _has_ai_relay_low_absolute_price(blob) and not any("0." in value and ("元" in value or "块" in value) for value in benefit_evidence):
        benefit_evidence.append("low_absolute_price")

    benefit_evidence = _unique_clean(benefit_evidence)
    has_direct_benefit = bool(benefit_evidence and not NEGATED_OFFER_SIGNAL_RE.search(blob))
    if has_direct_benefit:
        intent = "direct_offer"
    elif weak_terms and discussion_markers:
        intent = "question_or_discussion" if QUESTION_DISCUSSION_RE.search(blob) else "method_or_tutorial"
    elif weak_terms:
        intent = "ambiguous"
    else:
        intent = "ambiguous"
    return OfferFacts(
        intent=intent,
        benefit_evidence=benefit_evidence,
        weak_terms=weak_terms,
        discussion_markers=discussion_markers,
        has_direct_benefit=has_direct_benefit,
    )


def _has_positive_offer_signal(blob: str) -> bool:
    offer_facts = build_offer_facts(blob)
    if not STRONG_OFFER_SIGNAL_RE.search(blob):
        return False
    if offer_facts.intent in {"method_or_tutorial", "question_or_discussion"} and not offer_facts.has_direct_benefit:
        return False
    if not offer_facts.has_direct_benefit:
        return False
    return not bool(NEGATED_OFFER_SIGNAL_RE.search(blob))


def _ai_relay_rate_values(blob: str) -> list[float]:
    values: list[float] = []
    for match in AI_RELAY_RATE_RE.finditer(blob):
        raw_value = next((group for group in match.groups() if group), "")
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if value >= 0:
            values.append(value)
    return values


def _has_ai_relay_concrete_benefit(blob: str) -> bool:
    if NEGATED_OFFER_SIGNAL_RE.search(blob):
        return False
    return build_offer_facts(blob).has_direct_benefit


def _has_ai_relay_low_absolute_price(blob: str) -> bool:
    if NEGATED_OFFER_SIGNAL_RE.search(blob):
        return False
    return bool(AI_RELAY_LOW_ABSOLUTE_PRICE_RE.search(blob))


def _has_ai_relay_low_rate(blob: str) -> bool:
    if NEGATED_OFFER_SIGNAL_RE.search(blob):
        return False
    rates = _ai_relay_rate_values(blob)
    return bool(rates and min(rates) <= 0.1)


def _has_ai_relay_keep_value(blob: str) -> bool:
    return bool(
        _has_ai_relay_concrete_benefit(blob)
        or _has_ai_relay_low_absolute_price(blob)
        or _has_ai_relay_low_rate(blob)
    )


def _is_digital_game_commerce_offer(blob: str) -> bool:
    return bool(DIGITAL_GAME_DOMAIN_RE.search(blob) and DIGITAL_GAME_COMMERCE_RE.search(blob))


def _is_photography_camera_gear_offer(blob: str) -> bool:
    if not PHOTOGRAPHY_GEAR_DOMAIN_RE.search(blob):
        return False
    if not PHOTOGRAPHY_GEAR_COMMERCE_RE.search(blob):
        return False
    # Camera words in software contexts are not marketplace photography gear.
    if re.search(r"(相机|摄像头).{0,16}(?:权限|API|SDK|调用|驱动|WebRTC|会议|监控|识别|算法)", blob, re.I):
        return False
    return True


def _unique_categories(categories: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for category in categories:
        if category in seen:
            continue
        seen.add(category)
        result.append(category)
    return result


def _has_secondary_market_bare_price(blob: str) -> bool:
    return bool(_secondary_market_prefixed_bare_prices(blob) or _secondary_market_device_bare_prices(blob))


def _is_secondary_market_bare_price_context(blob: str, match: re.Match[str]) -> bool:
    tail = blob[match.end(1) : match.end(1) + 8]
    if re.match(r"\s*(?:新|年|天|次|%|折|G|GB|T|TB|Mhz|MHz|mhz)", tail, re.I):
        return False
    prefix = blob[max(0, match.start() - 20) : match.start()]
    if re.search(r"(自提|面交|面取).{0,8}(?:优惠|减|少|便宜)$", prefix, re.I):
        return False
    if re.search(r"(优惠|减|少|便宜)\s*$", prefix, re.I):
        return False
    return True


def _secondary_market_prefixed_bare_prices(blob: str) -> list[str]:
    values: list[str] = []
    for match in SECONDARY_MARKET_BARE_PRICE_RE.finditer(blob):
        if not _is_secondary_market_bare_price_context(blob, match):
            continue
        values.append(match.group(1))
    return _unique_clean(values)


def _secondary_market_device_bare_prices(blob: str) -> list[str]:
    values: list[str] = []
    for match in SECONDARY_MARKET_DEVICE_BARE_PRICE_RE.finditer(blob):
        if not _is_secondary_market_bare_price_context(blob, match):
            continue
        window = blob[max(0, match.start() - 8) : min(len(blob), match.end(1) + 8)]
        if re.search(r"(自提|面交|面取).{0,12}(?:优惠|减|少|便宜)", window, re.I):
            continue
        if re.search(r"(估价|回收价|爱回收|折抵|抵扣|可以抵|可抵|抵)", match.group(0), re.I):
            continue
        values.append(match.group(1))
    return _unique_clean(values)


def _has_secondary_market_device_bare_price(blob: str) -> bool:
    return bool(_secondary_market_device_bare_prices(blob))


def _secondary_market_money_evidence(blob: str) -> list[str]:
    values: list[str] = []
    for match in SECONDARY_MARKET_MONEY_WITH_UNIT_RE.finditer(blob):
        values.append(match.group(1).replace(" ", ""))
    for match in SECONDARY_MARKET_DISCOUNT_OR_BALANCE_RE.finditer(blob):
        values.append(match.group(1).replace(" ", ""))
    values.extend(_secondary_market_prefixed_bare_prices(blob))
    values.extend(_secondary_market_device_bare_prices(blob))
    for match in SECONDARY_MARKET_APPRAISAL_PRICE_RE.finditer(blob):
        values.append(match.group(1))
    return _unique_clean(values)


def _secondary_market_device_evidence(blob: str) -> list[str]:
    values: list[str] = []
    for match in SECONDARY_MARKET_DEVICE_EVIDENCE_RE.finditer(blob):
        value = " ".join(match.group(1).split())
        if not value:
            continue
        start, end = match.span(1)
        window = blob[max(0, start - 4) : min(len(blob), end + 4)]
        if value in {"手机", "SIM"} and re.search(r"(手机卡|电话卡|手机号|号码|SIM\s*卡|e[-\s]?SIM\s*卡)", window, re.I):
            continue
        values.append(value)
    return _unique_clean(values)


def _secondary_market_capability_terms(blob: str, device_evidence: list[str]) -> list[str]:
    terms: list[str] = []
    if device_evidence and SIM_CAPABILITY_CONTEXT_RE.search(blob):
        if re.search(r"(?<![A-Za-z])e[-\s]?SIM(?![A-Za-z])", blob, re.I):
            terms.append("eSIM")
        if re.search(r"(?<![A-Za-z])SIM(?![A-Za-z])", blob, re.I):
            terms.append("SIM")
    if device_evidence and re.search(r"(Apple\s*Watch|Watch|蜂窝版)", blob, re.I):
        terms.append("蜂窝版")
    return _unique_clean(terms)


def _has_telecom_card_primary(blob: str, *, device_evidence: list[str], capability_terms: list[str]) -> bool:
    if not TELECOM_CARD_RE.search(blob):
        return False
    if device_evidence and capability_terms:
        # iPhone/iPad/Watch listings often describe SIM/eSIM as a device
        # capability. Keep standalone phone-card/number/card-plan listings blocked.
        if not TELECOM_CARD_STRONG_CONTEXT_RE.search(blob):
            return False
    return True


def build_secondary_market_listing_facts(blob: str, *, is_secondary_market: bool, has_buy_request: bool) -> SecondaryMarketListingFacts:
    if not is_secondary_market:
        return SecondaryMarketListingFacts()

    device_evidence = _secondary_market_device_evidence(blob)
    capability_terms = _secondary_market_capability_terms(blob, device_evidence)
    money_evidence = _secondary_market_money_evidence(blob)
    has_listing_frame = bool(SECONDARY_MARKET_SALE_OFFER_RE.search(blob) or SECONDARY_MARKET_LISTING_FRAME_RE.search(blob))
    has_price_check = bool(re.search(r"(想出|先询价|询价|不知什么价合适|估价|回收价|可以抵|可抵|抵扣)", blob, re.I))

    transaction_role = ""
    if has_buy_request:
        transaction_role = "buyer"
    elif device_evidence and (has_listing_frame or money_evidence):
        transaction_role = "seller"

    if any(SECONDARY_MARKET_MONEY_WITH_UNIT_RE.search(value) for value in money_evidence):
        listing_maturity = "firm_price"
    elif has_price_check or SECONDARY_MARKET_APPRAISAL_PRICE_RE.search(blob):
        listing_maturity = "appraisal"
    elif has_listing_frame and device_evidence:
        listing_maturity = "no_price"
    else:
        listing_maturity = ""

    return SecondaryMarketListingFacts(
        transaction_role=transaction_role,
        listing_maturity=listing_maturity,
        device_evidence=device_evidence,
        money_evidence=money_evidence,
        capability_terms=capability_terms,
        has_listing_frame=has_listing_frame,
        has_telecom_card_primary=_has_telecom_card_primary(
            blob,
            device_evidence=device_evidence,
            capability_terms=capability_terms,
        ),
    )


def _price_evidence_kind(blob: str, *, is_secondary_market: bool) -> str:
    if SECONDARY_MARKET_MONEY_WITH_UNIT_RE.search(blob):
        return "explicit_unit_price"
    if is_secondary_market and _secondary_market_prefixed_bare_prices(blob):
        return "prefixed_bare_price"
    if is_secondary_market and _has_secondary_market_device_bare_price(blob):
        return "device_bare_price"
    if is_secondary_market and SECONDARY_MARKET_APPRAISAL_PRICE_RE.search(blob):
        return "appraisal_or_trade_in_price"
    if is_secondary_market and SECONDARY_MARKET_DISCOUNT_OR_BALANCE_RE.search(blob):
        return "discount_or_balance"
    if SECONDARY_MARKET_CONDITION_NUMBER_RE.search(blob):
        return "condition_newness_number"
    return ""


def _is_generic_telecom_compat_device(value: str) -> bool:
    normalized = re.sub(r"\s+", "", str(value or "").strip().lower())
    return normalized in {"手机", "iphone", "ipad", "applewatch", "watch", "samsung", "三星", "galaxy"}


def _has_high_value_hardware_evidence(blob: str, listing: SecondaryMarketListingFacts) -> bool:
    if listing.has_telecom_card_primary:
        return any(not _is_generic_telecom_compat_device(value) for value in listing.device_evidence)
    return bool(HIGH_VALUE_HARDWARE_RE.search(blob) or listing.device_evidence)


def _matched_locality_names(blob: str) -> list[str]:
    names: list[str] = []
    for term in BEIJING_LOCALITY_TERMS + NON_BEIJING_LOCALITY_TERMS:
        if term in blob and term not in names:
            names.append(term)
    return names


def _regex_context_evidence(pattern: re.Pattern[str], blob: str, *, limit: int = 4, window: int = 28) -> list[str]:
    values: list[str] = []
    for match in pattern.finditer(blob):
        snippet = blob[max(0, match.start() - window) : min(len(blob), match.end() + window)]
        text = strip_invisible_prompt_chars(" ".join(snippet.split())).strip(" ，。；;:：")
        if text and text not in values:
            values.append(text)
            if len(values) >= limit:
                break
    return values


def build_locality_facts(blob: str) -> LocalityFacts:
    locality_names = _matched_locality_names(blob)
    has_beijing = any(name in BEIJING_LOCALITY_TERMS for name in locality_names)
    has_non_beijing = any(name in NON_BEIJING_LOCALITY_TERMS for name in locality_names)
    local_only_evidence = _regex_context_evidence(LOCAL_ONLY_STRONG_CONTEXT_RE, blob)
    optional_pickup_evidence = _regex_context_evidence(LOCAL_OPTIONAL_PICKUP_RE, blob)
    remote_trade_evidence = _regex_context_evidence(REMOTE_TRADE_CONTEXT_RE, blob)

    if has_beijing:
        transaction_scope = "beijing_local_ok"
    elif has_non_beijing and local_only_evidence:
        transaction_scope = "non_beijing_local_only"
    elif has_non_beijing and optional_pickup_evidence and remote_trade_evidence:
        transaction_scope = "optional_pickup_or_remote"
    elif has_non_beijing and optional_pickup_evidence:
        transaction_scope = "non_beijing_local_only"
    elif has_non_beijing and LOCAL_ONLY_CONTEXT_RE.search(blob) and not remote_trade_evidence:
        transaction_scope = "non_beijing_local_only"
    elif not has_non_beijing and local_only_evidence:
        transaction_scope = "unknown_region_local_only"
    elif not has_non_beijing and LOCAL_ONLY_CONTEXT_RE.search(blob) and not remote_trade_evidence:
        transaction_scope = "unknown_region_local_only"
    elif optional_pickup_evidence or remote_trade_evidence:
        transaction_scope = "optional_pickup_or_remote"
    else:
        transaction_scope = "not_local"

    return LocalityFacts(
        transaction_scope=transaction_scope,
        locality_names=locality_names,
        local_only_evidence=local_only_evidence,
        optional_pickup_evidence=optional_pickup_evidence,
        remote_trade_evidence=remote_trade_evidence,
    )


def _is_local_service_offer(blob: str) -> bool:
    if not LOCAL_SERVICE_OFFER_RE.search(blob):
        return False
    # Keep online/software service wording out of the local-service bucket.
    if re.search(r"(API|OpenAI|ChatGPT|Codex|Claude|Gemini|LLM|模型|Base\s*URL|服务端|云服务|服务器|SaaS)", blob, re.I):
        return False
    return True


def _is_local_only_offer(blob: str) -> bool:
    return build_locality_facts(blob).transaction_scope in {
        "non_beijing_local_only",
        "unknown_region_local_only",
        "beijing_local_ok",
    }


def build_post_facts(post: Post) -> PostFacts:
    blob = _post_blob(post)
    title_blob = _post_title_blob(post)
    buy_request_blob = " ".join(post.tags + [post.title, post.core_title])
    has_buy_request = bool(SECONDARY_MARKET_BUY_REQUEST_RE.search(buy_request_blob))
    is_secondary_market = post.rough_type == "secondary_market" or any(tag == "二手交易" for tag in post.tags) or has_buy_request
    locality_prefix_sale = bool(
        is_secondary_market
        and re.search(
            rf"(?:{'|'.join(map(re.escape, BEIJING_LOCALITY_TERMS + NON_BEIJING_LOCALITY_TERMS))})\s*(?:出|出售|转让|卖)",
            title_blob,
            re.I,
        )
    )
    secondary_listing = build_secondary_market_listing_facts(
        blob,
        is_secondary_market=is_secondary_market,
        has_buy_request=has_buy_request,
    )
    has_sale_offer = bool(
        SECONDARY_MARKET_SALE_OFFER_RE.search(blob)
        or locality_prefix_sale
        or secondary_listing.transaction_role == "seller"
    )
    price_evidence_kind = _price_evidence_kind(blob, is_secondary_market=is_secondary_market)
    has_price_or_quota = price_evidence_kind in {
        "explicit_unit_price",
        "prefixed_bare_price",
        "device_bare_price",
        "appraisal_or_trade_in_price",
        "discount_or_balance",
    }
    has_high_value_hardware = _has_high_value_hardware_evidence(blob, secondary_listing)
    has_technical_resource = bool(TECH_RESOURCE_RE.search(blob) or TECH_LOW_ECONOMIC_VALUE_RE.search(blob))
    is_digital_game_commerce_offer = _is_digital_game_commerce_offer(blob)
    is_photography_camera_gear_offer = _is_photography_camera_gear_offer(blob)
    has_ai_context = bool(
        OPENAI_SUBSCRIPTION_CONTEXT_RE.search(blob)
        or OPENAI_PRO20_PRODUCT_RE.search(blob)
        or AI_API_RELAY_SITE_RE.search(blob)
        or OPENAI_USAGE_POOL_CONTEXT_RE.search(blob)
    )
    offer_facts = build_offer_facts(blob)
    lottery_facts = build_lottery_facts(blob)
    has_positive_offer_signal = _has_positive_offer_signal(blob)
    has_ai_economic_value = bool(
        has_ai_context
        and (
            has_positive_offer_signal
            or (AI_SUBSCRIPTION_CODE_RESOURCE_RE.search(blob) and not NEGATED_OFFER_SIGNAL_RE.search(blob))
            or (OPENAI_PRO20_PRODUCT_RE.search(blob) and AI_SUBSCRIPTION_CODE_RESOURCE_RE.search(blob) and has_sale_offer)
            or (AI_API_RELAY_SITE_RE.search(blob) and _has_ai_relay_keep_value(blob))
        )
    )
    has_secondary_market_value = bool(is_secondary_market and has_sale_offer and (has_price_or_quota or has_high_value_hardware))
    has_high_value_lottery = bool(lottery_facts.is_high_value and lottery_facts.has_actionable_entry)
    economic_value_types: list[str] = []
    if has_ai_economic_value:
        economic_value_types.append("ai_economic_value")
    if has_secondary_market_value:
        economic_value_types.append("secondary_market_value")
    if has_positive_offer_signal:
        economic_value_types.append("positive_offer_signal")
    if has_high_value_lottery:
        economic_value_types.append("high_value_lottery")
    marketplace_verticals: list[str] = []
    if has_high_value_hardware:
        marketplace_verticals.append("core_electronics")
    if is_photography_camera_gear_offer:
        marketplace_verticals.append("photography_camera_gear")
    if secondary_listing.has_telecom_card_primary:
        marketplace_verticals.append("telecom_card")
    if GAMING_DEVICE_RE.search(blob) or is_digital_game_commerce_offer:
        marketplace_verticals.append("gaming")
    if MEMBERSHIP_ENTITLEMENT_RE.search(blob) or SAMS_CLUB_CARD_RE.search(blob):
        marketplace_verticals.append("membership_entitlement")
    if re.search(r"京东\s*[eEＥｅ]?[-\s]*卡|京东礼品卡|京东购物卡|JD\s*(?:E[-\s]*)?卡|JD\s*(?:e[-\s]?card|gift\s*card)", blob, re.I):
        marketplace_verticals.append("jd_card")
    if re.search(r"\bPT\s*(?:站|邀请|邀请码|账号)|Private\s*Tracker|HDarea|M[-\s]?Team|馒头|猫站|聆音", blob, re.I):
        marketplace_verticals.append("pt_invite")
    negative_marketplace_categories = [
        category
        for category in [
            "photography_camera_gear" if is_photography_camera_gear_offer else "",
            "telecom_card" if secondary_listing.has_telecom_card_primary else "",
            "gaming_device" if GAMING_DEVICE_RE.search(blob) else "",
            "digital_game_commerce" if is_digital_game_commerce_offer else "",
            "membership_entitlement" if MEMBERSHIP_ENTITLEMENT_RE.search(blob) else "",
            "sams_club_card" if SAMS_CLUB_CARD_RE.search(blob) else "",
            "jd_card" if "jd_card" in marketplace_verticals else "",
            "pt_invite" if "pt_invite" in marketplace_verticals else "",
        ]
        if category
    ]
    has_mixed_high_value_hardware = bool(has_high_value_hardware and has_sale_offer and is_secondary_market)
    locality = build_locality_facts(blob)
    has_beijing_locality = locality.transaction_scope == "beijing_local_ok"
    has_non_beijing_locality = locality.transaction_scope == "non_beijing_local_only"
    return PostFacts(
        blob=blob,
        title_blob=title_blob,
        is_secondary_market=is_secondary_market,
        has_sale_offer=has_sale_offer,
        has_buy_request=has_buy_request,
        has_price_or_quota=has_price_or_quota,
        price_evidence_kind=price_evidence_kind,
        has_high_value_hardware=has_high_value_hardware,
        has_technical_resource=has_technical_resource,
        has_positive_offer_signal=has_positive_offer_signal,
        has_ai_context=has_ai_context,
        has_ai_economic_value=has_ai_economic_value,
        has_economic_value=bool(economic_value_types),
        economic_value_types=economic_value_types,
        marketplace_verticals=_unique_categories(marketplace_verticals),
        primary_marketplace_categories=[] if has_mixed_high_value_hardware else _unique_categories(negative_marketplace_categories),
        addon_marketplace_categories=_unique_categories(negative_marketplace_categories) if has_mixed_high_value_hardware else [],
        is_digital_game_commerce_offer=is_digital_game_commerce_offer,
        is_photography_camera_gear_offer=is_photography_camera_gear_offer,
        is_local_service_offer=_is_local_service_offer(blob),
        is_local_only_offer=_is_local_only_offer(blob),
        has_beijing_locality=has_beijing_locality,
        has_non_beijing_locality=has_non_beijing_locality,
        locality_names=locality.locality_names,
        secondary_listing=secondary_listing,
        offer_facts=offer_facts,
        lottery_facts=lottery_facts,
        locality=locality,
    )


def _matched_categories(facts: PostFacts, rules: list[tuple[str, re.Pattern[str]]]) -> list[str]:
    return [name for name, pattern in rules if pattern.search(facts.blob)]


def _openai_subscription_prices(facts: PostFacts) -> tuple[list[float], list[float]]:
    offers = _extract_ai_subscription_offers(facts.blob)
    return (
        [offer["price"] for offer in offers if offer["product"] == "chatgpt_plus"],
        [offer["price"] for offer in offers if offer["product"] == "chatgpt_pro"],
    )


def is_question_or_discussion(post: Post) -> bool:
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    if any(tag in QUESTION_DISCUSSION_TAGS for tag in post.tags):
        return True
    return bool(QUESTION_DISCUSSION_RE.search(blob))


def has_actionable_signal(post: Post) -> bool:
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    return bool(ACTIONABLE_SIGNAL_RE.search(blob))


def has_technical_resource_signal(post: Post) -> bool:
    return build_post_facts(post).has_technical_resource


def has_mixed_non_gaming_secondary_market_value(post: Post) -> bool:
    """Allow mixed lots when a blacklisted game item is only one part of a useful hardware sale."""
    facts = build_post_facts(post)
    if not facts.is_secondary_market or not GAMING_DEVICE_RE.search(facts.blob):
        return False
    return bool(facts.has_sale_offer and facts.has_price_or_quota and facts.has_high_value_hardware)


def has_economic_value_signal(post: Post) -> bool:
    return build_post_facts(post).has_economic_value


def has_strong_value_signal(post: Post) -> bool:
    """Compatibility wrapper: strong value now means economic value only."""
    return has_economic_value_signal(post)


def is_low_economic_value_technical_post(post: Post) -> bool:
    facts = build_post_facts(post)
    if facts.has_ai_economic_value:
        return False
    if any(tag in TOOL_SHARE_TAGS for tag in post.tags):
        return True
    if post.rough_type == "share_creation":
        return True
    return facts.has_technical_resource


def is_soft_excluded_by_value_signal(post: Post) -> bool:
    return has_economic_value_signal(post)


def is_secondary_market_warning_post(post: Post) -> bool:
    if post.rough_type != "secondary_market" and not any(tag == "二手交易" for tag in post.tags):
        return False
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    return bool(SECONDARY_MARKET_WARNING_RE.search(blob))


def is_ai_relay_public_site_candidate(post: Post, facts: PostFacts | None = None) -> bool:
    """Recognize relay resources that still lack a confirmed instant benefit."""
    facts = facts or build_post_facts(post)
    blob = facts.blob
    if facts.offer_facts.intent in {"method_or_tutorial", "question_or_discussion"} and not facts.offer_facts.has_direct_benefit:
        return False
    if not (facts.has_ai_context or len(_ai_relay_model_families(blob)) >= 2):
        return False
    if not _has_unnegated_ai_relay_match(AI_RELAY_REVIEW_ACCESS_RE, blob):
        return False
    if not _has_ai_relay_public_subject(blob):
        return False
    if not (_has_unnegated_ai_relay_match(AI_RELAY_PUBLIC_FACT_RE, blob) or len(_ai_relay_model_families(blob)) >= 2):
        return False
    return not _is_network_proxy_core_service_blob(blob)


def is_image_generation_only_promo(post: Post) -> bool:
    """Drop pure AI image-generation/prompt-gallery site promos per user feedback."""
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    if not IMAGE_GEN_SITE_RE.search(blob):
        return False
    if not IMAGE_GEN_PROMO_RE.search(blob):
        return False
    if is_ai_relay_public_site_candidate(post):
        return False
    # Keep broader developer/tooling resources for the model to judge; the noisy
    # class is closed image-gen sites with prompt galleries/free generation quota.
    return not _has_unnegated_ai_relay_match(IMAGE_GEN_DEV_VALUE_RE, blob)


def is_low_value_consumer_app_perk(post: Post, facts: PostFacts | None = None) -> bool:
    """Drop consumer-content app membership/code giveaways that are not work tools."""
    facts = facts or build_post_facts(post)
    blob = facts.blob
    if facts.has_ai_context or facts.is_secondary_market:
        return False
    if not CONSUMER_APP_SURFACE_RE.search(blob):
        return False
    if not CONSUMER_APP_PERK_RE.search(blob):
        return False
    if PRODUCTIVITY_OR_DEVELOPER_TOOL_RE.search(blob) or TECH_RESOURCE_RE.search(blob):
        return False
    return bool(CONSUMER_APP_LOW_VALUE_CONTEXT_RE.search(blob))


def _has_secondary_market_ready_trade_evidence(blob: str) -> bool:
    for match in SECONDARY_MARKET_READY_TRADE_RE.finditer(blob):
        prefix = blob[max(0, match.start() - 14) : match.start()]
        if re.search(r"(?:还)?(?:没|未|暂无|没有|无|不)\D{0,8}$", prefix, re.I):
            continue
        return True
    return False


def _secondary_market_sale_ready(facts: PostFacts) -> bool:
    if not facts.is_secondary_market or facts.has_buy_request or not facts.has_sale_offer:
        return False
    if not (facts.has_high_value_hardware or facts.has_economic_value):
        return False
    if facts.has_price_or_quota or facts.secondary_listing.money_evidence:
        return True
    if (
        facts.has_high_value_hardware
        and facts.secondary_listing.device_evidence
        and SECONDARY_MARKET_SALE_OFFER_RE.search(facts.title_blob)
    ):
        return True
    return bool(
        facts.has_high_value_hardware
        and facts.secondary_listing.device_evidence
        and _has_secondary_market_ready_trade_evidence(facts.blob)
    )


def _secondary_market_sale_incomplete(facts: PostFacts) -> bool:
    if not facts.is_secondary_market or facts.has_buy_request or not facts.has_sale_offer:
        return False
    if not facts.has_high_value_hardware or not facts.secondary_listing.device_evidence:
        return False
    return not _secondary_market_sale_ready(facts)


def is_ai_relay_review_candidate(post: Post, facts: PostFacts | None = None) -> bool:
    """Identify concrete relay promotions without a qualifying benefit."""
    facts = facts or build_post_facts(post)
    blob = facts.blob
    if facts.offer_facts.intent in {"method_or_tutorial", "question_or_discussion"} and not facts.offer_facts.has_direct_benefit:
        return False
    if not facts.has_ai_context or not AI_API_RELAY_SITE_RE.search(blob):
        if not is_ai_relay_public_site_candidate(post, facts):
            return False
    if _has_ai_relay_keep_value(blob):
        return False
    if is_ai_relay_public_site_candidate(post, facts):
        return True
    if is_image_generation_only_promo(post) or is_network_proxy_service_post(post):
        return False
    if not AI_RELAY_REVIEW_ACCESS_RE.search(blob):
        return False

    evidence_count = sum(
        1
        for matched in [
            AI_RELAY_REVIEW_MODEL_RE.search(blob),
            AI_RELAY_REVIEW_PRICING_RE.search(blob),
            AI_RELAY_REVIEW_INTEGRATION_RE.search(blob),
            PAID_AI_RELAY_PROMO_RE.search(blob),
        ]
        if matched
    )
    return evidence_count >= 3


def is_paid_only_ai_relay_promo(post: Post) -> bool:
    """Drop paid-only AI API relay/subscription promos with no clear user benefit."""
    if post.rough_type != "promotion_or_offer" and not any(tag == "推广" for tag in post.tags):
        return False
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    if not AI_API_RELAY_SITE_RE.search(blob):
        return False
    if not PAID_AI_RELAY_PROMO_RE.search(blob):
        return False
    if is_ai_relay_review_candidate(post):
        return False
    # Keep only if the post advertises a concrete free quota/coupon/V2EX benefit
    # or clearly low pricing. Generic “限时特惠/套餐已开放” or >0.1 rate is not enough.
    return not _has_ai_relay_keep_value(blob)


def _looks_like_openai_contact_number(window: str, start: int, end: int, *, require_price_suffix: bool) -> bool:
    number_text = window[start:end]
    if not require_price_suffix and len(re.sub(r"\D", "", number_text)) >= 5:
        return True
    context = window[max(0, start - 18) : start]
    return bool(OPENAI_CONTACT_NUMBER_CONTEXT_RE.search(context))


def _parse_rmb_price(value: str) -> float | None:
    try:
        return float(str(value or "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _add_openai_subscription_price(prices: list[float], value: str, *, following_text: str, require_price_suffix: bool) -> None:
    price = _parse_rmb_price(value)
    if price is None:
        return
    # Ignore model numbers, durations, discount percentages, token prices, etc.
    if price < 10:
        return
    if not require_price_suffix and OPENAI_NON_PRICE_SUFFIX_RE.match(following_text):
        return
    if not require_price_suffix and OPENAI_NON_PRICE_TIME_SUFFIX_RE.match(following_text):
        return
    if price not in prices:
        prices.append(price)


def _add_openai_subscription_k_price(prices: list[float], value: str) -> None:
    price = _parse_rmb_price(value)
    if price is None:
        return
    normalized = price * 1000
    if normalized not in prices:
        prices.append(normalized)


def _iter_openai_subscription_prices(window: str) -> list[float]:
    prices: list[float] = []
    for pattern in (OPENAI_PRICE_WITH_UNIT_RE, OPENAI_PRICE_WITH_PREFIX_RE):
        for price_match in pattern.finditer(window):
            _add_openai_subscription_price(
                prices,
                price_match.group(1),
                following_text=window[price_match.end(1) : price_match.end(1) + 8],
                require_price_suffix=True,
            )
    for price_match in OPENAI_BARE_PRICE_AFTER_CUE_RE.finditer(window):
        if _looks_like_openai_contact_number(window, price_match.start(1), price_match.end(1), require_price_suffix=False):
            continue
        _add_openai_subscription_price(
            prices,
            price_match.group(1),
            following_text=window[price_match.end(1) : price_match.end(1) + 8],
            require_price_suffix=False,
        )

    direct_match = OPENAI_BARE_PRODUCT_TAIL_PRICE_RE.match(window)
    if direct_match:
        if not _looks_like_openai_contact_number(window, direct_match.start(1), direct_match.end(1), require_price_suffix=False):
            _add_openai_subscription_price(
                prices,
                direct_match.group(1),
                following_text=window[direct_match.end(1) : direct_match.end(1) + 8],
                require_price_suffix=False,
            )
    for price_match in OPENAI_K_PRICE_RE.finditer(window):
        _add_openai_subscription_k_price(prices, price_match.group(1))
    return prices


def _extract_openai_subscription_prices(blob: str, product_re: re.Pattern[str]) -> list[float]:
    prices: list[float] = []
    for product_match in product_re.finditer(blob):
        next_product_match = OPENAI_ANY_SUBSCRIPTION_PRODUCT_RE.search(blob, product_match.end())
        start = product_match.start()
        end = min(len(blob), product_match.end() + 96)
        if next_product_match:
            end = min(end, next_product_match.start())
        window = blob[start:end]
        for price in _iter_openai_subscription_prices(window):
            if price not in prices:
                prices.append(price)
        tail = blob[product_match.end() : min(len(blob), product_match.end() + 32)]
        for price in _iter_openai_subscription_prices(tail):
            if price not in prices:
                prices.append(price)
    return prices


def _extract_ai_subscription_offers(blob: str) -> list[dict]:
    offers: list[dict] = []
    normalized = strip_invisible_prompt_chars(str(blob or ""))
    for product, product_re in AI_SUBSCRIPTION_PRODUCT_PATTERNS:
        for product_match in product_re.finditer(normalized):
            next_product_match = OPENAI_ANY_SUBSCRIPTION_PRODUCT_RE.search(normalized, product_match.end())
            start = product_match.start()
            end = min(len(normalized), product_match.end() + 120)
            if next_product_match:
                end = min(end, next_product_match.start())
            context_start = max(0, start - 48)
            context_end = min(len(normalized), end + 24)
            context = normalized[context_start:context_end]
            window = normalized[start:end]
            prices = _iter_openai_subscription_prices(window)
            tail = normalized[product_match.end() : min(len(normalized), product_match.end() + 32)]
            for price in _iter_openai_subscription_prices(tail):
                if price not in prices:
                    prices.append(price)
            if not prices:
                continue
            transaction_mode = "topup_or_resale" if AI_SUBSCRIPTION_TOPUP_CONTEXT_RE.search(context) else "unspecified"
            for price in prices:
                offer = {
                    "product": product,
                    "transaction_mode": transaction_mode,
                    "price": price,
                    "context": _compact_text(context, 160),
                }
                if offer not in offers:
                    offers.append(offer)
    return offers


def _ai_subscription_topup_offers(blob: str) -> list[dict]:
    return [offer for offer in _extract_ai_subscription_offers(blob) if offer.get("transaction_mode") == "topup_or_resale"]


def _ai_subscription_price_limit(offer: dict) -> float:
    if offer.get("product") == "chatgpt_pro":
        return OPENAI_PRO_SUBSCRIPTION_PRICE_LIMIT_RMB
    return AI_SUBSCRIPTION_PRICE_LIMIT_RMB


def _is_ai_subscription_code_sale_candidate(facts: PostFacts, offers: list[dict] | None = None) -> bool:
    blob = facts.blob
    if not OPENAI_PRO20_PRODUCT_RE.search(blob):
        return False
    if not AI_SUBSCRIPTION_CODE_RESOURCE_RE.search(blob):
        return False
    if not (facts.has_sale_offer or AI_SUBSCRIPTION_TOPUP_CONTEXT_RE.search(blob) or REMOTE_TRADE_CONTEXT_RE.search(blob)):
        return False
    offers = offers if offers is not None else _ai_subscription_topup_offers(blob)
    return any(
        offer.get("product") == "chatgpt_pro" and offer.get("price", 0) <= _ai_subscription_price_limit(offer)
        for offer in offers
    )


def _ai_subscription_code_sale_reason(facts: PostFacts) -> str:
    offers = _ai_subscription_topup_offers(facts.blob)
    if not _is_ai_subscription_code_sale_candidate(facts, offers):
        return ""
    prices = [
        offer["price"]
        for offer in offers
        if offer.get("product") == "chatgpt_pro" and offer.get("price", 0) <= _ai_subscription_price_limit(offer)
    ]
    price = max(prices) if prices else OPENAI_PRO_SUBSCRIPTION_PRICE_LIMIT_RMB
    price_text = f"{price:g}"
    return f"Pro20 优惠码/兑换码转让，价格 {price_text} RMB 在阈值内；注意官方退码、绑卡和交易来源风险"


def is_overpriced_openai_subscription_promo(post: Post) -> bool:
    """Drop third-party AI subscription top-up/resale promos above the user's threshold."""
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    if has_economic_value_signal(post) and (_has_ai_relay_keep_value(blob) or OPENAI_USAGE_POOL_CONTEXT_RE.search(blob)):
        return False
    if not OPENAI_SUBSCRIPTION_CONTEXT_RE.search(blob):
        return False

    return any(offer["price"] > AI_SUBSCRIPTION_PRICE_LIMIT_RMB for offer in _ai_subscription_topup_offers(blob))


def is_network_proxy_service_post(post: Post) -> bool:
    """Drop network proxy/proxy-IP service promos, even when they offer free test traffic/CDKs."""
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    if _is_network_proxy_core_service_blob(blob):
        return True
    if is_ai_relay_public_site_candidate(post):
        return False
    if not NETWORK_PROXY_BRAND_OR_DOMAIN_RE.search(blob):
        return False
    if not NETWORK_PROXY_COMMERCE_RE.search(blob):
        return False
    return bool(NETWORK_PROXY_USE_CASE_RE.search(blob) or re.search(r"(住宅|proxy)", blob, re.I))


def is_cloud_server_or_hosting_post(post: Post) -> bool:
    """Drop cloud-server/VPS/hosting offers per user feedback."""
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    return bool(CLOUD_SERVER_HOSTING_RE.search(blob))


def is_work_related_post(post: Post) -> bool:
    """Drop job/career/recruiting posts per user feedback."""
    if any(tag in WORK_RELATED_TAGS for tag in post.tags):
        return True
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    return bool(WORK_RELATED_RE.search(blob))


def is_low_value_info_post(post: Post) -> bool:
    """Drop informational network/operator posts that should not wake the model."""
    if any(tag in LOW_VALUE_INFO_TAGS for tag in post.tags):
        return True
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    if not LOW_VALUE_NETWORK_RE.search(blob):
        return False
    if not LOW_VALUE_NETWORK_DETAIL_RE.search(blob):
        return False
    return not post.rough_type == "secondary_market"


def is_low_value_device_refresh_discussion(post: Post) -> bool:
    """Drop purchase/upgrade chatter that only contains specs or residual-price talk."""
    if post.rough_type == "secondary_market":
        return False
    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    if SECONDARY_MARKET_INTENT_RE.search(blob):
        return False
    return bool(LOW_VALUE_DEVICE_REFRESH_DISCUSSION_RE.search(blob))


HARD_EXCLUDE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("blacklisted_merchant", BLACKLISTED_MERCHANT_RE),
    ("financial_account_opening", FINANCIAL_ACCOUNT_OPENING_RE),
    ("automotive_service", AUTOMOTIVE_SERVICE_RE),
    ("alcohol_liquor", ALCOHOL_LIQUOR_RE),
    ("software_license_transfer", SOFTWARE_LICENSE_TRANSFER_RE),
    ("network_proxy_service", NETWORK_PROXY_SERVICE_RE),
    ("cloud_server_hosting", CLOUD_SERVER_HOSTING_RE),
]

LOW_VALUE_ADDON_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("telecom_card", TELECOM_CARD_RE),
    ("gaming_device", GAMING_DEVICE_RE),
    ("membership_entitlement", MEMBERSHIP_ENTITLEMENT_RE),
    ("membership_entitlement", re.compile(r"(?:会员权益|权益卡|房券权益|酒店权益|出行权益)", re.I)),
    ("sams_club_card", SAMS_CLUB_CARD_RE),
]


def _matched_hard_exclude_categories(facts: PostFacts) -> list[str]:
    categories = _matched_categories(facts, HARD_EXCLUDE_RULES)
    proxy_probe = Post(
        message_id=0,
        telegram_url="",
        v2ex_url="",
        posted_at="",
        title=facts.title_blob,
        tags=[],
        core_title=facts.title_blob,
        rough_type="",
        body_excerpt=facts.blob,
    )
    if is_network_proxy_service_post(proxy_probe):
        categories.append("network_proxy_service")
    return _unique_categories(categories)


def _excluded_misc_categories(facts: PostFacts) -> list[str]:
    categories: list[str] = []
    if re.search(r"京东\s*[eEＥｅ]?[-\s]*卡|京东礼品卡|京东购物卡|JD\s*(?:E[-\s]*)?卡|JD\s*(?:e[-\s]?card|gift\s*card)", facts.blob, re.I):
        categories.append("jd_card")
    if re.search(r"\bPT\s*(?:站|邀请|邀请码|账号)|Private\s*Tracker|HDarea|M[-\s]?Team|馒头|猫站|聆音", facts.blob, re.I):
        categories.append("pt_invite")
    if re.search(r"租房|合租|整租|转租|求租|找室友|室友|房源|公寓|卖房|买房|房产|租售|出租\s*(?:房|车|车位)|车位\s*(?:出租|出售|转让)|卖车|出车|二手车|租车|汽车|电车|新能源车|油车|房车", facts.blob, re.I):
        categories.append("housing_or_vehicle")
    return categories


def _has_mixed_high_value_hardware_sale(facts: PostFacts) -> bool:
    return bool(facts.is_secondary_market and facts.has_sale_offer and facts.has_high_value_hardware)


def apply_user_preferences(facts: PostFacts) -> PreferenceDecision:
    """Classify user preference effects before generic economic-value routing."""
    hard_categories = _matched_hard_exclude_categories(facts)
    if is_excluded_device_deal(facts.blob):
        hard_categories.append("excluded_device_deal")

    misc_categories = _excluded_misc_categories(facts)
    if "housing_or_vehicle" in misc_categories:
        hard_categories.append("housing_or_vehicle")
        misc_categories = [category for category in misc_categories if category != "housing_or_vehicle"]

    low_value_categories = _matched_categories(facts, LOW_VALUE_ADDON_RULES)
    if facts.is_digital_game_commerce_offer:
        low_value_categories.append("digital_game_commerce")
    if facts.is_photography_camera_gear_offer:
        low_value_categories.append("photography_camera_gear")

    # AI relay/subscription posts can legitimately mention membership wording;
    # keep that older exception but only for the generic membership category.
    if facts.has_ai_context:
        low_value_categories = [category for category in low_value_categories if category != "membership_entitlement"]

    negative_categories = _unique_categories(misc_categories + low_value_categories)
    ignored_addon_categories: list[str] = []
    primary_blocking_categories: list[str] = []
    relaxation_categories: list[str] = []

    if negative_categories:
        if _has_mixed_high_value_hardware_sale(facts):
            ignored_addon_categories = negative_categories
            relaxation_categories.append("mixed_high_value_hardware")
        else:
            primary_blocking_categories = negative_categories

    return PreferenceDecision(
        negative_categories=negative_categories,
        relaxation_categories=_unique_categories(relaxation_categories),
        primary_blocking_categories=primary_blocking_categories,
        ignored_addon_categories=ignored_addon_categories,
        hard_exclude_categories=_unique_categories(hard_categories),
    )


def _preference_route_decision(preferences: PreferenceDecision) -> RouteDecision | None:
    if preferences.hard_exclude_categories:
        return RouteDecision("exclude", "全局硬排除类别", preferences.hard_exclude_categories + preferences.negative_categories)
    if preferences.primary_blocking_categories:
        return RouteDecision("exclude", "用户负向偏好主售品类排除", preferences.primary_blocking_categories)
    if preferences.ignored_addon_categories:
        return RouteDecision(
            "candidate",
            "高价值二手硬件混合帖，用户负向品类作为附带物忽略",
            ["secondary_market_sale"],
            preferences.ignored_addon_categories,
        )
    return None


def _openai_subscription_route_decision(facts: PostFacts) -> RouteDecision | None:
    if not OPENAI_SUBSCRIPTION_CONTEXT_RE.search(facts.blob):
        return None
    topup_offers = _ai_subscription_topup_offers(facts.blob)
    if not topup_offers:
        return None
    if _is_ai_subscription_code_sale_candidate(facts, topup_offers):
        return RouteDecision("candidate", "Pro20 订阅优惠码/兑换码价格在阈值内", ["ai_subscription_code_threshold"])
    if facts.has_ai_economic_value and (_has_ai_relay_keep_value(facts.blob) or OPENAI_USAGE_POOL_CONTEXT_RE.search(facts.blob)):
        return RouteDecision("candidate", "OpenAI/Codex 额度或号池福利", ["ai_economic_value"])

    if any(offer["price"] > _ai_subscription_price_limit(offer) for offer in topup_offers):
        return RouteDecision("exclude", "第三方 AI 订阅代充/兑换价格超过阈值且无明确福利", ["overpriced_ai_subscription_topup"])
    if any(offer["price"] <= _ai_subscription_price_limit(offer) for offer in topup_offers):
        return RouteDecision("candidate", "第三方 AI 订阅代充/兑换价格在阈值内", ["ai_subscription_threshold"])
    return None


def _locality_route_decision(facts: PostFacts) -> RouteDecision | None:
    categories: list[str] = []
    if facts.is_local_service_offer:
        categories.append("local_service_offer")
    if facts.locality.transaction_scope == "non_beijing_local_only" or (
        facts.is_local_service_offer
        and any(name in NON_BEIJING_LOCALITY_TERMS for name in facts.locality.locality_names)
        and not any(name in BEIJING_LOCALITY_TERMS for name in facts.locality.locality_names)
    ):
        categories.append("non_beijing_local")
    if facts.locality.transaction_scope == "unknown_region_local_only":
        categories.append("unknown_region_local_only")

    if not categories:
        return None
    if "local_service_offer" in categories:
        return RouteDecision("exclude", "本地服务/核销类内容排除", categories)
    if "non_beijing_local" in categories:
        return RouteDecision("exclude", "非北京本地限定内容排除", categories)
    if "unknown_region_local_only" in categories:
        return RouteDecision("exclude", "地域不明的本地限定内容排除", categories)
    return None


def _secondary_market_buy_request_route_decision(facts: PostFacts) -> RouteDecision | None:
    if not facts.is_secondary_market or not facts.has_buy_request or facts.has_sale_offer:
        return None

    categories = ["secondary_market_buy_request"]
    preference = apply_user_preferences(facts)
    negative_categories = _unique_categories(
        preference.hard_exclude_categories
        + preference.primary_blocking_categories
        + preference.negative_categories
    )
    if negative_categories:
        return RouteDecision("exclude", "用户负向品类求购排除", _unique_categories(categories + negative_categories))

    if not facts.has_high_value_hardware:
        return RouteDecision("exclude", "非核心数码硬件求购排除", categories)
    if not SECONDARY_MARKET_SPECIFIC_HARDWARE_BUY_RE.search(facts.blob):
        return RouteDecision("exclude", "缺少具体硬件对象的求购排除", categories)
    if not SECONDARY_MARKET_BUY_DETAIL_RE.search(facts.blob):
        return RouteDecision("exclude", "缺少具体条件的求购排除", categories)
    return RouteDecision("exclude", "具体数码硬件求购证据不足，静默", categories)


def _offer_method_or_question_route_decision(facts: PostFacts) -> RouteDecision | None:
    if facts.offer_facts.has_direct_benefit:
        return None
    if facts.offer_facts.intent not in {"method_or_tutorial", "question_or_discussion"}:
        return None
    return RouteDecision("exclude", "福利/额度方法讨论无直接领取证据", ["offer_method_or_question"])


def _has_actionable_offer_for_question(facts: PostFacts) -> bool:
    blob = facts.blob
    if _is_ai_subscription_code_sale_candidate(facts):
        return True
    if AI_API_RELAY_SITE_RE.search(blob) and _has_ai_relay_keep_value(blob) and ACTIONABLE_OFFER_FOR_QUESTION_RE.search(blob):
        return True
    if facts.is_secondary_market and facts.has_sale_offer and (facts.has_price_or_quota or _ai_subscription_topup_offers(blob)):
        return True
    if facts.has_technical_resource and APP_RELEASE_OR_RESOURCE_RE.search(blob):
        return True
    return bool(ACTIONABLE_OFFER_FOR_QUESTION_RE.search(blob) and facts.offer_facts.has_direct_benefit)


def _ai_support_question_route_decision(post: Post, facts: PostFacts) -> RouteDecision | None:
    if (post.rough_type == "promotion_or_offer" or any(tag == "推广" for tag in post.tags)) and not any(
        tag in QUESTION_DISCUSSION_TAGS for tag in post.tags
    ):
        return None
    if (post.rough_type == "promotion_or_offer" or any(tag in {"推广", "二手交易"} for tag in post.tags)) and (
        facts.has_economic_value or _ai_subscription_topup_offers(facts.blob)
    ):
        return None
    if not (is_question_or_discussion(post) or facts.offer_facts.intent == "question_or_discussion"):
        return None
    if not (facts.has_ai_context or AI_SUPPORT_QUESTION_CONTEXT_RE.search(facts.blob)):
        return None
    if _has_actionable_offer_for_question(facts):
        return None
    return RouteDecision("exclude", "AI 求助/订阅咨询无直接 offer", ["ai_support_question_without_offer"])


def _app_policy_support_question_route_decision(post: Post, facts: PostFacts) -> RouteDecision | None:
    if not APP_POLICY_SUPPORT_CONTEXT_RE.search(facts.blob):
        return None
    if (post.rough_type == "promotion_or_offer" or any(tag in {"推广", "二手交易"} for tag in post.tags)) and (
        facts.has_economic_value or _ai_subscription_topup_offers(facts.blob)
    ):
        return None
    if _has_actionable_offer_for_question(facts):
        return None
    looks_like_question_or_policy_discussion = bool(
        is_question_or_discussion(post) or APP_POLICY_DISCUSSION_MARKER_RE.search(facts.blob)
    )
    if not looks_like_question_or_policy_discussion:
        return None
    if APP_RELEASE_OR_RESOURCE_RE.search(facts.blob):
        return None
    return RouteDecision("exclude", "App 审核/政策/账号问答无产品发布资源", ["app_policy_support_question"])


def evaluate_post_for_monitor(post: Post) -> RouteDecision:
    """Single source of truth for V2EX candidate routing."""
    facts = build_post_facts(post)

    if is_cursor_related_post(post):
        return RouteDecision("exclude", "Cursor 相关内容全局排除", ["cursor"])
    if is_work_related_post(post):
        return RouteDecision("exclude", "招聘/职场内容排除", ["work_related"])
    if is_secondary_market_warning_post(post):
        return RouteDecision("exclude", "二手曝光/投诉/避坑内容排除", ["secondary_market_warning"])

    buy_request_decision = _secondary_market_buy_request_route_decision(facts)
    if buy_request_decision is not None:
        return buy_request_decision

    offer_method_decision = _offer_method_or_question_route_decision(facts)
    if offer_method_decision is not None:
        return offer_method_decision

    app_policy_decision = _app_policy_support_question_route_decision(post, facts)
    if app_policy_decision is not None:
        return app_policy_decision

    ai_support_decision = _ai_support_question_route_decision(post, facts)
    if ai_support_decision is not None:
        return ai_support_decision

    if is_low_value_consumer_app_perk(post, facts):
        return RouteDecision("exclude", "普通消费 App 会员/兑换码福利排除", ["consumer_app_perk_low_value"])

    locality_decision = _locality_route_decision(facts)
    if locality_decision is not None:
        return locality_decision

    if is_low_value_info_post(post):
        if facts.has_economic_value:
            return RouteDecision("candidate", "低价值信息帖含明确经济动作信号", ["low_value_info_with_economic_value"])
        return RouteDecision("exclude", "低价值网络/运营商信息排除", ["low_value_info"])
    if is_low_value_device_refresh_discussion(post):
        if facts.has_economic_value:
            return RouteDecision("candidate", "设备讨论帖含明确经济动作信号", ["device_discussion_with_economic_value"])
        return RouteDecision("exclude", "设备购买/升级泛讨论排除", ["device_refresh_discussion"])

    openai_decision = _openai_subscription_route_decision(facts)
    if openai_decision is not None:
        return openai_decision

    preference_decision = _preference_route_decision(apply_user_preferences(facts))
    if preference_decision is not None:
        return preference_decision

    if is_image_generation_only_promo(post):
        return RouteDecision("exclude", "纯 AI 生图站推广排除", ["image_generation_promo"])
    if is_ai_relay_review_candidate(post, facts):
        return RouteDecision("exclude", "AI 中转站疑似候选证据不足，静默", ["ai_relay_review"])
    if is_paid_only_ai_relay_promo(post):
        return RouteDecision("exclude", "无明确福利的 AI 中转推广排除", ["paid_only_ai_relay_promo"])

    if facts.has_ai_economic_value:
        return RouteDecision("candidate", "OpenAI/Codex/API 明确经济价值", ["ai_economic_value"])

    if facts.lottery_facts.is_high_value and facts.lottery_facts.has_actionable_entry:
        return RouteDecision("candidate", "高价值抽奖候选", ["high_value_lottery"])

    if is_low_economic_value_technical_post(post):
        return RouteDecision("exclude", "技术价值但低经济价值", ["low_economic_technical"])

    if facts.is_secondary_market:
        if facts.has_buy_request and not facts.has_sale_offer:
            if facts.has_economic_value:
                return RouteDecision("candidate", "二手求购帖含可交易经济资源", ["secondary_market_buy_with_value"])
            return RouteDecision("exclude", "二手求购证据不足，静默", ["secondary_market_buy_request"])
        if _secondary_market_sale_ready(facts):
            return RouteDecision("candidate", "二手出售候选", ["secondary_market_sale"])
        if _secondary_market_sale_incomplete(facts):
            return RouteDecision("exclude", "二手出售疑似候选证据不足，静默", ["secondary_market_sale_candidate"])

    if _is_low_value_tool_share(post, facts):
        return RouteDecision("exclude", "普通工具/产品发现证据不足，静默", ["tool_share_digest"])

    if is_question_or_discussion(post):
        if facts.has_economic_value:
            return RouteDecision("candidate", "泛讨论含明确经济价值", ["discussion_with_economic_value"])
        return RouteDecision("exclude", "泛讨论无明确经济价值", ["question_or_discussion"])

    if facts.has_economic_value:
        return RouteDecision("candidate", "明确经济价值候选", ["economic_value"])

    return RouteDecision("exclude", "未命中高经济价值条件", ["no_economic_value"])


def is_blacklisted_marketplace_post(post: Post) -> bool:
    """Compatibility wrapper for deterministic low-value marketplace exclusions."""
    decision = evaluate_post_for_monitor(post)
    return decision.route == "exclude" and bool(
        set(decision.matched_categories)
        - {
            "low_value_info",
            "device_refresh_discussion",
            "low_economic_technical",
            "question_or_discussion",
            "no_economic_value",
        }
    )


def _is_low_value_tool_share(post: Post, facts: PostFacts) -> bool:
    """Identify low-urgency tool discoveries without an economic benefit."""
    if facts.has_technical_resource or facts.has_economic_value:
        return False
    if post.rough_type == "secondary_market":
        return False
    if is_image_generation_only_promo(post) or is_paid_only_ai_relay_promo(post):
        return False

    has_share_context = (
        any(tag in TOOL_SHARE_TAGS for tag in post.tags)
        or post.rough_type == "share_creation"
        or bool(TOOL_SHARE_CONTEXT_RE.search(facts.blob))
    )
    has_tool_signal = bool(TOOL_SHARE_SIGNAL_RE.search(facts.blob))
    if not has_share_context or not has_tool_signal:
        return False

    # Explicit coupon, price or relay benefits require the offer filters.
    if post.rough_type == "promotion_or_offer" and TOOL_DIGEST_SKIP_PROMO_RE.search(facts.blob):
        return False
    return True


def route_post_for_monitor(post: Post) -> str:
    """Return the deterministic candidate route."""
    return evaluate_post_for_monitor(post).route


def is_instant_candidate(post: Post) -> bool:
    """Compatibility wrapper: True means this item should reach the model."""
    return route_post_for_monitor(post) == "candidate"


def _markdown_link_title(text: str) -> str:
    return strip_invisible_prompt_chars(str(text or "")).replace("[", "\\[").replace("]", "\\]").strip()


def _compact_text(text: str, limit: int = 96) -> str:
    normalized = strip_invisible_prompt_chars(" ".join(str(text or "").split()))
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip(" ，。；;:：") + "…"


def _unique_clean(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = strip_invisible_prompt_chars(str(value or "")).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _extract_price_or_quota_note(post: Post) -> str:
    blob = " ".join([post.title, post.core_title, post.body_excerpt])
    values = _unique_clean([match.group(1).replace(" ", "") for match in PRICE_OR_QUOTA_RE.finditer(blob)])
    if not values:
        return ""
    return "关键信息：" + "、".join(values[:4])


def _contains_price_value(text: str) -> bool:
    return bool(re.search(r"[¥￥$]|[0-9]+(?:\.[0-9]+)?\s*(?:元|块|刀|美元|USD|usd|折|%)", str(text or ""), re.I))


def instant_alert_reason(post: Post) -> str:
    """Deterministic final filter for the no-agent high-frequency cron path."""
    if not is_instant_candidate(post):
        return ""

    blob = " ".join(post.tags + [post.title, post.core_title, post.body_excerpt])
    price_note = _extract_price_or_quota_note(post)
    facts = build_post_facts(post)

    code_sale_reason = _ai_subscription_code_sale_reason(facts)
    if code_sale_reason:
        return code_sale_reason

    if post.rough_type == "secondary_market" or any(tag == "二手交易" for tag in post.tags):
        title_blob = " ".join([post.title, post.core_title])
        if not SECONDARY_MARKET_SALE_OFFER_RE.search(title_blob):
            return ""
        if not (DEVICE_OR_DEV_RESOURCE_RE.search(blob) or price_note or LOCAL_BEIJING_RE.search(blob)):
            return ""
        if price_note:
            if not _contains_price_value(price_note):
                return f"二手出售信息，{price_note}；价格待确认"
            return f"二手出售信息，{price_note}；价格优势需自行判断"
        return "二手出售信息，物品/配置较明确但价格未列，价格待确认"

    if is_question_or_discussion(post):
        return ""

    if facts.lottery_facts.is_high_value and facts.lottery_facts.has_actionable_entry:
        prize = "、".join(facts.lottery_facts.prize_evidence[:2]) or "高价值奖品"
        slots = "、".join(facts.lottery_facts.slot_evidence[:2])
        entry = "、".join((facts.lottery_facts.entry_evidence + facts.lottery_facts.draw_evidence)[:2])
        parts = [f"奖品为 {prize}"]
        if slots:
            parts.append(slots)
        if entry:
            parts.append(entry)
        risk = "；注意账号来源和封号风险" if facts.lottery_facts.risk_flags else ""
        return _compact_text("高价值抽奖，" + "，".join(parts) + risk, 120)

    if AI_API_RELAY_SITE_RE.search(blob):
        if _has_ai_relay_keep_value(blob):
            note = format_offer_detail_note(post.body_excerpt or blob) or price_note or "有明确免费额度/低价信息"
            return _compact_text(f"AI API 中转福利，{note}；注意中转来源和稳定性风险", 120)
        return ""

    if OPENAI_SUBSCRIPTION_CONTEXT_RE.search(blob):
        topup_offers = _ai_subscription_topup_offers(blob)
        if topup_offers and all(offer["price"] <= _ai_subscription_price_limit(offer) for offer in topup_offers):
            return f"AI 订阅代充/兑换价格在阈值内，{price_note or '仍需确认来源和账号风险'}"
        if OPENAI_USAGE_POOL_CONTEXT_RE.search(blob) and (_has_ai_relay_keep_value(blob) or price_note):
            note = format_offer_detail_note(post.body_excerpt or blob) or price_note or "有明确免费/低价信息"
            return _compact_text(f"Codex/OpenAI 号池/按量福利，{note}；注意账号池和稳定性风险", 120)
        if "激活码" in blob or "兑换码" in blob or "卡密" in blob:
            return f"OpenAI/ChatGPT 相关兑换资源，{price_note or '需核验来源'}"
        return ""

    if post.rough_type == "promotion_or_offer" or any(tag == "推广" for tag in post.tags):
        if not (OFFER_DETAIL_TOKEN_RE.search(blob) or price_note):
            return ""
        note = format_offer_detail_note(post.body_excerpt or blob) or price_note
        return _compact_text(f"有明确福利/价格信息，{note}", 120)

    return ""


def format_instant_alert_messages(posts: list[Post], arbitrage_messages: list[str]) -> list[str]:
    messages: list[str] = []
    for message in arbitrage_messages:
        message = strip_invisible_prompt_chars(str(message or "")).strip()
        if message:
            messages.append(message)

    for post in posts:
        reason = instant_alert_reason(post)
        if not reason:
            continue
        payload = post_payload(post)
        contact_note = str(payload.get("decoded_contact") or "").strip()
        if contact_note and contact_note not in reason:
            reason = f"{reason}；{contact_note}"
        messages.append(f"[{_markdown_link_title(post.title)}]({post.v2ex_url}) — {reason}")

    return messages


def format_instant_alert_output(posts: list[Post], arbitrage_messages: list[str]) -> str:
    lines: list[str] = []
    for message in arbitrage_messages:
        message = strip_invisible_prompt_chars(str(message or "")).strip()
        if message:
            lines.append(message)

    post_lines = format_instant_alert_messages(posts, [])
    if lines and post_lines:
        lines.append("## V2EX 高信号")
    lines.extend(post_lines)
    return "\n".join(lines).strip()


def parse_posts(html_text: str) -> List[Post]:
    posts: List[Post] = []
    parts = html_text.split(MARKER)
    for chunk in parts[1:]:
        id_part, rest = chunk.split('"', 1)
        try:
            message_id = int(id_part)
        except ValueError:
            continue

        text_match = re.search(
            r'<div class="tgme_widget_message_text js-message_text" dir="auto">(.*?)</div>',
            rest,
            flags=re.S,
        )
        if not text_match:
            continue
        text_html = text_match.group(1)

        href_match = re.search(r'<a href="([^"]+)"', text_html)
        if not href_match:
            continue
        v2ex_url = strip_invisible_prompt_chars(html.unescape(href_match.group(1)))

        time_match = re.search(r'<time datetime="([^"]+)"', rest)
        posted_at = time_match.group(1) if time_match else ""

        title = strip_tags(text_html)
        tags, core_title = extract_tags_and_core(title)
        rough_type = classify(tags, title, core_title)

        posts.append(
            Post(
                message_id=message_id,
                telegram_url=f"https://t.me/{CHANNEL}/{message_id}",
                v2ex_url=v2ex_url,
                posted_at=posted_at,
                title=title,
                tags=tags,
                core_title=core_title,
                rough_type=rough_type,
                body_excerpt="",
            )
        )

    posts.sort(key=lambda p: p.message_id, reverse=True)
    return posts


def load_state() -> dict:
    data = state_json.load(STATE_FILE, default={}, strict=True)
    if not isinstance(data, dict):
        raise ValueError(f"invalid monitor state: {STATE_FILE}")
    return data


def save_state(state: dict) -> None:
    state_json.save_atomic(STATE_FILE, state)


def normalize_pending_instant_alerts(value) -> list[str]:
    if not isinstance(value, list):
        return []
    alerts: list[str] = []
    for item in value:
        text = strip_invisible_prompt_chars(str(item or "")).strip()
        if text:
            alerts.append(text)
    return alerts


def pop_pending_instant_alert(state: dict) -> str:
    """Return one queued alert so cron delivery emits separate messages."""
    pending = normalize_pending_instant_alerts(state.get(PENDING_ALERTS_STATE_KEY))
    if not pending:
        state.pop(PENDING_ALERTS_STATE_KEY, None)
        return ""
    alert = pending.pop(0)
    if pending:
        state[PENDING_ALERTS_STATE_KEY] = pending
    else:
        state.pop(PENDING_ALERTS_STATE_KEY, None)
    return alert


def queue_pending_instant_alerts(state: dict, alerts: list[str]) -> None:
    cleaned = normalize_pending_instant_alerts(alerts)
    if not cleaned:
        return
    existing = normalize_pending_instant_alerts(state.get(PENDING_ALERTS_STATE_KEY))
    state[PENDING_ALERTS_STATE_KEY] = existing + cleaned


def agent_post_payload(post: Post) -> dict:
    payload = post_payload(post)
    decision = evaluate_post_for_monitor(post)
    facts = build_post_facts(post)
    listing = facts.secondary_listing
    locality = facts.locality
    payload.update(
        {
            "message_id": post.message_id,
            "telegram_url": post.telegram_url,
            "posted_at": post.posted_at,
            "core_title": post.core_title,
            "routing_context": {
                "route": decision.route,
                "reason": decision.reason,
                "matched_categories": decision.matched_categories,
                "ignored_low_value_categories": decision.ignored_low_value_categories,
                "economic_value_types": facts.economic_value_types,
                "marketplace_verticals": facts.marketplace_verticals,
                "primary_marketplace_categories": facts.primary_marketplace_categories,
                "addon_marketplace_categories": facts.addon_marketplace_categories,
                "price_evidence_kind": facts.price_evidence_kind,
            },
            "listing_facts": {
                "transaction_role": listing.transaction_role,
                "listing_maturity": listing.listing_maturity,
                "device_evidence": listing.device_evidence,
                "money_evidence": listing.money_evidence,
                "capability_terms": listing.capability_terms,
                "has_listing_frame": listing.has_listing_frame,
                "has_telecom_card_primary": listing.has_telecom_card_primary,
            },
            "locality_facts": {
                "transaction_scope": locality.transaction_scope,
                "locality_names": locality.locality_names,
                "local_only_evidence": locality.local_only_evidence,
                "optional_pickup_evidence": locality.optional_pickup_evidence,
                "remote_trade_evidence": locality.remote_trade_evidence,
            },
            "offer_facts": {
                "intent": facts.offer_facts.intent,
                "benefit_evidence": facts.offer_facts.benefit_evidence,
                "weak_terms": facts.offer_facts.weak_terms,
                "discussion_markers": facts.offer_facts.discussion_markers,
                "has_direct_benefit": facts.offer_facts.has_direct_benefit,
            },
            "lottery_facts": {
                "is_lottery": facts.lottery_facts.is_lottery,
                "is_high_value": facts.lottery_facts.is_high_value,
                "has_actionable_entry": facts.lottery_facts.has_actionable_entry,
                "prize_evidence": facts.lottery_facts.prize_evidence,
                "slot_evidence": facts.lottery_facts.slot_evidence,
                "draw_evidence": facts.lottery_facts.draw_evidence,
                "entry_evidence": facts.lottery_facts.entry_evidence,
                "tool_evidence": facts.lottery_facts.tool_evidence,
                "risk_flags": facts.lottery_facts.risk_flags,
            },
        }
    )
    return sanitize_for_prompt(payload)


def post_from_agent_payload(payload: dict) -> Post | None:
    if not isinstance(payload, dict):
        return None
    title = strip_invisible_prompt_chars(str(payload.get("title") or "")).strip()
    v2ex_url = strip_invisible_prompt_chars(str(payload.get("v2ex_url") or "")).strip()
    if not title or not v2ex_url:
        return None
    tags = payload.get("tags")
    if not isinstance(tags, list):
        tags = []
    clean_tags = [strip_invisible_prompt_chars(str(tag or "")).strip() for tag in tags if str(tag or "").strip()]
    parsed_tags, parsed_core_title = extract_tags_and_core(title)
    if parsed_tags:
        clean_tags = parsed_tags
    core_title = strip_invisible_prompt_chars(str(payload.get("core_title") or parsed_core_title or title)).strip()
    rough_type = strip_invisible_prompt_chars(str(payload.get("rough_type") or "")).strip()
    if not rough_type:
        rough_type = classify(clean_tags, title, core_title)
    try:
        message_id = int(payload.get("message_id") or 0)
    except Exception:
        message_id = 0
    return Post(
        message_id=message_id,
        telegram_url=strip_invisible_prompt_chars(str(payload.get("telegram_url") or "")).strip(),
        v2ex_url=v2ex_url,
        posted_at=strip_invisible_prompt_chars(str(payload.get("posted_at") or "")).strip(),
        title=title,
        tags=clean_tags,
        core_title=core_title,
        rough_type=rough_type,
        body_excerpt=strip_invisible_prompt_chars(str(payload.get("body_excerpt") or "")).strip(),
    )


def _normalize_agent_batch_item(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind") or "").strip()
    if kind == "post":
        post = value.get("post")
        if not isinstance(post, dict):
            return None
        cleaned = sanitize_for_prompt(post)
        if not cleaned.get("v2ex_url") or not cleaned.get("title"):
            return None
        return {"kind": "post", "post": cleaned}
    if kind == "arbitrage":
        message = strip_invisible_prompt_chars(str(value.get("message") or "")).strip()
        if not message:
            return None
        return {"kind": "arbitrage", "message": message}
    return None


def build_agent_delivery_queue(posts: list[Post], arbitrage_messages: list[str]) -> list[dict]:
    queue: list[dict] = []
    for message in arbitrage_messages:
        cleaned = strip_invisible_prompt_chars(str(message or "")).strip()
        if cleaned:
            queue.append({"kind": "arbitrage", "message": cleaned})
    for post in posts:
        queue.append({"kind": "post", "post": agent_post_payload(post)})
    return queue


def normalize_agent_delivery_batch(value) -> dict | None:
    if not isinstance(value, dict):
        return None

    queue = [_normalize_agent_batch_item(item) for item in value.get("queue", [])]
    queue = [item for item in queue if item is not None]

    active = _normalize_agent_batch_item(value.get("active"))
    raw_skipped = value.get("skipped_items", [])
    skipped = [sanitize_for_prompt(item) for item in raw_skipped if isinstance(item, dict)]

    batch = {
        "batch_id": strip_invisible_prompt_chars(str(value.get("batch_id") or "")).strip(),
        "source_last_seen_id": value.get("source_last_seen_id"),
        "target_last_seen_id": value.get("target_last_seen_id"),
        "queue": queue,
        "active": active,
        "attempts": int(value.get("attempts", 0) or 0),
        "last_emitted_at": strip_invisible_prompt_chars(str(value.get("last_emitted_at") or "")).strip(),
        "skipped_items": skipped,
    }
    if not batch["batch_id"]:
        batch["batch_id"] = f"v2ex-{batch['source_last_seen_id']}-{batch['target_last_seen_id']}"
    return batch


def _agent_batch_item_summary(item: dict) -> dict:
    if item.get("kind") == "post":
        post = item.get("post", {})
        return {
            "kind": "post",
            "message_id": post.get("message_id"),
            "title": post.get("title"),
            "v2ex_url": post.get("v2ex_url"),
        }
    if item.get("kind") == "arbitrage":
        return {
            "kind": "arbitrage",
            "message": item.get("message"),
        }
    return {"kind": "unknown"}


def _agent_batch_skip_active(batch: dict, active: dict, decision: RouteDecision) -> None:
    skipped = _agent_batch_item_summary(active)
    skipped["route"] = decision.route
    skipped["reason"] = decision.reason
    skipped["matched_categories"] = decision.matched_categories
    batch.setdefault("skipped_items", []).append(sanitize_for_prompt(skipped))
    _agent_batch_activate_next(batch)


def _agent_batch_revalidate_active(batch: dict) -> bool:
    active = batch.get("active")
    if not active or active.get("kind") != "post":
        return True
    post = post_from_agent_payload(active.get("post", {}))
    if post is None:
        _agent_batch_skip_active(batch, active, RouteDecision("exclude", "旧队列候选无法重建", ["invalid_queued_post"]))
        return False
    decision = evaluate_post_for_monitor(post)
    if decision.route == "candidate":
        return True
    _agent_batch_skip_active(batch, active, decision)
    return False


def _agent_batch_has_work(batch: dict) -> bool:
    return bool(batch.get("active") or batch.get("queue"))


def _agent_batch_clear_if_complete(state: dict, batch: dict) -> None:
    if batch.get("active") or batch.get("queue"):
        state[AGENT_BATCH_STATE_KEY] = batch
        return

    skipped_items = [item for item in batch.get("skipped_items", []) if isinstance(item, dict)]
    if skipped_items:
        existing_skipped = state.get("skipped_items")
        if not isinstance(existing_skipped, list):
            existing_skipped = []
        state["skipped_items"] = (existing_skipped + skipped_items)[-100:]

    source_last_seen_id = batch.get("source_last_seen_id")
    target_last_seen_id = batch.get("target_last_seen_id")
    if target_last_seen_id is not None:
        try:
            if source_last_seen_id is None:
                state["last_seen_id"] = int(target_last_seen_id)
            else:
                state["last_seen_id"] = max(int(source_last_seen_id), int(target_last_seen_id))
        except Exception:
            state["last_seen_id"] = target_last_seen_id
    state.pop(AGENT_BATCH_STATE_KEY, None)


def _agent_batch_initialize(
    state: dict,
    *,
    source_last_seen_id: int | None,
    target_last_seen_id: int,
    items: list[dict],
    now_iso: str,
) -> dict:
    active = items[0]
    queue = items[1:]
    batch = {
        "batch_id": f"v2ex-{source_last_seen_id}-{target_last_seen_id}-{int(time.time())}",
        "source_last_seen_id": source_last_seen_id,
        "target_last_seen_id": target_last_seen_id,
        "queue": queue,
        "active": active,
        "attempts": 0,
        "last_emitted_at": "",
        "skipped_items": [],
    }
    state[AGENT_BATCH_STATE_KEY] = batch
    return batch


def _agent_batch_activate_next(batch: dict) -> None:
    queue = batch.get("queue")
    if isinstance(queue, list) and queue:
        batch["active"] = queue.pop(0)
        batch["attempts"] = 0
        batch["last_emitted_at"] = ""
    else:
        batch["active"] = None
        batch["attempts"] = 0
        batch["last_emitted_at"] = ""


def _agent_batch_process_current(state: dict, batch: dict, now_iso: str) -> str:
    while _agent_batch_has_work(batch):
        if not batch.get("active"):
            _agent_batch_activate_next(batch)
        if not _agent_batch_revalidate_active(batch):
            continue
        active = batch["active"]
        if active.get("kind") == "arbitrage":
            message = str(active.get("message") or "").strip()
            reason = "confirmed_arbitrage"
        else:
            post = post_from_agent_payload(active.get("post", {}))
            reason = instant_alert_reason(post) if post else ""
            message = format_instant_alert_output([post], []) if reason else ""
        state["last_rule_decision"] = {
            **_agent_batch_item_summary(active), "rule_version": RULE_VERSION,
            "action": "push" if message else "silent",
            "reason": reason or "insufficient_actionable_evidence", "evaluated_at": now_iso,
        }
        _agent_batch_activate_next(batch)
        _agent_batch_clear_if_complete(state, batch)
        if message:
            return message
    _agent_batch_clear_if_complete(state, batch)
    return output.wake_agent_false()


def collect_recent_pages(last_seen_id: Optional[int], *, deadline: float | None = None) -> tuple[List[Post], dict]:
    seen: dict[int, Post] = {}
    before: Optional[int] = None
    pages_fetched = 0
    hit_last_seen = False
    fetch_error = ""

    for _ in range(MAX_PAGES):
        if deadline_remaining(deadline) < TELEGRAM_REQUEST_TIMEOUT + 1:
            break
        url = BASE_URL if before is None else f"{BASE_URL}?before={before}"
        try:
            html_text = fetch(
                url,
                timeout=TELEGRAM_REQUEST_TIMEOUT,
                attempts=TELEGRAM_FETCH_MAX_ATTEMPTS,
                phase="telegram_list",
            )
        except Exception as exc:
            fetch_error = f"{type(exc).__name__}: {exc}"
            print(f"v2ex_collect_page_failed page={pages_fetched + 1} error={fetch_error}", file=sys.stderr)
            break
        pages_fetched += 1
        posts = parse_posts(html_text)
        if not posts:
            break

        for post in posts:
            seen[post.message_id] = post

        min_id = min(p.message_id for p in posts)
        if last_seen_id is not None and any(p.message_id <= last_seen_id for p in posts):
            hit_last_seen = True
            break

        if before is not None and min_id >= before:
            break
        before = min_id

    ordered = sorted(seen.values(), key=lambda p: p.message_id, reverse=True)
    meta = {
        "pages_fetched": pages_fetched,
        "hit_last_seen": hit_last_seen,
        "fetch_error": fetch_error,
    }
    return ordered, meta


def post_payload(post: Post) -> dict:
    """Minimal JSON sent to the cron agent for high-signal filtering."""
    payload = {
        "title": post.title,
        "tags": post.tags,
        "rough_type": post.rough_type,
        "v2ex_url": post.v2ex_url,
        "body_excerpt": post.body_excerpt,
    }
    payload = sanitize_for_prompt(payload)
    contact_note = format_contact_note(f"{payload['title']}\n{payload['body_excerpt']}")
    if contact_note:
        payload["decoded_contact"] = strip_invisible_prompt_chars(contact_note)
    return payload


def route_decisions_by_message_id(posts: list[Post]) -> dict[int, RouteDecision]:
    return {post.message_id: evaluate_post_for_monitor(post) for post in posts}


def arbitrage_scan_posts(posts: list[Post], decisions: dict[int, RouteDecision]) -> list[Post]:
    return [post for post in posts if decisions.get(post.message_id, evaluate_post_for_monitor(post)).route == "candidate"]


def run_once(*, dry_run: bool = False) -> int:
    deadline = deadline_after(RUN_BUDGET_SECONDS)
    now_iso = utc_now_iso()
    state = copy.deepcopy(load_state())
    def persist(value: dict) -> None:
        if not dry_run:
            save_state(value)

    pending_alert = pop_pending_instant_alert(state)
    if pending_alert:
        if not dry_run:
            events.publish(pending_alert)
        persist(state)
        print(pending_alert)
        return 0

    batch = normalize_agent_delivery_batch(state.get(AGENT_BATCH_STATE_KEY))
    if batch:
        final_response = _agent_batch_process_current(state, batch, now_iso)
        if not dry_run:
            events.publish(final_response)
        persist(state)
        print(final_response)
        return 0

    last_seen_id = state.get("last_seen_id")
    is_bootstrap = last_seen_id is None

    recent_posts, meta = collect_recent_pages(last_seen_id if not is_bootstrap else None, deadline=deadline)
    if not recent_posts:
        # No candidates means the cron agent does not need any structured context.
        # Keep the injected prompt tiny for the 1-minute monitoring loop.
        if meta.get("fetch_error"):
            events.result("failed", str(meta["fetch_error"]))
            state["last_poll_meta"] = meta
            persist(state)
        print(output.wake_agent_false())
        return 0

    if meta.get("fetch_error") and not meta.get("hit_last_seen"):
        events.result("failed", str(meta["fetch_error"]))
        state["last_poll_meta"] = meta
        persist(state)
        print(output.wake_agent_false())
        return 0

    newest_id = recent_posts[0].message_id

    if is_bootstrap:
        candidate_posts = recent_posts[:BOOTSTRAP_LIMIT]
        next_state = {
            **state,
            "initialized_via": "bootstrap",
            "last_poll_meta": meta,
        }
        mode = "bootstrap"
        source_last_seen_id = None
        target_last_seen_id = newest_id
    else:
        candidate_posts = [p for p in recent_posts if p.message_id > int(last_seen_id)]
        candidate_posts.sort(key=lambda p: p.message_id, reverse=True)
        next_state = {
            **state,
            "last_poll_meta": meta,
        }
        mode = "incremental"
        source_last_seen_id = int(last_seen_id)
        target_last_seen_id = max(int(last_seen_id), newest_id)

    if not candidate_posts:
        # Nothing new after state comparison; keep normal quiet runs token-cheap.
        next_state["last_seen_id"] = target_last_seen_id
        persist(next_state)
        print(output.wake_agent_false())
        return 0

    enrich_posts_with_topic_excerpt(candidate_posts, deadline=deadline)
    route_decisions = route_decisions_by_message_id(candidate_posts)
    arbitrage_posts = arbitrage_scan_posts(candidate_posts, route_decisions)

    arbitrage_messages = sanitize_for_prompt(([] if dry_run else collect_arbitrage_messages(
        "v2ex",
        [
            {
                "id": p.message_id,
                "title": strip_invisible_prompt_chars(p.title),
                "url": strip_invisible_prompt_chars(p.v2ex_url),
                "body_excerpt": strip_invisible_prompt_chars(p.body_excerpt),
            }
            for p in arbitrage_posts
        ],
    ) or []))

    filtered_posts = [p for p in candidate_posts if route_decisions[p.message_id].route == "candidate"]
    if not filtered_posts and not arbitrage_messages:
        next_state["last_seen_id"] = target_last_seen_id
        persist(next_state)
        print(output.wake_agent_false())
        return 0

    agent_items = build_agent_delivery_queue(filtered_posts, arbitrage_messages)
    if not agent_items:
        next_state["last_seen_id"] = target_last_seen_id
        persist(next_state)
        print(output.wake_agent_false())
        return 0

    batch = _agent_batch_initialize(
        next_state,
        source_last_seen_id=source_last_seen_id,
        target_last_seen_id=target_last_seen_id,
        items=agent_items,
        now_iso=now_iso,
    )
    final_response = _agent_batch_process_current(next_state, batch, now_iso)
    if not dry_run:
        events.publish(final_response)
    persist(next_state)
    print(final_response)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor V2EX using deterministic rules.")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without saving state or recording arbitrage alerts.")
    args = parser.parse_args(argv)
    with locks.nonblocking_file_lock(STATE_DIR / "v2ex_notice.lock") as lock:
        if not lock.acquired:
            print(output.wake_agent_false())
            return 0
        return run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
