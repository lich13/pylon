#!/usr/bin/env python3
"""
Hermes-side Xianbao filter worker.

Environment:
  XIANBAO_RULES=/path/to/xianbao-hermes-rules.json
  XIANBAO_STATE=/path/to/xianbao-state.sqlite3
  XIANBAO_DATA_DIR=/path/to/runtime-data
  XIANBAO_DRY_RUN=1
  XIANBAO_USE_WEBOPS_BARK=1
  BARK_URL=https://api.day.app/<key>
  PUSH_WEBHOOK_URL=https://...
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import html
import http.client
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from xianbao_value_rules import is_poor_mobile_topup_discount, mobile_topup_discount_profile


DEFAULT_RULES = "/srv/pylon/profiles/default/config/xianbao-hermes-rules.json"
DEFAULT_STATE = "/srv/pylon/profiles/default/data/xianbao_filter/state.sqlite3"
DEFAULT_DATA_DIR = "/srv/pylon/profiles/default/data/xianbao_filter"
BASE_URL = "https://new.xianbao.fun"
DEFAULT_BODY_CACHE_TTL_SECONDS = 86400
DEFAULT_BODY_ERROR_CACHE_TTL_SECONDS = 3600
DEFAULT_POLL_JSON_CACHE_TTL_SECONDS = 6 * 3600
DEFAULT_POLL_JSON_STALE_IF_ERROR_SECONDS = 24 * 3600
DEFAULT_PRUNE_EVERY_SECONDS = 3600
DEFAULT_SEEN_REJECT_RETENTION_SECONDS = 7 * 86400
DEFAULT_SEEN_KEEP_RETENTION_SECONDS = 7 * 86400
DEFAULT_SENT_RETENTION_SECONDS = 180 * 86400
DEFAULT_DECISION_LOG_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_SUMMARY_LOG_MAX_BYTES = 10 * 1024 * 1024
DECISION_RULESET_VERSION = "20260907-deterministic-v1"
LAST_POLL_SUMMARY: dict[str, Any] = {}
LAST_PRUNE_AT = 0
LAST_ERROR_ALERT_AT = 0
SOURCE_UNAVAILABLE_STATE_FILE = "poll_json_source_unavailable_state.json"

UNSUPPORTED_ELIGIBILITY_RE = re.compile(r"(富滇银行|富滇银行卡|富滇卡|富滇)", re.I)
NEIMENG_REGION_RE = re.compile(r"(内蒙|内蒙古|呼和浩特)", re.I)
LOCAL_AREA_CODE_REGION_RE = re.compile(r"(?<![0-9])(?:020|021|022|023|024|025|027|028|029|031[0-9]|037[0-9]|075[0-9]|076[0-9])(?![0-9])", re.I)
LOCAL_OFFLINE_PARTICIPATION_RE = re.compile(r"(加油|油站|中石化|中石油|石化|到店|门店|商户|商家|线下|本地|地区|分行|分会场|广州|上海|天津|重庆|南京|武汉|成都|西安|郑州|深圳|佛山|东莞)", re.I)
BORROW_MERCHANT_CODE_QUESTION_RE = re.compile(
    r"((谁有|有没有|有无|求|找|帮|帮忙|来个|来一个|借|套一下|互扫|一人一半|分你|返你).{0,24}"
    r"(商家码|商户码|经营码|收款码|到店红包|店红包|zfb到店|支付宝到店)"
    r"|(商家码|商户码|经营码|收款码|到店红包|店红包|zfb到店|支付宝到店).{0,24}"
    r"(谁有|有没有|有无|求|找|帮|帮忙|来个|来一个|借|套一下|互扫|一人一半|分你|返你))",
    re.I,
)
PRODUCT_PURCHASE_REBATE_RE = re.compile(
    r"((买|购|加购|下单|付款|实付|拍[0-9一二三四五六七八九十]*件|到货|晒单|晒图|好评).{0,40}"
    r"(返|送|赠|贈|反送|抽|中|领|得|拿).{0,16}(E卡|e卡|京东卡|京豆|豆子|红包|返现)"
    r"|(返|送|赠|贈|反送|抽|中|领|得|拿).{0,16}(E卡|e卡|京东卡|京豆|豆子|红包|返现).{0,30}(到货|晒单|晒图|好评|下单|付款|实付))",
    re.I,
)
PRODUCT_CONTEXT_RE = re.compile(
    r"(商品|实物|到手|好价|低价|凑单|加购|购物车|店铺|客服|官旗|京东自营|淘宝|天猫|猫超|拼多多|pdd|"
    r"吹风机|暖风机|抽纸|纸巾|湿巾|洗脸巾|牛奶|饮料|食品|零食|面膜|美妆|面霜|防晒|牙刷|牙膏|"
    r"猫粮|狗粮|猫砂|鞋|衣|裤|短袖|T恤|t恤|服饰|面料|故事机|学习机|童书|绘本|音箱|电饼铛|键盘|路由器|玩具)",
    re.I,
)
PHYSICAL_PRODUCT_CONTEXT_RE = re.compile(
    r"(商品|实物|买实物|吹风机|暖风机|抽纸|纸巾|湿巾|洗脸巾|牛奶|饮料|食品|零食|玉米|水果|"
    r"面膜|美妆|面霜|防晒|牙刷|牙膏|洗衣液|洗洁精|猫粮|狗粮|犬粮|猫砂|鞋|衣|裤|短袖|T恤|t恤|"
    r"服饰|面料|故事机|学习机|童书|绘本|音箱|电饼铛|键盘|路由器|玩具|驱虫|护臀膏|尿不湿|纸尿裤|"
    r"冰淇淋|雪糕|酸奶|鲜奶|鱼油|鸭蛋|咸鸭蛋|虾|螃蟹|血糖仪|脱毛仪|工学椅|电竞椅|办公椅|"
    r"保鲜袋|过滤器|水龙头|小样|微波炉|薯片|点读笔|烘干箱|帐篷|自行车)",
    re.I,
)
PRODUCT_PURCHASE_FLOW_RE = re.compile(
    r"(买|购|购买|加购|下单|付款|实付|实符|拍[0-9一二三四五六七八九十]*件?|拍\s|凑单|凑褪|到手|到货|晒单|晒图|好评|"
    r"领券|领卷|券后|卷后|店铺券|平台券|无门槛|会员价|plus领|PLUS领|购物车|换着穿)",
    re.I,
)
PHYSICAL_PRODUCT_QUANTITY_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?\s*(?:ml|g|kg|斤|抽|包|支|盒|瓶|件|粒|片|枚|杯|袋|罐|个|cm)", re.I)
CONSUMABLE_PRODUCT_OBJECT_RE = re.compile(
    r"(饮料|甜水|汽水|纯净水|矿泉水|苏打水|牛奶|鲜奶|酸奶|奶茶|咖啡|果汁|水果|草莓|苹果|橙|梨|"
    r"冰淇淋|雪糕|零食|饼干|面包|蛋糕|咸鸭蛋|鸭蛋|虾|螃蟹|牛肉|肉丸|粮油|米面)",
    re.I,
)
QUANTITY_PURCHASE_FLOW_RE = re.compile(
    r"((买|购|购买|可以买|下单|加购|拍|凑单|囤|入|作业).{0,24}"
    r"[0-9一二两三四五六七八九十]+(?:瓶|盒|包|袋|罐|杯|支|件|个|斤|g|kg|ml)"
    r"|[0-9一二两三四五六七八九十]+(?:瓶|盒|包|袋|罐|杯|支|件|个|斤|g|kg|ml).{0,24}"
    r"(买|购|购买|可以买|下单|加购|拍|凑单|囤|入|作业|合适))",
    re.I,
)
PRODUCT_PRICEISH_RE = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?\s*(?:元|亓|块|r|R|毛)|[0-9]+(?:\.[0-9]+)?\s*(?:以内|以下|左右)|[0-9]+(?:\.[0-9]+)?\s*[~～到至-]\s*[0-9]+(?:\.[0-9]+)?)", re.I)
PHYSICAL_PRODUCT_SHAPE_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{1,24}(?:仪|椅|器|锅|箱|架|灯|镜|垫|鞋|裤|衣)", re.I)
NON_PRODUCT_VALUE_RE = re.compile(
    r"(银行|信用卡|xyk|云闪付|支付宝|微信支付|还款|转账|收款|缴费|话费|加油|立减金|刷卡金|支付券|还款券|"
    r"红包|现金|会员|月卡|季卡|年卡|猜大小|福利天天领)",
    re.I,
)
SHOPPING_CHANNEL_RE = re.compile(r"(京东|淘宝|天猫|猫超|拼多多|pdd|JD|TB|店铺|购物车|平台券|店铺券|plus领|PLUS领)", re.I)
PRODUCT_SPEC_OR_PRICE_RE = re.compile(
    r"([0-9]+(?:\.[0-9]+)?\s*(?:元|亓|块|r|R|ml|g|kg|抽|包|支|盒|瓶|件|粒|片|斤)|[0-9]+[-减][0-9]+|[0-9]+[gG][bB])",
    re.I,
)
BARE_SHOPPING_PRICE_RE = re.compile(r"(?<![A-Za-z0-9])(?:[1-9][0-9]{1,4})(?![A-Za-z0-9])", re.I)
SHOPPING_PRICE_CONTEXT_RE = re.compile(
    r"(京东|淘宝|天猫|猫超|拼多多|pdd|百亿补贴|百补|旗舰店|官旗|自营|店铺|商城|好价|低价|刚需|到手|实付|"
    r"券后|卷后|领券|领卷|补贴|福袋|可以买吗|能买吗|值吗|买吗|入吗|冲吗)",
    re.I,
)
ATTACHED_PRODUCT_REWARD_RE = re.compile(
    r"(返|送|赠|贈|反送|抽|中|领|得|拿|返现|红包|券|积分|豆子|E卡|e卡|京东卡|京豆|好评返|晒单返|到货返|赠送)",
    re.I,
)
MOBILE_TOPUP_RE = re.compile(r"(手机话费|话费券|话费红包|话费立减|手机充值|充话费|移动话费|联通话费|电信话费|通信费)", re.I)
NON_MOBILE_UTILITY_BILL_RE = re.compile(
    r"(网上国网|国家电网|国网|云缴费|生活缴费|缴费小美好|交电费|电费|水费|燃气费|燃气|水电燃气)",
    re.I,
)
AIRLINE_MILEAGE_RE = re.compile(r"(南航|中国南方航空|南方航空|航司|航空).{0,24}(里程|明珠)|(?:里程|明珠).{0,24}(南航|中国南方航空|南方航空|航司|航空)", re.I)
LOW_VALUE_PROMO_TASK_RE = re.compile(
    r"(看广告|看视频|看[0-9一二两三四五六七八九十]+分钟|浏览|下载|试玩|体验|口令|小程序).{0,24}(抽|红包|券|奖励|积分|%)|"
    r"(抽|红包|券|奖励|积分|%).{0,24}(看广告|看视频|看[0-9一二两三四五六七八九十]+分钟|浏览|下载|试玩|体验|口令|小程序)",
    re.I,
)
PASSIVE_WATCH_MEMBERSHIP_TASK_RE = re.compile(
    r"(腾讯云包场|包场|看视频|看[0-9一二两三四五六七八九十]+分钟).{0,36}(免费领|领|抽|得|拿).{0,16}(会员|月卡|季卡|年卡|VIP|vip)|"
    r"(会员|月卡|季卡|年卡|VIP|vip).{0,36}(腾讯云包场|包场|看视频|看[0-9一二两三四五六七八九十]+分钟)",
    re.I,
)
MINI_PROGRAM_BANNER_TASK_RE = re.compile(
    r"(小程序|红色火箭).{0,36}(横幅|中间第二个|第二个横幅|任务|口令)|"
    r"(横幅|中间第二个|第二个横幅).{0,36}(小程序|红色火箭)",
    re.I,
)
LOW_VALUE_TASK_CORE_PROTECT_RE = re.compile(
    r"(银行|信用卡|xyk|农行|农业银行|建行|建设银行|工行|工商银行|中行|中国银行|交行|交通银行|邮储|招商|招行|"
    r"云闪付|支付宝|微信支付|京东|还款|转账|收款|经营码|商户码|福利天天领632|猜大小|数币|数字人民币|"
    r"立减金|刷卡金|支付券|还款券)",
    re.I,
)
SHOPPING_SURVEY_TASK_RE = re.compile(
    r"(问卷|调研|调查|访谈|招募|人群|筛选|参与条件|受访|填写)",
    re.I,
)
SHOPPING_SURVEY_CONSUMER_RE = re.compile(
    r"(购物|消费|品类|购买决策|使用人|买过|购买过|订单|购买凭证|做饭|居住时长|小红书|母婴|美妆|护肤|"
    r"食品|饮料|日用|家清|个护|宠物|服饰|鞋|衣|裤|商品)",
    re.I,
)
SHOPPING_SURVEY_PROOF_RE = re.compile(
    r"(上传|提供|提交|购买凭证|订单截图|购物截图|实拍图|晒图|主页截图|小红书主页|带ID|带id|审核|"
    r"审核通过|服务通知|48小时|[0-9]+小时)",
    re.I,
)
SHOPPING_SURVEY_REWARD_RE = re.compile(
    r"(礼金|红包|奖励|报酬|佣金|现金|微信红包|[0-9]+(?:\.[0-9]+)?\s*(?:元|块|r|R))",
    re.I,
)
REUSABLE_FINANCIAL_VALUE_PATH_RE = re.compile(
    r"(银行|信用卡|xyk|农行|农业银行|建行|建设银行|工行|工商银行|中行|中国银行|交行|交通银行|邮储|招商|招行|"
    r"云闪付|支付宝|微信支付|京东|还款|转账|收款|经营码|商户码|数币|数字人民币|立减金|刷卡金|支付券|"
    r"还款券|福利天天领632|猜大小).{0,40}(入口|路径|直达|扫码|搜索|搜|领取|可领|能领|开通|报名|参加|抽|有水|必中|保底)|"
    r"(入口|路径|直达|扫码|搜索|搜|领取|可领|能领|开通|报名|参加|抽|有水|必中|保底).{0,40}"
    r"(银行|信用卡|xyk|农行|农业银行|建行|建设银行|工行|工商银行|中行|中国银行|交行|交通银行|邮储|招商|招行|"
    r"云闪付|支付宝|微信支付|京东|还款|转账|收款|经营码|商户码|数币|数字人民币|立减金|刷卡金|支付券|"
    r"还款券|福利天天领632|猜大小)",
    re.I,
)
INDEPENDENT_VALUE_PATH_RE = re.compile(
    r"(信用卡还款|还款|转账|收款|生活缴费|缴费|国网|网上国网|国家电网|电费|水费|燃气费|话费|手机充值|加油|"
    r"中石油|中石化|石化|油卡|公交|地铁|一卡通|交通卡|公交卡|地铁卡|乘车码|数币|数字人民币|"
    r"经营码|商家码|商户码|商户收款码|收款码|套现|无损套|无痛套|0损|零损|T出|折现|变现|"
    r"福利天天领632|猜大小|工行服务小程序|余额变动提醒|开通有礼)",
    re.I,
)


@dataclass
class Decision:
    keep: bool
    tier: int
    score: int
    reason: str
    value_label: str = ""
    content_source: str = "list"
    needs_body_fetch: bool = False
    duplicate: bool = False
    duplicate_of: str | None = None
    review_candidate: bool = False
    review_status: str = ""
    review_label: str = ""
    evidence: dict[str, Any] | None = None


@dataclass
class DecisionContext:
    title: str
    body: str
    category: str
    text: str
    content_source: str
    body_confirmed: bool
    body_source: str


@dataclass
class Evidence:
    entity_types: list[str]
    benefit_types: list[str]
    amount: float
    probability: str
    operation_path: list[str]
    timeliness_status: str
    risk_friction: list[str]
    source_trust: str
    body_confirmed: bool
    activity_identity: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_float_list(name: str, default: list[float]) -> list[float]:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    values: list[float] = []
    for part in re.split(r"[\s,]+", raw.strip()):
        if not part:
            continue
        try:
            values.append(float(part))
        except ValueError:
            continue
    return values or default




def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def compile_re(pattern: str | None) -> re.Pattern[str] | None:
    if not pattern:
        return None
    return re.compile(pattern, re.I)


def regex_fragments(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def compile_re_union(*values: Any) -> re.Pattern[str] | None:
    fragments: list[str] = []
    for value in values:
        fragments.extend(regex_fragments(value))
    if not fragments:
        return None
    return re.compile("|".join(f"(?:{fragment})" for fragment in fragments), re.I)


def rx(patterns: dict[str, re.Pattern[str] | None], name: str, text: str) -> bool:
    pattern = patterns.get(name)
    return bool(pattern and pattern.search(text))


def transient_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, FetchJsonDecodeError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 408 or exc.code == 429 or 500 <= exc.code <= 599
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, urllib.error.URLError, ssl.SSLError, http.client.HTTPException)):
        return True
    return False


class FetchJsonDecodeError(ValueError):
    def __init__(self, url: str, status: int | None, content_type: str, raw: bytes, detail: str):
        self.url = url
        self.status = status
        self.content_type = content_type or ""
        self.raw_len = len(raw)
        preview_text = raw[:240].decode("utf-8", errors="replace")
        self.preview = re.sub(r"\s+", " ", preview_text).strip()
        super().__init__(
            f"{detail}: url={url} status={status or 'unknown'} "
            f"content_type={self.content_type or 'unknown'} bytes={self.raw_len} "
            f"preview={self.preview[:160]!r}"
        )


def fetch_json_error_metadata(prefix: str, exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        f"{prefix}_error_type": type(exc).__name__,
        f"{prefix}_error": str(exc),
    }
    if isinstance(exc, FetchJsonDecodeError):
        payload.update(
            {
                f"{prefix}_fetch_status": exc.status,
                f"{prefix}_fetch_content_type": exc.content_type,
                f"{prefix}_fetch_bytes": exc.raw_len,
                f"{prefix}_fetch_preview": exc.preview[:240],
            }
        )
    return payload


def source_urlopen(req: urllib.request.Request, timeout: float):
    # An empty handler also prevents inherited HTTP(S)_PROXY settings from taking effect.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def fetch_json_once(url: str, timeout: float) -> list[dict[str, Any]]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Hermes-Xianbao-Filter/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with source_urlopen(req, timeout) as resp:
        raw = resp.read()
        status = getattr(resp, "status", None) or getattr(resp, "code", None)
        content_type = resp.headers.get("Content-Type", "") if getattr(resp, "headers", None) else ""
        if not raw.strip():
            raise FetchJsonDecodeError(url, status, content_type, raw, "empty json response")
        text = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FetchJsonDecodeError(url, status, content_type, raw, "invalid json response") from exc
        if not isinstance(payload, list):
            raise FetchJsonDecodeError(url, status, content_type, raw, f"unexpected json shape {type(payload).__name__}")
        return payload


def fetch_json(
    url: str,
    timeout: int = 15,
    log_retries: bool = True,
) -> list[dict[str, Any]]:
    timeout_seconds = env_float("XIANBAO_FETCH_JSON_TIMEOUT_SECONDS", float(timeout))
    attempts = max(1, env_int("XIANBAO_FETCH_JSON_RETRIES", 3))
    backoffs = env_float_list("XIANBAO_FETCH_JSON_RETRY_BACKOFF_SECONDS", [2.0, 5.0])
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            return fetch_json_once(url, timeout_seconds)
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts or not transient_fetch_error(exc):
                break
            delay = backoffs[min(attempt - 1, len(backoffs) - 1)] if backoffs else 0.0
            if log_retries:
                print(
                    "fetch json retry "
                    f"url={url} attempt={attempt}/{attempts} "
                    f"error={type(exc).__name__}: {exc} "
                    f"elapsed={time.monotonic() - started:.2f}s sleep={delay:.1f}s",
                    file=sys.stderr,
                    flush=True,
                )
            if delay > 0:
                time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def poll_json_cache_key(url: str) -> str:
    return f"poll-json:{url}"


def poll_json_urls(primary_url: str) -> list[str]:
    configured = os.getenv("XIANBAO_POLL_JSON_URLS", "").strip()
    values = configured.split(",") if configured else [primary_url]
    urls: list[str] = []
    for value in values:
        url = value.strip()
        if url and url not in urls:
            urls.append(url)
    if primary_url and primary_url not in urls:
        urls.append(primary_url)
    return urls


def first_cached_poll_json_row(db: sqlite3.Connection, urls: list[str]) -> tuple[str, Any] | None:
    for url in urls:
        row = db.execute(
            "select body, source, fetched_at from article_cache where url = ? and error is null",
            (poll_json_cache_key(url),),
        ).fetchone()
        if row:
            return url, row
    return None


def poll_json_failure_summary(url: str, exc: Exception) -> dict[str, Any]:
    return {
        "url": url,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }


def fetch_json_cached(
    db: sqlite3.Connection,
    url: str | list[str],
    ttl_seconds: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ttl = ttl_seconds if ttl_seconds is not None else env_int("XIANBAO_POLL_JSON_CACHE_TTL_SECONDS", DEFAULT_POLL_JSON_CACHE_TTL_SECONDS)
    stale_if_error = env_int("XIANBAO_POLL_JSON_STALE_IF_ERROR_SECONDS", DEFAULT_POLL_JSON_STALE_IF_ERROR_SECONDS)
    urls = url if isinstance(url, list) else [url]
    failures: list[dict[str, Any]] = []
    last_exc: Exception | None = None
    for candidate in urls:
        try:
            posts = fetch_json(candidate, log_retries=False)
        except Exception as exc:
            last_exc = exc
            failures.append(poll_json_failure_summary(candidate, exc))
            if not (transient_fetch_error(exc) or isinstance(exc, urllib.error.HTTPError)):
                raise
            continue

        now = int(time.time())
        db.execute(
            "insert or replace into article_cache(url, body, source, error, fetched_at) values (?, ?, ?, ?, ?)",
            (
                poll_json_cache_key(candidate),
                json.dumps(posts, ensure_ascii=False, sort_keys=True),
                "push-json",
                None,
                now,
            ),
        )
        db.commit()
        fallback: dict[str, Any] = {
            "poll_json_source_url": candidate,
            "poll_json_transport": "direct",
        }
        if failures:
            fallback["poll_json_failures"] = failures
        return posts, fallback

    assert last_exc is not None
    unavailable = {
        "poll_json_source_unavailable": True,
        "poll_json_source_url": urls[0] if urls else "",
        "poll_json_failures": failures,
        "poll_json_transport": "direct",
    }
    unavailable.update(fetch_json_error_metadata("poll_json_source", last_exc))
    cached_row = first_cached_poll_json_row(db, urls)
    if not cached_row:
        return [], unavailable

    cached_url, row = cached_row
    age = int(time.time()) - int(row[2])
    use_fresh_cache = ttl > 0 and age <= ttl
    use_stale_cache = transient_fetch_error(last_exc) and stale_if_error > 0 and age <= stale_if_error
    unavailable["poll_json_fallback_age_seconds"] = age
    # An expired cache is an empty poll during an outage, not a runtime alert.
    if not (use_fresh_cache or use_stale_cache):
        return [], unavailable
    try:
        cached = json.loads(str(row[0]))
    except json.JSONDecodeError:
        return [], unavailable
    if not isinstance(cached, list):
        return [], unavailable
    fallback = {
        "poll_json_fallback": True,
        "poll_json_fallback_url": cached_url,
        "poll_json_fallback_age_seconds": age,
        "poll_json_fallback_source": str(row[1]),
        "poll_json_stale_fallback": bool(not use_fresh_cache),
        "poll_json_failures": failures,
        "poll_json_transport": "cache",
    }
    fallback.update(fetch_json_error_metadata("poll_json_fallback", last_exc))
    return cached, fallback


def fetch_text(url: str, timeout: int = 18) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Hermes-Xianbao-Filter/1.1",
            "Accept": "text/html,*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with source_urlopen(req, timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def strip_tags(value: str) -> str:
    value = re.sub(r"<script[\s\S]*?</script>", " ", value, flags=re.I)
    value = re.sub(r"<style[\s\S]*?</style>", " ", value, flags=re.I)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    value = re.sub(r"</(p|div|li)>", "\n", value, flags=re.I)
    value = re.sub(r"<[^>]+>", " ", value)
    value = html.unescape(value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n\s+", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def strip_boilerplate(value: str) -> str:
    value = re.sub(r"📖\s*\[\s*智能识别\s*\][\s\S]*$", " ", value, flags=re.I)
    value = re.sub(r"以下图片是本站的(?:支付宝|微信)?(?:办理收款工具码|红包实体码)[\s\S]*$", " ", value, flags=re.I)
    value = value.replace("智能AI助手为您解析出二维码链接：", " ")
    value = re.sub(r"\b(?:https?://|mp://|tb\d?\.cn/)\S+", " ", value, flags=re.I)
    return value.strip()


def meta_description(page: str) -> str:
    match = re.search(r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']*)', page, flags=re.I)
    return html.unescape(match.group(1)).strip() if match else ""


def extract_article_body(page: str) -> tuple[str, str]:
    match = re.search(r'<div\b[^>]*class=["\'][^"\']*\barticle-content\b[^"\']*["\'][^>]*>', page, flags=re.I)
    if not match:
        return meta_description(page), "meta-description"
    after = page[match.end() :]
    end = len(after)
    for marker in (
        r'<div\b[^>]*id=["\']article-button',
        r'<div\b[^>]*class=["\'][^"\']*\bart-copyright\b',
        r'<div\b[^>]*class=["\'][^"\']*\bart-fujia\b',
        r'<div\b[^>]*id=["\']comment',
    ):
        idx = re.search(marker, after, flags=re.I)
        if idx and idx.start() < end:
            end = idx.start()
    body = strip_tags(after[:end])
    return (body or meta_description(page), "article-content" if body else "meta-description")


def cached_body(
    db: sqlite3.Connection,
    url: str,
    ttl_seconds: int,
    error_ttl_seconds: int = DEFAULT_BODY_ERROR_CACHE_TTL_SECONDS,
) -> tuple[str, str] | None:
    row = db.execute("select body, source, error, fetched_at from article_cache where url = ?", (url,)).fetchone()
    if not row:
        return None
    body, source, error, fetched_at = row
    age = int(time.time()) - int(fetched_at)
    if error:
        if age <= error_ttl_seconds:
            return "", "fetch-error-cached"
        return None
    if age > ttl_seconds:
        return None
    return str(body), str(source)


def fetch_body_cached(
    db: sqlite3.Connection,
    post: dict[str, Any],
    ttl_seconds: int = DEFAULT_BODY_CACHE_TTL_SECONDS,
    error_ttl_seconds: int = DEFAULT_BODY_ERROR_CACHE_TTL_SECONDS,
) -> tuple[str, str, bool]:
    url = full_url(post)
    cached = cached_body(db, url, ttl_seconds, error_ttl_seconds)
    if cached:
        return cached[0], cached[1], False
    try:
        page = fetch_text(url)
        body, source = extract_article_body(page)
        db.execute(
            "insert or replace into article_cache(url, body, source, error, fetched_at) values (?, ?, ?, ?, ?)",
            (url, body, source, None, int(time.time())),
        )
        db.commit()
        return body, source, True
    except Exception as exc:
        db.execute(
            "insert or replace into article_cache(url, body, source, error, fetched_at) values (?, ?, ?, ?, ?)",
            (url, "", "fetch-error", str(exc), int(time.time())),
        )
        db.commit()
        return "", "fetch-error", True


def ensure_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        """
        create table if not exists sent_posts (
          id text primary key,
          fingerprint text not null,
          title text not null,
          url text not null,
          tier integer not null,
          score integer not null,
          sent_at integer not null
        )
        """
    )
    db.execute("create index if not exists idx_sent_posts_sent_at on sent_posts(sent_at)")
    db.execute(
        """
        create table if not exists article_cache (
          url text primary key,
          body text not null,
          source text not null,
          error text,
          fetched_at integer not null
        )
        """
    )
    db.execute(
        """
        create table if not exists seen_posts (
          id text not null,
          rules_hash text not null,
          fingerprint text not null,
          title text not null,
          url text not null,
          decision_keep integer not null,
          tier integer not null,
          score integer not null,
          reason text not null,
          first_seen_at integer not null,
          last_seen_at integer not null,
          seen_count integer not null,
          primary key (id, rules_hash)
        )
        """
    )
    db.execute("create index if not exists idx_seen_posts_last_seen on seen_posts(last_seen_at)")
    db.execute(
        """
        create table if not exists activity_observations (
          exact_identity_fp text primary key,
          loose_identity_fp text not null,
          noise_fp text not null,
          first_post_id text not null,
          first_title text not null,
          first_url text not null,
          last_post_id text not null,
          last_title text not null,
          last_url text not null,
          identity_json text not null,
          pushed integer not null,
          highest_score integer not null,
          first_seen_at integer not null,
          last_seen_at integer not null,
          seen_count integer not null,
          material_change_json text not null
        )
        """
    )
    db.execute("create index if not exists idx_activity_observations_last_seen on activity_observations(last_seen_at)")
    db.execute("create index if not exists idx_activity_observations_loose on activity_observations(loose_identity_fp, last_seen_at)")
    db.commit()
    return db


def prune_runtime_state(
    db: sqlite3.Connection,
    rules: dict[str, Any],
    current_rules_hash: str,
    force: bool = False,
) -> dict[str, int]:
    global LAST_PRUNE_AT
    now = int(time.time())
    prune_every = max(60, env_int("XIANBAO_PRUNE_EVERY_SECONDS", DEFAULT_PRUNE_EVERY_SECONDS))
    if not force and now - LAST_PRUNE_AT < prune_every:
        return {}

    evaluation = rules.get("evaluation", {})
    body_ttl = int(evaluation.get("bodyCacheTtlSeconds", DEFAULT_BODY_CACHE_TTL_SECONDS))
    error_ttl = int(evaluation.get("bodyFetchErrorTtlSeconds", DEFAULT_BODY_ERROR_CACHE_TTL_SECONDS))
    seen_reject_ttl = max(86400, env_int("XIANBAO_SEEN_REJECT_RETENTION_SECONDS", DEFAULT_SEEN_REJECT_RETENTION_SECONDS))
    seen_keep_ttl = max(86400, env_int("XIANBAO_SEEN_KEEP_RETENTION_SECONDS", DEFAULT_SEEN_KEEP_RETENTION_SECONDS))
    sent_ttl = max(86400, env_int("XIANBAO_SENT_RETENTION_SECONDS", DEFAULT_SENT_RETENTION_SECONDS))

    stats: dict[str, int] = {}
    statements = (
        (
            "article_cache_body",
            "delete from article_cache where error is null and fetched_at < ?",
            (now - body_ttl,),
        ),
        (
            "article_cache_error",
            "delete from article_cache where error is not null and fetched_at < ?",
            (now - error_ttl,),
        ),
        (
            "seen_rejects",
            "delete from seen_posts where decision_keep = 0 and last_seen_at < ?",
            (now - seen_reject_ttl,),
        ),
        (
            "seen_keeps",
            "delete from seen_posts where decision_keep = 1 and last_seen_at < ?",
            (now - seen_keep_ttl,),
        ),
        (
            "sent_posts",
            "delete from sent_posts where sent_at < ?",
            (now - sent_ttl,),
        ),
        (
            "seen_stale_rules",
            "delete from seen_posts where rules_hash != ?",
            (current_rules_hash,),
        ),
    )
    for name, sql, params in statements:
        cursor = db.execute(sql, params)
        stats[name] = int(cursor.rowcount if cursor.rowcount is not None else 0)
    db.commit()
    LAST_PRUNE_AT = now
    return stats


def full_url(post: dict[str, Any]) -> str:
    url = str(post.get("url") or post.get("href") or post.get("path") or "")
    if url.startswith("http"):
        return url
    return BASE_URL + url


def text_of(post: dict[str, Any]) -> str:
    return strip_boilerplate(" ".join(str(post.get(key) or "") for key in ("title", "content", "body", "catename")))


def has_physical_product_signature(text: str) -> bool:
    if NON_PRODUCT_VALUE_RE.search(text) and not PRODUCT_PURCHASE_FLOW_RE.search(text):
        return False
    if has_quantity_product_purchase_signal(text):
        return True
    if shopping_price_noise_signature(text):
        return True
    priceish = bool(PRODUCT_PRICEISH_RE.search(text))
    if not priceish:
        return False
    return bool(PHYSICAL_PRODUCT_QUANTITY_RE.search(text) or PHYSICAL_PRODUCT_SHAPE_RE.search(text))


def has_quantity_product_purchase_signal(text: str) -> bool:
    normalized = normalize_cn_numbers(text)
    return bool(CONSUMABLE_PRODUCT_OBJECT_RE.search(normalized) and QUANTITY_PURCHASE_FLOW_RE.search(normalized))


def shopping_price_noise_signature(text: str) -> bool:
    if NON_PRODUCT_VALUE_RE.search(text) and not PRODUCT_PURCHASE_FLOW_RE.search(text):
        return False
    return bool(SHOPPING_PRICE_CONTEXT_RE.search(text) and (PRODUCT_PRICEISH_RE.search(text) or BARE_SHOPPING_PRICE_RE.search(text)))


def is_mobile_topup_text(text: str) -> bool:
    return bool(MOBILE_TOPUP_RE.search(text))


def is_non_mobile_utility_bill_text(text: str) -> bool:
    return bool(NON_MOBILE_UTILITY_BILL_RE.search(text)) and not is_mobile_topup_text(text)


def is_airline_mileage_reward_text(text: str) -> bool:
    if not AIRLINE_MILEAGE_RE.search(text):
        return False
    return bool(re.search(r"(可抽|抽|可领|领取|入口|mp://|小程序|公众号|直达|扫码|搜索|搜)", text, re.I))


def is_low_value_promo_task_text(text: str) -> bool:
    low_value_task = bool(
        LOW_VALUE_PROMO_TASK_RE.search(text)
        or PASSIVE_WATCH_MEMBERSHIP_TASK_RE.search(text)
        or MINI_PROGRAM_BANNER_TASK_RE.search(text)
    )
    if not low_value_task:
        return False
    return not bool(LOW_VALUE_TASK_CORE_PROTECT_RE.search(text))


def is_shopping_survey_reward_text(text: str) -> bool:
    value = str(text or "")
    if not (SHOPPING_SURVEY_TASK_RE.search(value) and SHOPPING_SURVEY_REWARD_RE.search(value)):
        return False
    if REUSABLE_FINANCIAL_VALUE_PATH_RE.search(value):
        return False
    if re.search(r"(人群|问卷|调研|调查|招募).{0,16}(礼金|红包|奖励|[0-9]+(?:\.[0-9]+)?\s*(?:元|块|r|R))", value, re.I):
        return True
    consumer_axis = bool(SHOPPING_SURVEY_CONSUMER_RE.search(value))
    proof_axis = bool(SHOPPING_SURVEY_PROOF_RE.search(value))
    if not (consumer_axis or proof_axis):
        return False
    return True


def physical_product_dominance(text: str, category: str = "", *, product_hint: bool = False) -> bool:
    combined = f"{text} {category}"
    product_context = bool(
        PHYSICAL_PRODUCT_CONTEXT_RE.search(combined)
        or has_physical_product_signature(combined)
        or shopping_price_noise_signature(combined)
        or product_hint
        or (PHYSICAL_PRODUCT_SHAPE_RE.search(combined) and PRODUCT_PURCHASE_FLOW_RE.search(combined))
    )
    if not product_context:
        return False
    has_purchase_or_price = bool(PRODUCT_PURCHASE_FLOW_RE.search(combined) or PRODUCT_SPEC_OR_PRICE_RE.search(combined) or shopping_price_noise_signature(combined))
    if not has_purchase_or_price:
        return False
    attached_reward = bool(ATTACHED_PRODUCT_REWARD_RE.search(combined))
    purchase_flow = bool(PRODUCT_PURCHASE_FLOW_RE.search(combined))
    product_purchase = purchase_flow or attached_reward or product_hint or bool(PRODUCT_SPEC_OR_PRICE_RE.search(combined))
    if not product_purchase:
        return False
    independent_path = bool(INDEPENDENT_VALUE_PATH_RE.search(combined))
    if independent_path and not (purchase_flow or attached_reward):
        return False
    return not independent_path


def load_terms(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def contains_any(text: str, terms: list[str]) -> bool:
    return any(term.lower() in text.lower() for term in terms)


def normalize_title(title: str) -> str:
    text = urllib.parse.unquote(title)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[0-9]+([.][0-9]+)?\s*(元|r|R|块|分|亓|毛|积分|份|次|点|日|号|月|k|K)?", " ", text)
    text = re.sub(r"(速度|速|手慢无|新毛|早毛|大毛|小毛|有水|刚中|去试试|看看|可领|领取|抽奖|红包|立减金|支付券)", " ", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text.lower()


def shingles(text: str, n: int = 2) -> set[str]:
    if len(text) <= n:
        return {text} if text else set()
    return {text[i : i + n] for i in range(len(text) - n + 1)}


CN_DIGIT = {
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def chinese_number_to_int(raw: str) -> int:
    text = str(raw or "").replace("兩", "两")
    if not text:
        return 0
    if text == "千":
        return 1000
    total = 0
    if "千" in text:
        head, text = text.split("千", 1)
        total += (CN_DIGIT.get(head, 1) if head else 1) * 1000
    if "百" in text:
        head, text = text.split("百", 1)
        total += (CN_DIGIT.get(head, 1) if head else 1) * 100
    if "十" in text:
        head, tail = text.split("十", 1)
        total += (CN_DIGIT.get(head, 1) if head else 1) * 10
        if tail:
            total += CN_DIGIT.get(tail, 0)
    elif text:
        total += CN_DIGIT.get(text, 0)
    return total


def normalize_cn_numbers(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        value = chinese_number_to_int(match.group(1))
        unit = match.group(2) or ""
        if unit == "千":
            value *= 1000
            unit = ""
        return f"{value}{unit}" if value else match.group(0)

    return re.sub(r"([一二两三四五六七八九十百千]+)(千|百|十|份|个|张|元|亓|块|点)?", repl, text)


ENTITY_ALIASES: dict[str, tuple[str, ...]] = {
    "招商银行": ("招行", "招商", "招商银行"),
    "工商银行": ("工行", "工商", "工商银行", "工银", "工银e生活", "工行服务"),
    "农业银行": ("农行", "老农", "农业银行", "中国农业银行"),
    "交通银行": ("交行", "交通银行", "买单吧"),
    "光大银行": ("光大", "光大银行", "阳光惠生活"),
    "中国银行": ("中行", "中国银行"),
    "建设银行": ("建行", "建设银行"),
    "邮储银行": ("邮储", "邮政储蓄"),
    "浦发银行": ("浦发", "浦大喜奔"),
    "广发银行": ("广发", "发现精彩"),
    "民生银行": ("民生", "全民生活"),
    "中信银行": ("中信", "动卡空间"),
    "华夏银行": ("华夏", "华彩生活"),
    "平安银行": ("平安", "口袋银行"),
    "京东": ("京东", "jd", "JD", "福利天天领", "猜大小"),
    "支付宝": ("支付宝", "zfb"),
    "微信": ("微信", "vx", "wx"),
    "云闪付": ("云闪付", "银联", "ysf", "YSF"),
}

PLATFORM_ALIASES: dict[str, tuple[str, ...]] = {
    "京东": ("京东", "jd", "JD", "京东APP", "京东 app"),
    "支付宝": ("支付宝", "zfb"),
    "微信": ("微信", "vx", "wx"),
    "云闪付": ("云闪付", "银联", "ysf", "YSF"),
    "买单吧": ("买单吧", "交行直播"),
    "工行服务小程序": ("工行服务", "工行服务小程序"),
    "工行APP": ("工行APP", "工商银行APP", "工银e生活"),
}

BENEFIT_ALIASES: dict[str, tuple[str, ...]] = {
    "红包": ("红包", "现金红包", "支付红包", "小红苞", "小荭苞"),
    "立减金": ("立减金", "立减斤", "立减巾", "ljj", "立减"),
    "支付券": ("支付券", "支付卷", "支付劵"),
    "刷卡金": ("刷卡金", "刷卡斤"),
    "还款券": ("还款券", "还款卷", "还款劵"),
    "贴金券": ("贴金券",),
    "福袋": ("福袋",),
    "i豆": ("i豆", "I豆", "积分", "豆子"),
    "猜大小红包": ("猜大小", "福利天天领632"),
    "消费季": ("消费季",),
    "云网券": ("云网券", "云网"),
    "转盘抽奖": ("转盘", "抽奖", "有水"),
    "会员": ("会员", "月卡", "季卡", "年卡", "VIP", "SVIP", "svip"),
    "加油优惠": ("加油", "中石化", "中石油", "石化", "油卡"),
    "云缴费": ("云缴费", "生活缴费", "电费", "水费", "燃气费"),
}

ENTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "福利天天领632": ("福利天天领632", "福利天天领 632"),
    "猜大小": ("猜大小",),
    "工行服务小程序": ("工行服务", "#小程序://工行服务", "小程序://工行服务"),
    "买单吧直播": ("交行直播", "买单吧直播", "直播福袋"),
    "云网": ("云网", "云网券"),
    "任务中心": ("任务中心", "任wu中心"),
    "摇一摇": ("摇一摇",),
}


def first_alias(text: str, aliases: dict[str, tuple[str, ...]]) -> str:
    lowered = text.lower()
    for canonical, values in aliases.items():
        for value in values:
            if value.lower() in lowered:
                return canonical
    return ""


def activity_amount_band(text: str) -> str:
    normalized = normalize_cn_numbers(text)
    amounts: list[str] = []
    for match in re.finditer(r"(?<![0-9])([0-9]+(?:\.[0-9]+)?)(?:\s*[~～到至-]\s*([0-9]+(?:\.[0-9]+)?))?\s*(?:元|亓|块|r|R|红包|立减金|支付券|刷卡金|还款券|份|个|张)?", normalized, re.I):
        raw = match.group(0).strip()
        if not raw:
            continue
        ctx = normalized[max(0, match.start() - 10) : min(len(normalized), match.end() + 12)]
        after = normalized[match.end() : min(len(normalized), match.end() + 2)]
        if re.match(r"\s*(点|时|号|日|月|年|岁)", after) and not re.search(r"(红包|券|减|份|个|张|元|亓|块)", raw):
            continue
        if re.search(r"福利天天领\s*$", normalized[max(0, match.start() - 8) : match.start()], re.I):
            continue
        if re.search(r"(红包|券|减|元|亓|块|份|个|张|i豆|积分|京豆|E卡|e卡|京东卡)", ctx, re.I):
            amounts.append(re.sub(r"\s+", "", raw))
    return ",".join(dict.fromkeys(amounts[:4]))


def activity_time_slot(text: str) -> str:
    normalized = normalize_cn_numbers(text)
    match = re.search(r"(?<![0-9])([0-2]?[0-9])\s*(?:点|时)(?:\s*([0-5]?[0-9])\s*分?)?", normalized)
    if match:
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    if re.search(r"零点|0点", normalized):
        return "00:00"
    return ""


def activity_period(text: str) -> str:
    if re.search(r"每天|每日|日限|一天一次|每日一次", text, re.I):
        return "daily"
    if re.search(r"每周|周[一二三四五六日天]|周末|周五|周六|周日", text, re.I):
        return "weekly"
    if re.search(r"每月|月限|月一次", text, re.I):
        return "monthly"
    if re.search(r"今天|今日|现在|当前|有水|补了|又有|还有", text, re.I):
        return "current"
    return ""


def activity_operation_path(text: str) -> str:
    markers: list[str] = []
    if re.search(r"搜|搜索", text, re.I):
        markers.append("搜索")
    if re.search(r"小程序|公众号", text, re.I):
        markers.append("小程序")
    if re.search(r"APP|app|页面|首页|任务中心|卡包|券包", text, re.I):
        markers.append("APP页面")
    if re.search(r"直播|福袋", text, re.I):
        markers.append("直播")
    if re.search(r"口令|复制|扫码|直达|链接", text, re.I):
        markers.append("口令/链接")
    return "+".join(unique_tags(markers))


def activity_identity_key(parts: list[str]) -> str:
    raw = "|".join(part for part in parts if part)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16] if raw else ""


def product_noise_fp(text: str) -> str:
    if not (
        physical_product_dominance(text)
        or re.search(r"(天猫U先|店铺入会礼|医疗|膨胀券|抽纸|纸巾|洗脸巾|湿巾|鞋|衣|裤|短袖|T恤|t恤|服饰|面料|故事机|学习机|童书|玩具|美妆|面霜|牛奶|猫超|饿了么|沪上阿姨|猫粮|狗粮|猫砂|吹风机)", text, re.I)
    ):
        return ""
    slim = normalize_title(text)
    return activity_identity_key(["noise", slim[:80]])


def normalize_activity_identity_text(text: str) -> str:
    text = normalize_cn_numbers(text)
    replacements = [
        (r"建[.\s·]*行", "建设银行"),
        (r"工[.\s·]*行", "工商银行"),
        (r"吱咐寳|支咐宝|支付宝|zfb", "支付宝"),
        (r"(?<![A-Za-z])ysf(?![A-Za-z])", "云闪付"),
        (r"收\s*[寛歀款]", "收款"),
        (r"发起收\s*[寛歀款]", "发起收款"),
        (r"ljj|立减J|立减j", "立减金"),
        (r"任wu", "任务"),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text, flags=re.I)
    return text


def normalized_semantic_text(text: str) -> str:
    return normalize_activity_identity_text(str(text or ""))


def has_payment_alias(text: str) -> bool:
    normalized = normalized_semantic_text(text)
    return bool(re.search(r"(支付宝|微信|vx|wx|云闪付|银联)", normalized, re.I))


def is_personal_transaction_notice_text(text: str) -> bool:
    normalized = normalized_semantic_text(text)
    has_account_notice = bool(
        re.search(r"(尊敬的用户|账户|钱包账户|尾号|银行卡|卡号|资金|交易|账务|流水|提现|到账|收款通知|付款通知)", normalized, re.I)
    )
    has_transaction_state = bool(
        re.search(r"(申请提现|提现.{0,16}(至|到|入|到账|预计)|预计.{0,24}到账|资金.{0,12}(到账|入账)|已到账|到账成功|请注意查收|交易成功|付款成功|收款成功)", normalized, re.I)
    )
    has_public_activity = bool(
        re.search(r"(活动|入口|路径|搜索|搜|报名|参加|领取|可领|能领|抽|转盘|任务|红包雨|立减金|支付券|刷卡金|还款券|优惠|有水|必中|保底)", normalized, re.I)
    )
    return has_account_notice and has_transaction_state and not has_public_activity


def strict_feedback_activity_group(text: str) -> dict[str, Any] | None:
    if re.search(r"建设银行", text) and re.search(r"(开宝箱|建融家园|去大学整点乐兹)", text, re.I):
        return {
            "entity": "建设银行",
            "platform": "建融家园",
            "benefit": "开宝箱立减金",
            "entry": "建融家园开宝箱",
            "period": "monthly",
            "time_slot": "",
            "amount_band": "",
            "operation_path": "APP页面",
            "strict_duplicate": True,
        }
    if re.search(r"支付宝", text, re.I) and re.search(r"工商银行|工行", text) and re.search(r"收款", text) and re.search(r"2000\s*[-减]\s*3\.?88", text):
        return {
            "entity": "工商银行",
            "platform": "支付宝",
            "benefit": "收款立减",
            "entry": "支付宝收款",
            "period": "",
            "time_slot": "",
            "amount_band": "2000-3.88",
            "operation_path": "APP页面",
            "strict_duplicate": True,
        }
    return None


def independent_activity_signal(text: str) -> bool:
    return bool(
        re.search(
            r"(银行|信用卡|xyk|xing.?用卡|云闪付|支付宝|微信支付|数币|数字人民币|还款|转账|收款|缴费|话费|加油|"
            r"立减金|刷卡金|还款券|支付券|无门槛|通用红包|现金红包|猜大小|福利天天领632|开通有礼|余额变动提醒|"
            r"小程序.{0,12}(工行服务|银行)|工行服务小程序)",
            text,
            re.I,
        )
    )


def extract_activity_identity(post: dict[str, Any] | DecisionContext) -> dict[str, Any]:
    if isinstance(post, DecisionContext):
        title = post.title
        body = post.body
        category = post.category
    else:
        title = str(post.get("title") or "")
        body = str(post.get("body") or post.get("content") or "")
        category = str(post.get("catename") or "")
    text = normalize_activity_identity_text("\n".join([title, body, category]))
    strict_group = strict_feedback_activity_group(text)
    entity = first_alias(text, ENTITY_ALIASES)
    platform = first_alias(text, PLATFORM_ALIASES)
    benefit = first_alias(text, BENEFIT_ALIASES)
    entry = first_alias(text, ENTRY_ALIASES)
    if strict_group:
        entity = str(strict_group.get("entity") or entity)
        platform = str(strict_group.get("platform") or platform)
        benefit = str(strict_group.get("benefit") or benefit)
        entry = str(strict_group.get("entry") or entry)
    if re.search(r"猜大小|福利天天领632", text, re.I):
        benefit = "猜大小红包"
        entry = entry or "福利天天领632"
    period = activity_period(text)
    time_slot = activity_time_slot(text)
    amount_band = activity_amount_band(text)
    operation_path = activity_operation_path(text)
    if strict_group:
        period = str(strict_group.get("period") or period)
        time_slot = str(strict_group.get("time_slot") or "")
        amount_band = str(strict_group.get("amount_band") or amount_band)
        operation_path = str(strict_group.get("operation_path") or operation_path)
    if benefit == "猜大小红包":
        entity = entity or "京东"
        platform = platform or "京东"
        entry = entry or "福利天天领632"
    if entry == "工行服务小程序":
        entity = entity or "工商银行"
        platform = platform or "工行服务小程序"
    if entry == "买单吧直播":
        entity = entity or "交通银行"
        platform = platform or "买单吧"

    exact_parts = ["exact", entity, platform, benefit, entry, period, amount_band]
    loose_parts = ["loose", entity, platform, benefit or entry]
    noise = product_noise_fp(text)
    product_noise_only = bool(noise) and physical_product_dominance(text, category)
    return {
        "entity": entity,
        "platform": platform,
        "benefit": benefit,
        "entry": entry,
        "period": period,
        "time_slot": time_slot,
        "amount_band": amount_band,
        "operation_path": operation_path,
        "exact_identity_fp": "" if product_noise_only else activity_identity_key(exact_parts),
        "loose_identity_fp": "" if product_noise_only else activity_identity_key(loose_parts),
        "noise_fp": noise,
        "strict_duplicate": bool(strict_group),
    }


def material_change_against(previous: dict[str, Any], current: dict[str, Any], text: str) -> tuple[bool, str]:
    reasons: list[str] = []
    prev_amount = str(previous.get("amount_band") or "")
    curr_amount = str(current.get("amount_band") or "")
    if curr_amount and curr_amount != prev_amount:
        reasons.append("新金额")
    if current.get("entry") and current.get("entry") != previous.get("entry"):
        reasons.append("新入口")
    if current.get("time_slot") and current.get("time_slot") != previous.get("time_slot"):
        reasons.append("新时间")
    if re.search(r"(补了|补货|恢复|又有|还有名额|名额恢复|新入口|新路径|新金额|更高)", text, re.I):
        reasons.append("补水/恢复")
    if re.search(r"(必中|保底)", text, re.I) and not re.search(r"(必中|保底)", json.dumps(previous, ensure_ascii=False), re.I):
        reasons.append("确定性提高")
    return bool(reasons), "、".join(unique_tags(reasons))


def activity_duplicate_check(db: sqlite3.Connection, post: dict[str, Any], window_seconds: int = 24 * 3600) -> dict[str, Any]:
    identity = extract_activity_identity(post)
    now = int(time.time())
    fp = identity.get("exact_identity_fp") or ""
    if not fp:
        return {"duplicate": False, "material_change": False, "identity": identity}
    row = db.execute(
        """
        select first_post_id, identity_json, last_seen_at
        from activity_observations
        where exact_identity_fp = ? and last_seen_at >= ?
        """,
        (fp, now - window_seconds),
    ).fetchone()
    match_kind = "exact"
    if not row and identity.get("loose_identity_fp"):
        row = db.execute(
            """
            select first_post_id, identity_json, last_seen_at
            from activity_observations
            where loose_identity_fp = ? and last_seen_at >= ?
            order by last_seen_at desc
            limit 1
            """,
            (identity.get("loose_identity_fp"), now - window_seconds),
        ).fetchone()
        match_kind = "loose"
    if not row:
        return {"duplicate": False, "material_change": False, "identity": identity}
    try:
        previous = json.loads(str(row[1] or "{}"))
    except json.JSONDecodeError:
        previous = {}
    if identity.get("strict_duplicate") or previous.get("strict_duplicate"):
        changed, reason = False, ""
    else:
        changed, reason = material_change_against(previous, identity, text_of(post))
    return {
        "duplicate": not changed,
        "duplicate_of": str(row[0]),
        "duplicate_group_id": fp,
        "duplicate_match_kind": match_kind,
        "material_change": changed,
        "material_change_reason_cn": reason,
        "identity": identity,
    }


def record_activity_observation(db: sqlite3.Connection, post: dict[str, Any], decision: Decision, *, pushed: bool = False) -> None:
    identity = extract_activity_identity(post)
    fp = str(identity.get("exact_identity_fp") or "")
    if not fp:
        return
    now = int(time.time())
    post_id = post_key(post)
    db.execute(
        """
        insert into activity_observations(
          exact_identity_fp, loose_identity_fp, noise_fp, first_post_id, first_title, first_url,
          last_post_id, last_title, last_url, identity_json, pushed, highest_score,
          first_seen_at, last_seen_at, seen_count, material_change_json
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
        on conflict(exact_identity_fp) do update set
          loose_identity_fp=excluded.loose_identity_fp,
          noise_fp=excluded.noise_fp,
          last_post_id=excluded.last_post_id,
          last_title=excluded.last_title,
          last_url=excluded.last_url,
          identity_json=excluded.identity_json,
          pushed=max(pushed, excluded.pushed),
          highest_score=max(highest_score, excluded.highest_score),
          last_seen_at=excluded.last_seen_at,
          seen_count=seen_count + 1,
          material_change_json=excluded.material_change_json
        """,
        (
            fp,
            str(identity.get("loose_identity_fp") or ""),
            str(identity.get("noise_fp") or ""),
            post_id,
            str(post.get("title") or ""),
            full_url(post),
            post_id,
            str(post.get("title") or ""),
            full_url(post),
            json.dumps(identity, ensure_ascii=False, sort_keys=True),
            1 if pushed else 0,
            int(decision.score or 0),
            now,
            now,
            json.dumps({}, ensure_ascii=False),
        ),
    )
    db.commit()




def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()




def amount_value(text: str) -> float:
    values: list[float] = []
    cleaned = re.sub(r"[¥￥]\s*([0-9]+(?:\.[0-9]+)?)", r"\1元", text).replace("¥", "元").replace("￥", "元")
    reward_noun = r"(?:通用)?(?:立减金|立减斤|立减巾|立减䋮|立减J|ljj|红包|支付券|支付卷|支付劵|刷卡金|刷卡斤|还款券|还款卷|消费金|消费券|提现券|话费券|电费券|水费券|燃气费券|通勤券|乘车券|公交券|地铁券|E卡|e卡|京东卡|油卡|数币|数字人民币)"
    face_value_noun = re.compile(r"^(话费|油卡|E卡|e卡|京东卡|数币|数字人民币|元|亓|块|r|R)$", re.I)
    reward_ctx = re.compile(r"(领|领取|领到|可领|得|获得|拿|拿到|必得|送|赠|赠送|返|反送|补|中|中奖|中了|抽中|抽到|刚中|白嫖|免费|免单|到账|奖励|撸|可撸|大毛|小毛|羊毛|权益|券到账|卡券到账|立减|减了|返现|现金|兑换)", re.I)
    spend_ctx = re.compile(r"(满|消费|单笔|订单|实付|支付|付款|还款|转账|充值|充|冲|买|购|购买|面额|成本|价格|原价|付|跳|选择|选|下单|收银台|抵扣前)", re.I)
    prize_list_ctx = re.compile(r"(一等奖|二等奖|三等奖|四等奖|五等奖|六等奖|七等奖|八等奖|九等奖|十等奖|最高|奖品|共[0-9一二两三四五六七八九十百千万]+份|名额[0-9一二两三四五六七八九十百千万]+)", re.I)
    actual_win_ctx = re.compile(r"(刚中|中了|抽中|中到|领到|到账|实得|保底|必中|领了|拿到)", re.I)

    def around(index: int, length: int, before: int = 16, after: int = 28) -> str:
        return cleaned[max(0, index - before) : min(len(cleaned), index + length + after)]

    def cn_number_value(raw: str) -> float:
        text = str(raw or "").replace("兩", "两")
        if not text:
            return 0
        digit = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        total = 0
        if "百" in text:
            head, text = text.split("百", 1)
            total += (digit.get(head, 1) if head else 1) * 100
        if "十" in text:
            head, tail = text.split("十", 1)
            total += (digit.get(head, 1) if head else 1) * 10
            if tail:
                total += digit.get(tail, 0)
        elif text:
            total += digit.get(text, 0)
        return float(total)

    def countable_reward_amount(raw: str, noun: str, ctx: str) -> bool:
        try:
            value = float(raw)
        except ValueError:
            return False
        if re.search(rf"{re.escape(raw)}\s*(?:号|日|点|时|月|个|张|份|次|万|[wW])", ctx, re.I):
            return False
        if re.search(rf"{re.escape(raw)}\s*(?:元|亓|块|r|R)?\s*[-减]\s*[0-9]", ctx, re.I):
            return False
        if prize_list_ctx.search(ctx) and not actual_win_ctx.search(ctx):
            return False
        if not face_value_noun.search(noun):
            return True
        if re.search(rf"{re.escape(raw)}\s*(?:以上|起|内|以内|以下)", ctx, re.I):
            return False
        if re.search(rf"(?:满|消费满|单笔满|还款|转账|支付|付款|充值|充|冲|买|购|购买|面额|实付|付)\s*{re.escape(raw)}", ctx, re.I):
            return False
        if re.search(rf"{re.escape(raw)}\s*(?:元|亓|块|r|R)?\s*(?:买入|买|购|购买|支付|付款|实付|付|转入|存入|充值|充|冲)", ctx, re.I):
            return False
        if value >= 30 and spend_ctx.search(ctx) and not reward_ctx.search(ctx):
            return False
        if value >= 30 and re.search(r"[-减]\s*[0-9]", ctx) and not reward_ctx.search(ctx):
            return False
        return True

    def push(value: float, raw: str = "") -> None:
        if raw and len(re.sub(r"\D", "", raw)) > 6:
            return
        if 0 < value <= 500:
            values.append(round(value, 2))

    for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(?:元|亓|块|r|R)?\s*(?:充|冲)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|亓|块|r|R)?\s*(?:话费|油卡|电费|水费|燃气费|E卡|e卡|京东卡)?", cleaned, re.I):
        paid = float(match.group(1))
        face = float(match.group(2))
        if face > paid:
            push(face - paid, match.group(0))
        cleaned = cleaned.replace(match.group(0), " ")
    for match in re.finditer(r"(?<![0-9])([1-9][0-9]?|[1-9](?:\.[0-9]+)?)\s*折\s*(?:充|冲|购|买)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|亓|块|r|R)?\s*(?:话费|油卡|电费|水费|燃气费|E卡|e卡|京东卡)?", cleaned, re.I):
        discount = float(match.group(1))
        if 10 < discount < 100:
            discount /= 10
        face = float(match.group(2))
        if 0 < discount < 10:
            push(face * (1 - discount / 10), match.group(0))
        cleaned = cleaned.replace(match.group(0), " ")
    for match in re.finditer(r"0\s*(?:元|亓|块|r|R)?\s*(?:充|冲)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|亓|块|r|R)?\s*(?:话费|油卡|电费|水费|燃气费|E卡|e卡|京东卡)?", cleaned, re.I):
        push(float(match.group(1)), match.group(0))
        cleaned = cleaned.replace(match.group(0), " ")

    for match in re.finditer(rf"([0-9]+)\s*张\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|亓|块|r|R)?\s*(?:的)?\s*({reward_noun})", cleaned, re.I):
        count = float(match.group(1))
        each = float(match.group(2))
        if 1 < count <= 50:
            push(count * each, match.group(0))
        cleaned = cleaned.replace(match.group(0), " ")
    for match in re.finditer(rf"([0-9]+(?:\.[0-9]+)?)\s*(?:元|亓|块|r|R)?\s*[=＝]\s*([0-9]+)\s*张\s*(?:{reward_noun})?", cleaned, re.I):
        each = float(match.group(1))
        count = float(match.group(2))
        if 1 < count <= 50:
            push(count * each, match.group(0))
        cleaned = cleaned.replace(match.group(0), " ")

    for match in re.finditer(rf"([0-9]+(?:\.[0-9]+)?)\s*分\s*(?:起)?购\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|亓|块|r|R)?\s*(?:微信|支付宝|{reward_noun})?", cleaned, re.I):
        push(float(match.group(2)), match.group(0))
        cleaned = cleaned.replace(match.group(0), " ")

    for match in re.finditer(rf"([0-9]+(?:\.[0-9]+)?)[ \t]*(?:的)?[ \t]*({reward_noun})", cleaned, re.I):
        if countable_reward_amount(match.group(1), match.group(2), around(match.start(), len(match.group(0)))):
            push(float(match.group(1)), match.group(0))
    for match in re.finditer(rf"({reward_noun})[ \t]*([0-9]+(?:\.[0-9]+)?)", cleaned, re.I):
        if countable_reward_amount(match.group(2), match.group(1), around(match.start(), len(match.group(0)))):
            push(float(match.group(2)), match.group(0))
    for match in re.finditer(r"([1-9][0-9](?:\.[0-9]+)?)\s*(?:元|亓|块|r|R)?\s*(?:开户|证券开户|开通|开过户).{0,8}(?:红包|立减金|立减|支付券|微信立减)", cleaned, re.I):
        push(float(match.group(1)), match.group(0))
    for match in re.finditer(r"(?:开户|证券开户|开通|开过户).{0,8}([1-9][0-9](?:\.[0-9]+)?)\s*(?:元|亓|块|r|R)?\s*(?:红包|立减金|立减|支付券|微信立减)", cleaned, re.I):
        push(float(match.group(1)), match.group(0))
    for match in re.finditer(r"(?:领满|领了|领取|领到|领|拿|拿到|可领|得|获得|返|补|中|中奖|中了|抽中|抽到|撸|可撸|保底|润|赚|净赚|省|收益|到手)\s*([0-9]+(?:\.[0-9]+)?)\s*(?:元|亓|块|r|R|毛|润)?", cleaned, re.I):
        push(float(match.group(1)), match.group(0))
    for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*[~～]\s*([0-9]+(?:\.[0-9]+)?)", cleaned, re.I):
        ctx = around(match.start(), len(match.group(0)))
        if re.search(r"(红包|立减|刷卡金|支付券|还款券|福袋|抽|中|保底|最高|还款|付款|缴费|话费|数币|数字人民币|银行|信用卡|xyk)", ctx, re.I):
            push(float(match.group(2)), match.group(0))
    for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)(?:\s*(?:或|/|、|~|～)\s*([0-9]+(?:\.[0-9]+)?))?\s*(?:大毛|小毛)", cleaned, re.I):
        push(float(match.group(2) or match.group(1)), match.group(0))
    for match in re.finditer(r"(?:大毛|小毛|毛)\s*([0-9]+(?:\.[0-9]+)?)", cleaned, re.I):
        push(float(match.group(1)), match.group(0))
    for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(?:润|收益|到手)", cleaned, re.I):
        push(float(match.group(1)), match.group(0))

    for match in re.finditer(r"(一|二|两|三|四|五|六|七|八|九|十|[一二两三四五六七八九十百]+)\s*(元|亓|块|毛|分|立减金|支付券|红包|刷卡金|还款券|E卡|e卡|京东卡|话费|油卡|电费券|水费券|燃气费券|通勤券|数币|数字人民币)", cleaned, re.I):
        value = cn_number_value(match.group(1))
        if value <= 0:
            continue
        unit = match.group(2)
        if unit == "毛":
            value /= 10
        elif unit == "分":
            value /= 100
        ctx = around(match.start(), len(match.group(0)))
        if prize_list_ctx.search(ctx) and not actual_win_ctx.search(ctx):
            continue
        push(value, match.group(0))
    for match in re.finditer(r"(?:领满|领了|领取|领到|领|拿|拿到|可领|得|获得|返|补|中|中奖|中了|抽中|抽到|撸|可撸|保底|润|赚|净赚|省|收益|到手)\s*([一二两三四五六七八九十百]+)\s*(?:元|亓|块|r|R|毛|润)?", cleaned, re.I):
        value = cn_number_value(match.group(1))
        if value > 0:
            push(value, match.group(0))

    for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(元|亓|块|r|R|毛|分|红包|立减金|支付券|刷卡金|还款券|E卡|e卡|京东卡|话费|油卡|电费券|水费券|燃气费券|通勤券)", cleaned, re.I):
        try:
            value = float(match.group(1))
        except ValueError:
            continue
        unit = match.group(2)
        if unit == "毛":
            value /= 10
        elif unit == "分":
            value /= 100
        if not countable_reward_amount(match.group(1), unit, around(match.start(), len(match.group(0)))):
            continue
        push(value, match.group(0))
    for match in re.finditer(r"(?<![0-9-])([0-9]+(?:\.[0-9]+)?)[-减]([0-9]+(?:\.[0-9]+)?)(?!-[0-9])", cleaned):
        left = float(match.group(1))
        right = float(match.group(2))
        start, end = match.span()
        ctx = cleaned[max(0, start - 10) : min(len(cleaned), end + 16)]
        if "减" not in match.group(0) and re.search(r"[0-9]+(?:\.[0-9]+)?\s*-\s*[0-9]+(?:\.[0-9]+)?\s*(?:年|岁|个月|月|日|号|点|时|代|款|%)", ctx, re.I):
            continue
        if right > 50 and left > right and re.search(r"润|收益|回本|赚|净|省|到手", ctx):
            push(left - right, match.group(0))
        elif "减" in match.group(0) or re.search(r"满|立减|随机减|支付|还款|刷卡|红包|券|优惠|返|省", ctx) or left >= 100 or right <= 50:
            push(right, match.group(0))
        elif left > right and re.search(r"润|收益|回本|赚|净|省|到手", ctx):
            push(left - right, match.group(0))
    for match in re.finditer(r"(?:立减|随机减|优惠|减免|[-－])\s*([0-9]+(?:\.[0-9]+)?)", cleaned, re.I):
        ctx = around(match.start(), len(match.group(0)))
        if float(match.group(1)) <= 50 and re.search(r"(信用卡|信.?用卡|xyk|储蓄卡|借记卡|银行卡|云闪付|支付宝|微信支付|京东支付|美团支付|银行|农行|建行|工行|中行|招行|交行|邮储|浦发|光大|广发|民生|中信|话费|电费|水费|燃气费|油卡|E卡|e卡|京东卡|数币|数字人民币|还款|转账|缴费|付款|收款)", ctx, re.I):
            push(float(match.group(1)), match.group(0))
    for match in re.finditer(r"([0-9]+(?:\.[0-9]+)?)\s*(?:元|亓|块|r|R)?\s*(?:买|购)\s*([0-9]+(?:\.[0-9]+)?)", cleaned, re.I):
        paid = float(match.group(1))
        face = float(match.group(2))
        if face > paid:
            push(face - paid, match.group(0))
    return max(values) if values else 0.0


def build_decision_context(post: dict[str, Any]) -> DecisionContext:
    title = str(post.get("title") or "")
    body = strip_boilerplate(str(post.get("body") or post.get("content") or ""))
    text = text_of(post)
    body_confirmed = bool(str(post.get("body") or "").strip())
    return DecisionContext(
        title=title,
        body=body,
        category=str(post.get("catename") or ""),
        text=text,
        content_source="body" if body_confirmed else "list",
        body_confirmed=body_confirmed,
        body_source=str(post.get("body_source") or ("body" if body_confirmed else "list")),
    )


def unique_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def extract_evidence(ctx: DecisionContext, patterns: dict[str, re.Pattern[str] | None] | None = None) -> Evidence:
    text = ctx.text
    patterns = patterns or {}
    activity_identity = extract_activity_identity(ctx)
    entity_types: list[str] = []
    benefit_types: list[str] = []
    operation_path: list[str] = []
    risk_friction: list[str] = []

    if rx(patterns, "bankStrong", text) or rx(patterns, "bank", text):
        entity_types.append("bank")
    if rx(patterns, "payment", text):
        entity_types.append("payment")
    if re.search(r"(京东|jd|JD|京东APP|京东 app|京东金融)", text, re.I):
        entity_types.append("jd")
    if re.search(r"(美团|大众点评|麦当劳|肯德基|瑞幸|库迪|餐饮|美食|外卖|电影)", text, re.I):
        entity_types.append("dining_entertainment")
    if has_payment_alias(text):
        entity_types.append("payment")
    if re.search(r"(南航|中国南方航空|南方航空|航司|航空)", text, re.I):
        entity_types.append("airline")

    if re.search(r"(红包|现金红包|通用红包|支付红包|微信红包|支付宝红包|红包雨)", text, re.I):
        benefit_types.append("red_packet")
    if re.search(r"(立减金|立减|ljj|刷卡金|支付券|还款券|消费券|优惠券|无门槛|随机减|满减)", text, re.I):
        benefit_types.append("coupon")
    if re.search(r"(会员|月卡|季卡|年卡|VIP|svip|SVIP)", text, re.I):
        benefit_types.append("membership")
    if re.search(r"(话费|电费|水费|燃气费|油卡|E卡|e卡|京东卡|数币|数字人民币|还款|转账|缴费|加油)", text, re.I):
        benefit_types.append("utility")
    if re.search(r"(积分|i豆|I豆|京豆|金豆|豆子)", text, re.I):
        benefit_types.append("points")
    if re.search(r"(里程|明珠)", text, re.I):
        benefit_types.append("mileage")
    if re.search(r"(转盘|抽奖|抽取|猜大小|有水|必中|保底|中签|中奖)", text, re.I):
        benefit_types.append("lottery")
    if re.search(r"(返现|现金|提现|可提现|收益到账|现金到账)", text, re.I):
        benefit_types.append("cash_like")

    if re.search(r"(APP|app|小程序|公众号|页面|横幅|首页|任务中心|卡包|券包)", text, re.I):
        operation_path.append("有入口或页面")
    if re.search(r"(搜|搜索|打开|进入|点开|点击|扫码|直达|路径|口令)", text, re.I):
        operation_path.append("给了操作路径")
    if re.search(r"(开通|报名|参加|领取|可领|能领|抽|转盘|猜大小|任务|签到|兑换|膨胀)", text, re.I):
        operation_path.append("有可执行动作")

    if re.search(r"(现在|今天|今日|有水|速度|速|可领|能领|还有|补了|又有|必中|保底|刚中|刚领|秒到)", text, re.I):
        timeliness_status = "current"
    elif re.search(r"(明天|后天|预约|定闹钟|几点|10点|零点|卡点)", text, re.I):
        timeliness_status = "scheduled"
    elif re.search(r"(过期|失效|结束|没了|无了|抢光|凉了|黄了|反撸|不到账|失败|翻车)", text, re.I):
        timeliness_status = "expired_or_negative"
    else:
        timeliness_status = "unknown"

    if re.search(r"(受邀|看脸|黑号|风控|风险测评|测评|资产|实名|邀请码|多号|小号)", text, re.I):
        risk_friction.append("资格或风控不确定")
    if re.search(r"(答题|问卷|调研|下载|试玩|看视频|浏览|签到|积分任务)", text, re.I):
        risk_friction.append("任务较重")
    if re.search(r"(怎么|咋|吗|么|有没有|求助|不到账|失败|翻车|投诉|吐槽|能不能|是不是)", text, re.I):
        risk_friction.append("疑问或复盘口吻")
    content_for_product = f"{ctx.title}\n{ctx.body}"
    if physical_product_dominance(content_for_product, ctx.category) or re.search(r"(商品|实物|牛奶|纸巾|食品|买实物|抽纸|洗脸巾|洗衣液|鞋|衣|裤|短袖|T恤|t恤|故事机|玩具|美妆|面霜)", content_for_product, re.I):
        risk_friction.append("商品消费主导")

    if ctx.body_confirmed:
        source_trust = "body-confirmed"
    elif len(re.sub(r"\s+", "", ctx.text)) >= 60:
        source_trust = "list-rich"
    else:
        source_trust = "list-thin"

    return Evidence(
        entity_types=unique_tags(entity_types),
        benefit_types=unique_tags(benefit_types),
        amount=amount_value(text),
        probability="certain" if re.search(r"(必中|必得|保底|直接到账|秒到)", text, re.I) else "lottery" if re.search(r"(抽|转盘|猜大小|随机|有水|看脸|概率)", text, re.I) else "unknown",
        operation_path=unique_tags(operation_path),
        timeliness_status=timeliness_status,
        risk_friction=unique_tags(risk_friction),
        source_trust=source_trust,
        body_confirmed=ctx.body_confirmed,
        activity_identity=activity_identity,
    )


def review_label_from_evidence(evidence: Evidence) -> str:
    parts: list[str] = []
    if "bank" in evidence.entity_types or "payment" in evidence.entity_types:
        parts.append("银行/支付")
    elif "airline" in evidence.entity_types:
        parts.append("航司")
    elif "jd" in evidence.entity_types:
        parts.append("京东")
    elif "dining_entertainment" in evidence.entity_types:
        parts.append("餐饮/娱乐")
    else:
        parts.append("疑似福利")

    if "red_packet" in evidence.benefit_types or "coupon" in evidence.benefit_types:
        parts.append("红包/券")
    elif "points" in evidence.benefit_types:
        parts.append("积分/I豆")
    elif "membership" in evidence.benefit_types:
        parts.append("会员")
    elif "mileage" in evidence.benefit_types:
        parts.append("里程权益")
    elif "lottery" in evidence.benefit_types:
        parts.append("抽奖/转盘")
    elif "utility" in evidence.benefit_types:
        parts.append("话费/缴费等权益")

    if evidence.probability == "certain":
        parts.append("有必中/保底口径")
    elif evidence.probability == "lottery":
        parts.append("抽奖/转盘口径")
    if evidence.operation_path:
        parts.append("有操作入口")
    if evidence.source_trust == "list-thin":
        parts.append("列表信息偏短")
    return "，".join(unique_tags(parts)) + "，建议人工确认"


def gray_review_decision(
    score: int,
    reason: str,
    source: str,
    needs_body_fetch: bool,
    evidence: Evidence,
    *,
    status: str | None = None,
) -> Decision:
    review_status = status or ("高分待复核" if score >= 65 else "疑似价值")
    return Decision(
        False,
        0,
        score,
        reason,
        value_label="疑似有金钱价值，进入日报复盘",
        content_source=source,
        needs_body_fetch=needs_body_fetch or not evidence.body_confirmed,
        review_candidate=True,
        review_status=review_status,
        review_label=review_label_from_evidence(evidence),
        evidence=evidence.to_dict(),
    )


def fingerprint(post: dict[str, Any]) -> str:
    title = normalize_title(str(post.get("title") or ""))
    cat = str(post.get("catename") or "").split("|")[0]
    key = f"{cat}:{title}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def seen_fingerprint(post: dict[str, Any]) -> str:
    key = "\n".join(
        [
            str(post.get("catename") or ""),
            normalize_title(str(post.get("title") or "")),
            str(post.get("content") or post.get("desc") or post.get("description") or ""),
            full_url(post),
        ]
    )
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def post_key(post: dict[str, Any]) -> str:
    return str(post.get("id") or post.get("url") or full_url(post))


def decision_evaluation_config(rules: dict[str, Any]) -> dict[str, Any]:
    evaluation = rules.get("evaluation", {})
    if not isinstance(evaluation, dict):
        return {}
    runtime_only = {
        "bodyCacheTtlSeconds",
        "bodyFetchErrorTtlSeconds",
        "maxBodyFetchesPerPoll",
        "needsBodyFetchRows",
    }
    return {key: value for key, value in evaluation.items() if key not in runtime_only}


def rules_hash(rules: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "positive": rules.get("positive", {}),
            "negative": rules.get("negative", {}),
            "regionPolicy": rules.get("regionPolicy", {}),
            "websiteSide": rules.get("websiteSide", {}),
            "evaluation": decision_evaluation_config(rules),
            "decisionRulesetVersion": DECISION_RULESET_VERSION,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def cached_seen_decision(db: sqlite3.Connection, post: dict[str, Any], rules_key: str, fp: str) -> tuple[int, int, int] | None:
    row = db.execute(
        "select decision_keep, tier, score, fingerprint from seen_posts where id = ? and rules_hash = ?",
        (post_key(post), rules_key),
    ).fetchone()
    if not row or row[3] != fp:
        return None
    return int(row[0]), int(row[1]), int(row[2])


def seen_skip_reason(
    db: sqlite3.Connection,
    post: dict[str, Any],
    rules_key: str,
    fp: str,
    send_tier2: bool,
    send_tier3: bool,
    tier3_min_score: int,
) -> str:
    cached = cached_seen_decision(db, post, rules_key, fp)
    if not cached:
        return ""
    decision_keep, tier, score = cached
    if not decision_keep:
        return "seen-reject"
    if tier == 2 and send_tier2:
        return ""
    if tier == 3 and send_tier3 and score >= tier3_min_score:
        return ""
    if tier > 1:
        return "seen-keep-nonpush"
    return ""


def should_push_decision(decision: Decision, send_tier2: bool, send_tier3: bool, tier3_min_score: int) -> bool:
    if not decision.keep or decision.duplicate:
        return False
    if decision.tier == 1:
        return True
    if decision.tier == 2:
        return send_tier2
    if decision.tier == 3:
        return send_tier3 and decision.score >= tier3_min_score
    return False


def mark_seen_post(db: sqlite3.Connection, post: dict[str, Any], decision: Decision, rules_key: str, fp: str) -> None:
    now = int(time.time())
    db.execute(
        """
        insert into seen_posts(
          id, rules_hash, fingerprint, title, url, decision_keep, tier, score, reason,
          first_seen_at, last_seen_at, seen_count
        )
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        on conflict(id, rules_hash) do update set
          fingerprint=excluded.fingerprint,
          title=excluded.title,
          url=excluded.url,
          decision_keep=excluded.decision_keep,
          tier=excluded.tier,
          score=excluded.score,
          reason=excluded.reason,
          last_seen_at=excluded.last_seen_at,
          seen_count=seen_count + 1
        """,
        (
            post_key(post),
            rules_key,
            fp,
            str(post.get("title") or ""),
            full_url(post),
            1 if decision.keep else 0,
            decision.tier,
            decision.score,
            decision.reason,
            now,
            now,
        ),
    )
    db.commit()


def near_duplicate(db: sqlite3.Connection, post: dict[str, Any], fp: str, window_seconds: int = 6 * 3600) -> tuple[bool, str | None]:
    post_id = post_key(post)
    row = db.execute("select id from sent_posts where id = ?", (post_id,)).fetchone()
    if row:
        return True, row[0]

    now = int(time.time())
    title_shingles = shingles(normalize_title(str(post.get("title") or "")))
    if not title_shingles:
        return False, None

    rows = db.execute(
        "select id, title, fingerprint from sent_posts where sent_at >= ?",
        (now - window_seconds,),
    ).fetchall()
    for old_id, old_title, old_fp in rows:
        if old_fp == fp:
            return True, old_id
        old_shingles = shingles(normalize_title(old_title))
        if not old_shingles:
            continue
        overlap = len(title_shingles & old_shingles) / len(title_shingles | old_shingles)
        if overlap >= 0.62:
            return True, old_id
    return False, None


def eligibility_reject_reason(text: str) -> str:
    if UNSUPPORTED_ELIGIBILITY_RE.search(text):
        return "unsupported-bank"
    return ""


def region_ok(text: str, patterns: dict[str, re.Pattern[str] | None]) -> tuple[bool, str]:
    if re.search(r"(北京|帝都|北京朝阳|朝阳区|北分|北京分行|北京地区|北京农商|(?<![0-9])010(?![0-9]))", text):
        return True, "beijing"
    if re.search(r"(黑龙江|哈尔滨|大庆|齐齐哈尔|牡丹江|佳木斯|绥化|鸡西|鹤岗|双鸭山|七台河|黑河|伊春|大兴安岭|龙江)", text):
        heilongjiang = patterns.get("heilongjiangAllowOnlyIf")
        if heilongjiang and heilongjiang.search(text):
            return True, "heilongjiang-abc"
        return False, "heilongjiang-non-abc"
    if NEIMENG_REGION_RE.search(text):
        return False, "other-region"
    if LOCAL_AREA_CODE_REGION_RE.search(text) and LOCAL_OFFLINE_PARTICIPATION_RE.search(text):
        return False, "other-region"
    bad_region = patterns.get("badRegion")
    if bad_region and bad_region.search(text):
        return False, "other-region"
    return True, "national-or-unspecified"


def classify(post: dict[str, Any], rules: dict[str, Any], allow_terms: list[str], block_terms: list[str]) -> Decision:
    ctx = build_decision_context(post)
    text = ctx.text
    cat = ctx.category
    title = ctx.title
    content_text = ctx.body
    title_and_body = f"{title}\n{content_text}"
    compact_content_len = len(re.sub(r"\s+", "", content_text))
    meaningful_content = compact_content_len >= 8 and not re.fullmatch(r"(rt|如题|如图|看图|图|1|一|无|空)", content_text.strip(), re.I)
    body_present = ctx.body_confirmed

    patterns = {
        "bankStrong": compile_re(rules["positive"].get("bankStrong")),
        "bank": compile_re(rules["positive"].get("bank")),
        "payment": compile_re(rules["positive"].get("payment")),
        "financialBenefit": compile_re(rules["positive"].get("financialBenefit")),
        "concreteReward": compile_re(rules["positive"].get("concreteReward")),
        "welfare": compile_re(rules["positive"].get("welfare")),
        "action": compile_re(rules["positive"].get("action")),
        "dailyBankDigest": compile_re(rules["positive"].get("dailyBankDigest")),
        "discussion": compile_re(rules["negative"].get("discussion")),
        "titleDiscussion": compile_re(rules["negative"].get("titleDiscussion")),
        "hardRisk": compile_re(rules["negative"].get("hardRisk")),
        "softRisk": compile_re(rules["negative"].get("softRisk")),
        "risk": compile_re(rules["negative"].get("risk")),
        "expiredOrNegative": compile_re(rules["negative"].get("expiredOrNegative")),
        "productText": compile_re(rules["negative"].get("productText")),
        "productPrimary": compile_re(rules["negative"].get("productPrimary")),
        "productCategory": compile_re(rules["negative"].get("productCategory")),
        "softProductCategory": compile_re(rules["negative"].get("softProductCategory")),
        "lowQualitySource": compile_re(rules["negative"].get("lowQualitySource")),
        "badRegion": compile_re_union(
            rules["regionPolicy"].get("rejectOtherRegions"),
            rules["regionPolicy"].get("rejectOtherRegionsExtra"),
        ),
        "heilongjiangAllowOnlyIf": compile_re(rules["regionPolicy"].get("heilongjiangAllowOnlyIf")),
    }
    evidence = extract_evidence(ctx, patterns)

    if contains_any(text, block_terms):
        return Decision(False, 0, 0, "manual-block", content_source="body" if body_present else "list")

    eligibility_reason = eligibility_reject_reason(text)
    if eligibility_reason:
        return Decision(False, 0, 0, f"eligibility:{eligibility_reason}", value_label="不可参与", content_source="body" if body_present else "list")

    ok, region_reason = region_ok(text, patterns)
    if not ok:
        return Decision(False, 0, 0, f"region:{region_reason}", value_label="地域不符合", content_source="body" if body_present else "list")

    mobile_topup_profile = mobile_topup_discount_profile(text)
    if mobile_topup_profile.parsed and (mobile_topup_profile.poor_discount or mobile_topup_profile.no_discount):
        return Decision(
            False,
            0,
            0,
            "mobile-topup-poor-discount-noise",
            value_label="话费充值折扣高于9折或无实际优惠",
            content_source="body" if body_present else "list",
        )

    has_strong_bank = rx(patterns, "bankStrong", text)
    has_bank = rx(patterns, "bank", text)
    has_payment = rx(patterns, "payment", text) or has_payment_alias(text)
    has_financial_benefit = rx(patterns, "financialBenefit", text)
    has_concrete_reward = rx(patterns, "concreteReward", text)
    has_welfare = rx(patterns, "welfare", text)
    has_action = rx(patterns, "action", text)
    daily_bank_digest = rx(patterns, "dailyBankDigest", title)
    product_category = rx(patterns, "productCategory", cat)
    soft_product_category = rx(patterns, "softProductCategory", cat)
    product_text = rx(patterns, "productText", text)
    product_primary = rx(patterns, "productPrimary", title)
    physical_product_dominant = physical_product_dominance(
        text,
        cat,
        product_hint=bool(product_category or product_text or product_primary),
    )
    low_quality = rx(patterns, "lowQualitySource", cat)
    discussion = rx(patterns, "discussion", text)
    title_discussion = rx(patterns, "titleDiscussion", title)
    hard_risk = rx(patterns, "hardRisk", text)
    soft_risk = rx(patterns, "softRisk", text)
    risk = rx(patterns, "risk", text)
    expired_or_negative = rx(patterns, "expiredOrNegative", text)
    manual_allow = contains_any(text, allow_terms)
    direct_financial = has_financial_benefit
    payment_benefit = (has_strong_bank or has_bank or has_payment) and direct_financial
    amount = amount_value(text)
    non_mobile_utility_bill = is_non_mobile_utility_bill_text(text)
    airline_mileage_reward = is_airline_mileage_reward_text(text)
    low_value_promo_task = is_low_value_promo_task_text(text)
    shopping_survey_reward = is_shopping_survey_reward_text(text)
    strict_consumption = bool(
        re.search(r"(信用卡还款|还款|转账|收款|生活缴费|缴费|国网|网上国网|国家电网|电费|水费|燃气费|话费|手机充值|加油|中石油|中石化|石化|油卡|E卡|e卡|京东卡|公交|地铁|一卡通|交通卡|公交卡|地铁卡|乘车码)", text, re.I)
    )
    mobility_payment_utility = bool(
        re.search(r"(滴滴|打车|出行|出租车).{0,24}(云闪付|银联|付款|支付|立减|优惠|[0-9]+折|[0-9]+元封顶)|(云闪付|银联).{0,24}(滴滴|打车|出行|出租车)", text, re.I)
    ) and not re.search(r"(酒店|旅游|机票|火车票|高铁|12306)", text, re.I)
    direct_utility = strict_consumption or bool(
        re.search(r"(数字人民币|数币|立减金|立减斤|立减巾|ljj|支付券|支付卷|刷卡金|刷卡斤|还款券|还款卷|红包|(?<![0-9-])[0-9]+[-减][0-9]+(?!-[0-9]))", text, re.I)
    ) or mobility_payment_utility
    cash_like = bool(
        re.search(r"(现金红包|通用红包|支付红包|微信红包|支付宝红包|无门槛红包|红包雨|立减金|立减斤|立减巾|立减䋮|立减J|ljj|立减卷|刷卡金|刷卡卷|刷卡斤|支付券|支付卷|支付劵|还款券|还款卷|还款劵|话费|油卡|E卡|e卡|京东卡|现金到账)", text, re.I)
    )
    review_path = bool(
        re.search(r"(经营码|商家码|商户码|商户收款码|收款码|套现|无损套|无痛套|0损|零损|T出来|T出|闲鱼|咸鱼|海鲜市场|小黄鱼|代拉|代领|秒出|证券|基金|理财|开户|实名|多号|受邀|邀请码|损耗|低损|小损|折现|变现)", text, re.I)
    )
    invite_path = bool(re.search(r"(邀请|邀请码|填码|邀请人|邀请好友|被邀请|受邀好友|助力)", text, re.I))
    social_hard = bool(
        re.search(r"(需邀请[0-9一二三四五六七八九十]+人|邀请[0-9一二三四五六七八九十]+人|邀请好友助力|邀请.{0,8}助力满|助力.{0,8}(好友|团队|满|提现|砍|人)|拉人|人头|代推|互助|组队.{0,6}助力|(?<!下)拉[0-9]+(?:人|个|位|小|新)?|(?<![下扒])拉[一二三四五六七八九十]+(?:人|个|位|小|新))", text, re.I)
    )
    ongoing_cue = bool(
        re.search(r"(补了|补货|又有|还有|(?<!没)有了|上了|开了|来了|可领|能领|刚领|领到|秒到|恢复|回来了|卡点|每天|每月|零点|补名额|再开|续开|更新|仍可|继续|还在|在开|可用|能用)", text, re.I)
    )
    complaint_cue = bool(
        re.search(r"(失败|翻车|不到账|没到账|没给|不给|不送|没退款|投诉|被骗|吐槽|避雷|求助|不支持|不能用|用不了|没名额|没有了|没了|结束了|抢光|已抢光|凉了|黄了|反撸)", text, re.I)
    )
    hard_complaint = bool(
        re.search(r"(失败|翻车|不到账|没到账|没给|不给|不送|没退款|投诉|被骗|吐槽|避雷|求助|不支持|不能用|用不了|过期|失效|结束了|抢光|已抢光|凉了|黄了|反撸)", text, re.I)
    )
    availability_cue = bool(
        re.search(r"(还有名额|现在还有|刚弄完还有|有名额|可领|能领|刚领|领到|秒到|恢复|回来了|每天.{0,8}(零点|卡点)|每月一次|如果没了等明天|没名额就每天|没名额.{0,12}(明天|次日|零点|每天|再试)|无了.{0,8}(明天|次日|零点|每天|再试))", text, re.I)
    )
    misleading_availability = bool(
        re.search(r"(还有什么|还有没有|还有其[它他]路子|谁知道|是不是|不行了|红包不出|扫不出|被扫不出|不出.{0,6}(啊|吗|么|怎么|为啥|为什么)|不敢上了?|上不了|怎么用掉|怎么消耗|什么方法|求方法|怎么挽救|毛越来越难撸|撸不到|折腾够呛)", text, re.I)
    )
    resolved_negative = bool(
        re.search(r"(试了下有了|现在.{0,8}有了|又有了|补了|恢复|回来了|刚弄完还有|还有名额|如果没了等明天|今天没名额就明天试|没名额.{0,12}(明天|次日|零点|每天|再试)|无了.{0,8}(明天|次日|零点|每天|再试)|若无了.{0,8}(明天|次日|零点)|不出.{0,10}(换号|换小号|再试|明天|第二天)|出不出|不出就是没名额|等第二天|次日去)", text, re.I)
    )
    urgency_before_gone = bool(
        re.search(r"(速度|速|快|赶紧).{0,24}(不然|否则).{0,10}(黄|没|无|没了|结束|抢光)|(不然|否则).{0,10}(黄|没|无|没了|结束|抢光)", text, re.I)
    )
    explicit_offer_title = bool(
        re.search(r"(立减金|立减斤|立减巾|ljj|消费金|刷卡金|支付券|还款券|支付有礼|红包|话费|油卡|E卡|e卡|京东卡|宝箱|达标|抽奖|专享|限时|补了|又有|可领|领取|返现|返现券)", title, re.I)
    )
    validity_instruction = bool(
        re.search(r"(请在.{0,24}前领取|领取后.{0,18}有效|有效期至|今日有效|限今日|活动时间|截止.{0,12}(前|日|号)|到期前)", text, re.I)
    ) and payment_benefit and (has_action or explicit_offer_title)
    recovered_positive = (
        availability_cue or (ongoing_cue and not hard_complaint and not misleading_availability) or resolved_negative or validity_instruction or urgency_before_gone
    ) and (has_action or explicit_offer_title or payment_benefit) and bool(
        re.search(r"(补了|补货|又有|还有|(?<!没)有了|上了|开了|来了|可领|能领|刚领|领到|秒到|恢复|回来了|卡点|每天|每月|零点|补名额|再开|续开|更新|仍可|继续|还在|在开|可用|能用|试了下有了|不然|否则|没名额.{0,12}(明天|次日|零点|每天|再试)|无了.{0,8}(明天|次日|零点|每天|再试)|出不出|不出就是没名额|请在|有效期|今日有效|截止)", text, re.I)
    )
    negative_blocks_offer = (complaint_cue or expired_or_negative) and not recovered_positive
    actionable_offer = (
        payment_benefit
        and (has_financial_benefit or has_welfare or direct_utility or cash_like)
        and (has_action or direct_utility or cash_like or review_path or ongoing_cue)
        and not negative_blocks_offer
    )
    attached_product_rebate = bool(PRODUCT_PURCHASE_REBATE_RE.search(text) and (PRODUCT_CONTEXT_RE.search(text) or SHOPPING_CHANNEL_RE.search(text))) or bool(
        physical_product_dominant and ATTACHED_PRODUCT_REWARD_RE.search(text)
    )
    product_reusable = (strict_consumption or mobility_payment_utility or bool(
        re.search(r"(E卡|e卡|京东卡|支付券|支付卷|立减金|刷卡金|还款券|红包|数币|数字人民币|任意|无损|低损|小损|套出|变现|收款|转账|还款|缴费|国网|网上国网|国家电网|加油|话费|公交|地铁|一卡通|交通卡|公交卡|地铁卡|乘车码)", text, re.I)
    )) and not attached_product_rebate
    practical_product_utility = (strict_consumption or mobility_payment_utility or bool(
        re.search(r"(数币|数字人民币|E卡|e卡|京东卡|话费|油卡|缴费|加油|还款|转账|收款|公交|地铁|一卡通|交通卡|公交卡|地铁卡|乘车码)", text, re.I)
    )) and not attached_product_rebate
    bank_merchant_benefit = bool(product_category or product_text or product_primary) and payment_benefit and practical_product_utility
    productish = (
        ((product_category or product_text) and not bank_merchant_benefit)
        or (product_primary and not strict_consumption and not cash_like and not bank_merchant_benefit)
    )
    product_low_value = (bool(product_category or product_text or product_primary or productish) and not practical_product_utility) or physical_product_dominant
    user_utility = strict_consumption or bool(re.search(r"(数币|数字人民币)", text, re.I))
    core_utility_no_ecard = bool(
        re.search(r"(信用卡还款|还款|转账|收款|生活缴费|缴费|国网|网上国网|国家电网|电费|水费|燃气费|话费|手机充值|加油|中石油|中石化|石化|油卡|公交|地铁|一卡通|交通卡|公交卡|地铁卡|乘车码)", text, re.I)
    )
    real_utility_beyond_traffic_card = bool(
        re.search(r"(信用卡还款|还款|转账|收款|生活缴费|缴费|国网|网上国网|国家电网|电费|水费|燃气费|话费|手机充值|加油|中石油|中石化|石化|油卡|公交|地铁|一卡通|公交卡|地铁卡|乘车码|E卡|e卡|京东卡|数币|数字人民币)", text, re.I)
    )
    card_primary_benefit = bool(
        re.search(r"((E卡|e卡|京东卡|油卡|还款券|支付券|立减金|刷卡金|数币|数字人民币).{0,12}(领|兑|兑换|买|购|充|可用|通用)|(领|兑|兑换|买|购|充).{0,12}(E卡|e卡|京东卡|油卡|还款券|支付券|立减金|刷卡金|数币|数字人民币))", text, re.I)
    )
    concrete_product = physical_product_dominant or bool(
        re.search(r"(农夫山泉|送水|牛奶|饮料|阿萨姆|咖啡|奶茶|麦当劳|肯德基|瑞幸|库迪|赵一鸣|山姆|奈雪|霸王茶姬|喜茶|必胜客|海底捞|益禾堂|蜜雪|蜜雪冰城|mxbc|新品券|雪王|华莱士|塔斯汀|711|便利店|外卖|团购|闪购|电影票|电影优惠券|酒店|旅游|机票|火车票|猫粮|狗粮|猫砂|纸巾|抽纸|洗衣液|洗洁精|家清|沐浴露|面膜|防晒|牙膏|衣|鞋|裤|食品|果蔬|粮油|母婴|美妆|家用|数码|冰箱|洗衣机|暖风机|电视|创维|回音壁|扫地机|国补|国家补贴|以旧换新|汉堡|年糕|披萨|套餐|早餐|三全|汤圆|饺子|虾皇饺|盒马|飞天|茅台|礼盒|顺丰券|宝宝用品|绿植|筷子|银耳|莲子|枸杞|红米|唯品会|游戏|手游|三国志|无期迷途|爱奇艺|Keep年卡|keep年卡|车厘子|猕猴桃|鲜奶|酸奶|牛肉|肉丸|纯净水|置物架|墨镜|眼镜|平板|联想|iPhone|玉米|零食|瓜子|威化|凤梨酥|杂志|酸奶券|面霜|爽肤水|卫生棉|卫生巾|高洁丝|尿不湿|纸尿裤|帮宝适|苹果余额|超市卡|视频会员|云盘会员|黄金会员|网易超级会员|家政|晋江币|建材|路由器|玩具|盲盒|小冰狗|车模|车展|故宫|国博|博物馆|升旗|乐园|纪念币|银币|淘金币|封装版|泡泡玛特|医疗器械)", text, re.I)
    )
    merchant_channel = bool(
        re.search(r"(美团(?!支付|月付)|大众点评|大润发|抖音-?商城|京东购物|闪购|便利店|711|必胜客|麦当劳|肯德基|瑞幸|奈雪|霸王茶姬|来伊份|小果园|电影)", text, re.I)
    )
    food_or_commodity = concrete_product or merchant_channel
    incidental_product_rebate = (
        (concrete_product or attached_product_rebate)
        and bool(re.search(r"(下单|付款|购机|购买|买实物|到货|晒[单图]返.{0,8}(E卡|e卡)|返.{0,8}(E卡|e卡)|复购.{0,8}(E卡|e卡)|申请.{0,8}E卡|e卡津贴|品牌种草|跟团|赠|送|国补|国家补贴|以旧换新|新品发售|6期分期|延保|碎屏保)", text, re.I))
        and not core_utility_no_ecard
    )
    product_specific_marketplace = (
        concrete_product
        and bool(re.search(r"(闲鱼|咸鱼|海鲜市场|小黄鱼|某鱼|鱼上|鱼里|代下单|找.{0,8}代)", text, re.I))
        and bool(re.search(r"(代下单|下单|拿下|买|购|买一送一|便宜|低价|找.{0,8}代)", text, re.I))
        and not core_utility_no_ecard
    )
    physical_freebie_product = (
        concrete_product
        and bool(re.search(r"(0买|0元|免费|[0-9.]+元|[0-9.]+亓|买[一二三四五六七八九十0-9]+|购[一二三四五六七八九十0-9]+|[一二三四五六七八九十0-9]+瓶|[一二三四五六七八九十0-9]+枚|[一二三四五六七八九十0-9]+盒)", text, re.I))
        and not core_utility_no_ecard
    )
    dominant_product = physical_product_dominant or incidental_product_rebate or attached_product_rebate or (
        (food_or_commodity or product_primary or product_category)
        and bool(re.search(r"(到手|凑单|拍[0-9一二三四五六七八九十]+件|换购|购物车|官旗|尺码|买实物|到货|下单|付款|回购|津贴|补贴|国补|国家补贴|以旧换新|新品发售|电影|淘票票|影院|IMAX|阿凡达|晋江|iPhone|平板|电视|创维|回音壁|扫地机|面霜|爽肤水|颈霜|套头|维他|柠檬茶)", text, re.I))
        and not core_utility_no_ecard
        and not card_primary_benefit
    )
    marketplace_self_contained_worth = (
        bool(re.search(r"(闲鱼|咸鱼|海鲜市场|小黄鱼|某鱼|鱼上|鱼里|去鱼|挂鱼|挂闲)", text, re.I))
        and (cash_like or direct_utility or bool(re.search(r"(秒出|利润|净赚|买e卡|互拍|红包|返现|返[0-9.]|开通返|收资格|出资格|路径如下|搜索|代拍|出名额|有赚|可出)", text, re.I)))
        and (
            amount >= 5
            or bool(re.search(r"(8\.8|9\.9|[5-9](?:\.\d+)?\s*[元Rr]|[1-9][0-9].{0,6}(利润|净赚|秒出)|买e卡|互拍)", text, re.I))
            or (payment_benefit and amount >= 3 and re.search(r"(返[0-9.]+|返现|收资格|出资格|路径如下|搜索|咸鱼搜索|闲鱼搜索|去鱼)", text, re.I))
        )
        and not product_specific_marketplace
        and not incidental_product_rebate
        and not physical_freebie_product
        and not dominant_product
        and not negative_blocks_offer
    )
    merchant_bank_payment_reward = (
        merchant_channel
        and payment_benefit
        and bool(re.search(r"(支付券|支付卷|立减金|立减斤|立减巾|立减䋮|刷卡金|还款券|红包|随机减|数币|数字人民币|E卡|e卡|京东卡|返现|积分|消费金)", text, re.I))
        and not incidental_product_rebate
        and not (concrete_product and bool(re.search(r"(到手|凑单|下单|付款|实付|低至|买|购|0\.01|1元|[0-9]+[-减][0-9]+)", text, re.I)))
    )
    merchant_coupon_only = merchant_channel and not merchant_bank_payment_reward and not core_utility_no_ecard and not re.search(
        r"(美团支付|美团月付|还款|话费|缴费|加油|转账|收款|经营码|商户码|收款码|套现)", text, re.I
    )
    marketplace_self_contained_allowed = (
        marketplace_self_contained_worth
        and not merchant_coupon_only
        and not productish
        and not product_text
        and not product_category
        and not product_primary
        and not food_or_commodity
        and not re.search(r"(优酷|svip|SVIP|会员|洗车|皮肤|固态|硬盘|WPS|soma|SOMA|电影|美食|餐饮|外卖|游戏|手游|视频|音乐)", text, re.I)
    )
    soft_source_personal_trade_listing = (
        soft_product_category
        and bool(
            re.search(
                r"(^|[\s｜|【】\[\]（）()，,。；;:：])(?:交易|出\s*[0-9一二两三四五六七八九十]+|转让|闲鱼特惠寄|特惠寄|拼邮|包邮|自提)",
                text,
                re.I,
            )
        )
        and not (has_strong_bank or rx(patterns, "bank", text) or has_payment or core_utility_no_ecard or payment_benefit or card_primary_benefit)
    )
    conversion_worth = bool(
        re.search(r"(经营码|商家码|商户码|商户收款码|收款码|套现|无损套|无痛套|0损|零损|T出来|T出|损耗|低损|小损|无损|套卡|套豆|套券|折现|变现)", text, re.I)
    ) and payment_benefit and (
        amount >= 1
        or practical_product_utility
        or bool(re.search(r"(0损|无损|套出|T出|收款|付款|经营码|商户码)", text, re.I))
    )
    account_problem = bool(
        re.search(r"(账号.{0,10}(限制|被限|违规|风控|冻结)|被限制转账|限制转账|不能转账|封[0-9一二三四五六七八九十]*天|提供.{0,8}资料|黑设备|资金.{0,8}(冻结|限制))", text, re.I)
    )
    card_pack_utility_coupon = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and bool(re.search(r"(卡包|我的券|券包|京东专区券|专区券)", text, re.I))
        and bool(re.search(r"(E卡|e卡|京东卡|油卡|话费|支付券|支付卷|立减金|立减斤|立减巾|立减䋮|刷卡金|还款券|红包|数币|数字人民币)", text, re.I))
        and bool(re.search(r"(点进|点开|查看|看看|可能有|有|可充|可用|领|领取|兑换|搜|卡包)", text, re.I))
        and not negative_blocks_offer
        and not account_problem
        and not dominant_product
    )
    found_account_coupon = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and amount >= 5
        and bool(re.search(r"(不知道哪来的|莫名|突然|多了|发了|送了|给了|刚发现|看一下|看看|小红苞|小荭苞)", text, re.I))
        and bool(re.search(r"(通用.{0,8}(抵扣|红包|立减|支付券|还款券)|数币|数字人民币|立减金|刷卡金|支付券|支付卷|支付劵|还款券|还款卷|微信立减|支付宝红包|现金红包|小红苞|小荭苞)", text, re.I))
        and not re.search(r"(寄件|菜鸟|美食券|餐饮|电商券|购物券|苹果券|海底捞|淘宝|天猫|百亿补贴|护发|精油|火鸡面|三养)", text, re.I)
        and not productish
        and not product_text
        and not product_category
        and not product_primary
        and not food_or_commodity
        and not negative_blocks_offer
        and not account_problem
        and not dominant_product
        and not merchant_coupon_only
    )
    collector = bool(re.search(r"(实体卡|卡包|收集)", text, re.I)) and not card_pack_utility_coupon and not re.search(
        r"(领|领取|兑换|立减金|刷卡金|支付券|还款券|红包|返现|[0-9]+[-减][0-9]+|0\.01|(E卡|e卡|京东卡|油卡).{0,12}(买|购|领|兑|减|[0-9])|([0-9]|买|购|领|兑).{0,12}(E卡|e卡|京东卡|油卡))",
        text,
        re.I,
    )
    local_event_or_collectible = bool(
        re.search(r"(故宫|国博|博物馆|车展|车模|升旗|景区|乐园|纪念币|银币|封装版|泡泡玛特)", text, re.I)
    ) and not (review_path or core_utility_no_ecard or cash_like)
    app_promo_code = bool(
        re.search(r"(元宝App|元寶App|復.?制|复制分享|口令|邀请码|春节.*红包|红包封面)", text, re.I)
    ) and not (has_strong_bank or has_payment or core_utility_no_ecard or cash_like)
    account_or_reservation = bool(
        re.search(r"(未通过|解锁|人民银行核查|预约兑换|逾期不予受理|啥行情|什么行情|行情如何)", text, re.I)
    ) and not (payment_benefit or review_path)
    future_schedule = bool(re.search(r"(明天|后天|定闹钟|提醒我|预约提醒|几点开抢|补库|等补|等明天)", text, re.I))
    dead_low_value = bool(re.search(r"(名存实亡|要说再见|活动缩水|缩水|下个月.{0,12}(没|无)|连[0-9.]+都没|只剩下流量|没得减|不减了|不让买|不能缴费)", text, re.I))
    pure_trial_fund = bool(re.search(r"体验金", text, re.I)) and bool(
        re.search(r"(基金|理财|财富|黄金|债券|存管|证券|资产|招行|招商银行|天弘|博时|华夏|中欧)", text, re.I)
    ) and not re.search(r"(数币|数字人民币|红包|立减金|立减斤|立减巾|刷卡金|支付券|还款券|消费金|返现|现金|话费|E卡|e卡|京东卡|收益.{0,8}(归你|到手|到账)|可提现|提现)", text, re.I)
    title_has_allowed_local = bool(
        re.search(r"(北京|北分|帝都|(?<![0-9])010(?![0-9])|黑龙江.{0,12}(农行|农业银行|老农)|农行.{0,12}(黑龙江|龙江|哈尔滨|大庆|齐齐哈尔|牡丹江|佳木斯))", title, re.I)
    )
    title_direct_utility = bool(
        re.search(r"([0-9]+[-减][0-9]+|立减金|刷卡金|支付券|还款券|话费|缴费|国网|加油|E卡|e卡|京东卡|数币|数字人民币|微信立减|转账|还款|收款|省钱月卡)", title, re.I)
    )
    broad_region_digest = bool(
        re.search(r"(各省市|各省|各地区|各地|各行|各大银行|全省|省市|全国各地|银行优惠活动|银行活动分享|银行优惠汇总|信用卡优惠汇总|今日银行|每日银行|优惠活动分享|总表|查漏补缺)", text, re.I)
        or (
            daily_bank_digest
            and re.search(r"(江西|厦门|广东|广西|浙江|湖北|河北|河南|山东|安徽|江苏|福建|四川|重庆|湖南|陕西|山西|辽宁|吉林|天津|上海|深圳|广州|成都|杭州|南京|苏州)", text, re.I)
            and re.search(r"(各地区|地区|多地|合集|每月更新|如下搜|入口)", text, re.I)
        )
    ) and not (
        title_has_allowed_local
        and not re.search(r"(各地区|各省|各地|多地|合集|江西|厦门|广东|广西|浙江|湖北|河北|河南|山东|安徽|江苏|福建|四川|重庆|湖南|陕西|山西|辽宁|吉林|天津|上海|深圳|广州|成都|杭州|南京|苏州)", text, re.I)
    )
    daily_digest_noise = daily_bank_digest and not title_has_allowed_local and not title_direct_utility
    broad_generic_bank_digest_title = bool(
        re.search(r"(各省市|各省|各地区|各地|各大银行|银行优惠活动|银行活动分享|银行优惠汇总|信用卡优惠汇总|优惠活动分享|每日活动总贴|[0-9]{1,2}\.[0-9]{1,2}各大银行|[0-9]{1,2}月[0-9]{1,2}日.*银行活动)", title, re.I)
    )
    region_names = set(
        re.findall(
            r"(江西|厦门|广东|广西|北京|浙江|湖北|河北|河南|山东|安徽|江苏|福建|四川|重庆|湖南|陕西|山西|辽宁|吉林|天津|上海|深圳|广州|成都|杭州|南京|苏州)",
            text,
            re.I,
        )
    )
    multi_region_digest = bool(
        len(region_names) >= 3
        and re.search(r"(各地区|各省|各地|多地|多省|合集|每月更新|如下搜|入口)", text, re.I)
        and (has_strong_bank or rx(patterns, "bank", text) or has_payment or payment_benefit)
    )
    narrow_bank_utility_digest = (
        daily_bank_digest
        and (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and (has_financial_benefit or has_welfare or has_concrete_reward or amount >= 1 or direct_utility or cash_like or re.search(r"(经营码|商家码|商户码|收款码|码子|套现|T出)", text, re.I))
        and bool(re.search(r"(转账|还款|收款|省钱月卡|立减金|立减斤|立减巾|立减䋮|刷卡金|支付券|支付卷|还款券|还款卷|红包|缴费|云缴费).{0,24}(汇总|合集|查漏补缺|最后一天)|(汇总|合集|查漏补缺|最后一天).{0,24}(转账|还款|收款|省钱月卡|立减金|立减斤|立减巾|立减䋮|刷卡金|支付券|支付卷|还款券|还款卷|红包|缴费|云缴费)", title, re.I))
        and not broad_generic_bank_digest_title
        and not dominant_product
        and not merchant_coupon_only
        and not negative_blocks_offer
        and not hard_risk
    )
    game_task_welfare = bool(
        re.search(r"(游戏|手游|玩游戏|预约.{0,8}(游戏|手游)|三国志|无期迷途|爱奇艺积分|Keep年卡|keep年卡|短剧|看短剧|追剧|剧场)", text, re.I)
    ) and not core_utility_no_ecard and not review_path and not (payment_benefit and cash_like)
    points_consumption_question = bool(
        re.search(r"(积分|万里通).{0,20}(买啥|买什么|怎么用|咋用|能不能兑|能不能兑换|兑什么|换什么|兑换什么|买哪个|换哪个)", text, re.I)
    ) and not strict_consumption
    status_only = bool(re.search(r"(已缴费成功|天塌了|赔付.{0,12}(只能|限制|上限)|一个月只能|到期了.{0,18}取出来|可以取出来|活动到期|名额.{0,8}没了|链接没有了)", text, re.I))
    participant_only_postmortem = bool(
        re.search(r"(昨天|前天|上次|前几天|之前|白天|刚才|已经|已|买了|买过|参加了|参与了|报名了|没到|不到账|没到账).{0,28}(到账|到了|给补|补了|可以领了|去领|记得领|注意|退款|退回)|(到账|到了|给补|补了|可以领了|去领|记得领|注意).{0,28}(昨天|前天|上次|前几天|之前|白天|刚才|买了|买过|参加了|参与了|报名了)", text, re.I)
    ) and not re.search(r"(新户|新用户|新活动|新毛|又来|又有|重新|现在可领|现在能领|现在还有|今天可领|今天能领|可报名|能报名|入口|路径|直达|扫码|搜索|搜).{0,24}(领|报名|参加|入口|路径|直达|扫码|搜索|搜)", text, re.I) and not review_path
    retention_or_insurance = bool(re.search(r"(销卡威胁|车险|保险报价|保险到期|到期.{0,8}保险)", text, re.I)) and not strict_consumption and not review_path
    survey_only = bool(re.search(r"(问卷|调研|乱填)", text, re.I)) and not (has_strong_bank or has_payment) and not re.search(r"[5-9][0-9]*\s*(?:元|r|R|块|E卡|e卡|京东卡)", text, re.I)
    deleted_or_stale_marked = bool(re.search(r"^(【删】|（删）|\(删\)|\[删\]|删[:：])", title.strip(), re.I))
    valuable_deleted_title = (
        deleted_or_stale_marked
        and payment_benefit
        and (amount >= 8 or re.search(r"【(中国农业银行|农业银行|中国银行|建设银行|工商银行|交通银行|邮储银行|招商银行|中信银行|光大银行|浦发银行|广发银行|民生银行|平安银行|华夏银行)】", text) or core_utility_no_ecard)
        and (has_action or direct_utility or cash_like)
        and not hard_complaint
        and not dominant_product
        and not merchant_coupon_only
        and not account_problem
        and not hard_risk
    )
    deleted_or_stale_title = deleted_or_stale_marked and not valuable_deleted_title
    tiny_repayment_coupon_worth = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and bool(re.search(r"(还款|还花呗|花呗|白条还款|信用卡|xyk|xing\/用卡|还.?用卡)", text, re.I))
        and bool(re.search(r"(0\.\d+|[0-4](?:\.\d+)?\s*(?:元|块|r|R)?|[0-9]+[-减][0-9]+|券|红包|立减|随机减)", text, re.I))
        and (cash_like or has_financial_benefit or has_concrete_reward or re.search(r"(券|红包|立减|随机减|可还|能还)", text, re.I))
        and not daily_bank_digest
        and not low_quality
        and not dominant_product
        and not merchant_coupon_only
        and not negative_blocks_offer
        and not account_problem
    )
    bank_trial_cash_withdraw_worth = (
        (has_strong_bank or rx(patterns, "bank", text))
        and re.search(r"体验金", text, re.I)
        and re.search(r"(现金红包|红包|收益).{0,20}(提现|银行卡|秒到)|提现到银行卡|可提现|收益.{0,12}提现", text, re.I)
        and not daily_bank_digest
        and not low_quality
        and not productish
        and not dominant_product
        and not merchant_coupon_only
        and not negative_blocks_offer
    )
    social_farming = bool(
        re.search(r"(组队|成团|拼团|互点|助力|邀请好友|分享好友|好友助力|拉好友).{0,24}(红包|瓜分|奖励|现金|提现|立减金|抽奖|任务)|(红包|瓜分|奖励|现金|提现|立减金|抽奖|任务).{0,24}(组队|成团|拼团|互点|助力|邀请好友|分享好友|好友助力|拉好友)", text, re.I)
    ) and not (
        payment_benefit
        and not invite_path
        and not social_hard
        and (amount >= 8 or (region_reason in {"beijing", "heilongjiang-abc"} and amount >= 5))
    )
    assisted_local_bank_reward = (
        social_farming
        and payment_benefit
        and region_reason in {"beijing", "heilongjiang-abc"}
        and amount >= 3
        and not social_hard
        and not negative_blocks_offer
        and not low_quality
        and not dominant_product
        and not merchant_coupon_only
    )
    micro_reward = (
        0 < amount < 1
        and (cash_like or direct_utility or has_financial_benefit or has_concrete_reward)
        and not tiny_repayment_coupon_worth
        and not ((region_reason in {"beijing", "heilongjiang-abc"}) and payment_benefit and core_utility_no_ecard)
    )
    small_friction_reward = (
        0 < amount < 2
        and bool(re.search(r"(答题|问卷|调研|打电话|智铃|彩铃|试玩|浏览|签到|积分抽|抽奖|任务|分享|小号|同实名|评论区|链接|自动续费)", text, re.I))
        and not tiny_repayment_coupon_worth
        and not (payment_benefit and (region_reason in {"beijing", "heilongjiang-abc"} or amount >= 1.5) and not soft_risk)
    )
    low_probability_micro_bank_task = (
        0 < amount < 2.5
        and (payment_benefit or ((has_strong_bank or rx(patterns, "bank", text)) and has_financial_benefit))
        and bool(re.search(r"(非必中|未必中|不必中|概率|随机|0\.[0-9]+|任务|浏览|签到|抽红包|抽奖|抽取)", text, re.I))
        and not tiny_repayment_coupon_worth
        and not (region_reason in {"beijing", "heilongjiang-abc"} and amount >= 2 and not soft_risk and not discussion)
        and not review_path
    )
    low_value_finance = (
        review_path
        and bool(re.search(r"(基金|理财|黄金|证券|开户|存管|财富)", text, re.I))
        and amount < 5
        and not core_utility_no_ecard
        and not re.search(r"(E卡|e卡|京东卡|油卡|还款|缴费|转账|收款|现金到账|可提现|提现到|收益.{0,8}(归你|到账)|刷卡金)", text, re.I)
    )
    collect_first_bank_reward = (
        (has_strong_bank or rx(patterns, "bank", text))
        and has_financial_benefit
        and has_concrete_reward
        and amount >= 8
        and bool(re.search(r"(先领|先抢|先拿|先报名|先参加|先存着|先兑换)", text, re.I))
        and (title_discussion or discussion)
        and not negative_blocks_offer
        and not account_problem
        and not hard_risk
        and not low_quality
        and not dominant_product
    )
    finance_strategy_worth = (
        review_path
        and (has_strong_bank or rx(patterns, "bank", text))
        and amount >= 20
        and bool(re.search(r"(证券|存管|三方|第三方|换绑|绑定银行卡|转进转出|月均|存款银行|储蓄卡|活动路径|先报名|先参加)", text, re.I))
        and bool(re.search(r"(立减金|立减|刷卡金|支付券|支付红包|开户红包|红包|E卡|e卡|京东卡|拿[0-9一二两三四五六七八九十百千万]+|领[0-9一二两三四五六七八九十百千万]+|返[0-9一二两三四五六七八九十百千万]+|给[0-9一二两三四五六七八九十百千万]+)", text, re.I))
        and not negative_blocks_offer
        and not hard_risk
        and not dominant_product
        and not merchant_coupon_only
    )
    problem_question = (
        (title_discussion or re.search(r"(求助|怎么用|扫不出|找不到|领不了|哪里申请|在哪里申请|有知道|啥问题|什么情况|可不可以|能不能|靠谱吗|值不值|还有其它路子|还有其他路子)", title, re.I) or re.search(r"(求助|哪里申请|在哪里申请|有知道|啥问题|怎么办|还有什么办法|还有别的办法)", text, re.I))
        and re.search(r"(怎么用|扫不出|找不到|领不了|哪里申请|在哪里申请|有知道|啥问题|什么情况|可不可以|能不能|靠谱吗|值不值|还有其它路子|还有其他路子|不敢上|怎么挽救|毛越来越难撸|撸不到|折腾够呛|预算不足|线下交易|盯着.{0,6}没用)", text, re.I)
        and not recovered_positive
        and not collect_first_bank_reward
        and not (amount >= 8 and payment_benefit and not title_discussion)
    )
    low_certainty_actionable_question = (
        (title_discussion or discussion)
        and not recovered_positive
        and not collect_first_bank_reward
        and not finance_strategy_worth
        and not marketplace_self_contained_worth
        and not conversion_worth
        and not re.search(r"【(中国农业银行|农业银行|中国银行|建设银行|工商银行|交通银行|邮储银行|招商银行|中信银行|光大银行|浦发银行|广发银行|民生银行|平安银行|华夏银行)】", text)
        and (
            soft_risk
            or soft_product_category
            or bool(re.search(r"(怎么|咋|吗|么|有没有|还有吗|哪|谁|求|问|能不能|是不是|出给谁|能出|不知道|不确定|啥|如何|费率|T出来|套出来|怎么用|能不能|值不值|靠谱吗|还有其它|还有其他|哪里|在哪)", text, re.I))
        )
        and not (payment_benefit and amount >= 20 and not soft_risk and not soft_product_category and (has_action or direct_utility))
    )
    borrow_merchant_code_question = bool(BORROW_MERCHANT_CODE_QUESTION_RE.search(text)) and not re.search(
        r"(官方|活动|入口|路径|搜索|搜|领取|可领|教程|攻略|任意商户|任意门店|无需.{0,6}(商家|商户|收款码)|全国|通用)",
        text,
        re.I,
    )
    low_value_payment_task = (
        has_payment
        and not has_strong_bank
        and 0 < amount < 5
        and bool(re.search(r"(答题|扫码|抽奖|任务|基金|中欧|财富号|小程序|会员|短剧|签到|积分|下载|体验|到店红包|到店)", text, re.I))
        and (amount < 3 or bool(re.search(r"(答题|任务|基金|中欧|财富号|会员|短剧|签到|积分|下载|体验|到店红包|到店|集心愿|合成|攒红包)", text, re.I)))
        and not core_utility_no_ecard
        and not tiny_repayment_coupon_worth
    )
    low_value_fund_payment_task = (
        has_payment
        and review_path
        and not has_strong_bank
        and amount < 5
        and bool(re.search(r"(基金|财富号|蚂蚁财富|中欧|华夏|博时|股票行情).{0,90}(任务|买入|购买|体验|自选|消费红包|支付红包|兑换|0\.1|1毛|一毛)|(任务|买入|购买|体验|自选|消费红包|支付红包|兑换|0\.1|1毛|一毛).{0,90}(基金|财富号|蚂蚁财富|中欧|华夏|博时|股票行情)", text, re.I))
        and not re.search(r"(秒卖|低损|小损|无损|可领.{0,6}(?:[8-9]|[1-9][0-9])|[8-9](?:\.\d+)?[-减]|[1-9][0-9](?:\.\d+)?[-减]|100[-减]8\.88|50毛|几十|每一万|大额闲置|红包.{0,8}(?:[8-9]|[1-9][0-9]))", text, re.I)
    )
    low_value_nonbank_registration_reward = (
        not (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and amount <= 5
        and bool(re.search(r"(注册|实名|实名认证|关注|新人|新用户|授权手机号)", text, re.I))
        and bool(re.search(r"(调查员|公众号|审核|[3３]-?[5５]天|授权手机号|积分.{0,12}(换|兑)|下载.{0,12}(补贴|返现)|新人补贴|新用户补贴)", text, re.I))
        and not core_utility_no_ecard
        and not re.search(r"(经营码|商家码|商户码|收款码|套现|T出|闲鱼|咸鱼|海鲜市场|代拉|代领|秒出)", text, re.I)
    )
    tiny_nonbank_point_lottery = (
        not (has_strong_bank or rx(patterns, "bank", text))
        and 0 < amount <= 3
        and bool(re.search(r"(抖音积分|积分兑换周|积分周|丸子猫|挖宝|金豆|转盘|打卡[0-9一二三四五六七八九十]*天|看运气|概率|随机|抽取|抽奖|签到|口令|灵犀)", text, re.I))
        and not core_utility_no_ecard
        and not tiny_repayment_coupon_worth
        and not review_path
        and not (has_payment and amount >= 3 and re.search(r"(还款|转账|收款|缴费|话费|电费|水费|燃气费|加油|E卡|e卡|京东卡)", text, re.I))
    )
    low_certainty_nonbank_lottery = (
        not (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and amount <= 5
        and bool(re.search(r"(活动抽奖|抽奖|抽桨|抽取|转盘|概率|看脸|每一期|中一两个号|二中[一1]|水不大|随机|自测)", text, re.I))
        and bool(re.search(r"(E卡|e卡|京东卡|卡密|立减金|立减斤|立减巾|支付券|支付卷|红包|话费|现金|网盘|卡包)", text, re.I))
        and not review_path
        and not (region_reason == "beijing" and amount >= 5)
    )
    eligibility_problem = (
        (title_discussion or discussion or re.search(r"(提示|资格|实名)", text, re.I))
        and bool(re.search(r"(没有领取资格|无领取资格|没资格|无资格|不让领|不让领取|不能领取|提示没有|同一个实名|同实名.{0,12}(不让|不能|没资格|无资格))", text, re.I))
        and amount < 8
        and not review_path
    )
    short_drama_reward = (
        bool(re.search(r"(短剧|看短剧|追剧|剧场|看剧|看短视频)", text, re.I))
        and bool(re.search(r"(提现券|提现卷|红包|微信|vx|VX|现金|会员|奖励)", text, re.I))
        and not core_utility_no_ecard
        and not ((has_strong_bank or rx(patterns, "bank", text)) and payment_benefit and amount >= 8)
        and not review_path
    )
    payment_membership = bool(re.search(r"(买会员|购会员|开会员|会员购|会员卡|年卡|月卡|[0-9一二三四五六七八九十]+天会员|[0-9一二三四五六七八九十]+个月会员|音乐会员|视频会员|云盘会员|苹果音乐|QQ音乐|腾讯视频|爱奇艺|央视频|自动续费)", text, re.I)) and (has_payment or has_financial_benefit or has_concrete_reward) and not core_utility_no_ecard and not card_primary_benefit and not review_path
    concrete_product_purchase_in_content = bool(
        re.search(r"(美团(?!支付|月付)|大众点评|麦当劳|肯德基|瑞幸|库迪|奶茶|咖啡|早餐|汉堡|套餐|电影|会员|央视频|猫砂|冰箱|洗衣机|空调|电视|面膜|洗面奶|抽纸|奶茶卡|爆品|闪购|外卖|团购|新品|特价团)", title_and_body, re.I)
    )
    bank_product_consumption = (
        (concrete_product_purchase_in_content or bool(re.search(r"(央视频|[0-9一二三四五六七八九十]+天会员|[0-9一二三四五六七八九十]+个月会员|音乐会员|视频会员|云盘会员)", title_and_body, re.I)))
        and (has_strong_bank or rx(patterns, "bank", text) or has_payment or payment_benefit)
        and bool(re.search(r"(美团|大众点评|麦当劳|肯德基|瑞幸|库迪|奶茶|咖啡|早餐|汉堡|套餐|电影|会员|央视频|新品|特价团|下单|付款|实付|买|购|1元买|0\.01)", title_and_body, re.I))
        and not real_utility_beyond_traffic_card
        and not card_primary_benefit
        and not review_path
    )
    small_nonbank_utility = (
        not (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and direct_utility
        and 0 < amount < 2
        and region_reason not in {"beijing", "heilongjiang-abc"}
        and not review_path
    )
    non_financial_news = bool(
        re.search(r"(据报道|报告|协会|车型|特斯拉|Model\s?[Y3]|缺陷率|可靠性|悬架|刹车盘|T[?·.\s-]*V报告)", text, re.I)
    ) and not re.search(
        r"(银行APP|银行app|农行|农业银行|建行|工行|中行|招行|交行|邮储|浦发|广发|光大|民生|平安银行|兴业|华夏|中信银行|银联|云闪付|支付宝|微信支付|立减金|刷卡金|支付券|还款券|红包|数币|数字人民币|话费|缴费|E卡|e卡|京东卡)",
        text,
        re.I,
    )
    gold_jewelry_purchase = (
        bool(re.search(r"(金手镯|手镯|金店|打金|工费|多少钱\/?克|一克|每克|克重|金条)", text, re.I))
        and bool(re.search(r"(买|打|预算|生日|妈妈|金店|工费|多少钱|注意什么|克)", text, re.I))
        and not re.search(r"(立减金|立减斤|立减巾|刷卡金|支付券|支付卷|还款券|还款卷|现金红包|支付宝红包|微信红包|数币|数字人民币|E卡|e卡|京东卡|返现|收款|转账|还款|缴费)", text, re.I)
    )
    bank_card_dining_offer = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and bool(re.search(r"(美食|餐饮|吃货卡|外卖|团购|电影|影院|麦当劳|肯德基|瑞幸|库迪|浦大喜奔|半价)", text, re.I))
        and bool(re.search(r"(半价|[0-9]+[-减][0-9]+|最高\s*[-减]?\s*[0-9]+|[0-9]+折|优惠|立减|随机减|券)", text, re.I))
        and not negative_blocks_offer
        and not expired_or_negative
        and not complaint_cue
        and not title_discussion
        and not discussion
        and not hard_risk
        and not risk
        and not low_quality
        and not social_farming
    )
    local_bank_lottery_activity = (
        (has_strong_bank or rx(patterns, "bank", text))
        and region_reason in {"beijing", "heilongjiang-abc"}
        and bool(re.search(r"(抽奖|转盘|有水|大毛|活动|报名|参加|参与|入口|二维码|链接|直达|扫码|搜索)", text, re.I))
        and not negative_blocks_offer
        and not expired_or_negative
        and not complaint_cue
        and not title_discussion
        and not discussion
        and not hard_risk
        and not risk
        and not low_quality
        and not productish
        and not dominant_product
        and not merchant_coupon_only
        and not social_farming
    )
    dining_or_grocery_primary = (
        bool(re.search(r"(美食|餐饮|吃货卡|外卖|团购|水果|砂糖桔|草莓|橘子|橙子|芭乐|年货|麦当劳|肯德基|瑞幸|库迪|奶茶|咖啡|汉堡|套餐|电影|影院)", text, re.I))
        and (has_strong_bank or rx(patterns, "bank", text) or has_payment or has_financial_benefit or has_welfare)
        and bool(re.search(r"(券|[0-9]+[-减][0-9]+|抢到|中了|有水|抽不到|没人发|怎么出|卖掉|车头|复购|复购补贴|返.{0,8}(?:E卡|e卡|京东卡)|领券|下单|买|购)", text, re.I))
        and not core_utility_no_ecard
        and not bank_card_dining_offer
        and not ((has_strong_bank or rx(patterns, "bank", text) or has_payment) and cash_like and amount >= 8 and re.search(r"(立减金|立减斤|立减巾|刷卡金|支付券|支付卷|还款券|还款卷|现金红包|微信立减|支付宝红包)", text, re.I) and not re.search(r"(美食券|餐饮券|外卖券|团购券|电影票|电影券)", text, re.I))
        and not re.search(r"(通用.{0,8}(?:立减|支付券|红包)|(?:立减金|立减斤|立减巾|刷卡金|还款券|还款卷|支付券|支付卷|数币|数字人民币).{0,16}(?:还款|转账|收款|缴费|话费|加油|通用|无门槛)|(?:还款|转账|收款|缴费|话费|加油).{0,16}(?:立减金|刷卡金|还款券|支付券|数币|数字人民币))", text, re.I)
    )
    entertainment_membership_primary = (
        bool(re.search(r"(优酷|腾讯视频|爱奇艺|QQ音乐|音乐会员|视频会员|云盘会员|双V会员|会员购|买会员|购会员|开通.{0,8}会员|刚需会员|[0-9一二三四五六七八九十]+(?:天|个月|月|年)会员|免费得[0-9一二三四五六七八九十]+个月|周卡|月卡|年卡|VIP|svip|SVIP|看个视频|接单)", text, re.I))
        and not re.search(r"(还款券|还款卷|信用卡[券劵]|刷卡金|支付券|支付卷|立减金|立减斤|立减巾|数币|数字人民币).{0,16}(?:还款|转账|收款|缴费|话费|加油|通用|无门槛)?", text, re.I)
        and not (
            core_utility_no_ecard
            and amount >= 5
            and re.search(r"(加油|话费|缴费|电费|水费|燃气费|还款|转账|收款|E卡|e卡|京东卡)", text, re.I)
            and not re.search(r"(会员|视频|云盘|优酷|QQ音乐|VIP|svip|SVIP|周卡|月卡|年卡).{0,24}(开通|接单|直充|免费得|[0-9一二三四五六七八九十]+个月)", text, re.I)
        )
    )
    points_product_lottery = (
        bool(re.search(r"(积分|盲盒|抽奖|转盘|兑换券|实物|日历|皮肤|游戏|手游|9积分|积分抽)", text, re.I))
        and bool(re.search(r"(概率|不中的|第二天再试|实物|兑换券|日历|皮肤|盲盒|会员红包盲盒|9积分|积分抽|每次用[0-9一二三四五六七八九十]+积分)", text, re.I))
        and (has_payment or has_financial_benefit or has_welfare)
        and not ((has_strong_bank or rx(patterns, "bank", text)) and cash_like and amount >= 8 and re.search(r"(立减金|刷卡金|支付券|还款券|现金红包|支付宝红包|微信红包)", text, re.I))
    )
    mobile_pay_card_add_reward = (
        bool(re.search(r"(手机\s*pay|Apple\s*Pay|安卓pay|Huawei\s*Pay|Mi\s*Pay|pay里|跳转pay|新增.{0,8}(?:信用卡|xyk|xing\/?用卡)|绑.{0,8}(?:信用卡|xyk|xing\/?用卡))", text, re.I))
        and (has_strong_bank or rx(patterns, "bank", text) or payment_benefit)
        and bool(re.search(r"(新增|添加|绑定|绑卡|加卡|申请|开通).{0,30}(立减金|支付券|红包|刷卡金|送[0-9一二三四五六七八九十]+|[0-9]+(?:\.[0-9]+)?\s*(?:元|r|R|块))|(?:立减金|支付券|红包|刷卡金).{0,30}(新增|添加|绑定|绑卡|加卡|申请|开通)", text, re.I))
        and amount >= 5
        and not negative_blocks_offer
        and not hard_risk
    )
    ecommerce_physical_arbitrage = (
        bool(re.search(r"(京东自营|iPhone|苹果手机|平板|购机|返卡商品|低价|不可叠加|价保|买实物|退货|店铺客服|客服不管|京东超市|天猫.{0,20}券|淘宝.{0,20}券|商品补贴|返卡金额)", text, re.I))
        and bool(re.search(r"(买|下单|付款|实付|价保|返卡|退货|套e卡|套E卡|低价|不可叠加|补贴|券后|领券|客服)", text, re.I))
        and not core_utility_no_ecard
        and not mobile_pay_card_add_reward
        and not (
            card_primary_benefit
            and not re.search(r"(iPhone|苹果手机|平板|京东自营|返卡商品|低价|不可叠加|价保|退货|客服|商品补贴|天猫.{0,12}券|淘宝.{0,12}券)", text, re.I)
        )
    )
    product_postmortem_warning = (
        bool(re.search(r"(不要用|提醒大家不要|规则才知道|犯同样的错误|客服不管|退货后|结果就给|没法申请|支付券退不回来)", text, re.I))
        and bool(re.search(r"(返卡|商品|京东超市|京东自营|支付券|E卡|e卡|京东卡)", text, re.I))
        and not core_utility_no_ecard
        and not review_path
        and not marketplace_self_contained_allowed
    )
    low_value_platform_game_payment = (
        has_payment
        and not (has_strong_bank or rx(patterns, "bank", text))
        and 0 < amount <= 3
        and bool(re.search(r"(挖宝|丸子猫|每日|每天|小程序|积分|任务|抽奖|转盘|签到|看运气|随机)", text, re.I))
        and not re.search(r"(还款|转账|收款|缴费|电费|水费|燃气费|加油|银行|信用卡|xyk|xing\/用卡)", text, re.I)
        and not review_path
        and not marketplace_self_contained_allowed
    )
    task_heavy_nonbank_utility = (
        not (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and direct_utility
        and bool(re.search(r"(答题|问卷|调研|知识|年度报告|集福|元宝|任务|打卡|积分|抽奖|概率|随机|口令|灵犀|应用商店|下载|广告|体验|低碳专区)", text, re.I))
        and not review_path
        and not marketplace_self_contained_worth
        and not (amount >= 20 and re.search(r"(必中|保底|直接到账|秒到|无门槛)", text, re.I))
    )
    invoice_lottery = (
        bool(re.search(r"(发票|fa票|fp|开票).{0,24}(抽奖|有水|票|开|中|摇)|(?:抽奖|有水|中).{0,24}(发票|fa票|fp|开票)", text, re.I))
        and not core_utility_no_ecard
        and not (payment_benefit and amount >= 20 and region_reason in {"beijing", "heilongjiang-abc"})
    )
    soft_source_commodity_rebate = (
        soft_product_category
        and bool(re.search(r"(返.{0,8}(?:E卡|e卡|京东卡|津贴|卡)|换e卡|返卡|清风|湿厕纸|牙线|小酒|买酒|糯米|油画|货号|商品|下单|实付|到手|凑单|复购|津贴)", text, re.I))
        and not core_utility_no_ecard
        and not payment_benefit
        and not review_path
        and not marketplace_self_contained_worth
    )
    soft_risk_small_bank_reward = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and soft_risk
        and 0 < amount < 8
        and bool(re.search(r"(黑号|受邀|邀约|定投|风险测评|财富评测|测评|看脸|资产|特邀)", text, re.I))
        and not core_utility_no_ecard
        and not review_path
        and not marketplace_self_contained_worth
    )
    low_marketplace = bool(re.search(r"(闲鱼|咸鱼|海鲜市场|小黄鱼|某鱼|鱼上|鱼里|去鱼|挂鱼|挂闲)", text, re.I)) and ((0 < amount < 5) or bool(re.search(r"0\.\d|[1-4]\s*(?:元|块|r|R)|[1-4]\s*🧧|买e卡.{0,8}(没试|应该|可以没试)", text, re.I))) and not (payment_benefit and amount >= 8) and not marketplace_self_contained_allowed and not core_utility_no_ecard
    bank_meta_discussion = (has_strong_bank or has_payment) and discussion and bool(
        re.search(r"(怎么挽救|人气|毛越来越难撸|撸不到|干瞪眼|折腾够呛|预算不足|线下交易|盯着.{0,6}没用|限额.{0,24}(气人|坑人|怎么办|办法)|账户被管控|解控|银监会)", text, re.I)
    ) and not recovered_positive and not review_path
    merchant_tax_discussion = (
        (has_payment or review_path or bool(re.search(r"(收款码|经营码|商家码|商户码|营业执照|实体店)", text, re.I)))
        and bool(re.search(r"(收税|税务|纳税|税局|税点|税费)", text, re.I))
        and bool(re.search(r"(会不会|以后会不会|是不是|怎么|咋|为什么|该怎么|要不要)", text, re.I))
        and not bool(re.search(r"(返现|红包|立减金|刷卡金|支付券|话费|加油|E卡|e卡|京东卡|补贴|优惠|到账|秒到|可领|能领|有水|大毛|小毛)", text, re.I))
    )
    payment_problem_question = (
        (has_strong_bank or has_payment or strict_consumption or review_path or bool(re.search(r"(数字人民币|数币|收款码|付款|扣款|支付成功|付款成功|扣款成功)", text, re.I)))
        and (title_discussion or discussion or bool(re.search(r"(请教|求助|哪位.{0,8}(知道|大佬)|大佬.{0,8}(知道|看看)|钱.{0,12}(丢|卡)|卡哪|丢哪|怎么办|怎么回事|什么情况)", text, re.I)))
        and bool(
            re.search(
                r"(扣款成功|付款成功|支付成功|实付|扫.{0,12}收款码|收款码|付款|扣款).{0,60}(没收到|没到账|不到账|无记录|没有记录|记录都没有|钱.{0,12}(卡|丢)|卡哪|丢哪)|(没收到|没到账|不到账|无记录|没有记录|记录都没有|钱.{0,12}(卡|丢)|卡哪|丢哪).{0,60}(扣款|付款|支付|收款码|微信|支付宝|数币|数字人民币)",
                text,
                re.I,
            )
        )
    )
    personal_idle_rant = (
        bool(re.search(r"(今天|今日|最近|这两天).{0,16}(0收入|零收入|没收入|还倒贴|倒贴|白忙|白撸|没撸到|没毛)|0收入|零收入|还倒贴", title_and_body, re.I))
        and bool(re.search(r"(我|俺|一个人|刚用|刚打开|点开|打开赚吧|撸毛|今天拼多多)", text, re.I))
        and bool(re.search(r"(点开.{0,12}(都是|全是|是|只有)0\.[0-9]|点开.{0,12}(是|只有)[一1]豆|没保底|没有保底|倒贴|0收入|零收入|还交了|白忙|白撸|没撸到|没毛)", text, re.I))
        and not bool(re.search(r"(路径|入口|直达|可领|能领|领取|报名|参加|扫码|搜索|补了|又有|现在还有|仍可|亲测|自测|保底.{0,8}(可|能|有)|大毛|必中)", title, re.I))
    )
    billing_problem = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and bool(re.search(r"(利息|年费|账单|逾期).{0,30}(退回|申请|吐血|好贵|收了|核对|多扣|被扣|不对|能不能|能否|怎么办)|收了我[0-9一二两三四五六七八九十百千万]+.{0,10}(利息|年费)", text, re.I))
        and not has_financial_benefit
        and not cash_like
        and not review_path
    )
    high_friction_giveaway = (
        not payment_benefit
        and not has_strong_bank
        and soft_product_category
        and (cash_like or direct_utility or has_financial_benefit or has_welfare)
        and bool(re.search(r"(答题|评论区|链接.{0,8}吞|搜答案|有的题目有难度|今天可能没水|没水了|明天.{0,8}再来|后排.{0,12}提醒)", text, re.I))
        and not review_path
    )
    soft_thin_welfare = (
        soft_product_category
        and not (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and (cash_like or direct_utility or has_financial_benefit or has_welfare)
        and amount < 5
        and not re.search(r"【(中国农业银行|农业银行|中国银行|建设银行|工商银行|交通银行|邮储银行|招商银行|中信银行|光大银行|浦发银行|广发银行|民生银行|平安银行|华夏银行)】", text)
        and not review_path
        and (compact_content_len < 40 or discussion or future_schedule or re.search(r"(链接|口令|复制|c\s*n\s*/\s*v|换小号|小号|号多|可以看看|试试|不知道)", text, re.I))
    )
    platform_membership = (
        bool(re.search(r"(支付宝会员|微信会员|京东会员|淘宝vip|淘宝会员|会员购|会员卡|年卡|月卡|会员[0-9一二三四五六七八九十.]+\s*(?:元|块|r|R)?|[0-9.]+\s*(?:元|块|r|R)?\s*会员)", text, re.I))
        and (has_payment or has_financial_benefit or has_concrete_reward or cash_like)
        and amount <= 20
        and not core_utility_no_ecard
        and not card_primary_benefit
        and not review_path
    )
    generic_pool_reward = (
        (has_payment or has_financial_benefit or cash_like)
        and bool(re.search(r"(瓜分[0-9一二三四五六七八九十百千万wW]*|集.{0,12}(瓜分|心愿|红包|齐|卡)|合成.{0,12}(瓜分|红包|卡|封面)|集齐|卡片|封面|红包雨|攒红包|月礼.{0,12}(攒|有财|红包)|月礼月有财|支付有礼.{0,12}瓜分|蚂蚁阿福|月月赚)", text, re.I))
        and amount < 8
        and not core_utility_no_ecard
        and not (review_path and amount >= 8)
        and not (has_strong_bank and (amount >= 5 or re.search(r"(立减金|刷卡金|还款券|支付券|满[0-9]+[-减][0-9]+)", text, re.I)))
    )
    tiny_title_only_bank_reward = (
        not body_present
        and 0 < amount < 3
        and (payment_benefit or ((has_strong_bank or rx(patterns, "bank", text) or has_payment) and (has_financial_benefit or cash_like or has_concrete_reward)))
        and not re.search(r"【(中国农业银行|农业银行|中国银行|建设银行|工商银行|交通银行|邮储银行|招商银行|中信银行|光大银行|浦发银行|广发银行|民生银行|平安银行|华夏银行)】", text)
        and not core_utility_no_ecard
        and not review_path
        and region_reason not in {"beijing", "heilongjiang-abc"}
    )
    high_ceiling_bank_payment_try = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and has_action
        and bool(re.search(r"(有大|大水|最高\s*(?:[8-9]|[1-9][0-9])|(?:[8-9]|[1-9][0-9]).{0,8}(?:立减|红包|支付券|还款券|刷卡金|试试水|有水))", text, re.I))
        and bool(re.search(r"(支付宝|微信|云闪付|立减|红包|支付券|还款券|刷卡金|数币|数字人民币)", text, re.I))
        and not soft_risk
        and not low_quality
        and not daily_bank_digest
        and not productish
        and not dominant_product
        and not merchant_coupon_only
        and not negative_blocks_offer
    )
    low_quality_strong_offer = (
        low_quality
        and (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and (has_financial_benefit or has_welfare or cash_like)
        and (amount >= 8 or ((strict_consumption or direct_utility) and amount >= 5) or (has_strong_bank and has_financial_benefit and amount >= 5))
        and (has_action or explicit_offer_title or direct_utility)
        and not title_discussion
        and not discussion
        and not negative_blocks_offer
        and not productish
        and not dominant_product
        and not merchant_coupon_only
        and not social_farming
        and not short_drama_reward
        and not platform_membership
        and not generic_pool_reward
        and not high_friction_giveaway
        and not soft_thin_welfare
    )
    bank_statement_reward = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and bool(re.search(r"(刷卡金|账单.{0,10}([0-9一二两三四五六七八九十]+|有大|大毛|小毛|券|红包)|还款.{0,10}(券|红包|立减|[0-9一二两三四五六七八九十]+)|(付款码|收付款).{0,10}(券|红包|立减|[0-9一二两三四五六七八九十]+))", text, re.I))
        and (has_action or explicit_offer_title or cash_like or direct_utility or amount >= 1)
        and not re.search(r"(利息|年费|账单.{0,12}(核对|异常|不对|多扣|被扣)|收了我|还不上|逾期)", text, re.I)
        and not daily_bank_digest
        and not expired_or_negative
        and not complaint_cue
        and not title_discussion
        and not discussion
        and not low_quality
        and not soft_risk
        and not negative_blocks_offer
        and not dominant_product
        and not merchant_coupon_only
    )
    bank_task_lifecycle_reward = (
        (has_strong_bank or rx(patterns, "bank", text))
        and bool(re.search(r"(任务|活动|报名|参加|参与|达标|资产|时点|月均|日均|存款|提升|积分|奖励|权益)", text, re.I))
        and bool(re.search(r"(开始|开了|上了|来了|可报名|能报名|报名|参加|参与|领取|可领|能领|可以领取|去领|入口|二维码|链接|直达|扫码|搜索|达标|到账|发放|补发)", text, re.I))
        and not bool(re.search(r"(怎么|咋|吗|么|有没有|哪里|在哪|谁知道|求助|不确定|领不了|不到账|没到账|找不到|扫不出|可不可以|能不能|靠谱吗|值不值)", title, re.I))
        and not daily_bank_digest
        and not negative_blocks_offer
        and not expired_or_negative
        and not complaint_cue
        and not account_problem
        and not hard_risk
        and not risk
        and not soft_risk
        and not low_quality
        and not productish
        and not dominant_product
        and not merchant_coupon_only
    )
    bank_context_reward_cue = bool(
        re.search(r"(通用?ljj|立减[金斤巾䋮jJ]|刷卡金|刷卡斤|支付[券卷劵]|还款[券卷劵]|信用卡[券劵]包|还.?用卡[券劵]|红包雨|现金红包|返现券|省钱[0-9一二三四五六七八九十.]|保底[0-9一二三四五六七八九十.]|送[0-9一二三四五六七八九十.]|给[0-9一二三四五六七八九十.]|发钱|有大|大水|放水|随机一分购|随机减|券包|(?<!券后)[券劵])", text, re.I)
    )
    active_weak_reward_cue = has_action or ongoing_cue or bool(
        re.search(r"(回归|可以领|可领|领|抽|购|送|保底|有大|发钱|放水|加速领取|领取|浏览完|扫码|打开|V打开|微信打开|底部地址|跳转|小程序)", text, re.I)
    )
    bank_payment_weak_reward = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and bank_context_reward_cue
        and (has_concrete_reward or amount >= 0.1 or cash_like or has_financial_benefit)
        and active_weak_reward_cue
        and not title_discussion
        and not discussion
        and not soft_risk
        and not low_quality
        and not broad_region_digest
        and not daily_bank_digest
        and not productish
        and not dominant_product
        and not merchant_coupon_only
        and not short_drama_reward
        and not platform_membership
        and not generic_pool_reward
        and not negative_blocks_offer
    )
    chatty_source_question = (
        bool(re.search(r"(哥哥们|老哥们|大佬们|兄弟们|吧友们)", text, re.I))
        and bool(re.search(r"(哪里有|哪有|有没有|有无|求(?:大佬|推荐|指路|链接|入口|渠道|优惠)?|求.{0,8}(?:推荐|指路|链接|入口|渠道|优惠))", text, re.I))
    )
    chatty_recovery_question = bool(
        re.search(r"(多久|什么时候|啥时候|何时).{0,16}(恢复|解除|能绑|能用|好)|(恢复|解除|能绑|能用|好).{0,16}(多久|什么时候|啥时候|何时)", text, re.I)
    )
    chatty_operation_problem = bool(
        re.search(
            r"(操作繁忙|绑定失败|绑不了|限制|异常).{0,24}(多久|怎么|怎么办|恢复|什么时候|啥时候|何时)|(多久|怎么|怎么办|恢复|什么时候|啥时候|何时).{0,24}(操作繁忙|绑定失败|绑不了|限制|异常)",
            text,
            re.I,
        )
    )
    chatty_self_contained_offer = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment or bool(re.search(r"(京东|云闪付|支付宝|微信|Apple\s*Pay)", text, re.I)))
        and bool(
            re.search(
                r"([0-9]+(?:\.[0-9]+)?[-减][0-9]+(?:\.[0-9]+)?|必中.{0,8}[0-9]|立减金|立减斤|立减巾|刷卡金|支付券|支付卷|还款券|还款卷|红包|E卡|e卡|京东卡|数币|数字人民币)",
                text,
                re.I,
            )
        )
        and (
            amount >= 8
            or bool(re.search(r"(入口|路径|直达|扫码|搜索|搜|链接|领取|可领|能领|刚领|领到|到账|亲测|自测|报名|参加)", text, re.I))
        )
        and not chatty_operation_problem
    )
    chatty_question_noise = (
        (chatty_source_question or chatty_recovery_question or chatty_operation_problem)
        and not chatty_self_contained_offer
        and not collect_first_bank_reward
        and not finance_strategy_worth
        and not conversion_worth
        and not marketplace_self_contained_worth
        and not mobile_pay_card_add_reward
        and not bank_statement_reward
        and not bank_task_lifecycle_reward
        and not card_pack_utility_coupon
        and not found_account_coupon
        and not narrow_bank_utility_digest
        and not bank_card_dining_offer
        and not local_bank_lottery_activity
    )
    low_quality_registration_task = (
        low_quality
        and bool(re.search(r"(新人|新用户|注册|下载|游戏|手游|荒野秘境|免费领)", text, re.I))
        and not (has_strong_bank or rx(patterns, "bank", text))
        and not core_utility_no_ecard
        and not mobility_payment_utility
    )
    vague_title = bool(re.fullmatch(r"(粗|冲|中|中了|有水|速度|速|大毛|小毛|新毛|自测|亲测|试试|看看|这个|刚中|去|领|抽|红包|立减金|低保|福利|补了|到了|开了|上了)", title.strip(), re.I))
    needs_body = (not body_present) and (vague_title or (len(re.sub(r"\s+", "", text)) < 80 and not product_category) or (payment_benefit and soft_risk))
    source = ctx.content_source
    personal_transaction_notice = is_personal_transaction_notice_text(text)
    unionpay_alias_reward = (
        has_payment_alias(text)
        and re.search(r"云闪付", normalized_semantic_text(text), re.I)
        and (cash_like or has_financial_benefit or has_concrete_reward or bool(re.search(r"(红包|立减|随机减|支付券|优惠)", text, re.I)))
        and has_action
        and amount >= 0.1
        and not negative_blocks_offer
        and not hard_risk
        and not account_problem
        and not dominant_product
        and not merchant_coupon_only
    )

    valuable_review_benefits = {"red_packet", "coupon", "membership", "utility", "points", "cash_like", "lottery", "mileage"}
    has_review_benefit = bool(valuable_review_benefits & set(evidence.benefit_types))
    has_review_action = bool(evidence.operation_path) or evidence.timeliness_status in {"current", "scheduled"} or evidence.probability in {"certain", "lottery"}
    hard_review_block = (
        hard_risk
        or social_hard
        or account_problem
        or personal_idle_rant
        or merchant_tax_discussion
        or payment_problem_question
        or billing_problem
        or personal_transaction_notice
        or (negative_blocks_offer and not recovered_positive)
        or (hard_complaint and not recovered_positive)
        or evidence.timeliness_status == "expired_or_negative"
    )
    bank_payment_reviewable = (
        ("bank" in evidence.entity_types or "payment" in evidence.entity_types)
        and has_review_benefit
        and has_review_action
        and not (product_low_value or dominant_product or merchant_coupon_only or productish or low_quality)
        and not hard_review_block
    )
    bank_task_lottery_reviewable = (
        ("bank" in evidence.entity_types or "payment" in evidence.entity_types)
        and ("coupon" in evidence.benefit_types or "red_packet" in evidence.benefit_types or "lottery" in evidence.benefit_types)
        and evidence.probability == "lottery"
        and evidence.timeliness_status == "current"
        and has_review_action
        and not (product_low_value or dominant_product or merchant_coupon_only or productish or low_quality)
        and not hard_review_block
    )
    jd_guess_reviewable = (
        "jd" in evidence.entity_types
        and ("red_packet" in evidence.benefit_types or "coupon" in evidence.benefit_types)
        and ("lottery" in evidence.benefit_types or re.search(r"猜大小", text, re.I))
        and not hard_review_block
        and not (dominant_product or product_low_value)
    )
    dining_membership_reviewable = (
        "dining_entertainment" in evidence.entity_types
        and ("coupon" in evidence.benefit_types or "red_packet" in evidence.benefit_types or "membership" in evidence.benefit_types)
        and has_review_action
        and "商品消费主导" not in evidence.risk_friction
        and not (product_low_value or dominant_product or productish or product_category)
        and not hard_review_block
    )
    gray_reviewable = bank_payment_reviewable or bank_task_lottery_reviewable or jd_guess_reviewable or dining_membership_reviewable

    def review_status_for(score_value: int) -> str:
        if score_value >= 65 and ("bank" in evidence.entity_types or "payment" in evidence.entity_types):
            return "高分待复核"
        return "疑似价值"

    if personal_transaction_notice and not manual_allow:
        return Decision(False, 0, 0, "personal-transaction-notice", value_label="个人账户流水/提现到账通知", content_source=source, needs_body_fetch=needs_body)
    if (broad_region_digest or multi_region_digest) and not narrow_bank_utility_digest:
        return Decision(False, 0, 0, "broad-region-digest-noise", value_label="泛地区银行活动合集", content_source=source, needs_body_fetch=needs_body)
    if non_mobile_utility_bill and not manual_allow:
        return Decision(False, 0, 0, "non-mobile-utility-bill-noise", value_label="非手机话费生活缴费小额优惠", content_source=source, needs_body_fetch=needs_body)
    if unionpay_alias_reward:
        return Decision(True, 3, 62, "unionpay-alias-red-packet", value_label="云闪付入口红包/立减，低优先级复核", content_source=source, needs_body_fetch=needs_body, evidence=evidence.to_dict())
    if shopping_survey_reward and not manual_allow:
        return Decision(False, 0, 0, "shopping-survey-reward-noise", value_label="购物调研/人群问卷礼金，不作为可复用线报权益", content_source=source, needs_body_fetch=needs_body)
    if low_value_promo_task and not manual_allow:
        return Decision(False, 0, 0, "low-value-promo-task-noise", value_label="口令/看广告/看视频等低价值任务", content_source=source, needs_body_fetch=needs_body)
    if airline_mileage_reward and not (manual_allow or hard_review_block):
        return gray_review_decision(66, "airline-mileage-high-review", source, needs_body, evidence, status="高分待复核")
    if deleted_or_stale_title and not manual_allow:
        return Decision(False, 0, 0, "deleted-or-stale-title", value_label="标题标记已删/过时", content_source=source, needs_body_fetch=needs_body)
    if low_quality_registration_task and not manual_allow:
        return Decision(False, 0, 0, "low-quality-registration-task", value_label="低质量注册/游戏任务源", content_source=source, needs_body_fetch=needs_body)
    if low_value_nonbank_registration_reward and not manual_allow:
        return Decision(False, 0, 0, "low-value-registration-reward", value_label="非银行低金额注册/实名福利", content_source=source, needs_body_fetch=needs_body)
    if tiny_nonbank_point_lottery and not manual_allow:
        return Decision(False, 0, 0, "tiny-point-lottery-reward", value_label="小额积分/抽奖福利", content_source=source, needs_body_fetch=needs_body)
    if low_certainty_nonbank_lottery and not manual_allow:
        if gray_reviewable:
            return gray_review_decision(50, "low-certainty-nonbank-lottery", source, needs_body, evidence, status="疑似价值")
        return Decision(False, 0, 0, "low-certainty-nonbank-lottery", value_label="非银行低确定性抽奖福利", content_source=source, needs_body_fetch=needs_body)
    if eligibility_problem and not manual_allow:
        return Decision(False, 0, 0, "eligibility-problem-noise", value_label="资格/实名问题帖", content_source=source, needs_body_fetch=needs_body)
    if non_financial_news and not manual_allow:
        return Decision(False, 0, 0, "non-financial-news-noise", value_label="资讯/新闻误命中", content_source=source, needs_body_fetch=needs_body)
    if gold_jewelry_purchase and not manual_allow:
        return Decision(False, 0, 0, "gold-jewelry-purchase-noise", value_label="黄金/珠宝实物询价", content_source=source, needs_body_fetch=needs_body)
    if personal_idle_rant and not manual_allow:
        return Decision(False, 0, 0, "personal-idle-rant", value_label="个人闲聊/吐槽无可操作权益", content_source=source, needs_body_fetch=needs_body)
    if bank_card_dining_offer:
        return Decision(True, 3, 62, "bank-card-dining-offer", value_label="银行信用卡餐饮/美食半价活动，低优先级复核", content_source=source, needs_body_fetch=needs_body)
    if local_bank_lottery_activity:
        return Decision(True, 3, 62, "local-bank-lottery-activity", value_label="本地允许范围内银行抽奖/活动入口，低优先级复核", content_source=source, needs_body_fetch=needs_body)
    if dining_or_grocery_primary and not manual_allow:
        if gray_reviewable:
            return gray_review_decision(45, "dining-grocery-coupon-noise", source, needs_body, evidence, status="疑似价值")
        return Decision(False, 0, 0, "dining-grocery-coupon-noise", value_label="餐饮/水果/电影等消费券主导", content_source=source, needs_body_fetch=needs_body)
    if entertainment_membership_primary and not manual_allow:
        if gray_reviewable:
            return gray_review_decision(45, "entertainment-membership-primary-noise", source, needs_body, evidence, status="疑似价值")
        return Decision(False, 0, 0, "entertainment-membership-primary-noise", value_label="会员/娱乐开通或转售主导", content_source=source, needs_body_fetch=needs_body)
    if points_product_lottery and not manual_allow:
        return Decision(False, 0, 0, "points-product-lottery-noise", value_label="积分抽奖/实物兑换低确定性", content_source=source, needs_body_fetch=needs_body)
    if task_heavy_nonbank_utility and not manual_allow:
        return Decision(False, 0, 0, "task-heavy-nonbank-utility-noise", value_label="非银行问卷/答题/积分任务型话费或缴费", content_source=source, needs_body_fetch=needs_body)
    if invoice_lottery and not manual_allow:
        return Decision(False, 0, 0, "invoice-lottery-noise", value_label="发票/开票抽奖或复盘，不是稳定可领权益", content_source=source, needs_body_fetch=needs_body)
    if soft_source_commodity_rebate and not manual_allow:
        return Decision(False, 0, 0, "soft-source-commodity-rebate-noise", value_label="软来源商品返卡/津贴主导", content_source=source, needs_body_fetch=needs_body)
    if soft_source_personal_trade_listing and not manual_allow:
        return Decision(False, 0, 0, "soft-source-personal-trade-listing", value_label="豆瓣拼组/发组个人交易或转卖帖", content_source=source, needs_body_fetch=needs_body)
    if soft_risk_small_bank_reward and not manual_allow:
        return Decision(False, 0, 0, "soft-risk-small-bank-reward-noise", value_label="受邀/黑号/测评类小额权益", content_source=source, needs_body_fetch=needs_body)
    if mobile_pay_card_add_reward:
        tier = 2 if (amount >= 10 or region_reason in {"beijing", "heilongjiang-abc"}) else 3
        return Decision(True, tier, 68 if tier == 2 else 62, "mobile-pay-card-add-reward", value_label="手机 Pay 新增/绑定银行卡送立减金，需复核", content_source=source, needs_body_fetch=needs_body)
    if ecommerce_physical_arbitrage and not manual_allow:
        return Decision(False, 0, 0, "ecommerce-physical-arbitrage-noise", value_label="电商实物/低价券套利主导", content_source=source, needs_body_fetch=needs_body)
    if product_postmortem_warning and not manual_allow:
        return Decision(False, 0, 0, "product-postmortem-warning-noise", value_label="商品返卡/支付券踩坑复盘", content_source=source, needs_body_fetch=needs_body)
    if low_value_platform_game_payment and not manual_allow:
        return Decision(False, 0, 0, "low-value-platform-game-payment-noise", value_label="平台小游戏/积分低金额支付券", content_source=source, needs_body_fetch=needs_body)
    if bank_product_consumption and not manual_allow:
        return Decision(False, 0, 0, "bank-product-consumption-noise", value_label="银行优惠但商品/会员消费主导", content_source=source, needs_body_fetch=needs_body)
    if small_nonbank_utility and not manual_allow:
        return Decision(False, 0, 0, "small-nonbank-utility-noise", value_label="非银行小额缴费/话费券", content_source=source, needs_body_fetch=needs_body)
    if social_farming and not (manual_allow or assisted_local_bank_reward):
        return Decision(False, 0, 0, "social-farming-noise", value_label="组队/助力/瓜分噪声", content_source=source, needs_body_fetch=needs_body)
    if micro_reward and not manual_allow:
        return Decision(False, 0, 0, "micro-reward-noise", value_label="0.x 元级小额任务", content_source=source, needs_body_fetch=needs_body)
    if low_probability_micro_bank_task and not manual_allow:
        return Decision(False, 0, 0, "low-probability-micro-bank-task", value_label="小额随机/非必中银行任务", content_source=source, needs_body_fetch=needs_body)
    if small_friction_reward and not manual_allow:
        return Decision(False, 0, 0, "small-friction-reward", value_label="小额答题/链接/任务噪声", content_source=source, needs_body_fetch=needs_body)
    if high_friction_giveaway and not manual_allow:
        return Decision(False, 0, 0, "high-friction-giveaway-noise", value_label="答题/评论区/链接吞等低确定性泛福利", content_source=source, needs_body_fetch=needs_body)
    if soft_thin_welfare and not manual_allow:
        return Decision(False, 0, 0, "soft-thin-welfare-noise", value_label="软来源低金额/路径不完整福利", content_source=source, needs_body_fetch=needs_body)
    if low_value_fund_payment_task and not manual_allow:
        return Decision(False, 0, 0, "low-value-fund-payment-task", value_label="低金额基金/财富号支付红包任务", content_source=source, needs_body_fetch=needs_body)
    if low_value_finance and not manual_allow:
        return Decision(False, 0, 0, "low-value-finance-task", value_label="低价值基金/理财/开户任务", content_source=source, needs_body_fetch=needs_body)
    if borrow_merchant_code_question and not manual_allow:
        return Decision(False, 0, 0, "borrow-merchant-code-question", value_label="借商家码/收款码互助询问帖", content_source=source, needs_body_fetch=needs_body)
    if problem_question and not manual_allow:
        if gray_reviewable:
            return gray_review_decision(55, "problem-question-gray-review", source, needs_body, evidence, status="疑似价值")
        return Decision(False, 0, 0, "problem-question-noise", value_label="问题/求助/路径不完整", content_source=source, needs_body_fetch=needs_body)
    if chatty_question_noise and not manual_allow:
        if gray_reviewable:
            return gray_review_decision(55, "chatty-question-gray-review", source, needs_body, evidence, status="疑似价值")
        return Decision(False, 0, 0, "chatty-question-noise", value_label="闲聊/提问/抱怨，无明确可操作权益", content_source=source, needs_body_fetch=needs_body)
    if low_certainty_actionable_question and not manual_allow:
        if gray_reviewable:
            return gray_review_decision(55, "low-certainty-actionable-gray-review", source, needs_body, evidence, status="疑似价值")
        return Decision(False, 0, 0, "low-certainty-actionable-question", value_label="疑问/受邀/软来源导致确定性不足", content_source=source, needs_body_fetch=needs_body)
    if low_value_payment_task and not manual_allow:
        return Decision(False, 0, 0, "low-value-payment-task", value_label="支付平台低金额任务", content_source=source, needs_body_fetch=needs_body)
    if short_drama_reward and not manual_allow:
        return Decision(False, 0, 0, "short-drama-reward-noise", value_label="短剧/看剧提现券噪声", content_source=source, needs_body_fetch=needs_body)
    if platform_membership and not manual_allow:
        return Decision(False, 0, 0, "platform-membership-noise", value_label="平台会员/娱乐消费小额权益", content_source=source, needs_body_fetch=needs_body)
    if generic_pool_reward and not manual_allow:
        return Decision(False, 0, 0, "generic-pool-reward-noise", value_label="瓜分/红包雨类不确定奖池", content_source=source, needs_body_fetch=needs_body)
    if payment_membership and not manual_allow:
        return Decision(False, 0, 0, "payment-membership-noise", value_label="会员/娱乐消费优惠", content_source=source, needs_body_fetch=needs_body)
    if tiny_repayment_coupon_worth:
        return Decision(True, 3, 62, "tiny-repayment-coupon-review", value_label="小额还款/花呗/信用卡券，低优先级复核", content_source=source, needs_body_fetch=needs_body)
    if bank_trial_cash_withdraw_worth:
        return Decision(True, 3, 62, "bank-trial-cash-withdraw-review", value_label="体验金收益/现金红包可提现，低优先级复核", content_source=source, needs_body_fetch=needs_body)
    if low_marketplace and not manual_allow:
        return Decision(False, 0, 0, "low-marketplace-noise", value_label="低金额闲鱼/二级市场线索", content_source=source, needs_body_fetch=needs_body)
    if marketplace_self_contained_allowed:
        return Decision(True, 3, 62, "marketplace-self-contained-worth", value_label="闲鱼/二级市场低损变现或直接可用权益，低优先级复核", content_source=source, needs_body_fetch=needs_body)
    if bank_meta_discussion and not manual_allow:
        return Decision(False, 0, 0, "bank-meta-discussion", value_label="银行/支付闲聊或问题复盘", content_source=source, needs_body_fetch=needs_body)
    if merchant_tax_discussion and not manual_allow:
        return Decision(False, 0, 0, "merchant-tax-discussion", value_label="商家码/税务讨论噪声", content_source=source, needs_body_fetch=needs_body)
    if payment_problem_question and not manual_allow:
        return Decision(False, 0, 0, "payment-problem-question", value_label="支付/收款异常问答帖", content_source=source, needs_body_fetch=needs_body)
    if billing_problem:
        return Decision(False, 0, 0, "billing-problem-discussion", value_label="账单/利息/年费问题帖", content_source=source, needs_body_fetch=needs_body)
    if participant_only_postmortem and not manual_allow:
        return Decision(False, 0, 0, "participant-only-postmortem", value_label="已参与/补发/到账类复盘", content_source=source, needs_body_fetch=needs_body)
    if title_discussion and not (manual_allow or actionable_offer or collect_first_bank_reward):
        return Decision(False, 0, 0, "title-discussion", value_label="讨论/求助/转让", content_source=source, needs_body_fetch=needs_body)
    if hard_risk and not manual_allow:
        return Decision(False, 0, 0, "hard-risk", value_label="风险或人头任务", content_source=source, needs_body_fetch=needs_body)
    if product_category and product_text and not (manual_allow or (payment_benefit and product_reusable)):
        return Decision(False, 0, 0, "product-category", value_label="纯商品低价", content_source=source, needs_body_fetch=needs_body)
    if low_quality and not (manual_allow or low_quality_strong_offer):
        return Decision(False, 0, 0, "low-quality-source", content_source=source, needs_body_fetch=needs_body)
    if risk and not soft_risk and not (manual_allow or (payment_benefit and re.search(r"【(中国农业银行|农业银行|中国银行|建设银行|工商银行|交通银行|邮储银行|招商银行|中信银行|光大银行|浦发银行|广发银行|民生银行|平安银行|华夏银行)】", text))):
        return Decision(False, 0, 0, "risk", value_label="风险或人头任务", content_source=source, needs_body_fetch=needs_body)
    if account_problem and not recovered_positive:
        return Decision(False, 0, 0, "account-risk-discussion", value_label="账号/风控/限制类求助", content_source=source, needs_body_fetch=needs_body)
    if (hard_complaint or negative_blocks_offer) and not recovered_positive:
        return Decision(False, 0, 0, "expired-or-negative", value_label="失败/过期/不可用讨论", content_source=source, needs_body_fetch=needs_body)
    if expired_or_negative and not recovered_positive:
        return Decision(False, 0, 0, "expired-or-negative", value_label="失败/过期/不可用讨论", content_source=source, needs_body_fetch=needs_body)
    if collector:
        return Decision(False, 0, 0, "collector-discussion", value_label="收藏/闲聊无可操作权益", content_source=source, needs_body_fetch=needs_body)
    if local_event_or_collectible:
        return Decision(False, 0, 0, "local-event-or-collectible", value_label="本地展会/纪念币/实物噪声", content_source=source, needs_body_fetch=needs_body)
    if app_promo_code:
        return Decision(False, 0, 0, "app-promo-code", value_label="App 口令/红包封面噪声", content_source=source, needs_body_fetch=needs_body)
    if account_or_reservation:
        return Decision(False, 0, 0, "account-or-reservation-discussion", value_label="账号/预约/行情讨论", content_source=source, needs_body_fetch=needs_body)
    if dead_low_value and not recovered_positive:
        return Decision(False, 0, 0, "dead-low-value-offer", value_label="缩水/失效/低价值尾声", content_source=source, needs_body_fetch=needs_body)
    if pure_trial_fund:
        return Decision(False, 0, 0, "pure-trial-fund-reward", value_label="纯基金/理财体验金", content_source=source, needs_body_fetch=needs_body)
    if daily_digest_noise and not narrow_bank_utility_digest:
        return Decision(False, 0, 0, "daily-digest-noise", value_label="泛银行活动合集", content_source=source, needs_body_fetch=needs_body)
    if narrow_bank_utility_digest:
        tier = 2 if (amount >= 8 or region_reason in {"beijing", "heilongjiang-abc"}) else 3
        return Decision(True, tier, 68 if tier == 2 else 62, "narrow-bank-utility-digest", value_label="窄口径银行/支付权益合集，需摘要复核", content_source=source, needs_body_fetch=needs_body)
    if game_task_welfare:
        return Decision(False, 0, 0, "game-task-welfare-noise", value_label="游戏/会员任务福利", content_source=source, needs_body_fetch=needs_body)
    if points_consumption_question:
        return Decision(False, 0, 0, "points-consumption-question", value_label="积分消耗讨论", content_source=source, needs_body_fetch=needs_body)
    if tiny_title_only_bank_reward and not manual_allow:
        return Decision(False, 0, 0, "tiny-title-only-bank-reward", value_label="标题-only 小额银行/支付权益", content_source=source, needs_body_fetch=needs_body)
    if status_only and not recovered_positive:
        return Decision(False, 0, 0, "status-only-or-postmortem", value_label="状态/复盘/限制信息", content_source=source, needs_body_fetch=needs_body)
    if retention_or_insurance:
        return Decision(False, 0, 0, "retention-or-insurance-discussion", value_label="销卡挽留/保险报价类讨论", content_source=source, needs_body_fetch=needs_body)
    if survey_only:
        return Decision(False, 0, 0, "survey-only-low-value", value_label="问卷/调研低价值任务", content_source=source, needs_body_fetch=needs_body)
    if found_account_coupon:
        return Decision(True, 3, 62, "found-account-coupon-review", value_label="账户/卡包里已发可用券，低优先级复核", content_source=source, needs_body_fetch=needs_body)
    if discussion and not payment_benefit and not strict_consumption and not review_path and not collect_first_bank_reward and not (cash_like and has_action):
        return Decision(False, 0, 0, "discussion-low-value", value_label="讨论/求助/转让", content_source=source, needs_body_fetch=needs_body)
    if (incidental_product_rebate or product_specific_marketplace or physical_freebie_product) and not review_path and not (payment_benefit and cash_like and not dominant_product):
        return Decision(False, 0, 0, "product-arbitrage", value_label="商品导购或凑单", content_source=source, needs_body_fetch=needs_body)
    if dominant_product and not review_path:
        return Decision(False, 0, 0, "product-arbitrage", value_label="商品导购或凑单", content_source=source, needs_body_fetch=needs_body)
    if merchant_coupon_only and not review_path:
        return Decision(False, 0, 0, "product-merchant", value_label="商品导购或凑单", content_source=source, needs_body_fetch=needs_body)
    if merchant_channel and not merchant_bank_payment_reward and not product_reusable and not review_path:
        return Decision(False, 0, 0, "product-merchant", value_label="商品导购或凑单", content_source=source, needs_body_fetch=needs_body)
    if product_text and food_or_commodity and not merchant_bank_payment_reward and not product_reusable and not review_path:
        return Decision(False, 0, 0, "product-commodity", value_label="商品导购或凑单", content_source=source, needs_body_fetch=needs_body)
    if product_text and (soft_product_category or discussion) and not ((has_strong_bank or rx(patterns, "bank", text)) and payment_benefit and product_reusable) and not review_path:
        return Decision(False, 0, 0, "product-discussion", value_label="商品导购或凑单", content_source=source, needs_body_fetch=needs_body)
    if discussion and not (manual_allow or actionable_offer or collect_first_bank_reward) and not re.search(r"(刚中|有水|必中|可领|速领|领[0-9一二三四五六七八九十.]+|立减金|刷卡金|支付券|红包雨|首绑|0\.01|0元|亲测|自测|卡点|每天|每月|零点|补货|补名额|还有|恢复|回来了)", text, re.I):
        return Decision(False, 0, 0, "discussion", value_label="讨论/求助/转让", content_source=source, needs_body_fetch=needs_body)
    if product_text and not (manual_allow or bank_statement_reward or (payment_benefit and (product_reusable or cash_like))):
        return Decision(False, 0, 0, "product-text", value_label="商品导购或凑单", content_source=source, needs_body_fetch=needs_body)
    if (
        not (has_strong_bank or rx(patterns, "bank", text) or has_payment or strict_consumption)
        and re.search(r"(?<![0-9-])[0-9]+(?:\.[0-9]+)?[-减][0-9]+(?:\.[0-9]+)?(?!-[0-9])", text)
    ):
        return Decision(False, 0, 0, "weak-signal", value_label="信息弱或不可操作", content_source=source, needs_body_fetch=needs_body)

    score = 0
    reasons: list[str] = []
    if has_strong_bank:
        score += 35
        reasons.append("bank")
    if has_payment:
        score += 25
        reasons.append("payment")
    if has_financial_benefit:
        score += 25
        reasons.append("financial-benefit")
    if has_concrete_reward:
        score += 10
        reasons.append("concrete-reward")
    if has_welfare:
        score += 15
        reasons.append("welfare")
    if tiny_repayment_coupon_worth:
        score += 8
        reasons.append("tiny-repayment-coupon-worth")
    if bank_trial_cash_withdraw_worth:
        score += 8
        reasons.append("bank-trial-cash-withdraw-worth")
    if bank_task_lifecycle_reward:
        score += 25
        reasons.append("bank-task-lifecycle-reward")
    if direct_utility:
        score += 8
        reasons.append("direct-utility")
    if cash_like:
        score += 6
        reasons.append("cash-like")
    if has_action:
        score += 15
        reasons.append("action")
    if region_reason == "beijing":
        score += 10
        reasons.append("beijing")
    if region_reason == "heilongjiang-abc":
        score += 8
        reasons.append("heilongjiang-abc")
    if marketplace_self_contained_allowed:
        score += 10
        reasons.append("marketplace-self-contained-worth")
    if soft_product_category:
        score -= 15
        reasons.append("soft-product-source")
    if productish:
        score -= 22
        reasons.append("productish-digest-only")
        if not direct_utility:
            score -= 18
            reasons.append("product-low-value")
    if soft_risk:
        score -= 18
        reasons.append("soft-risk-digest-only")
    if manual_allow:
        score += 40
        reasons.append("manual-allow")
    if daily_bank_digest:
        score += 8
        reasons.append("daily-bank-digest")

    if has_strong_bank and has_action and (has_financial_benefit or has_welfare) and not productish and not soft_risk:
        score = max(score, 80)
        reasons.append("bank-actionable-floor")
    elif has_payment and has_action and (has_financial_benefit or has_welfare) and not productish and not soft_risk:
        score = max(score, 70)
        reasons.append("payment-actionable-floor")

    if soft_risk or productish or product_text or product_primary or review_path or soft_product_category or low_quality or (vague_title and not body_present):
        score = min(score, 78)
    if productish and not direct_utility:
        score = min(score, 52)
    if discussion and not actionable_offer:
        score = min(score, 65)
    if actionable_offer:
        score = max(score, 65)
    needs_body = needs_body or ((not body_present) and score >= 68)
    thin_payment_reward = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and (has_financial_benefit or has_concrete_reward or cash_like or re.search(r"(笔笔减|碰一碰|碰一下|红包雨|立减|刷卡金|支付券|随机减|满减)", text, re.I))
        and not (soft_risk or low_quality or daily_bank_digest or productish or product_category or dominant_product or merchant_coupon_only or negative_blocks_offer)
    )
    utility_thin_reward = (
        practical_product_utility
        and (has_financial_benefit or has_concrete_reward or cash_like)
        and not (soft_risk or low_quality or dominant_product or merchant_coupon_only or negative_blocks_offer)
    )
    generic_actionable = (
        payment_benefit
        or strict_consumption
        or mobility_payment_utility
        or marketplace_self_contained_allowed
        or conversion_worth
        or ((has_strong_bank or rx(patterns, "bank", text) or has_payment) and cash_like and has_action and amount >= 1 and not productish and not product_category)
    )
    a1_evidence_complete = meaningful_content and (
        has_action
        or direct_utility
        or re.search(r"([0-9]+[-减][0-9]+|立减金|刷卡金|支付券|还款券|红包|数币|数字人民币|E卡|e卡|京东卡|话费|油卡)", content_text, re.I)
    )
    a1_high_confidence = amount >= 8 and (core_utility_no_ecard or cash_like or practical_product_utility)

    a1_eligible = (
        (has_action or direct_utility)
        and (has_strong_bank or has_payment)
        and has_financial_benefit
        and (has_concrete_reward or cash_like or direct_utility)
        and generic_actionable
        and a1_evidence_complete
        and a1_high_confidence
        and body_present
        and not (
            soft_risk
            or productish
            or product_category
            or product_text
            or product_primary
            or review_path
            or soft_product_category
            or low_quality
            or daily_bank_digest
            or discussion
            or title_discussion
            or expired_or_negative
            or complaint_cue
            or future_schedule
            or dominant_product
            or merchant_coupon_only
            or bank_payment_weak_reward
        )
    )

    if score >= 80 and a1_eligible:
        return Decision(True, 1, min(score, 100), "+".join(reasons), value_label="马上可用的银行/信用卡/支付优惠", content_source=source, needs_body_fetch=needs_body)
    if bank_statement_reward:
        tier = 2 if (amount >= 8 or region_reason in {"beijing", "heilongjiang-abc"}) else 3
        return Decision(True, tier, max(score, 68 if tier == 2 else 62), "+".join(reasons) or "bank-statement-reward", value_label="账单/刷卡金类银行支付权益，需摘要复核", content_source=source, needs_body_fetch=needs_body)
    if bank_task_lifecycle_reward:
        return Decision(True, 1, max(score, 85), "+".join(reasons) or "bank-task-lifecycle-reward", value_label="银行任务/达标奖励开始或可领取", content_source=source, needs_body_fetch=needs_body)
    if collect_first_bank_reward:
        return Decision(True, 2, max(score, 68), "+".join(reasons) or "collect-first-bank-reward", value_label="先领取再确认用法的银行权益，需复核", content_source=source, needs_body_fetch=needs_body)
    if card_pack_utility_coupon:
        return Decision(True, 3, max(score, 62), "+".join(reasons) or "hidden-cardpack-utility-coupon-review", value_label="卡包/券包里可能有可消耗权益，低优先级复核", content_source=source, needs_body_fetch=needs_body)
    compact_bank_reward = (
        (has_strong_bank or rx(patterns, "bank", text) or has_payment)
        and has_concrete_reward
        and amount >= 1
        and (has_action or explicit_offer_title or user_utility)
        and not productish
        and not soft_risk
        and not negative_blocks_offer
    )
    soft_risk_bank_offer = (
        payment_benefit
        and has_concrete_reward
        and (amount >= 5 or ((region_reason in {"beijing", "heilongjiang-abc"}) and amount >= 1))
        and (has_action or explicit_offer_title)
        and soft_risk
        and not product_category
        and not dominant_product
        and not merchant_coupon_only
        and not negative_blocks_offer
    )
    if score >= 65 and generic_actionable:
        generic_tier = 2 if score >= 78 and not soft_risk and not soft_product_category and not low_quality and not title_discussion and not discussion and source != "list" else 3
        return Decision(True, generic_tier, score if generic_tier == 2 else max(62, min(score, 67)), "+".join(reasons), value_label="可领福利/支付优惠，价值中等", content_source=source, needs_body_fetch=needs_body)
    if compact_bank_reward and score >= 48:
        tier = 2 if ((amount >= 8 and meaningful_content and not soft_product_category and not title_discussion and not discussion) or (region_reason in {"beijing", "heilongjiang-abc"} and amount >= 5 and meaningful_content)) else 3
        return Decision(True, tier, max(score, 68 if tier == 2 else 62), "+".join(reasons) or "compact-bank-reward", value_label="银行/支付短帖明确金额权益", content_source=source, needs_body_fetch=needs_body)
    if soft_risk_bank_offer and score >= 50:
        tier = 2 if ((amount >= 20 and meaningful_content) or ((region_reason in {"beijing", "heilongjiang-abc"}) and amount >= 8 and meaningful_content)) else 3
        return Decision(True, tier, max(score, 68 if tier == 2 else 62), "+".join(reasons) or "soft-risk-bank-offer", value_label="受邀/看脸但有明确银行权益，低优先级复核", content_source=source, needs_body_fetch=needs_body)
    if bank_payment_weak_reward and score >= 50:
        tier = 2 if ((amount >= 8 and meaningful_content and not title_discussion and not discussion) or ((region_reason in {"beijing", "heilongjiang-abc"}) and amount >= 5 and meaningful_content and not title_discussion and not discussion)) else 3
        return Decision(True, tier, max(score, 68 if tier == 2 else 62), "+".join(reasons) or "bank-payment-weak-reward", value_label="短标题银行/支付权益，需摘要复核", content_source=source, needs_body_fetch=needs_body)
    if thin_payment_reward and score >= 50:
        tier = 2 if ((amount >= 8 or region_reason in {"beijing", "heilongjiang-abc"}) and meaningful_content and not title_discussion and not discussion) else 3
        return Decision(True, tier, max(score, 68 if tier == 2 else 62), "+".join(reasons) or "thin-payment-reward", value_label="短帖银行/支付权益，需摘要复核", content_source=source, needs_body_fetch=needs_body)
    if utility_thin_reward and score >= 48:
        tier = 2 if ((amount >= 8 or region_reason in {"beijing", "heilongjiang-abc"}) and meaningful_content and not title_discussion and not discussion) else 3
        return Decision(True, tier, max(score, 68 if tier == 2 else 62), "+".join(reasons) or "thin-utility-reward", value_label="话费/缴费/E卡等短帖福利", content_source=source, needs_body_fetch=needs_body)
    if high_ceiling_bank_payment_try and score >= 58:
        return Decision(True, 3, max(score, 62), "+".join(reasons) or "high-ceiling-bank-payment-review", value_label="银行/支付高上限抽券，低优先级复核", content_source=source, needs_body_fetch=needs_body)
    if gray_reviewable and score >= 45:
        return gray_review_decision(score, "weak-signal", source, needs_body, evidence, status=review_status_for(score))
    return Decision(False, 0, score, "weak-signal", value_label="信号弱或不可操作", content_source=source, needs_body_fetch=needs_body)


def push(post: dict[str, Any], decision: Decision, dry_run: bool) -> None:
    title = str(post.get("title") or "线报酷")
    content = str(post.get("body") or post.get("content") or "").strip()
    if re.fullmatch(r"(rt|如题|如图|看图|图|1|一|无|空)", content, re.I):
        content = ""
    if len(content) > 420:
        content = content[:420] + "..."
    body = content or title
    url = full_url(post)

    if dry_run:
        print(f"DRY-RUN push tier={decision.tier} score={decision.score} {title} {url}")
        return

    if os.getenv("XIANBAO_USE_WEBOPS_BARK") == "1":
        profile_root = Path(os.getenv("HERMES_HOME", "/srv/pylon/profiles/default"))
        sys.path.insert(0, str(profile_root))
        from lib.webops.bark import send_push

        result = send_push(
            title=title,
            body=body,
            url=url,
            group=os.getenv("XIANBAO_BARK_GROUP", "xianbao"),
            level=os.getenv("XIANBAO_BARK_LEVEL", "active"),
            volume=env_int("XIANBAO_BARK_VOLUME", 5),
            secret_name=os.getenv("XIANBAO_BARK_SECRET_NAME") or None,
            timeout_seconds=env_int("XIANBAO_PUSH_TIMEOUT_SECONDS", 15),
        )
        if not result.ok:
            raise RuntimeError(f"WebOps Bark push failed: status={result.http_status} code={result.code} message={result.message}")
        return

    bark_url = os.getenv("BARK_URL")
    if bark_url:
        target = bark_url.rstrip("/") + "/" + urllib.parse.quote(title) + "/" + urllib.parse.quote(body)
        target += "?" + urllib.parse.urlencode({"url": url})
        urllib.request.urlopen(target, timeout=15).read()
        return

    webhook = os.getenv("PUSH_WEBHOOK_URL")
    if webhook:
        payload = json.dumps({"title": title, "body": body, "url": url, "tier": decision.tier, "score": decision.score}).encode("utf-8")
        req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
        return

    raise RuntimeError("No push endpoint configured. Set BARK_URL or PUSH_WEBHOOK_URL, or run with XIANBAO_DRY_RUN=1.")


def validate_runtime_config(dry_run: bool) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    bark_url = (os.getenv("BARK_URL") or "").strip()
    webhook = (os.getenv("PUSH_WEBHOOK_URL") or "").strip()
    webops_bark = os.getenv("XIANBAO_USE_WEBOPS_BARK") == "1"
    push_targets = [value for value in (bark_url, webhook) if value]
    if webops_bark:
        push_targets.append("XIANBAO_USE_WEBOPS_BARK")

    if len(push_targets) > 1:
        errors.append("set only one push target: XIANBAO_USE_WEBOPS_BARK=1, BARK_URL, or PUSH_WEBHOOK_URL")
    if not dry_run and not push_targets:
        errors.append("missing push target: set XIANBAO_USE_WEBOPS_BARK=1, BARK_URL, or PUSH_WEBHOOK_URL, or set XIANBAO_DRY_RUN=1")
    for name, value in (("BARK_URL", bark_url), ("PUSH_WEBHOOK_URL", webhook)):
        if value and not re.match(r"^https?://", value, re.I):
            errors.append(f"{name} must start with http:// or https://")
        if value and re.search(r"REPLACE|example\.com", value, re.I):
            errors.append(f"{name} still looks like a placeholder")
    if dry_run and not push_targets:
        warnings.append("dry-run mode has no push target configured")
    if webops_bark:
        profile_root = Path(os.getenv("HERMES_HOME", "/srv/pylon/profiles/default"))
        bark_helper = profile_root / "lib" / "webops" / "bark.py"
        if not bark_helper.exists():
            errors.append(f"missing WebOps Bark helper: {bark_helper}")
        secrets_dir = profile_root / "secrets"
        secret_candidates = []
        secret_name = os.getenv("XIANBAO_BARK_SECRET_NAME", "").strip()
        if secret_name and not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", secret_name):
            errors.append("XIANBAO_BARK_SECRET_NAME must be 1-80 chars: letters, numbers, dot, underscore, dash")
        if secret_name:
            secret_candidates.append(secrets_dir / secret_name)
        secret_candidates.extend((secrets_dir / "bark_device_key", secrets_dir / "bark_arbitrage_device_key"))
        if not any(path.exists() and path.stat().st_size > 0 for path in secret_candidates):
            errors.append(f"missing WebOps Bark secret under {secrets_dir}")
    return errors, warnings


def rotate_jsonl_if_needed(path: Path, max_bytes: int) -> None:
    if max_bytes <= 0 or not path.exists() or path.stat().st_size < max_bytes:
        return
    rotated = path.with_name(path.name + ".1")
    path.replace(rotated)


def append_jsonl(path: Path, payload: dict[str, Any], max_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rotate_jsonl_if_needed(path, max_bytes)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def decision_payload(post: dict[str, Any], decision: Decision) -> dict[str, Any]:
    payload = {
        "ts": int(time.time()),
        "id": post.get("id"),
        "title": post.get("title"),
        "catename": post.get("catename"),
        "url": full_url(post),
        "decision": decision.__dict__,
    }
    evidence = decision.evidence if isinstance(decision.evidence, dict) else {}
    if decision.duplicate_of:
        payload["duplicate_of"] = decision.duplicate_of
    if evidence.get("duplicate_group_id"):
        payload["duplicate_group_id"] = evidence.get("duplicate_group_id")
    if "material_change" in evidence:
        payload["material_change"] = bool(evidence.get("material_change"))
    if evidence.get("material_change_reason_cn"):
        payload["material_change_reason_cn"] = evidence.get("material_change_reason_cn")
    return payload


def log_decision(data_dir: Path, post: dict[str, Any], decision: Decision) -> bool:
    if decision.duplicate and os.getenv("XIANBAO_LOG_DUPLICATES") != "1":
        return False
    payload = decision_payload(post, decision)
    append_jsonl(
        data_dir / "decisions.jsonl",
        payload,
        env_int("XIANBAO_MAX_DECISION_LOG_BYTES", DEFAULT_DECISION_LOG_MAX_BYTES),
    )
    return True


def log_poll_summary(data_dir: Path, summary: dict[str, Any]) -> None:
    append_jsonl(
        data_dir / "poll_summaries.jsonl",
        summary,
        env_int("XIANBAO_MAX_SUMMARY_LOG_BYTES", DEFAULT_SUMMARY_LOG_MAX_BYTES),
    )


def log_poll_error_summary(data_dir: Path, exc: Exception, consecutive_errors: int) -> None:
    global LAST_POLL_SUMMARY
    payload: dict[str, Any] = {
        "ts": int(time.time()),
        "poll_error": True,
        "consecutive_errors": consecutive_errors,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    if isinstance(exc, FetchJsonDecodeError):
        payload.update(
            {
                "fetch_url": exc.url,
                "fetch_status": exc.status,
                "fetch_content_type": exc.content_type,
                "fetch_bytes": exc.raw_len,
                "fetch_preview": exc.preview[:240],
            }
        )
    LAST_POLL_SUMMARY = payload
    log_poll_summary(data_dir, payload)


def log_push_error(data_dir: Path, post: dict[str, Any], decision: Decision, exc: Exception) -> None:
    payload = decision_payload(post, decision)
    payload["push_error"] = str(exc)
    append_jsonl(
        data_dir / "push_errors.jsonl",
        payload,
        env_int("XIANBAO_MAX_DECISION_LOG_BYTES", DEFAULT_DECISION_LOG_MAX_BYTES),
    )


def alert_runtime_error(exc: Exception, consecutive_errors: int) -> None:
    global LAST_ERROR_ALERT_AT
    threshold = env_int("XIANBAO_ALERT_CONSECUTIVE_ERRORS", 3)
    if threshold <= 0 or consecutive_errors < threshold:
        return
    cooldown = env_int("XIANBAO_ALERT_COOLDOWN_SECONDS", 900)
    now = int(time.time())
    if cooldown > 0 and now - LAST_ERROR_ALERT_AT < cooldown:
        return
    script = Path(os.getenv("XIANBAO_ALERT_SCRIPT", "/srv/pylon/profiles/default/scripts/xianbao_alert.py"))
    if not script.exists():
        print(f"alert script missing: {script}", file=sys.stderr, flush=True)
        LAST_ERROR_ALERT_AT = now
        return
    message = (
        f"worker loop has {consecutive_errors} consecutive poll errors; "
        f"latest error: {type(exc).__name__}: {exc}"
    )
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--kind",
                "runtime-error",
                "--message",
                message,
                "--cooldown-seconds",
                str(cooldown),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            print(f"alert send failed: {detail}", file=sys.stderr, flush=True)
        LAST_ERROR_ALERT_AT = now
    except Exception as alert_exc:
        LAST_ERROR_ALERT_AT = now
        print(f"alert send failed: {alert_exc}", file=sys.stderr, flush=True)


def source_unavailable_summary(summary: dict[str, Any]) -> bool:
    return bool(summary.get("poll_json_source_unavailable") or summary.get("poll_json_fallback"))


def update_source_unavailable_state(data_dir: Path, summary: dict[str, Any]) -> None:
    state_path = data_dir / SOURCE_UNAVAILABLE_STATE_FILE
    if not source_unavailable_summary(summary):
        if state_path.exists():
            try:
                state_path.unlink()
            except OSError:
                pass
        return

    now = int(time.time())
    state: dict[str, Any] = {}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    consecutive = int(state.get("consecutive", 0) or 0) + 1
    state.pop("last_alert_at", None)
    cache_age = summary.get("poll_json_fallback_age_seconds")
    stale_window = env_int("XIANBAO_POLL_JSON_STALE_IF_ERROR_SECONDS", DEFAULT_POLL_JSON_STALE_IF_ERROR_SECONDS)
    source_url = str(summary.get("poll_json_fallback_url") or summary.get("poll_json_source_url") or "")
    state.update(
        {
            "consecutive": consecutive,
            "first_seen_at": int(state.get("first_seen_at", now) or now),
            "last_seen_at": now,
            "url": source_url,
            "cache_age_seconds": cache_age,
            "stale_if_error_seconds": stale_window,
            "last_error_type": str(summary.get("poll_json_source_error_type") or summary.get("poll_json_fallback_error_type") or ""),
            "last_error": str(summary.get("poll_json_source_error") or summary.get("poll_json_fallback_error") or ""),
        }
    )

    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(state_path)


def maybe_fetch_body(
    post: dict[str, Any],
    decision: Decision,
    rules: dict[str, Any],
    db: sqlite3.Connection,
    remaining: int,
) -> tuple[dict[str, Any], int, bool]:
    evaluation = rules.get("evaluation", {})
    if not evaluation.get("fetchBodyBeforeDecision", False):
        return post, remaining, False
    if remaining <= 0 or str(post.get("body") or "").strip():
        return post, remaining, False
    should_fetch = decision.needs_body_fetch or decision.keep or (45 <= decision.score <= 85)
    if not should_fetch:
        return post, remaining, False
    body, source, fetched = fetch_body_cached(
        db,
        post,
        int(evaluation.get("bodyCacheTtlSeconds", DEFAULT_BODY_CACHE_TTL_SECONDS)),
        int(evaluation.get("bodyFetchErrorTtlSeconds", DEFAULT_BODY_ERROR_CACHE_TTL_SECONDS)),
    )
    updated = dict(post)
    updated["body"] = body
    updated["body_source"] = source
    return updated, remaining - 1 if fetched else remaining, fetched


def process_once(
    rules: dict[str, Any],
    db: sqlite3.Connection,
    data_dir: Path,
    dry_run: bool,
    send_tier2: bool,
    send_tier3: bool,
    tier3_min_score: int,
) -> int:
    global LAST_POLL_SUMMARY
    allow_terms = load_terms(data_dir / "allow_terms.txt")
    block_terms = load_terms(data_dir / "block_terms.txt")
    posts, poll_json_fallback = fetch_json_cached(db, poll_json_urls(rules["polling"]["pushJson"]))
    sent = 0
    kept = 0
    duplicates = 0
    activity_duplicates = 0
    activity_material_changes = 0
    body_fetches = 0
    body_fetch_promotions = 0
    body_fetch_demotions = 0
    review_candidates = 0
    push_errors = 0
    skipped_seen_cached = 0
    skipped_seen_rejects = 0
    skipped_seen_keep_nonpush = 0
    logged_decisions = 0
    reason_counts: Counter[str] = Counter()
    rules_key = rules_hash(rules)
    prune_stats = prune_runtime_state(db, rules, rules_key)
    body_fetch_remaining = int(rules.get("evaluation", {}).get("maxBodyFetchesPerPoll", 8))
    for post in reversed(posts[: int(rules["polling"].get("maxPushRowsPerPoll", 150))]):
        fp = fingerprint(post)
        seen_fp = seen_fingerprint(post)
        skip_reason = seen_skip_reason(db, post, rules_key, seen_fp, send_tier2, send_tier3, tier3_min_score)
        if skip_reason:
            skipped_seen_cached += 1
            if skip_reason == "seen-reject":
                skipped_seen_rejects += 1
            elif skip_reason == "seen-keep-nonpush":
                skipped_seen_keep_nonpush += 1
            reason_counts[skip_reason] += 1
            continue
        decision = classify(post, rules, allow_terms, block_terms)
        dup, dup_of = near_duplicate(db, post, fp)
        if dup:
            duplicates += 1
            decision.duplicate = True
            decision.duplicate_of = dup_of
            reason_counts[decision.reason] += 1
            logged_decisions += int(log_decision(data_dir, post, decision))
            continue
        before_fetch_decision = decision
        post, body_fetch_remaining, fetched_body = maybe_fetch_body(post, decision, rules, db, body_fetch_remaining)
        body_fetches += int(fetched_body)
        if str(post.get("body") or "").strip():
            decision = classify(post, rules, allow_terms, block_terms)
            if fetched_body:
                before_rank = 2 if before_fetch_decision.keep else 1 if before_fetch_decision.review_candidate else 0
                after_rank = 2 if decision.keep else 1 if decision.review_candidate else 0
                body_fetch_promotions += int(after_rank > before_rank)
                body_fetch_demotions += int(after_rank < before_rank)
        activity_dup = activity_duplicate_check(db, post)
        identity = activity_dup.get("identity") if isinstance(activity_dup, dict) else {}
        if isinstance(identity, dict):
            if decision.evidence is None:
                decision.evidence = {}
            decision.evidence["activity_identity"] = identity
        if activity_dup.get("material_change"):
            activity_material_changes += 1
            if decision.evidence is None:
                decision.evidence = {}
            decision.evidence["material_change"] = True
            decision.evidence["material_change_reason_cn"] = activity_dup.get("material_change_reason_cn", "")
        if activity_dup.get("duplicate") and not activity_dup.get("material_change"):
            duplicates += 1
            activity_duplicates += 1
            decision.duplicate = True
            decision.duplicate_of = str(activity_dup.get("duplicate_of") or "")
            if decision.evidence is None:
                decision.evidence = {}
            decision.evidence["duplicate_group_id"] = activity_dup.get("duplicate_group_id")
            decision.evidence["material_change"] = False
            logged_decisions += int(log_decision(data_dir, post, decision))
            mark_seen_post(db, post, decision, rules_key, seen_fp)
            continue
        if decision.evidence is None:
            decision.evidence = {}
        decision.evidence["rule_version"] = rules_key
        decision.evidence["decision_engine"] = "deterministic-v1"
        should_push = should_push_decision(decision, send_tier2, send_tier3, tier3_min_score)
        reason_counts[decision.reason] += 1
        kept += int(decision.keep)
        review_candidates += int(decision.review_candidate)
        logged_decisions += int(log_decision(data_dir, post, decision))
        if not should_push:
            mark_seen_post(db, post, decision, rules_key, seen_fp)
            record_activity_observation(db, post, decision, pushed=False)
        if should_push:
            try:
                push(post, decision, dry_run)
            except Exception as exc:
                push_errors += 1
                reason_counts["push-error"] += 1
                log_push_error(data_dir, post, decision, exc)
                print(f"push error: id={post.get('id')} title={post.get('title')!r}: {exc}", file=sys.stderr, flush=True)
                continue
            if dry_run:
                continue
            mark_seen_post(db, post, decision, rules_key, seen_fp)
            record_activity_observation(db, post, decision, pushed=True)
            db.execute(
                "insert or replace into sent_posts(id, fingerprint, title, url, tier, score, sent_at) values (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(post.get("id") or post.get("url") or full_url(post)),
                    fp,
                    str(post.get("title") or ""),
                    full_url(post),
                    decision.tier,
                    decision.score,
                    int(time.time()),
                ),
            )
            db.commit()
            sent += 1
    LAST_POLL_SUMMARY = {
        "ts": int(time.time()),
        "seen": min(len(posts), int(rules["polling"].get("maxPushRowsPerPoll", 150))),
        "kept": kept,
        "sent": sent,
        "duplicates": duplicates,
        "activity_duplicates": activity_duplicates,
        "activity_material_changes": activity_material_changes,
        "body_fetches": body_fetches,
        "body_fetch_promotions": body_fetch_promotions,
        "body_fetch_demotions": body_fetch_demotions,
        "review_candidates": review_candidates,
        "decision_engine": "deterministic-v1",
        "rules_hash": rules_key,
        "push_errors": push_errors,
        "skipped_seen_cached": skipped_seen_cached,
        "skipped_seen_rejects": skipped_seen_rejects,
        "skipped_seen_keep_nonpush": skipped_seen_keep_nonpush,
        "body_fetch_remaining": body_fetch_remaining,
        "logged_decisions": logged_decisions,
        "send_tier2": bool(send_tier2),
        "send_tier3": bool(send_tier3),
        "tier3_min_score": int(tier3_min_score),
        "reason_top": reason_counts.most_common(12),
    }
    if prune_stats:
        LAST_POLL_SUMMARY["pruned"] = prune_stats
    if poll_json_fallback:
        LAST_POLL_SUMMARY.update(poll_json_fallback)
    log_poll_summary(data_dir, LAST_POLL_SUMMARY)
    update_source_unavailable_state(data_dir, LAST_POLL_SUMMARY)
    return sent


def validate_rules(rules: dict[str, Any]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    def get_path(path: str) -> Any:
        current: Any = rules
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                errors.append(f"missing required key: {path}")
                return None
            current = current[part]
        return current

    for key in (
        "polling.pushJson",
        "polling.intervalSeconds",
        "polling.maxPushRowsPerPoll",
        "evaluation.fetchBodyBeforeDecision",
        "evaluation.maxBodyFetchesPerPoll",
        "evaluation.bodyCacheTtlSeconds",
        "positive",
        "negative",
        "regionPolicy.rejectOtherRegions",
        "regionPolicy.heilongjiangAllowOnlyIf",
    ):
        get_path(key)

    push_url = get_path("polling.pushJson")
    if push_url and not str(push_url).startswith("https://"):
        warnings.append("polling.pushJson is not https")
    interval = get_path("polling.intervalSeconds")
    if isinstance(interval, int) and interval < 10:
        warnings.append("polling.intervalSeconds below 10 may be too aggressive")
    max_rows = get_path("polling.maxPushRowsPerPoll")
    if isinstance(max_rows, int) and max_rows > 300:
        warnings.append("polling.maxPushRowsPerPoll above 300 may increase CPU/log volume")
    max_body = get_path("evaluation.maxBodyFetchesPerPoll")
    if isinstance(max_body, int) and max_body > 20:
        warnings.append("evaluation.maxBodyFetchesPerPoll above 20 may hammer article pages")

    regex_sections = ("positive", "negative", "websiteSide")
    for section in regex_sections:
        values = rules.get(section, {})
        if not isinstance(values, dict):
            errors.append(f"{section} must be an object")
            continue
        for name, pattern in values.items():
            if not isinstance(pattern, str) or not pattern:
                continue
            try:
                re.compile(pattern, re.I)
            except re.error as exc:
                errors.append(f"invalid regex {section}.{name}: {exc}")

    region = rules.get("regionPolicy", {})
    if isinstance(region, dict):
        for name in ("rejectOtherRegions", "rejectOtherRegionsExtra", "heilongjiangAllowOnlyIf"):
            value = region.get(name)
            if not value:
                continue
            if not isinstance(value, (str, list, tuple)):
                errors.append(f"regionPolicy.{name} must be a string or array of regex fragments")
                continue
            for pattern in regex_fragments(value):
                try:
                    re.compile(str(pattern), re.I)
                except re.error as exc:
                    errors.append(f"invalid regex regionPolicy.{name}: {exc}")
    else:
        errors.append("regionPolicy must be an object")

    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--rules", default=os.getenv("XIANBAO_RULES", DEFAULT_RULES))
    parser.add_argument("--state", default=os.getenv("XIANBAO_STATE", DEFAULT_STATE))
    parser.add_argument("--data-dir", default=os.getenv("XIANBAO_DATA_DIR", DEFAULT_DATA_DIR))
    parser.add_argument("--send-tier2", action="store_true", default=os.getenv("XIANBAO_SEND_TIER2") == "1")
    parser.add_argument("--send-tier3", action="store_true", default=os.getenv("XIANBAO_SEND_TIER3") == "1")
    parser.add_argument("--tier3-min-score", type=int, default=env_int("XIANBAO_TIER3_MIN_SCORE", 62))
    args = parser.parse_args()

    dry_run = args.dry_run or os.getenv("XIANBAO_DRY_RUN") == "1"
    rules = load_json(Path(args.rules))
    errors, warnings = validate_rules(rules)
    runtime_errors, runtime_warnings = validate_runtime_config(dry_run)
    errors.extend(runtime_errors)
    warnings.extend(runtime_warnings)
    if args.validate:
        print(json.dumps({"ok": not errors, "errors": errors, "warnings": warnings}, ensure_ascii=False, sort_keys=True))
        return 0 if not errors else 1
    if errors:
        raise SystemExit("invalid Xianbao rules: " + "; ".join(errors))
    for warning in warnings:
        print(f"rules warning: {warning}", file=sys.stderr, flush=True)
    db = ensure_db(Path(args.state))
    data_dir = Path(args.data_dir)
    interval = int(rules["polling"].get("intervalSeconds", 20))
    consecutive_errors = 0

    while True:
        try:
            sent = process_once(
                rules,
                db,
                data_dir,
                dry_run=dry_run,
                send_tier2=args.send_tier2,
                send_tier3=args.send_tier3,
                tier3_min_score=args.tier3_min_score,
            )
            consecutive_errors = 0
            summary = LAST_POLL_SUMMARY
            print(
                "processed poll "
                f"sent={sent} seen={summary.get('seen', 0)} kept={summary.get('kept', 0)} "
                f"duplicates={summary.get('duplicates', 0)} body_fetches={summary.get('body_fetches', 0)}",
                flush=True,
            )
        except Exception as exc:
            consecutive_errors += 1
            print(f"poll error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            log_poll_error_summary(data_dir, exc, consecutive_errors)
            alert_runtime_error(exc, consecutive_errors)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
