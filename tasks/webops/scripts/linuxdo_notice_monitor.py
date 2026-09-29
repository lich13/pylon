#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import http.client
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.webops import locks, output, paths, state_json

PRIMARY_CHANNEL = "LinuxDoNew"
SUPPLEMENT_CHANNEL = "linux_do_channel"
LINUXDO_JOB_ID = "b7a84d1d9f21"
HTTP_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

CONFIG_FILE = paths.PROFILE_ROOT / "config" / "linuxdo_notice_monitor.json"
STATE_FILE = paths.CRON_STATE_DIR / "linuxdo_notice.json"
LINUXDO_DATA_DIR = paths.DATA_DIR / "linuxdo_notice"
DECISION_LOG_FILE = LINUXDO_DATA_DIR / "decisions.jsonl"

FETCH_RETRY_DELAY_SECONDS = 1.0
RUN_BUDGET_SECONDS = 45
DELIVERY_QUEUE_STATE_KEY = "delivery_queue"
RULE_VERSION = "deterministic-v1"

DECISION_LOG_TEXT_MAX_CHARS = 360
DECISION_LOG_EVIDENCE_MAX_CHARS = 700
DECISION_LOG_SKIP_CATEGORIES = {
    "low_signal",
}
DECISION_LOG_HIGH_SIGNAL_SILENT_CATEGORIES = {
    "public_site_shutdown_low_value",
    "public_site_chatter_low_value",
    "research_academic_low_value",
    "support_troubleshooting_help",
    "forum_chatter_low_value",
    "design_resource_low_value",
    "game_resource_low_value",
    "domain_registration_welfare",
    "ambiguous_consumer_welfare_low_value",
    "technical_reference",
    "domestic_or_non_top_model_welfare",
    "non_beijing_local_welfare",
    "user_excluded_service_welfare",
    "welfare_question_low_value",
    "welfare_subscription_question_low_value",
    "non_frontline_model",
    "domestic_only_model",
    "email_account_giveaway",
    "infrastructure_hosting_welfare",
    "media_subscription_welfare",
    "private_tracker_welfare",
    "forum_currency_chatter",
    "question_discussion_low_value",
}

INVISIBLE_PROMPT_CHARS_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e]")
LINUXDO_TOPIC_RE = re.compile(r"https?://linux\.do/t/(?:topic|[^/\s]+)/(\d+)(?:/(\d+))?", re.I)
URL_RE = re.compile(r"https?://[^\s<>()\"']+")
IMAGE_URL_RE = re.compile(r"https?://[^\s\"'()<>]+?\.(?:png|jpe?g|webp|gif)(?:\?[^\s\"'()<>]*)?", re.I)
RELATIVE_IMAGE_URL_RE = re.compile(r"/[^\s\"'()<>]+?\.(?:png|jpe?g|webp|gif)(?:\?[^\s\"'()<>]*)?", re.I)
TOPIC_TEXT_MAX_CHARS = 900
EVIDENCE_TEXT_MAX_CHARS = 700

NON_FRONTLINE_MODEL_TERMS = [
    "lumo",
    "proton",
    "qwen",
    "通义千问",
    "千问",
    "qwq",
    "qvq",
    "kimi",
    "moonshot",
    "月之暗面",
    "ernie",
    "文心",
    "混元",
    "hunyuan",
    "doubao",
    "豆包",
    "seed",
    "glm",
    "chatglm",
    "智谱",
    "yi-",
    "零一万物",
    "deepseek",
    "minimax",
    "海螺",
    "baichuan",
    "百川",
    "stepfun",
    "阶跃",
    "sensenova",
    "商汤",
    "日日新",
    "internlm",
    "书生",
    "skywork",
    "天工",
    "modelscope",
    "魔搭",
    "xunfei",
    "讯飞",
    "星火",
    "mimo",
    "xiaomi mimo",
    "小米mimo",
    "小米 mimo",
    "小米",
]

DEFAULT_DOMESTIC_MODEL_FAMILIES = {
    "qwen": [
        "qwen",
        "通义千问",
        "千问",
        "通义",
        "qwq",
        "qvq",
        "qwen-coder",
        "qwen3-coder",
        "阿里云百炼",
        "百炼",
        "dashscope",
        "model studio",
        "aliyun",
        "通义灵码",
        "qoder",
    ],
    "deepseek": ["deepseek", "深度求索"],
    "kimi": ["kimi", "moonshot", "moonshot-v1", "月之暗面", "kimi k2", "kimi code"],
    "glm": ["glm", "chatglm", "智谱", "zhipu", "bigmodel", "智谱清言", "清言", "z.ai", "zai", "autoglm"],
    "doubao": ["doubao", "豆包", "doubao-seed", "字节", "火山引擎", "火山方舟", "火山", "方舟", "volcengine"],
    "ernie": ["ernie", "ernie bot", "文心", "文心一言", "百度千帆", "千帆", "qianfan", "baidu"],
    "hunyuan": ["hunyuan", "混元", "腾讯元宝", "元宝"],
    "minimax": ["minimax", "海螺", "hailuo", "abab", "星野"],
    "xunfei": ["spark", "讯飞", "科大讯飞", "星火", "讯飞星火", "iflytek", "xf-yun", "xfyun"],
    "yi": ["yi-", "yi large", "yi-coder", "零一万物", "01.ai", "01ai"],
    "baichuan": ["baichuan", "百川"],
    "stepfun": ["stepfun", "step-", "step plan", "阶跃", "阶跃星辰", "跃问"],
    "sensenova": ["sensenova", "商汤", "日日新", "sensetime"],
    "internlm": ["internlm", "书生", "书生浦语"],
    "skywork": ["skywork", "天工"],
    "pangu": ["pangu", "pangulm", "盘古", "华为云盘古"],
    "siliconflow": ["siliconflow", "siliconcloud", "硅基流动"],
    "openbmb": ["openbmb", "minicpm", "面壁智能"],
    "xverse": ["xverse", "360智脑"],
    "mimo": ["mimo", "xiaomi mimo", "小米mimo", "小米 mimo", "小米"],
    "modelscope": ["modelscope", "魔搭", "api-inference"],
    "opencode": ["opencode", "opencode go"],
}

DEFAULT_TOP_MODEL_FAMILIES = {
    "gpt": ["GPT", "GPT-4", "GPT-5", "ChatGPT"],
    "claude": ["Claude", "Sonnet", "Opus"],
    "grok": ["Grok"],
    "gemini": ["Gemini"],
}

DEFAULT_DOMESTIC_ONLY_SIGNALS = [
    "仅国产模型",
    "只支持国产模型",
    "只有国产模型",
    "仅支持国产模型",
    "国模",
    "国产大模型",
    "only domestic",
    "domestic only",
    "国产 only",
    "国产模型 only",
    "仅 DeepSeek",
    "只有 DeepSeek",
    "只支持 DeepSeek",
    "仅支持 DeepSeek",
]

DEFAULT_DOMESTIC_MODEL_RESOURCE_TERMS = [
    "glm",
    "智谱",
    "qwen",
    "qoder",
    "kimi",
    "deepseek",
    "doubao",
    "豆包",
    "minimax",
    "火山",
    "方舟",
    "文心",
    "ernie",
    "混元",
    "hunyuan",
    "yi-",
    "零一万物",
    "mimo",
    "xiaomi mimo",
    "小米mimo",
    "小米 mimo",
    "mimo-tts",
    "opencode",
    "opencode go",
    "商汤",
    "sensetime",
    "日日新",
    "讯飞",
    "科大讯飞",
    "星火",
    "spark",
    "xf-yun",
    "xfyun",
]

DEFAULT_DOMESTIC_MODEL_PROVIDER_TERMS = [
    "智谱",
    "zhipu",
    "阿里",
    "aliyun",
    "通义",
    "月之暗面",
    "kimi",
    "deepseek",
    "字节",
    "火山",
    "volcengine",
    "方舟",
    "minimax",
    "小米",
    "xiaomi",
    "mimo",
    "opencode",
    "百度",
    "文心",
    "腾讯",
    "混元",
    "商汤",
    "sensetime",
    "讯飞",
    "科大讯飞",
    "星火",
    "iflytek",
    "xf-yun",
    "xfyun",
    "联通",
    "中国联通",
    "cucloud",
    "cucloud.cn",
    "aigw",
    "aigw-gzgy",
    "移动云",
    "中国移动",
    "ecloud",
    "电信",
    "天翼云",
    "天翼",
    "ctyun",
]

DEFAULT_EXCLUDED_INFRASTRUCTURE_WELFARE_TERMS = [
    "搬瓦工",
    "bandwagon",
    "bandwagonhost",
]

DEFAULT_EXCLUDED_MEDIA_SUBSCRIPTION_TERMS = [
    "喜马拉雅",
    "ximalaya",
    "波点音乐",
    "波点",
    "bodian",
]

DEFAULT_EXCLUDED_PRIVATE_TRACKER_TERMS = [
    "PTTIME",
    "PT 站",
    "PT站",
    "PT 邀请",
    "PT邀请",
    "PT 邀请码",
    "PT邀请码",
]

DEFAULT_FORUM_CURRENCY_CHATTER_TERMS = [
    "LDC",
    "论坛币",
    "站内币",
]

DEFAULT_MODEL_RESOURCE_CONTEXT_TERMS = [
    "coding plan",
    "token plan",
    "体验卡",
    "套餐",
    "订阅",
    "key",
    "token",
    "api key",
    "额度",
    "积分",
    "公益站",
    "公益中转",
    "中转站",
    "模型列表",
    "模型接入",
    "Base URL",
    "baseurl",
    "key填任意值",
]

DEFAULT_TOP_MODEL_RESOURCE_REQUIRED_TERMS = [
    "模型列表",
    "支持",
    "提供",
    "包含",
    "接入",
    "可用",
    "开放",
    "额度",
    "不限",
    "Base URL",
    "baseurl",
    "api key",
    "token",
    "key填任意值",
    "team",
    "账号",
    "子号",
]

DEFAULT_TOP_MODEL_NON_RESOURCE_CONTEXT_TERMS = [
    "Claude Code",
    "Claude Max",
    "Cursor",
    "OpenAI-compatible",
    "兼容",
    "对比",
    "倍",
    "用量",
    "教程",
    "工具",
    "未说明",
    "没有说明",
    "非顶级",
    "不是",
    "仅作对比",
    "订阅问题",
    "涨价",
    "报错",
    "错误",
    "失败",
    "provider",
    "/model",
    "model 选择",
]


@dataclass
class LinuxDoPost:
    channel: str
    message_id: int
    telegram_url: str
    posted_at: str
    topic_id: str
    topic_url: str
    topic_post_no: str
    title: str
    author: str
    section: str
    topic_created_at: str
    summary: str
    source_role: str
    photo_urls: list[str] = field(default_factory=list)

    @property
    def is_reply(self) -> bool:
        try:
            return bool(self.topic_post_no and int(self.topic_post_no) > 1)
        except ValueError:
            return bool(self.topic_post_no and self.topic_post_no != "1")


@dataclass
class RouteDecision:
    route: str
    reason: str = ""
    matched_categories: list[str] = field(default_factory=list)


def strip_invisible_prompt_chars(text: str) -> str:
    return INVISIBLE_PROMPT_CHARS_RE.sub("", str(text or ""))


def sanitize_for_prompt(value: Any) -> Any:
    if isinstance(value, str):
        return strip_invisible_prompt_chars(value)
    if isinstance(value, list):
        return [sanitize_for_prompt(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_prompt(item) for item in value)
    if isinstance(value, dict):
        return {sanitize_for_prompt(key): sanitize_for_prompt(val) for key, val in value.items()}
    return value


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def deadline_after(seconds: int) -> float:
    return time.monotonic() + max(1, seconds)


def deadline_remaining(deadline: float | None) -> float:
    if deadline is None:
        return 999999.0
    return max(0.0, deadline - time.monotonic())


def strip_tags(text: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return strip_invisible_prompt_chars(html.unescape(" ".join(text.split()))).strip()


def _compact_text(text: str) -> str:
    return strip_invisible_prompt_chars(" ".join(str(text or "").split())).strip()


def _truncate_text(text: str, max_chars: int) -> str:
    compacted = _compact_text(text)
    if len(compacted) <= max_chars:
        return compacted
    return compacted[: max_chars - 1].rstrip() + "..."


def _unique_strings(values: list[Any], *, limit: int = 12) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _compact_text(str(value or ""))
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
        if len(result) >= limit:
            break
    return result


def default_config() -> dict:
    return {
        "primary_channel": PRIMARY_CHANNEL,
        "supplement_channel": SUPPLEMENT_CHANNEL,
        "supplement_enabled": True,
        "priority_authors": [],
        "max_pages": 6,
        "bootstrap_limit": 60,
        "dedupe_window_hours": 72,
        "fetch_timeout_seconds": 8,
        "fetch_attempts": 3,
        "run_budget_seconds": RUN_BUDGET_SECONDS,
        "topic_json_enabled": True,
        "welfare_full_push_enabled": True,
        "delivered_topic_ttl_hours": 168,
        "top_model_families": {key: list(aliases) for key, aliases in DEFAULT_TOP_MODEL_FAMILIES.items()},
        "domestic_model_families": {key: list(aliases) for key, aliases in DEFAULT_DOMESTIC_MODEL_FAMILIES.items()},
        "non_frontline_model_terms": list(NON_FRONTLINE_MODEL_TERMS),
        "domestic_only_signals": list(DEFAULT_DOMESTIC_ONLY_SIGNALS),
        "domestic_model_resource_terms": list(DEFAULT_DOMESTIC_MODEL_RESOURCE_TERMS),
        "domestic_model_provider_terms": list(DEFAULT_DOMESTIC_MODEL_PROVIDER_TERMS),
        "model_resource_context_terms": list(DEFAULT_MODEL_RESOURCE_CONTEXT_TERMS),
        "top_model_resource_required_terms": list(DEFAULT_TOP_MODEL_RESOURCE_REQUIRED_TERMS),
        "top_model_non_resource_context_terms": list(DEFAULT_TOP_MODEL_NON_RESOURCE_CONTEXT_TERMS),
        "model_stream_enabled": True,
        "allowed_local_regions": ["北京"],
        "local_welfare_keywords": ["地区", "线下", "门店", "到店", "核销", "自提", "同城", "咖啡", "移动", "联通", "电信", "流量"],
        "non_beijing_local_regions": [
            "上海",
            "山西",
            "天津",
            "重庆",
            "河北",
            "河南",
            "山东",
            "江苏",
            "浙江",
            "安徽",
            "福建",
            "江西",
            "湖北",
            "湖南",
            "广东",
            "广西",
            "海南",
            "四川",
            "贵州",
            "云南",
            "陕西",
            "甘肃",
            "青海",
            "辽宁",
            "吉林",
            "黑龙江",
            "内蒙古",
            "宁夏",
            "新疆",
            "西藏",
            "香港",
            "澳门",
            "台湾",
        ],
        "excluded_service_welfare_terms": ["WPS"],
        "excluded_infrastructure_welfare_terms": list(DEFAULT_EXCLUDED_INFRASTRUCTURE_WELFARE_TERMS),
        "excluded_media_subscription_terms": list(DEFAULT_EXCLUDED_MEDIA_SUBSCRIPTION_TERMS),
        "excluded_private_tracker_terms": list(DEFAULT_EXCLUDED_PRIVATE_TRACKER_TERMS),
        "forum_currency_chatter_terms": list(DEFAULT_FORUM_CURRENCY_CHATTER_TERMS),
        "excluded_domain_welfare_domains": ["netcup.com", "namecheap.com", "spaceship.com", "dynadot.com", "godaddy.com", "namesilo.com"],
        "excluded_domain_welfare_terms": ["域名", "domain", "registrar", "de域名", ".de", "续费", "注册"],
        "ambiguous_consumer_welfare_terms": ["袋鼠", "雪王", "奶茶", "咖啡", "饮料", "外卖", "立减金", "话费券"],
        "design_resource_domains": ["figma.com", "canva.com", "adobe.com", "framer.com"],
        "design_resource_keywords": [
            "Figma",
            "Canva",
            "Adobe",
            "Framer",
            "设计",
            "素材",
            "模板",
            "UI kit",
            "图标",
            "design credits",
            "制作积分",
        ],
        "game_resource_domains": [
            "store.epicgames.com",
            "steampowered.com",
            "steamcommunity.com",
            "gog.com",
            "itch.io",
            "xbox.com",
            "playstation.com",
            "nintendo.com",
            "taptap.cn",
        ],
        "game_resource_keywords": [
            "游戏",
            "手游",
            "Epic",
            "Steam",
            "GOG",
            "DLC",
            "礼包",
            "道具",
            "皮肤",
            "限免",
            "立省",
            "免费获取",
            "回合制策略游戏",
        ],
        "public_site_chatter_tags": [
            "纯水",
            "搞七捻三",
            "快问快答",
            "闲聊",
        ],
        "public_site_action_required_terms": [
            "Base URL",
            "baseurl",
            "key填任意值",
            "api key",
            "token",
            "兑换码",
            "补货",
            "补了",
            "恢复",
            "已恢复",
            "修复",
            "扩容",
            "开放",
            "新增",
            "规则",
            "额度政策",
            "模型列表",
            "模型接入",
            "IP限制",
            "封禁",
            "封号",
            "不限额度",
            "不限并发",
            "不限速",
            "火力全开",
        ],
        "public_site_terminal_status_terms": [
            "关闭",
            "关了",
            "关站",
            "停运",
            "停止服务",
            "停止提供",
            "停止维护",
            "不再维护",
            "下线",
            "结束服务",
            "服务结束",
            "不开放",
            "永久关闭",
            "跑路",
            "没了",
            "寄了",
            "失效",
            "不可用",
            "不再提供",
            "shutdown",
            "closed",
            "stop service",
        ],
        "public_site_actionable_exception_terms": [
            "Base URL",
            "baseurl",
            "新地址",
            "新网址",
            "迁移",
            "迁到",
            "替代",
            "备用",
            "补偿",
            "补货",
            "补了",
            "兑换码",
            "key填任意值",
            "api key",
            "token",
            "领取",
            "可领取",
            "恢复",
            "已恢复",
            "重开",
            "重新开放",
            "扩容",
            "开放",
            "新增",
            "规则",
            "额度政策",
            "模型接入",
            "模型列表",
            "IP限制",
            "封禁",
            "封号",
            "不限额度",
            "不限并发",
            "不限速",
            "GPT",
            "Claude",
            "Grok",
            "Gemini",
            "Codex",
        ],
        "research_academic_domains": [
            "arxiv.org",
            "doi.org",
            "scholar.google.com",
            "pubmed.ncbi.nlm.nih.gov",
            "researchgate.net",
            "semanticscholar.org",
            "sciencedirect.com",
            "springer.com",
            "nature.com",
            "ieee.org",
            "acm.org",
        ],
        "research_academic_keywords": [
            "科研",
            "论文",
            "学术",
            "期刊",
            "文献",
            "数据集",
            "实验报告",
            "科研绘图",
            "科研工具",
            "PDF",
            "arXiv",
            "DOI",
            "Google Scholar",
            "PubMed",
            "research paper",
            "academic",
            "journal",
            "literature",
            "dataset",
        ],
        "mixed_model_policy": "top_tier_wins",
        "frontline_model_allowlist": [
            "codex",
            "gpt",
            "openai",
            "chatgpt",
            "claude",
            "sonnet",
            "opus",
            "gemini pro",
            "gemini 2.5 pro",
            "gemini 3",
            "grok",
        ],
        "public_site_keywords": [
            "公益站",
            "公益中转",
            "中转站",
            "codex公益",
            "claude公益",
            "gpt公益",
            "api公益",
            "不限额度",
            "不限并发",
            "不限速",
            "火力全开",
            "免费额度",
            "key填任意值",
            "兑换码",
            "邀请码",
        ],
        "email_account_giveaway_keywords": [
            "hotmail",
            "outlook",
            "gmail",
            "邮箱",
            "邮箱账号",
            "mail account",
            "email account",
        ],
        "welfare_refresh_keywords": [
            "补货",
            "补了",
            "追加",
            "新增",
            "继续送",
            "兑换码",
            "key",
            "token",
            "附件",
            "zip",
            "额度",
            "不限",
            "规则",
            "封禁",
            "恢复",
            "可用",
            "状态",
            "更新",
        ],
        "technical_reference_keywords": [
            "教程",
            "指南",
            "经验",
            "经验帖",
            "博客",
            "参数",
            "命令",
            "使用",
            "配置",
            "技巧",
            "ripgrep",
            "rg --",
        ],
        "support_troubleshooting_keywords": [
            "求助",
            "请教",
            "报错",
            "错误",
            "失败",
            "不支持",
            "not supported",
            "error=",
            "valid levels",
            "怎么解决",
            "如何解决",
            "配置",
            "provider",
            "model 选择",
            "/model",
            "openai-compatibility",
            "api-key",
            "xhigh",
            "levels",
            "CPA",
            "自定义供应商",
        ],
        "tool_keywords": [
            "Codex",
            "OpenAI",
            "ChatGPT",
            "Claude",
            "Gemini",
            "Grok",
            "Cursor",
            "API",
            "2api",
            "反代",
            "中转",
            "Workers",
            "开源",
            "GitHub",
            "Docker",
            "自托管",
        ],
        "status_keywords": [
            "IP限制",
            "封禁",
            "封号",
            "风控",
            "恢复",
            "不可用",
            "报错",
            "5xx",
            "50X",
            "Cloudflare",
            "卡",
            "降智",
            "额度",
            "限速",
        ],
        "generic_question_keywords": [
            "求助",
            "请教",
            "咨询",
            "有没有",
            "有无",
            "如何",
            "怎么",
            "吗",
            "？",
            "?",
            "求推荐",
            "想问",
        ],
    }


def load_config() -> dict:
    cfg = default_config()
    data = state_json.load(CONFIG_FILE, default={})
    if isinstance(data, dict):
        for key, value in data.items():
            cfg[key] = value
    return cfg


def _is_retryable_fetch_error(exc: Exception) -> bool:
    if isinstance(exc, (http.client.IncompleteRead, TimeoutError)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 425, 429, 500, 502, 503, 504}
    return isinstance(exc, urllib.error.URLError)


def fetch(
    url: str,
    *,
    timeout: int = 8,
    attempts: int = 3,
    phase: str = "fetch",
) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": HTTP_USER_AGENT})
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:
            elapsed = time.monotonic() - started
            print(
                f"linuxdo_fetch_error phase={phase} attempt={attempt}/{attempts} "
                f"elapsed={elapsed:.2f}s error={type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            if not _is_retryable_fetch_error(exc) or attempt >= attempts:
                raise
            last_error = exc
            time.sleep(FETCH_RETRY_DELAY_SECONDS * attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError("fetch() exited without result")


def fetch_with_curl(url: str, *, timeout: int = 8, phase: str = "curl_fetch") -> str:
    completed = subprocess.run(
        [
            "curl",
            "-L",
            "-sS",
            "--max-time",
            str(max(1, timeout)),
            "-H",
            f"User-Agent: {HTTP_USER_AGENT}",
            str(url),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=max(2, timeout + 2),
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{phase} failed rc={completed.returncode}: {_truncate_text(completed.stderr, 200)}")
    return completed.stdout


def fetch_json(
    url: str,
    *,
    timeout: int = 8,
    attempts: int = 3,
    phase: str = "fetch_json",
) -> dict:
    try:
        text = fetch(url, timeout=timeout, attempts=attempts, phase=phase)
    except urllib.error.HTTPError as exc:
        parsed = urlparse(str(url))
        if exc.code != 403 or not parsed.netloc.endswith("linux.do") or phase != "topic_json":
            raise
        text = fetch_with_curl(url, timeout=timeout, phase=phase)
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _extract_first(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.S | re.I)
    return _compact_text(match.group(1)) if match else ""


def _extract_topic_from_html(text_html: str, text_plain: str) -> tuple[str, str, str]:
    candidates = re.findall(r'href="([^"]+)"', text_html, flags=re.I)
    candidates.extend(URL_RE.findall(text_plain))
    for candidate in candidates:
        url = html.unescape(candidate)
        match = LINUXDO_TOPIC_RE.search(url)
        if match:
            topic_id = match.group(1)
            post_no = match.group(2) or ""
            topic_url = f"https://linux.do/t/topic/{topic_id}" + (f"/{post_no}" if post_no else "")
            return topic_id, topic_url, post_no
    return "", "", ""


def _extract_telegram_photo_urls(chunk: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"background-image:url\((.*?)\)", str(chunk or ""), flags=re.S | re.I):
        url = html.unescape(match.group(1).strip().strip("\\\"'"))
        if url and url not in urls:
            urls.append(url)
    return urls


def _parse_structured_post(raw_text: str) -> dict:
    return {
        "title": _extract_first(r"标题:\s*(.*?)\s*作者:", raw_text),
        "author": _extract_first(r"作者:\s*(.*?)\s*板块:", raw_text),
        "section": _extract_first(r"板块:\s*(.*?)\s*编号:", raw_text),
        "topic_created_at": _extract_first(r"时间:\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", raw_text),
        "summary": _extract_first(r"摘要:\s*(.*)$", raw_text),
    }


def _parse_unstructured_post(raw_text: str) -> dict:
    match = re.search(r"^(.*?)\s+在\s+(.*?)\s+中发帖\s*(.*)$", raw_text, flags=re.S)
    if match:
        return {
            "author": _compact_text(match.group(1)),
            "title": _compact_text(match.group(2)),
            "summary": _compact_text(match.group(3)),
            "section": "",
            "topic_created_at": "",
        }
    return {"author": "", "title": raw_text[:120], "summary": raw_text, "section": "", "topic_created_at": ""}


def parse_channel_posts(html_text: str, channel: str, source_role: str) -> list[LinuxDoPost]:
    marker = f'data-post="{channel}/'
    posts: list[LinuxDoPost] = []
    for chunk in str(html_text or "").split(marker)[1:]:
        id_part, _, rest = chunk.partition('"')
        try:
            message_id = int(id_part)
        except ValueError:
            continue

        text_match = re.search(
            r'<div class="tgme_widget_message_text js-message_text" dir="auto">(.*?)</div>',
            rest,
            flags=re.S | re.I,
        )
        if not text_match:
            continue
        text_html = text_match.group(1)
        raw_text = strip_tags(text_html)
        if not raw_text:
            continue

        topic_id, topic_url, topic_post_no = _extract_topic_from_html(text_html, raw_text)
        if not topic_id:
            continue

        if raw_text.startswith("标题:"):
            fields = _parse_structured_post(raw_text)
        else:
            fields = _parse_unstructured_post(raw_text)

        title = fields.get("title") or raw_text[:120]
        time_match = re.search(r'<time datetime="([^"]+)"', rest, flags=re.I)
        posted_at = time_match.group(1) if time_match else ""
        posts.append(
            LinuxDoPost(
                channel=channel,
                message_id=message_id,
                telegram_url=f"https://t.me/{channel}/{message_id}",
                posted_at=posted_at,
                topic_id=topic_id,
                topic_url=topic_url,
                topic_post_no=topic_post_no,
                title=_compact_text(title),
                author=_compact_text(fields.get("author") or ""),
                section=_compact_text(fields.get("section") or ""),
                topic_created_at=_compact_text(fields.get("topic_created_at") or ""),
                summary=_compact_text(fields.get("summary") or ""),
                source_role=source_role,
                photo_urls=_extract_telegram_photo_urls(rest),
            )
        )
    posts.sort(key=lambda post: post.message_id, reverse=True)
    return posts


def _post_sort_key(post: LinuxDoPost) -> tuple[int, int]:
    role_score = 1 if post.source_role == "primary" else 0
    return (role_score, post.message_id)


def dedupe_posts(posts: list[LinuxDoPost]) -> list[LinuxDoPost]:
    by_topic: dict[str, LinuxDoPost] = {}
    for post in posts:
        key = post.topic_id or f"{post.channel}/{post.message_id}"
        current = by_topic.get(key)
        if current is None or _post_sort_key(post) > _post_sort_key(current):
            by_topic[key] = post
    return sorted(by_topic.values(), key=lambda post: post.message_id, reverse=True)


def _contains_any(text: str, terms: list[Any]) -> bool:
    lowered = text.lower()
    return any(str(term or "").strip().lower() in lowered for term in terms if str(term or "").strip())


def _config_list(config: dict, key: str, default: list[str]) -> list[str]:
    value = config.get(key)
    if isinstance(value, list):
        result = [str(item).strip() for item in value if str(item or "").strip()]
        return result or list(default)
    return list(default)


def _top_model_family_aliases(config: dict) -> dict[str, list[str]]:
    value = config.get("top_model_families")
    if isinstance(value, dict):
        result: dict[str, list[str]] = {}
        for family, aliases in value.items():
            name = str(family or "").strip().lower()
            if not name:
                continue
            if isinstance(aliases, list):
                family_aliases = [str(alias).strip() for alias in aliases if str(alias or "").strip()]
            else:
                family_aliases = [str(aliases).strip()] if str(aliases or "").strip() else []
            result[name] = family_aliases or [name]
        if result:
            return result
    if isinstance(value, list):
        result: dict[str, list[str]] = {}
        for item in value:
            name = str(item or "").strip().lower()
            if name in {"gpt", "claude", "grok", "gemini"}:
                result[name] = DEFAULT_TOP_MODEL_FAMILIES[name]
        if result:
            return result
    return {key: list(aliases) for key, aliases in DEFAULT_TOP_MODEL_FAMILIES.items()}


def _domestic_model_family_aliases(config: dict) -> dict[str, list[str]]:
    value = config.get("domestic_model_families")
    if isinstance(value, dict):
        result: dict[str, list[str]] = {}
        for family, aliases in value.items():
            name = str(family or "").strip().lower()
            if not name:
                continue
            if isinstance(aliases, list):
                family_aliases = [str(alias).strip() for alias in aliases if str(alias or "").strip()]
            else:
                family_aliases = [str(aliases).strip()] if str(aliases or "").strip() else []
            if family_aliases:
                result[name] = family_aliases
        if result:
            return result
    return {key: list(aliases) for key, aliases in DEFAULT_DOMESTIC_MODEL_FAMILIES.items()}


def _domestic_model_alias_terms(config: dict) -> list[str]:
    terms: list[str] = []
    for aliases in _domestic_model_family_aliases(config).values():
        terms.extend(aliases)
    return _unique_strings(terms, limit=220)


def _term_pattern(term: str) -> str:
    text = str(term or "").strip()
    escaped = re.escape(text)
    if re.fullmatch(r"[A-Za-z0-9 .+_-]+", text):
        return rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"
    return escaped


def _match_terms_with_context(blob: str, terms: list[str], *, limit: int = 12, context_required: bool = False) -> list[str]:
    matches: list[str] = []
    for term in terms:
        text = str(term or "").strip()
        if not text:
            continue
        for match in re.finditer(_term_pattern(text), blob, flags=re.I):
            if context_required:
                snippet = _context_window(blob, match.start(), match.end(), radius=56)
                if not _model_resource_action_context(snippet, {}):
                    continue
            matches.append(text)
            break
    return _unique_strings(matches, limit=limit)


def _post_blob(post: LinuxDoPost) -> str:
    return _compact_text(" ".join([post.title, post.author, post.section, post.summary]))


def _normalize_author(author: str) -> str:
    text = _compact_text(author).lstrip("#")
    return text.lower()


def _is_priority_author(post: LinuxDoPost, config: dict) -> bool:
    wanted = {_normalize_author(author) for author in config.get("priority_authors", []) if str(author or "").strip()}
    return bool(wanted and _normalize_author(post.author) in wanted)


def _is_generic_question(post: LinuxDoPost, config: dict, blob: str) -> bool:
    if not _contains_any(blob, config.get("generic_question_keywords", [])):
        return False
    high_signal_terms = list(config.get("public_site_keywords", [])) + list(config.get("status_keywords", []))
    return not _contains_any(blob, high_signal_terms)


def _has_question_signal(blob: str, config: dict) -> bool:
    return _contains_any(blob, config.get("generic_question_keywords", []))


def _is_welfare_topic(post: LinuxDoPost, evidence: dict | None = None) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    section = _compact_text(post.section)
    if "福利羊毛" in section:
        return True
    title = _compact_text(post.title)
    topic_text = _compact_text(str(evidence.get("topic_text") or ""))
    return bool(re.search(r"(?:^|[#\s:：/])福利羊毛(?:$|[#\s:：/])", " ".join([title, topic_text])))


def _is_welfare_new_topic(post: LinuxDoPost, config: dict, evidence: dict | None = None) -> bool:
    return bool(config.get("welfare_full_push_enabled", True) and _is_welfare_topic(post, evidence) and not post.is_reply)


def _is_email_account_giveaway(blob: str, config: dict) -> bool:
    lowered = str(blob or "").lower()
    if not _contains_any(lowered, config.get("email_account_giveaway_keywords", [])):
        return False
    giveaway_context = re.search(r"(?:放|送|赠|给|分享|发|丢|领|领取|先到先得|批量|一批|\d+\s*个).{0,40}(?:hotmail|outlook|gmail|邮箱|mail\s+account|email\s+account)", lowered, flags=re.I)
    account_context = re.search(r"(?:邮箱|mail\s+account|email\s+account|账号|account).{0,30}(?:密码|password|----|:|：)", lowered, flags=re.I)
    provider_account_context = re.search(r"(?:hotmail|outlook|gmail).{0,30}(?:账号|account|密码|password|----)", lowered, flags=re.I)
    batch_email_passwords = len(re.findall(r"[\w.+-]+@(?:gmail|hotmail|outlook)\.com.{0,12}(?:----|:|：|密码|password)", lowered, flags=re.I)) >= 1
    return bool(giveaway_context or account_context or provider_account_context or batch_email_passwords)


def _has_reply_refresh_signal(blob: str, config: dict) -> bool:
    return _contains_any(blob, config.get("welfare_refresh_keywords", []))


def _has_invite_code_action(text: str) -> bool:
    blob = str(text or "")
    if re.search(r"(?:求|有没有|有无|谁有|想要|给一个|来一个|蹲|收).{0,24}(?:邀请码|invite\s*code)", blob, flags=re.I):
        return False
    return bool(
        re.search(
            r"(?:邀请码|invite\s*code).{0,30}(?:领取|发放|兑换|补货|可用|送|分享|限时|先到先得|放出|开放|免费|赠|填任意值)"
            r"|(?:领取|发放|兑换|补货|可用|送|分享|限时|先到先得|放出|开放|免费|赠).{0,30}(?:邀请码|invite\s*code)",
            blob,
            flags=re.I,
        )
    )


def _has_actionable_resource_terms(text: str) -> bool:
    blob = str(text or "")
    strong_terms = re.search(
        r"(?:不限额度|不限并发|不限速|火力全开|兑换码|补货|补了|继续送|key\s*填任意值|team\s*bug|team子号|sub2|su2api|免费领取|领取|可用)",
        blob,
        flags=re.I,
    )
    token_action = re.search(
        r"(?:token|api\s*key|key).{0,24}(?:领取|获取|可用|免费|分享|送|赠|兑换|填任意值)|(?:领取|获取|分享|送|赠|兑换).{0,24}(?:token|api\s*key|key)",
        blob,
        flags=re.I,
    )
    public_site_action = re.search(
        r"(?:公益站|公益中转|中转站).{0,30}(?:网址|地址|链接|活动|开放|扩容|流畅|不限|额度|兑换码|补货|免费|可用)",
        blob,
        flags=re.I,
    )
    direct_url_action = re.search(r"(?:网址|地址|Base\s*URL|baseurl)[:：]?\s*https?://", blob, flags=re.I)
    return bool(strong_terms or token_action or public_site_action or direct_url_action or _has_invite_code_action(blob))


def _has_resource_status_update_signal(blob: str, config: dict) -> bool:
    text = str(blob or "")
    if _has_actionable_resource_terms(text):
        return True
    public_site_terms = r"(?:公益站|公益中转|中转站|RawChat|Codex|OpenAI|Claude|GPT|API)"
    status_update_terms = r"(?:恢复|扩容|规则|封禁|封号|IP限制|额度|不限|限速|降智|开放|可用|不可用|5xx|50X|Cloudflare)"
    return bool(re.search(public_site_terms + r".{0,36}" + status_update_terms, text, flags=re.I))


def _evidence_context_blob(post: LinuxDoPost, evidence: dict | None) -> str:
    evidence = evidence if isinstance(evidence, dict) else {}
    return _compact_text(
        " ".join(
            [
                _post_blob(post),
                str(evidence.get("topic_text") or ""),
                str(evidence.get("topic_category") or ""),
                " ".join(str(item) for item in evidence.get("topic_tags") or []),
                " ".join(str(item) for item in evidence.get("link_titles") or []),
                str(evidence.get("reference_url") or ""),
                str(evidence.get("resource_url") or ""),
                str(evidence.get("access_method") or ""),
                str(evidence.get("limits") or ""),
                str(evidence.get("provider") or ""),
                str(evidence.get("why") or ""),
            ]
        )
    )


def _public_site_signal_facts(blob: str, evidence: dict | None, config: dict) -> dict:
    evidence = evidence if isinstance(evidence, dict) else {}
    resource_url = str(evidence.get("resource_url") or "").strip()
    access_method = str(evidence.get("access_method") or "").strip()
    limits = str(evidence.get("limits") or "").strip()
    model_coverage = evidence.get("model_coverage") if isinstance(evidence.get("model_coverage"), dict) else {}
    action_terms = _config_list(config, "public_site_action_required_terms", [])
    terminal_terms = _config_list(config, "public_site_terminal_status_terms", [])
    actionable_exception_terms = _config_list(config, "public_site_actionable_exception_terms", [])
    negative_access_context = bool(re.search(r"(?:领取|访问|打开|官网).{0,16}(?:空白|报错|错误|失败|403|502|5xx|打不开)", blob, flags=re.I))
    terminal_status = bool(
        _contains_any(blob, terminal_terms)
        or re.search(
            r"(?:公益站|公益中转|中转站|服务|站点|本站|本服务).{0,36}(?:关闭|关站|停运|停止(?:服务|提供|维护)|不再(?:维护|提供)|下线|结束服务|永久关闭|跑路|没了|寄了|失效|不可用|shutdown|closed|stop\\s+service)"
            r"|(?:关闭|关站|停运|停止(?:服务|提供|维护)|不再(?:维护|提供)|下线|结束服务|永久关闭|跑路|没了|寄了|失效|不可用|shutdown|closed|stop\\s+service).{0,36}(?:公益站|公益中转|中转站|服务|站点|本站|本服务)",
            blob,
            flags=re.I,
        )
    )
    negated_exception_context = bool(
        re.search(
            r"(?:没有|无|未|不提供|不再提供|缺少|暂无).{0,24}(?:Base\s*URL|baseurl|新(?:地址|网址|入口)|迁移|迁到|替代|备用|兑换码|key|token|api[-_ ]?key|领取|补偿|恢复|重开|重新开放)",
            blob,
            flags=re.I,
        )
    )
    explicit_action = bool(
        _contains_any(blob, action_terms)
        or re.search(
            r"(?:Base\s*URL|baseurl|api[-_ ]?key|token|key\s*填任意值|兑换码|补货|补了|恢复|已恢复|修复|扩容|开放|新增|模型列表|模型接入|IP限制|封禁|封号|不限额度|不限并发|不限速|火力全开)",
            blob,
            flags=re.I,
        )
        or _has_invite_code_action(blob)
    )
    terminal_actionable_exception = bool(
        not negated_exception_context
        and (
            re.search(
                r"(?:关闭|关站|停运|停止(?:服务|提供|维护)|不再(?:维护|提供)|下线|结束服务|永久关闭).{0,80}(?:迁移|迁到|新(?:地址|网址|入口)|替代|备用|重开|重新开放|恢复|已恢复|补偿.{0,12}(?:领取|兑换码|key|token|额度)|可领取|Base\s*URL|baseurl|https?://|api[-_ ]?key|token|key\s*填任意值|兑换码)"
                r"|(?:迁移|迁到|新(?:地址|网址|入口)|替代|备用|重开|重新开放|恢复|已恢复|补偿.{0,12}(?:领取|兑换码|key|token|额度)|可领取|Base\s*URL|baseurl|https?://|api[-_ ]?key|token|key\s*填任意值|兑换码).{0,80}(?:关闭|关站|停运|停止(?:服务|提供|维护)|不再(?:维护|提供)|下线|结束服务|永久关闭)",
                blob,
                flags=re.I,
            )
            or bool(resource_url and not _resource_url_is_image_only(resource_url) and re.search(r"(?:迁移|迁到|新(?:地址|网址|入口)|替代|备用|Base\s*URL|baseurl|key\s*填任意值|token|api[-_ ]?key|兑换码|可领取|重开|恢复)", blob, flags=re.I))
        )
    )
    has_public_site = bool(
        _contains_any(blob, config.get("public_site_keywords", []))
        or re.search(r"(?:AI\s*)?(?:公益站|公益中转|中转站)|api\s*公益|Base\s*URL|baseurl|Codex\s*公益", blob, flags=re.I)
    )
    actionable_exception = bool(
        _contains_any(blob, actionable_exception_terms)
        or re.search(
            r"(?:新(?:地址|网址|入口)|迁移|迁到|替代|备用|补偿|可领取|领取|重开|重新开放|恢复|已恢复|扩容|补货|补了|Base\s*URL|baseurl|api[-_ ]?key|token|key\s*填任意值|兑换码|不限额度|不限并发|不限速)",
            blob,
            flags=re.I,
        )
        or _has_invite_code_action(blob)
    )
    has_direct_resource = bool(resource_url and not negative_access_context and not _resource_url_is_image_only(resource_url) and (access_method or limits or model_coverage.get("present_top_families") or explicit_action))
    has_action_update = bool(explicit_action and actionable_exception and not negative_access_context and (not terminal_status or terminal_actionable_exception))
    operator_notice = bool(
        re.search(
            r"(?:站长|管理员|官方|公告|通知|我们|本服务|本站).{0,40}(?:恢复|修复|扩容|开放|新增|规则|封禁|IP限制|额度|模型|关闭|关站|停运|停止|下线)",
            blob,
            flags=re.I,
        )
    )
    positive_operator_notice = bool(
        operator_notice
        and re.search(
            r"(?:站长|管理员|官方|公告|通知|我们|本服务|本站).{0,40}(?:恢复|修复|扩容|开放|新增|规则|封禁|IP限制|额度|模型)",
            blob,
            flags=re.I,
        )
    )
    return {
        "has_public_site": has_public_site,
        "has_direct_resource": has_direct_resource,
        "has_action_update": has_action_update,
        "has_operator_notice": positive_operator_notice,
        "has_any_operator_notice": operator_notice,
        "terminal_status": terminal_status,
        "actionable_exception": actionable_exception,
        "terminal_actionable_exception": terminal_actionable_exception,
        "loss_only": bool(has_public_site and terminal_status and not (has_direct_resource or has_action_update or positive_operator_notice or terminal_actionable_exception)),
        "resource_url": resource_url,
        "access_method": access_method,
    }


def build_post_value_profile(post: LinuxDoPost, evidence: dict | None, config: dict) -> dict:
    evidence = evidence if isinstance(evidence, dict) else {}
    blob = _evidence_context_blob(post, evidence)
    public_site_signal = _public_site_signal_facts(blob, evidence, config)
    welfare = _is_welfare_topic(post, evidence)
    model_coverage = evidence.get("model_coverage") if isinstance(evidence.get("model_coverage"), dict) else {}
    model_profile = evidence.get("model_resource_profile") if isinstance(evidence.get("model_resource_profile"), dict) else {}
    low_value_context = list(evidence.get("low_value_context") or []) if isinstance(evidence.get("low_value_context"), list) else []
    has_top_model = bool(
        model_coverage.get("present_top_families")
        or model_profile.get("top_model_resource_families")
        or model_profile.get("top_model_is_core_resource")
    )
    has_direct_resource = bool(public_site_signal.get("has_direct_resource") or _has_direct_non_image_resource(evidence))
    has_action_update = bool(public_site_signal.get("has_action_update"))
    has_public_site = bool(public_site_signal.get("has_public_site"))
    terminal_status = bool(public_site_signal.get("terminal_status"))
    actionable_exception = bool(public_site_signal.get("actionable_exception"))
    terminal_actionable_exception = bool(public_site_signal.get("terminal_actionable_exception"))

    if has_public_site:
        domain = "public_site"
    elif _has_model_or_api_resource_context(blob, evidence):
        domain = "ai_model_api"
    elif welfare:
        domain = "welfare"
    elif _has_developer_project_context(blob):
        domain = "tooling"
    else:
        domain = "discussion"

    replacement_available = bool(
        has_direct_resource
        and re.search(r"(?:迁移|迁到|新(?:地址|网址|入口)|替代|备用|replacement|migrate)", blob, flags=re.I)
    )
    resource_available = bool(has_direct_resource or (has_action_update and not terminal_status))
    loss_only = bool(has_public_site and terminal_status and not (resource_available or replacement_available or terminal_actionable_exception))

    if loss_only:
        actionability = "negative_terminal"
        value_tier = "silent"
        benefit_type = "none"
        hard_silent_reason = "public_site_shutdown_low_value"
    elif replacement_available:
        actionability = "replacement_available"
        value_tier = "candidate"
        benefit_type = "replacement"
        hard_silent_reason = ""
    elif has_direct_resource:
        actionability = "direct_use"
        value_tier = "must_push" if (has_top_model or not has_public_site) else "candidate"
        benefit_type = "access"
        hard_silent_reason = ""
    elif has_action_update:
        actionability = "positive_change"
        value_tier = "candidate"
        benefit_type = "top_model" if has_top_model else "rule_change"
        hard_silent_reason = ""
    elif has_public_site:
        actionability = "discussion_only"
        value_tier = "silent" if "public_site_chatter_low_value" in low_value_context else "judge"
        benefit_type = "none"
        hard_silent_reason = "public_site_chatter_low_value" if value_tier == "silent" else ""
    else:
        actionability = "unknown"
        value_tier = "judge" if welfare else "silent"
        benefit_type = "none"
        hard_silent_reason = ""

    if has_top_model and benefit_type in {"none", "rule_change"} and (has_direct_resource or has_action_update):
        benefit_type = "top_model"

    return {
        "domain": domain,
        "actionability": actionability,
        "benefit_type": benefit_type,
        "resource_available": resource_available,
        "replacement_available": replacement_available,
        "top_model_available": has_top_model,
        "user_action_required": bool(resource_available or replacement_available),
        "loss_only": loss_only,
        "value_tier": value_tier,
        "hard_silent_reason": hard_silent_reason,
    }


def finalize_post_value_profile(post: LinuxDoPost, evidence: dict, config: dict) -> dict:
    if not isinstance(evidence, dict):
        return evidence
    blob = _evidence_context_blob(post, evidence)
    evidence["public_site_signal"] = _public_site_signal_facts(blob, evidence, config)
    evidence["post_value_profile"] = build_post_value_profile(post, evidence, config)
    return evidence


def _is_public_site_shutdown_low_value(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    profile = build_post_value_profile(post, evidence, config)
    return bool(
        profile.get("domain") == "public_site"
        and profile.get("actionability") == "negative_terminal"
        and profile.get("loss_only")
        and profile.get("hard_silent_reason") == "public_site_shutdown_low_value"
    )


def _is_public_site_chatter_low_value(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    blob = _evidence_context_blob(post, evidence)
    if not blob:
        return False
    facts = _public_site_signal_facts(blob, evidence, config)
    if not facts["has_public_site"]:
        return False
    if facts["has_direct_resource"] or facts["has_action_update"] or facts["has_operator_notice"]:
        return False
    support_config_help = bool(
        re.search(
            r"(?:求助|请教|怎么解决|如何解决|help).{0,80}(?:配置|provider|model|/model|xhigh|levels|openai-compatibility|api[-_ ]?key|CPA|自定义供应商)"
            r"|(?:配置|provider|model|/model|xhigh|levels|openai-compatibility|api[-_ ]?key|CPA|自定义供应商).{0,80}(?:求助|请教|怎么解决|如何解决|报错|错误|失败|not\s+supported)",
            blob,
            flags=re.I,
        )
    )
    if support_config_help:
        return False
    tags_and_category = _compact_text(
        " ".join([post.section, str(evidence.get("topic_category") or ""), " ".join(str(item) for item in evidence.get("topic_tags") or [])])
    )
    low_value_board = _contains_any(tags_and_category, _config_list(config, "public_site_chatter_tags", ["纯水", "搞七捻三", "快问快答"]))
    end_user_chatter = bool(
        re.search(
            r"(?:今天|现在|这几天|基本上|有没有|有无|求|求稳定|求推荐|吐槽|讨论|闲聊|蹬|用不出去|挂了|又挂|打不开|空白|403|502|5xx|50X|Cloudflare|卡|卡顿|报错|错误|额度(?:变|显示|很多)|几分钟|十几分钟|不稳定)",
            blob,
            flags=re.I,
        )
    )
    service_level_status = bool(
        re.search(r"(?:全站|服务级|所有用户|所有人|站点).{0,20}(?:恢复|修复|扩容|开放|封禁|IP限制|规则|额度政策)", blob, flags=re.I)
    )
    return bool((low_value_board or end_user_chatter) and end_user_chatter and not service_level_status)


def _is_research_academic_low_value(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    blob = _evidence_context_blob(post, evidence)
    if not blob:
        return False
    parsed_urls = [
        str(evidence.get("resource_url") or ""),
        str(evidence.get("reference_url") or ""),
    ]
    research_domains = _config_list(config, "research_academic_domains", [])
    has_research_domain = any(_domain_matches(urlparse(url).netloc, research_domains) for url in parsed_urls if url)
    has_research_terms = _contains_any(blob, _config_list(config, "research_academic_keywords", [])) or bool(
        re.search(r"(?:论文|科研|学术|期刊|文献|数据集|实验报告|科研绘图|科研工具|PDF|arXiv|DOI|Google\s*Scholar|PubMed|research\s+paper|academic|journal|literature|dataset)", blob, flags=re.I)
    )
    if not (has_research_terms or has_research_domain):
        return False
    if _has_developer_project_context(blob):
        return False
    if _is_ai_public_site_resource(post, evidence, config):
        return False
    model_coverage = evidence.get("model_coverage") if isinstance(evidence.get("model_coverage"), dict) else {}
    if model_coverage.get("present_top_families") and _has_model_or_api_resource_context(blob, evidence) and _has_actionable_resource_terms(blob):
        return False
    if re.search(r"(?:Base\s*URL|baseurl|api[-_ ]?key|token|key\s*填任意值|兑换码|Codex|Claude|GPT|Gemini|Grok).{0,40}(?:可用|开放|领取|接入|支持|免费|不限)", blob, flags=re.I):
        return False
    return True


def _is_support_troubleshooting_help(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if _is_welfare_topic(post, evidence):
        return False
    blob = _compact_text(
        " ".join(
            [
                _post_blob(post),
                str(evidence.get("topic_text") or ""),
                " ".join(evidence.get("link_titles") or []),
                str(evidence.get("reference_url") or ""),
                str(evidence.get("resource_url") or ""),
            ]
        )
    )
    if not blob:
        return False
    value_profile = evidence.get("post_value_profile") if isinstance(evidence.get("post_value_profile"), dict) else {}
    intent_signal = bool(
        re.search(
            r"(?:求助|请教|咨询|疑惑|疑问|问题|报错|错误|失败|超时|不支持|怎么解决|如何解决|怎么|如何|为啥|为什么|有佬|help|timeout|APITimeoutError|not\s+supported|error\s*=|valid\s+levels)",
            blob,
            flags=re.I,
        )
    )
    troubleshooting_context = _contains_any(blob, config.get("support_troubleshooting_keywords", [])) or bool(
        re.search(
            r"(?:CPA|Codex|OpenAI|Claude|GPT|provider|model|/model|xhigh|levels|api[-_ ]?key|apikey|openai-compatibility|自定义供应商|配置|Hub\s*站|public-sub2api|中转|公益站|Base\s*URL|baseurl)",
            blob,
            flags=re.I,
        )
    )
    explicit_support_context = bool(intent_signal and troubleshooting_context)
    if value_profile.get("actionability") in {"direct_use", "positive_change", "replacement_available"} and not explicit_support_context:
        return False
    if _has_resource_status_update_signal(blob, config) and not explicit_support_context:
        return False
    return explicit_support_context


def _is_forum_chatter_low_value(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    blob = _compact_text(
        " ".join(
            [
                _post_blob(post),
                str(evidence.get("topic_text") or ""),
                " ".join(str(item) for item in evidence.get("link_titles") or []),
                str(evidence.get("reference_url") or ""),
                str(evidence.get("resource_url") or ""),
            ]
        )
    )
    if not blob:
        return False
    if _is_welfare_topic(post, evidence) and _has_actionable_resource_terms(blob):
        return False
    if _has_invite_code_action(blob):
        return False
    resource_terms_without_forum_noise = re.sub(r"(?:邀请码|invite\s*code)", "", blob, flags=re.I)
    if _has_actionable_resource_terms(resource_terms_without_forum_noise) or _has_resource_status_update_signal(resource_terms_without_forum_noise, config):
        return False
    forum_context = bool(
        re.search(
            r"(?:Linux\s*Do|linux\.do|L站|论坛|社区|等级|升级|三级|二级|一级|0级|1级|2级|3级|level\s*[0-9]|trust\s*level|邀请码|注册|发帖|帖子|审核|纯水|水帖|截图|截屏)",
            blob,
            flags=re.I,
        )
    )
    low_value_intent = bool(
        re.search(
            r"(?:邀请码.{0,12}(?:没了|没有|没|取消|关闭|末班车)|(?:注册|发帖).{0,18}(?:末班车|审核|被拒|拒了|规则)|(?:马上|快要|还差|差).{0,18}(?:三级|二级|一级|帖子)|(?:三级|二级|一级|0级|1级|2级|3级).{0,18}(?:还差|帖子|邀请|邀请码)|(?:截图|截屏).{0,18}(?:KB|png|jpg)|纯水|水帖)",
            blob,
            flags=re.I,
        )
    )
    return bool(forum_context and low_value_intent)


def _domain_matches(host: str, domains: list[str]) -> bool:
    host = str(host or "").strip().lower()
    if not host:
        return False
    for domain in domains:
        wanted = str(domain or "").strip().lower()
        if wanted and (host == wanted or host.endswith(f".{wanted}")):
            return True
    return False


def _has_model_or_api_resource_context(blob: str, evidence: dict) -> bool:
    coverage = evidence.get("model_coverage") if isinstance(evidence.get("model_coverage"), dict) else {}
    if coverage.get("present_top_families") or evidence.get("model_names"):
        return True
    return bool(
        re.search(
            r"(?:GPT|Claude|Grok|Gemini|Codex|OpenAI|LLM|模型列表|模型接入|公益站|公益中转|中转站|Base\s*URL|baseurl|api[-_ ]?key|token|key\s*填任意值|su2api|2api)",
            blob,
            flags=re.I,
        )
    )


def _has_developer_project_context(blob: str) -> bool:
    has_code_host = bool(re.search(r"(?:github\.com|gitlab\.com|GitHub|GitLab|仓库|repo|源码|开源)", blob, flags=re.I))
    has_dev_terms = bool(
        re.search(
            r"(?:MCP|plugin|插件|SDK|CLI|Docker|脚本|自动化|代码|开发工具|API|Codex|Claude|GPT|OpenAI|生成代码|转代码)",
            blob,
            flags=re.I,
        )
    )
    return bool(has_code_host and has_dev_terms)


def _is_design_resource_low_value(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    resource_url = str(evidence.get("resource_url") or "")
    provider = str(evidence.get("provider") or "")
    blob = _compact_text(
        " ".join(
            [
                _post_blob(post),
                str(evidence.get("topic_text") or ""),
                " ".join(str(item) for item in evidence.get("link_titles") or []),
                provider,
                resource_url,
            ]
        )
    )
    domains = _config_list(config, "design_resource_domains", ["figma.com", "canva.com", "adobe.com", "framer.com"])
    keywords = _config_list(config, "design_resource_keywords", ["Figma", "Canva", "Adobe", "Framer", "设计", "素材", "模板", "UI kit", "图标", "design credits", "制作积分"])
    parsed = urlparse(resource_url)
    design_context = _domain_matches(parsed.netloc, domains) or _domain_matches(provider, domains) or _contains_any(blob, keywords)
    if not design_context:
        return False
    if _has_developer_project_context(blob) or _has_model_or_api_resource_context(blob, evidence):
        return False
    freebie_context = bool(
        _is_welfare_topic(post, evidence)
        or resource_url
        or re.search(r"(?:免费|free|领取|promo|credits?|积分|制作积分|模板|素材|UI\s*kit|图标|icons?|下载|trial|pro)", blob, flags=re.I)
    )
    return freebie_context


def _is_game_resource_low_value(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    resource_url = str(evidence.get("resource_url") or "")
    provider = str(evidence.get("provider") or "")
    blob = _compact_text(
        " ".join(
            [
                _post_blob(post),
                str(evidence.get("topic_text") or ""),
                " ".join(str(item) for item in evidence.get("link_titles") or []),
                provider,
                resource_url,
            ]
        )
    )
    domains = _config_list(
        config,
        "game_resource_domains",
        [
            "store.epicgames.com",
            "steampowered.com",
            "steamcommunity.com",
            "gog.com",
            "itch.io",
            "xbox.com",
            "playstation.com",
            "nintendo.com",
            "taptap.cn",
        ],
    )
    keywords = _config_list(
        config,
        "game_resource_keywords",
        ["游戏", "手游", "Epic", "Steam", "GOG", "DLC", "礼包", "道具", "皮肤", "限免", "立省", "免费获取", "回合制策略游戏"],
    )
    parsed = urlparse(resource_url)
    game_context = _domain_matches(parsed.netloc, domains) or _domain_matches(provider, domains) or _contains_any(blob, keywords)
    if not game_context:
        return False
    if _has_developer_project_context(blob) or _has_model_or_api_resource_context(blob, evidence):
        return False
    freebie_context = bool(
        _is_welfare_topic(post, evidence)
        or resource_url
        or re.search(r"(?:免费|free|领取|限免|立省|折扣|DLC|礼包|道具|皮肤|兑换码|入库|游戏)", blob, flags=re.I)
    )
    return freebie_context


def _has_direct_public_site_activity(blob: str) -> bool:
    if any(term in blob for term in ["不限额度", "不限并发", "不限速", "火力全开", "兑换码", "key填任意值"]):
        return True
    if _has_invite_code_action(blob):
        return True
    if re.search(r"(?:api\s*)?(?:key|token|兑换码).{0,24}(?:给|送|分享|领取|可用|填任意值)", blob, flags=re.I):
        return True
    if re.search(r"(?:网址|地址|站点|Base\s*URL|baseurl)[:：]?\s*https?://", blob, flags=re.I) and not re.search(
        r"(?:http_proxy|https_proxy|proxy|代理|报错|错误|失败|disconnected|completion)", blob, flags=re.I
    ):
        return True
    return False


def _has_tool_action_signal(blob: str) -> bool:
    if re.search(r"(Linux\s*Do|L站|论坛).{0,16}(?:书签|等级|邀请|板块|发帖|帖子|账号|风控)", blob, flags=re.I):
        return False
    return bool(
        re.search(
            r"(发布|开源|上线|做了个|写了个|分享|教程|指南|脚本|项目|组件库|自托管|workers|docker|github\s*repo|仓库|避雷|价格监控)",
            blob,
            flags=re.I,
        )
    )


def _empty_evidence() -> dict:
    return {
        "topic_text": "",
        "resource_url": "",
        "reference_url": "",
        "access_method": "",
        "model_names": [],
        "provider": "",
        "limits": "",
        "price": "",
        "risk_notes": "",
        "why": "",
        "frontline_model_verdict": "unknown",
        "model_coverage": {
            "present_top_families": [],
            "present_non_frontline_families": [],
            "domestic_only": False,
            "top_model_absent_explicit": False,
            "confidence": "unknown",
            "source": [],
            "snippets": [],
        },
        "model_resource_profile": {
            "is_model_resource": False,
            "subject": "",
            "subject_terms": [],
            "provider_terms": [],
            "domestic_or_non_top_subject": False,
            "top_model_resource_families": [],
            "top_model_non_resource_mentions": [],
            "top_model_is_core_resource": False,
            "unknown_non_top_plan": False,
            "confidence": "unknown",
            "snippets": [],
        },
        "image_text_status": "not_needed",
        "image_urls": [],
        "image_dimensions": [],
        "link_titles": [],
        "topic_metrics": {},
        "topic_category": "",
        "topic_tags": [],
        "public_site_signal": {},
        "post_value_profile": {},
        "welfare_resource_profile": {},
        "low_value_context": [],
    }


def _merge_evidence(*items: dict | None) -> dict:
    merged = _empty_evidence()
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if key in {"model_names", "image_urls", "link_titles", "topic_tags", "low_value_context"}:
                values = merged.get(key, [])
                if not isinstance(values, list):
                    values = []
                if isinstance(value, list):
                    values.extend(value)
                elif value:
                    values.append(str(value))
                merged[key] = _unique_strings(values)
            elif key == "image_dimensions":
                existing = merged.get(key, [])
                if not isinstance(existing, list):
                    existing = []
                seen = {str(entry.get("url") or "") for entry in existing if isinstance(entry, dict)}
                for entry in value if isinstance(value, list) else []:
                    if not isinstance(entry, dict):
                        continue
                    url = str(entry.get("url") or "")
                    if url and url not in seen:
                        existing.append(entry)
                        seen.add(url)
                merged[key] = existing[:8]
            elif key == "topic_metrics":
                current = merged.get(key, {})
                if not isinstance(current, dict):
                    current = {}
                if isinstance(value, dict):
                    current.update({k: v for k, v in value.items() if v not in ("", None, [])})
                merged[key] = current
            elif key == "public_site_signal":
                current = merged.get(key, {})
                if not isinstance(current, dict):
                    current = {}
                if isinstance(value, dict):
                    for signal_key, signal_value in value.items():
                        if isinstance(signal_value, list):
                            existing = current.get(signal_key, [])
                            if not isinstance(existing, list):
                                existing = []
                            existing.extend(signal_value)
                            current[signal_key] = _unique_strings(existing, limit=8)
                        elif signal_value not in ("", None, []):
                            current[signal_key] = signal_value
                merged[key] = current
            elif key == "post_value_profile":
                current = merged.get(key, {})
                if not isinstance(current, dict):
                    current = {}
                if isinstance(value, dict):
                    current.update({k: v for k, v in value.items() if v not in ("", None, [], {})})
                merged[key] = current
            elif key == "model_coverage":
                merged["model_coverage"] = _merge_model_coverage(merged.get("model_coverage"), value if isinstance(value, dict) else None)
            elif key == "model_resource_profile":
                merged["model_resource_profile"] = _merge_model_resource_profiles(
                    merged.get("model_resource_profile") if isinstance(merged.get("model_resource_profile"), dict) else None,
                    value if isinstance(value, dict) else None,
                )
            elif key == "welfare_resource_profile":
                merged["welfare_resource_profile"] = _merge_welfare_resource_profiles(
                    merged.get("welfare_resource_profile") if isinstance(merged.get("welfare_resource_profile"), dict) else None,
                    value if isinstance(value, dict) else None,
                )
            elif key == "frontline_hint":
                hint = str(value or "").strip().lower()
                if hint in {"non_frontline", "domestic_only"}:
                    merged["frontline_model_verdict"] = "non_frontline"
            elif value not in ("", None, []):
                if key in {"topic_text", "risk_notes"} and merged.get(key):
                    merged[key] = _truncate_text(f"{merged[key]} {value}", TOPIC_TEXT_MAX_CHARS if key == "topic_text" else EVIDENCE_TEXT_MAX_CHARS)
                else:
                    merged[key] = value
    merged["model_names"] = _unique_strings(merged.get("model_names", []), limit=10)
    merged["image_urls"] = _unique_strings(merged.get("image_urls", []), limit=8)
    merged["link_titles"] = _unique_strings(merged.get("link_titles", []), limit=8)
    merged["topic_tags"] = _unique_strings(merged.get("topic_tags", []), limit=8)
    merged["low_value_context"] = _unique_strings(merged.get("low_value_context", []), limit=6)
    return merged


def _extract_image_dimensions(cooked_html: str) -> list[dict]:
    result: list[dict] = []
    for match in re.finditer(r"<img\b([^>]+)>", str(cooked_html or ""), flags=re.S | re.I):
        attrs = match.group(1)
        src_match = re.search(r'\bsrc="([^"]+)"', attrs, flags=re.I)
        if not src_match:
            continue
        entry: dict[str, Any] = {"url": html.unescape(src_match.group(1))}
        for attr in ("width", "height"):
            attr_match = re.search(rf'\b{attr}="(\d+)"', attrs, flags=re.I)
            if attr_match:
                entry[attr] = int(attr_match.group(1))
        result.append(entry)
    return result


def _extract_image_urls_from_html(cooked_html: str) -> list[str]:
    urls: list[str] = []
    for attr in re.findall(r'\b(?:src|href)="([^"]+)"', str(cooked_html or ""), flags=re.I):
        url = html.unescape(attr)
        if IMAGE_URL_RE.search(url) or RELATIVE_IMAGE_URL_RE.search(url):
            urls.append(url)
    urls.extend(IMAGE_URL_RE.findall(str(cooked_html or "")))
    urls.extend(RELATIVE_IMAGE_URL_RE.findall(str(cooked_html or "")))
    return _unique_strings(urls)


def _is_internal_image_or_upload_url(url: str) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    parsed = urlparse(text)
    path = parsed.path.lower()
    host = parsed.netloc.lower()
    if IMAGE_URL_RE.search(text) or RELATIVE_IMAGE_URL_RE.search(text):
        return True
    if path.startswith("/uploads/") or "/uploads/" in path:
        return True
    if host.endswith("ldstatic.com") or host.endswith("linux.do"):
        return True
    return False


def _first_resource_url(topic_text: str, cooked_html: str, link_counts: list[Any]) -> str:
    candidates: list[str] = []
    for link in link_counts:
        if isinstance(link, dict):
            candidates.append(str(link.get("url") or ""))
    candidates.extend(re.findall(r'href="([^"]+)"', str(cooked_html or ""), flags=re.I))
    candidates.extend(URL_RE.findall(topic_text))
    for candidate in candidates:
        url = html.unescape(candidate).rstrip("，。；;)")
        if not url or _is_internal_image_or_upload_url(url):
            continue
        host = urlparse(url).netloc.lower()
        if host.endswith("linux.do") or host.endswith("ldstatic.com"):
            continue
        return url
    return ""


def _looks_like_reference_url(topic_text: str, link_titles: list[str], resource_url: str, config: dict) -> bool:
    if not resource_url:
        return False
    blob = _compact_text(" ".join([topic_text, " ".join(link_titles), resource_url]))
    parsed = urlparse(resource_url)
    path = f"{parsed.netloc} {parsed.path} {parsed.fragment}".lower()
    support_reference_link = "github.com" in parsed.netloc.lower() and re.search(r"/(?:issues|discussions)/\d+|/(?:wiki|docs?)(?:/|$)", parsed.path, flags=re.I)
    if support_reference_link and _contains_any(blob, config.get("support_troubleshooting_keywords", [])):
        return True
    research_reference_link = _domain_matches(parsed.netloc, _config_list(config, "research_academic_domains", [])) or bool(
        re.search(r"(?:\.pdf(?:$|[?#])|arxiv|doi\.org|paper|论文|科研|学术|期刊|文献|数据集|实验报告|research\s+paper|academic|journal|dataset)", blob, flags=re.I)
    )
    if research_reference_link and not _has_model_or_api_resource_context(blob, {"resource_url": resource_url, "link_titles": link_titles}):
        return True
    if _has_actionable_resource_terms(blob):
        return False
    if _contains_any(blob, config.get("technical_reference_keywords", [])):
        return True
    return bool(re.search(r"(?:blog|post|article|docs?|guide|tutorial|notes?)", path, flags=re.I))


def _extract_model_names(text: str, config: dict | None = None) -> list[str]:
    patterns = [
        r"\bGPT[-\s]?[0-9A-Za-z.]+",
        r"\bChatGPT\b",
        r"\bCodex\b",
        r"\bClaude(?:\s+(?:Sonnet|Opus|Haiku))?(?:\s+[0-9.]+)?",
        r"\bGemini(?:\s+[0-9.]+)?(?:\s+Pro)?",
        r"\bGrok(?:\s+[0-9.]+)?",
        r"\bDeepSeek(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bQwen(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bQwQ(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bQVQ(?:[-\s]?[A-Za-z0-9.]+)?",
        r"通义千问[一-龥A-Za-z0-9. +_-]*",
        r"千问[一-龥A-Za-z0-9. +_-]*",
        r"\bKimi(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bMoonshot(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bErnie(?:[-\s]?[A-Za-z0-9.]+)?(?:\s+VL)?",
        r"文心一言[一-龥A-Za-z0-9. ]*",
        r"\bLumo\b",
        r"\bProton\s+Lumo\b",
        r"(?<![A-Za-z0-9])GLM(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bChatGLM(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bMiniMax(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bHailuo(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bDoubao(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bDoubao-Seed(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bHunyuan(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bBaichuan(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bStepFun(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bSenseNova(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bInternLM(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bSkywork(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bPanguLM(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bPangu(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bSiliconFlow(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bMiMo(?:[-\s]?[A-Za-z0-9.]+)?",
        r"\bXiaomi\s+MiMo(?:[-\s]?[A-Za-z0-9.]+)?",
        r"豆包",
        r"文心[一-龥A-Za-z0-9. ]*",
        r"智谱",
        r"火山方舟",
        r"百度千帆",
        r"腾讯混元",
        r"百川",
        r"阶跃星辰",
        r"商汤日日新",
        r"书生浦语",
        r"硅基流动",
        r"华为云盘古",
        r"魔搭",
        r"小米\s*MiMo",
    ]
    found: list[str] = []
    for pattern in patterns:
        found.extend(match.group(0) for match in re.finditer(pattern, text, flags=re.I))
    if config is not None:
        for term in _domestic_model_alias_terms(config):
            if len(term) <= 3 and re.fullmatch(r"[A-Za-z0-9.]+", term):
                continue
            if re.search(_term_pattern(term), text, flags=re.I):
                found.append(term)
    return _unique_strings(found, limit=10)


def _extract_limits(text: str) -> str:
    signals = []
    for pattern in [
        r"不限额度",
        r"不限并发",
        r"不限速",
        r"免费额度[^，。；\n]{0,40}",
        r"额度[^，。；\n]{0,40}",
        r"限速[^，。；\n]{0,40}",
        r"key填任意值",
        r"key\s*填任意值",
    ]:
        signals.extend(match.group(0) for match in re.finditer(pattern, text, flags=re.I))
    return "，".join(_unique_strings(signals, limit=4))


def _extract_access_method(text: str) -> str:
    if re.search(r"key\s*填任意值|key填任意值", text, flags=re.I):
        return "key填任意值"
    if re.search(r"(?:直接|访问).{0,12}https?://", text, flags=re.I):
        return "直接访问"
    if re.search(r"(?:token|api\s*key|key).{0,24}(?:领取|获取|可用|免费)", text, flags=re.I):
        return _truncate_text(re.search(r"(?:token|api\s*key|key).{0,40}", text, flags=re.I).group(0), 80)
    return ""


def _extract_provider(text: str, link_titles: list[str], resource_url: str) -> str:
    joined = " ".join(link_titles + [text, resource_url])
    for pattern in [
        r"RawChat",
        r"Proton(?:\s+AI\s+Gateway)?",
        r"Lumo2API",
        r"OpenAI",
        r"Claude",
        r"Gemini",
        r"Grok",
    ]:
        match = re.search(pattern, joined, flags=re.I)
        if match:
            return _compact_text(match.group(0))
    host = urlparse(resource_url).netloc
    return host


def _normalise_model_family_name(term: str) -> str:
    lowered = str(term or "").strip().lower()
    if not lowered:
        return ""
    if "mimo" in lowered or "小米" in lowered:
        return "mimo"
    if "siliconflow" in lowered or "siliconcloud" in lowered or "硅基流动" in lowered:
        return "siliconflow"
    if "modelscope" in lowered or "魔搭" in lowered or "api-inference" in lowered:
        return "modelscope"
    if "openbmb" in lowered or "minicpm" in lowered or "面壁" in lowered:
        return "openbmb"
    if "xverse" in lowered or "360智脑" in lowered:
        return "xverse"
    if "pangu" in lowered or "盘古" in lowered:
        return "pangu"
    if "minimax" in lowered:
        return "minimax"
    if "海螺" in lowered or "hailuo" in lowered or "abab" in lowered or "星野" in lowered:
        return "minimax"
    if "火山" in lowered or "方舟" in lowered or "volc" in lowered or "doubao" in lowered or "豆包" in lowered:
        return "doubao"
    if "智谱" in lowered or "zhipu" in lowered or "chatglm" in lowered or "glm" in lowered or "bigmodel" in lowered or "清言" in lowered or "z.ai" in lowered or "autoglm" in lowered:
        return "glm"
    if "deepseek" in lowered:
        return "deepseek"
    if "qwen" in lowered or "千问" in lowered or "通义" in lowered or "qwq" in lowered or "qvq" in lowered or "百炼" in lowered or "dashscope" in lowered:
        return "qwen"
    if "qoder" in lowered:
        return "qoder"
    if "kimi" in lowered or "moonshot" in lowered or "月之暗面" in lowered:
        return "kimi"
    if "ernie" in lowered or "文心" in lowered or "千帆" in lowered or "qianfan" in lowered:
        return "ernie"
    if "lumo" in lowered:
        return "lumo"
    if "proton" in lowered:
        return "proton"
    if lowered.startswith("yi-") or lowered == "yi" or "零一万物" in lowered or "01.ai" in lowered or "01ai" in lowered:
        return "yi"
    if "混元" in lowered or "hunyuan" in lowered or "腾讯元宝" in lowered:
        return "hunyuan"
    if "baichuan" in lowered or "百川" in lowered:
        return "baichuan"
    if "stepfun" in lowered or "step-" in lowered or "阶跃" in lowered or "跃问" in lowered:
        return "stepfun"
    if "sensenova" in lowered or "商汤" in lowered or "日日新" in lowered or "sensetime" in lowered:
        return "sensenova"
    if "internlm" in lowered or "书生" in lowered:
        return "internlm"
    if "skywork" in lowered or "天工" in lowered:
        return "skywork"
    if "讯飞" in lowered or "星火" in lowered or "iflytek" in lowered or "xf-yun" in lowered or "xfyun" in lowered:
        return "xunfei"
    if "联通" in lowered or "cucloud" in lowered or "aigw" in lowered:
        return "china_unicom"
    if "移动云" in lowered or "中国移动" in lowered or "ecloud" in lowered:
        return "china_mobile"
    if "电信" in lowered or "天翼" in lowered or "ctyun" in lowered:
        return "china_telecom"
    cleaned = re.sub(r"[^a-z0-9一-龥]+", "_", lowered).strip("_")
    return cleaned[:32]


def _context_window(text: str, start: int, end: int, *, radius: int = 36) -> str:
    return _compact_text(text[max(0, start - radius) : min(len(text), end + radius)])


def _is_top_model_negative_context(snippet: str) -> bool:
    top = r"(?:gpt|claude|grok|gemini|sonnet|opus|chatgpt)"
    negative_before = r"(?:不支持|无|没有|不含|不包含|不提供|未说明|没有说明|不是|非|仅作对比|unsupported|not\s+support|not\s+available|not\s+included)"
    negative_after = r"(?:不支持|不可用|报错|错误|失败|不含|不包含|不提供|未说明|没有说明|非资源|仅作对比|unsupported|not\s+support|not\s+available|not\s+included)"
    return bool(
        re.search(
            rf"{negative_before}.{{0,24}}{top}|{top}.{{0,24}}{negative_after}",
            snippet,
            flags=re.I,
        )
    )


def _extract_model_coverage(text: str, model_names: list[str], config: dict, *, image_hint: str = "") -> dict:
    blob = _compact_text(" ".join([text, " ".join(model_names)]))
    aliases = _top_model_family_aliases(config)
    non_frontline_terms = _unique_strings(
        _config_list(config, "non_frontline_model_terms", NON_FRONTLINE_MODEL_TERMS)
        + _config_list(config, "domestic_model_resource_terms", DEFAULT_DOMESTIC_MODEL_RESOURCE_TERMS)
        + _config_list(config, "domestic_model_provider_terms", DEFAULT_DOMESTIC_MODEL_PROVIDER_TERMS)
        + _domestic_model_alias_terms(config),
        limit=220,
    )
    domestic_only_signals = _config_list(config, "domestic_only_signals", DEFAULT_DOMESTIC_ONLY_SIGNALS)
    present_top: list[str] = []
    present_non_frontline: list[str] = []
    snippets: list[str] = []
    lowered = blob.lower()

    for family, family_aliases in aliases.items():
        for alias in family_aliases:
            escaped = re.escape(str(alias or "").strip())
            if not escaped:
                continue
            pattern = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])" if re.fullmatch(r"[A-Za-z0-9 .+-]+", str(alias or "")) else escaped
            for match in re.finditer(pattern, blob, flags=re.I):
                snippet = _context_window(blob, match.start(), match.end())
                if _is_top_model_negative_context(snippet):
                    continue
                present_top.append(family)
                snippets.append(snippet)
                break
            if family in present_top:
                break

    for term in non_frontline_terms:
        term_text = str(term or "").strip()
        if not term_text:
            continue
        if re.search(_term_pattern(term_text), blob, flags=re.I):
            present_non_frontline.append(_normalise_model_family_name(term_text))
    domestic_families = set(_domestic_model_family_aliases(config))
    for model_name in model_names:
        family = _normalise_model_family_name(str(model_name or ""))
        if family and family in domestic_families:
            present_non_frontline.append(family)

    only_domestic = _contains_any(blob, domestic_only_signals)
    if not only_domestic and present_non_frontline and not present_top:
        explicit_only = re.search(
            r"(?:仅|只|只有|仅支持|只支持|only|only\s+supports?|support(?:s|ed)?\s+only).{0,36}"
            r"(?:deepseek|qwen|kimi|glm|doubao|豆包|文心|ernie|国产)",
            lowered,
            flags=re.I,
        )
        only_domestic = bool(explicit_only)

    image_hint = str(image_hint or "").strip().lower()
    if image_hint == "non_frontline" and present_non_frontline and not present_top:
        only_domestic = True

    top_absent_explicit = bool(only_domestic and not present_top)
    if present_top:
        confidence = "explicit"
    elif top_absent_explicit or present_non_frontline:
        confidence = "explicit_absent"
    else:
        confidence = "unknown"

    return {
        "present_top_families": _unique_strings(present_top, limit=4),
        "present_non_frontline_families": _unique_strings(present_non_frontline, limit=8),
        "domestic_only": bool(only_domestic),
        "top_model_absent_explicit": top_absent_explicit,
        "confidence": confidence,
        "source": ["text"] if blob else [],
        "snippets": _unique_strings([_truncate_text(snippet, 120) for snippet in snippets], limit=3),
    }


def _merge_model_coverage(*coverages: dict | None) -> dict:
    merged = {
        "present_top_families": [],
        "present_non_frontline_families": [],
        "domestic_only": False,
        "top_model_absent_explicit": False,
        "confidence": "unknown",
        "source": [],
        "snippets": [],
    }
    confidence_rank = {"unknown": 0, "explicit_absent": 1, "explicit": 2}
    best_rank = 0
    for coverage in coverages:
        if not isinstance(coverage, dict):
            continue
        merged["present_top_families"].extend(coverage.get("present_top_families") or [])
        merged["present_non_frontline_families"].extend(coverage.get("present_non_frontline_families") or [])
        merged["source"].extend(coverage.get("source") or [])
        merged["snippets"].extend(coverage.get("snippets") or [])
        merged["domestic_only"] = bool(merged["domestic_only"] or coverage.get("domestic_only"))
        merged["top_model_absent_explicit"] = bool(merged["top_model_absent_explicit"] or coverage.get("top_model_absent_explicit"))
        confidence = str(coverage.get("confidence") or "unknown")
        rank = confidence_rank.get(confidence, 0)
        if rank > best_rank:
            best_rank = rank
            merged["confidence"] = confidence
    merged["present_top_families"] = _unique_strings(merged["present_top_families"], limit=4)
    merged["present_non_frontline_families"] = _unique_strings(merged["present_non_frontline_families"], limit=8)
    merged["source"] = _unique_strings(merged["source"], limit=4)
    merged["snippets"] = _unique_strings([_truncate_text(str(item), 120) for item in merged["snippets"]], limit=3)
    if merged["present_top_families"]:
        merged["top_model_absent_explicit"] = False
        if merged["confidence"] == "explicit_absent":
            merged["confidence"] = "explicit"
    return merged


def _model_verdict_from_coverage(coverage: dict, model_names: list[str], config: dict | None = None) -> str:
    if coverage.get("present_top_families"):
        if coverage.get("present_non_frontline_families"):
            policy = str((config or {}).get("mixed_model_policy") or "top_tier_wins").strip().lower()
            if policy not in {"top_tier_wins", "frontline_wins"}:
                return "unknown"
        return "frontline"
    if coverage.get("top_model_absent_explicit") or coverage.get("domestic_only"):
        return "non_frontline"
    if coverage.get("present_non_frontline_families") and model_names:
        return "non_frontline"
    return "unknown"


def _matched_config_terms(blob: str, terms: list[str], *, limit: int = 12) -> list[str]:
    return _match_terms_with_context(str(blob or ""), terms, limit=limit)


def _top_model_resource_mentions(blob: str, config: dict) -> tuple[list[str], list[str], list[str]]:
    aliases = _top_model_family_aliases(config)
    required_terms = _config_list(config, "top_model_resource_required_terms", DEFAULT_TOP_MODEL_RESOURCE_REQUIRED_TERMS)
    non_resource_terms = _config_list(config, "top_model_non_resource_context_terms", DEFAULT_TOP_MODEL_NON_RESOURCE_CONTEXT_TERMS)
    resource_families: list[str] = []
    non_resource_mentions: list[str] = []
    snippets: list[str] = []
    for family, family_aliases in aliases.items():
        for alias in family_aliases:
            alias_text = str(alias or "").strip()
            if not alias_text:
                continue
            escaped = re.escape(alias_text)
            pattern = rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])" if re.fullmatch(r"[A-Za-z0-9 .+-]+", alias_text) else escaped
            for match in re.finditer(pattern, blob, flags=re.I):
                snippet = _context_window(blob, match.start(), match.end(), radius=64)
                if _is_top_model_negative_context(snippet):
                    non_resource_mentions.append(snippet)
                    continue
                has_required_context = _contains_any(snippet, required_terms) or bool(
                    re.search(
                        r"(?:模型列表|支持|提供|包含|接入|可用|开放|额度|不限|Base\s*URL|baseurl|api[-_ ]?key|token|key\s*填任意值|team|子号)",
                        snippet,
                        flags=re.I,
                    )
                )
                has_non_resource_context = _contains_any(snippet, non_resource_terms) or bool(
                    re.search(r"(?:Claude\s+(?:Code|Max)|Cursor|OpenAI-compatible|兼容|教程|报错|错误|失败|涨价|订阅问题|用量|对比|provider|/model)", snippet, flags=re.I)
                )
                concrete_top_model = bool(
                    re.search(
                        r"(?:\bGPT\b|GPT[-\s]?[45][0-9A-Za-z.]*|ChatGPT\s*(?:Team|Plus|Pro)?|Claude\s+(?:Sonnet|Opus|Haiku)(?:\s+[0-9.]+)?|Gemini(?:\s+[0-9.]+)?(?:\s+Pro)?|Grok(?:\s+[0-9.]+)?)",
                        snippet,
                        flags=re.I,
                    )
                )
                if has_required_context and concrete_top_model and not has_non_resource_context:
                    resource_families.append(family)
                    snippets.append(snippet)
                else:
                    non_resource_mentions.append(snippet)
            if family in resource_families:
                break
    return (
        _unique_strings(resource_families, limit=4),
        _unique_strings([_truncate_text(item, 120) for item in non_resource_mentions], limit=4),
        _unique_strings([_truncate_text(item, 120) for item in snippets], limit=3),
    )


def _model_resource_action_context(blob: str, config: dict) -> bool:
    terms = _config_list(config, "model_resource_context_terms", DEFAULT_MODEL_RESOURCE_CONTEXT_TERMS)
    return bool(
        _contains_any(blob, terms)
        or re.search(
            r"(?:coding\s*plan|token\s*plan|体验卡|套餐|订阅|积分|api[-_ ]?key|token|key|模型|公益站|公益中转|中转站|Base\s*URL|baseurl)",
            blob,
            flags=re.I,
        )
    )


def _extract_model_resource_profile(text: str, model_names: list[str], model_coverage: dict, config: dict) -> dict:
    blob = _compact_text(" ".join([text, " ".join(model_names)]))
    domestic_terms = _unique_strings(
        _config_list(config, "domestic_model_resource_terms", DEFAULT_DOMESTIC_MODEL_RESOURCE_TERMS)
        + _config_list(config, "non_frontline_model_terms", NON_FRONTLINE_MODEL_TERMS),
        limit=80,
    )
    provider_terms = _config_list(config, "domestic_model_provider_terms", DEFAULT_DOMESTIC_MODEL_PROVIDER_TERMS)
    subject_terms = _matched_config_terms(blob, domestic_terms, limit=10)
    provider_matches = _matched_config_terms(blob, provider_terms, limit=8)
    top_resource_families, top_non_resource_mentions, top_snippets = _top_model_resource_mentions(blob, config)
    present_non_top = list(model_coverage.get("present_non_frontline_families") or [])
    is_model_resource = bool(
        _model_resource_action_context(blob, config)
        or model_names
        or subject_terms
        or provider_matches
        or present_non_top
        or top_resource_families
    )
    domestic_subject = bool(is_model_resource and (subject_terms or provider_matches or present_non_top))
    unknown_non_top_plan = bool(
        is_model_resource
        and not domestic_subject
        and not top_resource_families
        and re.search(
            r"(?:coding\s*plan|token\s*plan|套餐|体验卡|订阅).{0,30}(?:用不完|分享|送|赠|领|领取|免费|剩余|还有|转让|福利)"
            r"|(?:分享|送|赠|领|领取|免费|剩余|还有|转让|福利).{0,30}(?:coding\s*plan|token\s*plan|套餐|体验卡|订阅)",
            blob,
            flags=re.I,
        )
    )
    snippets: list[str] = list(top_snippets)
    for term in subject_terms[:4]:
        match = re.search(re.escape(term), blob, flags=re.I)
        if match:
            snippets.append(_context_window(blob, match.start(), match.end(), radius=50))
    confidence = "explicit" if top_resource_families or domestic_subject or unknown_non_top_plan else "unknown"
    subject = ""
    for value in subject_terms + provider_matches + model_names:
        if str(value or "").strip():
            subject = _compact_text(str(value))
            break
    return {
        "is_model_resource": bool(is_model_resource),
        "subject": subject,
        "subject_terms": _unique_strings(subject_terms, limit=8),
        "provider_terms": _unique_strings(provider_matches, limit=6),
        "domestic_or_non_top_subject": bool(domestic_subject),
        "top_model_resource_families": top_resource_families,
        "top_model_non_resource_mentions": top_non_resource_mentions,
        "top_model_is_core_resource": bool(top_resource_families),
        "unknown_non_top_plan": bool(unknown_non_top_plan),
        "confidence": confidence,
        "snippets": _unique_strings([_truncate_text(snippet, 120) for snippet in snippets], limit=4),
    }


def _merge_model_resource_profiles(*profiles: dict | None) -> dict:
    merged = {
        "is_model_resource": False,
        "subject": "",
        "subject_terms": [],
        "provider_terms": [],
        "domestic_or_non_top_subject": False,
        "top_model_resource_families": [],
        "top_model_non_resource_mentions": [],
        "top_model_is_core_resource": False,
        "unknown_non_top_plan": False,
        "confidence": "unknown",
        "snippets": [],
    }
    confidence_rank = {"unknown": 0, "explicit": 1}
    best_rank = 0
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        for key in ("is_model_resource", "domestic_or_non_top_subject", "top_model_is_core_resource", "unknown_non_top_plan"):
            merged[key] = bool(merged[key] or profile.get(key))
        if not merged["subject"] and str(profile.get("subject") or "").strip():
            merged["subject"] = _compact_text(str(profile.get("subject")))
        for key in ("subject_terms", "provider_terms", "top_model_resource_families", "top_model_non_resource_mentions", "snippets"):
            values = merged.get(key, [])
            if not isinstance(values, list):
                values = []
            incoming = profile.get(key)
            if isinstance(incoming, list):
                values.extend(incoming)
            elif incoming:
                values.append(str(incoming))
            merged[key] = values
        confidence = str(profile.get("confidence") or "unknown")
        rank = confidence_rank.get(confidence, 0)
        if rank > best_rank:
            best_rank = rank
            merged["confidence"] = confidence
    merged["subject_terms"] = _unique_strings(merged["subject_terms"], limit=8)
    merged["provider_terms"] = _unique_strings(merged["provider_terms"], limit=6)
    merged["top_model_resource_families"] = _unique_strings(merged["top_model_resource_families"], limit=4)
    merged["top_model_non_resource_mentions"] = _unique_strings([_truncate_text(str(item), 120) for item in merged["top_model_non_resource_mentions"]], limit=4)
    merged["snippets"] = _unique_strings([_truncate_text(str(item), 120) for item in merged["snippets"]], limit=4)
    return merged


def _merge_welfare_resource_profiles(*profiles: dict | None) -> dict:
    merged = {
        "offer_intent": False,
        "question_intent": False,
        "resource_is_image_only": False,
        "domain_context": False,
        "consumer_context": False,
        "explicit_consumer_value": False,
        "top_model_exception": False,
        "has_direct_resource": False,
        "model_resource_subject": False,
        "subscription_question_context": False,
        "signals": [],
    }
    for profile in profiles:
        if not isinstance(profile, dict):
            continue
        for key in (
            "offer_intent",
            "question_intent",
            "resource_is_image_only",
            "domain_context",
            "consumer_context",
            "explicit_consumer_value",
            "top_model_exception",
            "has_direct_resource",
            "model_resource_subject",
            "subscription_question_context",
        ):
            merged[key] = bool(merged.get(key) or profile.get(key))
        signals = merged.get("signals", [])
        if not isinstance(signals, list):
            signals = []
        incoming = profile.get("signals")
        if isinstance(incoming, list):
            signals.extend(incoming)
        elif incoming:
            signals.append(str(incoming))
        merged["signals"] = _unique_strings(signals, limit=10)
    return merged


def classify_frontline_model(model_names: list[str], text: str, config: dict) -> str:
    coverage = _extract_model_coverage(text, model_names, config)
    return _model_verdict_from_coverage(coverage, model_names, config)


def build_evidence_from_text(text: str, config: dict, *, link_titles: list[str] | None = None, resource_url: str = "") -> dict:
    link_titles = link_titles or []
    blob = _compact_text(" ".join([text, " ".join(link_titles), resource_url]))
    model_names = _extract_model_names(blob, config)
    model_coverage = _extract_model_coverage(blob, model_names, config)
    model_resource_profile = _extract_model_resource_profile(blob, model_names, model_coverage, config)
    frontline_model_verdict = _model_verdict_from_coverage(model_coverage, model_names, config)
    evidence = _empty_evidence()
    evidence.update(
        {
            "topic_text": _truncate_text(text, TOPIC_TEXT_MAX_CHARS),
            "resource_url": resource_url,
            "access_method": _extract_access_method(blob),
            "model_names": model_names,
            "provider": _extract_provider(blob, link_titles, resource_url),
            "limits": _extract_limits(blob),
            "price": "免费" if re.search(r"免费|公益|free", blob, flags=re.I) else "",
            "link_titles": link_titles,
            "frontline_model_verdict": frontline_model_verdict,
            "model_coverage": model_coverage,
            "model_resource_profile": model_resource_profile,
        }
    )
    if evidence["frontline_model_verdict"] == "non_frontline":
        evidence["risk_notes"] = "非一线或模型路由资源"
    if resource_url and not evidence["why"]:
        evidence["why"] = "发现可直接访问资源"
    return evidence


def finalize_model_coverage(evidence: dict, config: dict) -> dict:
    profile = evidence.get("model_resource_profile") if isinstance(evidence.get("model_resource_profile"), dict) else {}
    text = _compact_text(
        " ".join(
            [
                str(evidence.get("topic_text") or ""),
                str(evidence.get("provider") or ""),
                str(evidence.get("limits") or ""),
                str(evidence.get("risk_notes") or ""),
                str(evidence.get("why") or ""),
                str(evidence.get("resource_url") or ""),
                " ".join(str(item) for item in evidence.get("link_titles") or []),
                " ".join(str(item) for item in evidence.get("model_names") or []),
                " ".join(str(item) for item in profile.get("subject_terms", [])),
                " ".join(str(item) for item in profile.get("provider_terms", [])),
            ]
        )
    )
    model_names = _unique_strings(list(evidence.get("model_names") or []) + _extract_model_names(text, config), limit=10)
    image_hint = "non_frontline" if str(evidence.get("frontline_model_verdict") or "").lower() == "non_frontline" else ""
    model_coverage = _merge_model_coverage(
        evidence.get("model_coverage") if isinstance(evidence.get("model_coverage"), dict) else None,
        _extract_model_coverage(text, model_names, config, image_hint=image_hint),
    )
    if evidence.get("domestic_only") and not model_coverage.get("present_top_families"):
        model_coverage["domestic_only"] = True
        model_coverage["top_model_absent_explicit"] = True
        model_coverage["confidence"] = "explicit_absent"
    model_resource_profile = _merge_model_resource_profiles(
        evidence.get("model_resource_profile") if isinstance(evidence.get("model_resource_profile"), dict) else None,
        _extract_model_resource_profile(text, model_names, model_coverage, config),
    )
    verdict = _model_verdict_from_coverage(model_coverage, model_names, config)
    if image_hint == "non_frontline" and not model_coverage.get("present_top_families"):
        verdict = "non_frontline"
    evidence["model_names"] = model_names
    evidence["model_coverage"] = model_coverage
    evidence["model_resource_profile"] = model_resource_profile
    evidence["frontline_model_verdict"] = verdict
    if verdict == "non_frontline" and not evidence.get("risk_notes"):
        evidence["risk_notes"] = "非一线或国产-only 模型资源"
    return evidence


def _resource_url_is_image_only(url: str) -> bool:
    text = str(url or "").strip()
    if not text:
        return False
    return text.startswith("#") or _is_internal_image_or_upload_url(text)


def _has_direct_non_image_resource(evidence: dict) -> bool:
    url = str(evidence.get("resource_url") or "").strip()
    return bool(url and not _resource_url_is_image_only(url))


def _low_value_intent_profile(post: LinuxDoPost, evidence: dict | None, config: dict) -> dict:
    evidence = evidence if isinstance(evidence, dict) else {}
    blob = _welfare_low_value_blob(post, evidence)
    topic_context = _compact_text(
        " ".join(
            [
                post.section,
                str(evidence.get("topic_category") or ""),
                " ".join(str(item) for item in evidence.get("topic_tags") or []),
            ]
        )
    )
    welfare = _is_welfare_topic(post, evidence)
    resource_is_image_only = bool(
        str(evidence.get("resource_url") or "").strip()
        and _resource_url_is_image_only(str(evidence.get("resource_url") or ""))
    )
    has_direct_resource = _has_direct_non_image_resource(evidence)
    has_access_method = bool(str(evidence.get("access_method") or "").strip())
    resource_request_intent = bool(
        re.search(
            r"(?:求|蹲|收|想要|来一个|给一个|有没有|有无|谁有|哪位|哪家|求推荐|求稳定|求个|求一个|蹲一个|蹲个|找一个|找个|推荐一个|推荐个).{0,48}(?:公益站|公益中转|中转站|Base\s*URL|baseurl|api[-_ ]?key|token|key|兑换码|邀请码|账号|子号|额度|GPT|Claude|Grok|Gemini|Codex)"
            r"|(?:公益站|公益中转|中转站|Base\s*URL|baseurl|api[-_ ]?key|token|key|兑换码|邀请码|账号|子号|额度|GPT|Claude|Grok|Gemini|Codex).{0,48}(?:求|蹲|收|想要|来一个|给一个|有没有|有无|谁有|求推荐|求稳定|求个|求一个|蹲一个|蹲个|找一个|找个|推荐一个|推荐个)",
            blob,
            flags=re.I,
        )
    )
    availability_question = bool(
        re.search(
            r"(?:现在|今天|最近|这几天|目前|一直|还能|还可以|还行).{0,36}(?:能用|可用|用不|行不|稳定|卡顿|挂了|报错|打不开|空白)"
            r"|(?:能用|可用|用不|行不|稳定|卡顿|挂了|报错|打不开|空白).{0,36}(?:吗|不|么|有没有|有人|现在|今天|最近|这几天|目前|一直|还能|还可以|还行|测过)",
            blob,
            flags=re.I,
        )
    )
    assertive_offer = bool(
        re.search(
            r"(?:我来|给大家|分享|发放|送|赠送|继续送|补货|补了|开放|新增|上线|发布|回馈社区|给需要的佬|先到先得|评论留\s*id|留\s*id\s*送).{0,60}(?:公益站|Base\s*URL|baseurl|api[-_ ]?key|token|key|兑换码|邀请码|账号|子号|额度|GPT|Claude|Grok|Gemini|Codex|福利)"
            r"|(?:公益站|Base\s*URL|baseurl|api[-_ ]?key|token|key|兑换码|邀请码|账号|子号|额度|GPT|Claude|Grok|Gemini|Codex|福利).{0,60}(?:分享|发放|送|赠送|继续送|补货|补了|开放|新增|上线|发布|回馈社区|给需要的佬|先到先得|评论留\s*id|留\s*id\s*送)",
            blob,
            flags=re.I,
        )
    )
    has_actionable_offer = bool(
        has_direct_resource
        or has_access_method
        or _has_invite_code_action(blob)
        or re.search(
            r"(?:兑换码|api[-_ ]?key|token|key\s*填任意值|Base\s*URL|baseurl|补货|补了|恢复|已恢复|修复|扩容|开放|新增|不限额度|不限并发|不限速|火力全开|继续送|发放|分享|赠送|送|评论留\s*id|留\s*id\s*送)",
            blob,
            flags=re.I,
        )
    )
    question_or_help = bool(
        re.search(r"(?:^|[\s\[【（(])(?:求助|请教|求问|咨询|问一下|讨论)(?:[\s\]】）)]|$)", blob, flags=re.I)
        or re.search(
            r"(?:是不是|是否|还有吗|还有.*吗|能用吗|能不能用|一直能用吗|有吗|有没有|有无|为什么|为啥|怎么|如何|咋|怎么办|咋办|请问|求问|咨询|一些问题|问题求助|求助帖|在干什么|干什么|做什么|拿来做什么|用来做什么|用什么方式|什么方式|投票|吗\??|吗？|\?|？)",
            blob,
            flags=re.I,
        )
    )
    troubleshooting_or_availability = bool(
        re.search(
            r"(?:二验|验证|风控|报错|错误|失败|打不开|空白|403|404|429|502|5xx|Service\\s+Unavailable|Request\\s+rejected|API\\s+Error|可用|能用|挂了|卡|不稳定|降智)",
            blob,
            flags=re.I,
        )
    )
    negative_only = bool(
        re.search(
            r"(?:被反薅|反薅|不符合领取|领取失败|失败|错误代码|报错|不可用|不能用|无法领取|领不了|没领到|挂了|拉闸|被封|封号|风控|Service\\s+Unavailable|Request\\s+rejected|API\\s+Error)",
            blob,
            flags=re.I,
        )
    )
    public_status_action = bool(
        re.search(
            r"(?:站长|管理员|官方|公告|通知|本服务|本站|全站|所有用户|所有人|服务级).{0,48}(?:恢复|修复|扩容|开放|新增|规则|封禁|IP限制|额度|补货)"
            r"|(?:恢复|修复|扩容|开放|新增|规则|封禁|IP限制|额度|补货).{0,48}(?:公告|通知|全站|所有用户|所有人|服务级)",
            blob,
            flags=re.I,
        )
    )
    screenshot_or_display_chatter = bool(
        re.search(
            r"(?:截图|截屏|晒|蹬上了|登录了|看起来|显示|它说|感觉|水帖|纯水|调侃|离谱|笑死|什么情况|肥波)",
            blob,
            flags=re.I,
        )
    )
    water_or_screenshot = bool(
        screenshot_or_display_chatter
        and (
            _contains_any(topic_context, ["纯水", "搞七捻三", "快问快答"])
            or resource_is_image_only
            or re.search(r"(?:any|公益站|中转站|Codex|Claude|GPT|Gemini|Grok).{0,40}(?:截图|截屏|蹬上了|显示|它说|肥波)", blob, flags=re.I)
        )
    )
    model_coverage = evidence.get("model_coverage") if isinstance(evidence.get("model_coverage"), dict) else {}
    model_profile = evidence.get("model_resource_profile") if isinstance(evidence.get("model_resource_profile"), dict) else {}
    has_top_model_api_resource = bool(
        (model_profile.get("top_model_is_core_resource") or model_profile.get("top_model_resource_families") or model_coverage.get("present_top_families"))
        and (has_direct_resource or has_access_method)
        and not resource_is_image_only
        and not negative_only
        and not question_or_help
    )
    return {
        "welfare": welfare,
        "question_or_help": question_or_help,
        "resource_request_intent": resource_request_intent,
        "availability_question": availability_question,
        "troubleshooting_or_availability": troubleshooting_or_availability,
        "negative_only": negative_only,
        "water_or_screenshot": water_or_screenshot,
        "has_actionable_offer": has_actionable_offer,
        "assertive_offer": assertive_offer,
        "has_direct_resource": has_direct_resource,
        "has_access_method": has_access_method,
        "has_top_model_api_resource": has_top_model_api_resource,
        "resource_is_image_only": resource_is_image_only,
        "public_status_action": public_status_action,
        "topic_context": topic_context,
    }


def _append_low_value_context(evidence: dict | None, label: str) -> None:
    if not isinstance(evidence, dict):
        return
    evidence["low_value_context"] = _unique_strings(list(evidence.get("low_value_context") or []) + [label], limit=6)


def _welfare_resource_profile(post: LinuxDoPost, evidence: dict | None, config: dict) -> dict:
    evidence = evidence if isinstance(evidence, dict) else {}
    blob = _welfare_low_value_blob(post, evidence)
    resource_url = str(evidence.get("resource_url") or "").strip()
    provider = str(evidence.get("provider") or "").strip()
    domains = _config_list(config, "excluded_domain_welfare_domains", [])
    domain_terms = _config_list(config, "excluded_domain_welfare_terms", ["域名", "domain", "registrar", "de域名", ".de", "续费", "注册"])
    consumer_terms = _config_list(config, "ambiguous_consumer_welfare_terms", [])
    host = urlparse(resource_url).netloc

    has_direct_resource = _has_direct_non_image_resource(evidence)
    resource_is_image_only = bool(resource_url and _resource_url_is_image_only(resource_url))
    domain_host = bool(_domain_matches(host, domains) or _domain_matches(provider, domains))
    domain_text = bool(
        _contains_any(blob, domain_terms)
        and re.search(r"(?:域名|domain|registrar|注册|续费|返场|特价|优惠|deal|deals?|\.de|\.com|\.net|\.org|欧/年|元/年|年付)", blob, flags=re.I)
    )
    consumer_context = bool(_contains_any(blob, consumer_terms))
    question_intent = bool(
        re.search(r"(?:还能|能不能|有没有|有无|还有吗|请问|求问|怎么|如何|吗|\?|？)", blob, flags=re.I)
    )
    subscription_question_context = bool(
        re.search(r"(?:订阅|套餐|价格|涨价|便宜|开通|购买|付款|支付|App\s*Store|网页|卢比|印度|土区|美区|港区|区|月|个月|年).{0,48}(?:问题|奇怪|为什么|为啥|怎么|如何|吗|\?|？|显示|变成|差异|差价|划算|合算)", blob, flags=re.I)
        or re.search(r"(?:问题|奇怪|为什么|为啥|怎么|如何|吗|\?|？|显示|变成|差异|差价|划算|合算).{0,48}(?:订阅|套餐|价格|涨价|便宜|开通|购买|付款|支付|App\s*Store|网页|卢比|印度|土区|美区|港区|区|月|个月|年)", blob, flags=re.I)
    )
    explicit_value_absent = bool(re.search(r"(?:没有|无|未见|缺少|不含).{0,12}(?:金额|兑换码|券码|链接|口令)", blob, flags=re.I))
    explicit_amount_value = bool(
        re.search(r"(?:\d+(?:\.\d+)?\s*(?:元|块|折|%|毛|分)|满\s*\d+|减\s*\d+|0\s*元|立减金|话费券)", blob, flags=re.I)
    )
    explicit_code_value = bool(
        re.search(
            r"(?:券码|兑换码|口令).{0,24}(?:领取|领|兑换|可用|分享|送|赠|填|复制|输入|使用)"
            r"|(?:领取|领|兑换|可用|分享|送|赠|填|复制|输入|使用).{0,24}(?:券码|兑换码|口令)",
            blob,
            flags=re.I,
        )
    )
    explicit_platform_value = bool(
        has_direct_resource and re.search(r"(?:小程序|支付宝|微信|京东|淘宝|天猫|美团|饿了么|农行|建行|工行|移动|联通|电信).{0,30}(?:领取|兑换|活动|券|福利)", blob, flags=re.I)
    )
    explicit_consumer_value = bool((explicit_amount_value or explicit_code_value or explicit_platform_value) and not explicit_value_absent)
    top_model_exception = _top_model_api_exception(post, evidence, config)
    model_profile = evidence.get("model_resource_profile") if isinstance(evidence.get("model_resource_profile"), dict) else {}
    model_resource_subject = bool(model_profile.get("is_model_resource") or _has_model_or_api_resource_context(blob, evidence))
    offer_intent = bool(
        _has_actionable_resource_terms(blob)
        or str(evidence.get("access_method") or "").strip()
        or has_direct_resource
        or re.search(
            r"(?:发放|领取|领|兑换|补货|可用|送|分享|赠|限时|先到先得|免费|活动|福利|key|token|api[-_ ]?key|Base\s*URL|baseurl|额度|账号|子号|team)",
            blob,
            flags=re.I,
        )
    )
    signals: list[str] = []
    for key, active in {
        "domain_host": domain_host,
        "domain_text": domain_text,
        "consumer_context": consumer_context,
        "question_intent": question_intent,
        "image_only": resource_is_image_only,
        "direct_resource": has_direct_resource,
        "explicit_consumer_value": explicit_consumer_value,
        "top_model_exception": top_model_exception,
        "model_resource_subject": model_resource_subject,
        "subscription_question_context": subscription_question_context,
    }.items():
        if active:
            signals.append(key)
    return {
        "offer_intent": offer_intent,
        "question_intent": question_intent,
        "resource_is_image_only": resource_is_image_only,
        "domain_context": bool(domain_host or domain_text),
        "consumer_context": consumer_context,
        "explicit_consumer_value": explicit_consumer_value,
        "top_model_exception": top_model_exception,
        "has_direct_resource": has_direct_resource,
        "model_resource_subject": model_resource_subject,
        "subscription_question_context": subscription_question_context,
        "signals": _unique_strings(signals, limit=10),
    }


def finalize_welfare_resource_profile(post: LinuxDoPost, evidence: dict, config: dict) -> dict:
    if not isinstance(evidence, dict):
        return evidence
    profile = _merge_welfare_resource_profiles(
        evidence.get("welfare_resource_profile") if isinstance(evidence.get("welfare_resource_profile"), dict) else None,
        _welfare_resource_profile(post, evidence, config),
    )
    evidence["welfare_resource_profile"] = profile
    return evidence


def parse_topic_json(topic: dict, config: dict | None = None) -> dict:
    config = config or default_config()
    posts = (((topic or {}).get("post_stream") or {}).get("posts") or [])
    first_post = {}
    for post in posts if isinstance(posts, list) else []:
        if isinstance(post, dict) and int(post.get("post_number") or 0) == 1:
            first_post = post
            break
    if not first_post and isinstance(posts, list) and posts and isinstance(posts[0], dict):
        first_post = posts[0]

    cooked = str(first_post.get("cooked") or "")
    topic_text = strip_tags(cooked)
    link_counts = first_post.get("link_counts") if isinstance(first_post.get("link_counts"), list) else []
    link_titles = _unique_strings([link.get("title") for link in link_counts if isinstance(link, dict) and link.get("title")], limit=8)
    resource_url = _first_resource_url(topic_text, cooked, link_counts)
    reference_url = ""
    if _looks_like_reference_url(topic_text, link_titles, resource_url, config):
        reference_url = resource_url
        resource_url = ""
    image_urls = _extract_image_urls_from_html(cooked)
    for key in ("image_url",):
        if topic.get(key):
            image_urls.append(str(topic.get(key)))
    for thumb in topic.get("thumbnails") or []:
        if isinstance(thumb, dict) and thumb.get("url"):
            image_urls.append(str(thumb.get("url")))

    evidence = build_evidence_from_text(topic_text, config, link_titles=link_titles, resource_url=resource_url)
    topic_tags = _unique_strings(
        [
            *(topic.get("tags") if isinstance(topic.get("tags"), list) else []),
            *[
                str(tag.get("name") or "")
                for tag in (topic.get("tag_objects") if isinstance(topic.get("tag_objects"), list) else [])
                if isinstance(tag, dict)
            ],
        ],
        limit=8,
    )
    category_obj = topic.get("category") if isinstance(topic.get("category"), dict) else {}
    topic_category = str(topic.get("category_name") or "").strip()
    if not topic_category and isinstance(topic.get("category"), str):
        topic_category = str(topic.get("category") or "").strip()
    if not topic_category:
        topic_category = str(category_obj.get("name") or category_obj.get("slug") or "").strip()
    topic_category = _compact_text(topic_category)
    context_evidence = dict(evidence)
    context_evidence.update({"topic_text": topic_text, "topic_tags": topic_tags, "topic_category": topic_category})
    public_site_signal = _public_site_signal_facts(_evidence_context_blob(LinuxDoPost("", 0, "", "", "", "", "", str(topic.get("title") or ""), "", topic_category, "", topic_text, "primary"), context_evidence), context_evidence, config)
    low_value_context: list[str] = []
    synthetic_post = LinuxDoPost("", 0, "", "", "", "", "", str(topic.get("title") or ""), "", topic_category, "", topic_text, "primary")
    if _is_public_site_chatter_low_value(synthetic_post, context_evidence, config):
        low_value_context.append("public_site_chatter_low_value")
    if _is_public_site_shutdown_low_value(synthetic_post, context_evidence, config):
        low_value_context.append("public_site_shutdown_low_value")
    if _is_research_academic_low_value(synthetic_post, context_evidence, config):
        low_value_context.append("research_academic_low_value")
    post_value_profile = build_post_value_profile(synthetic_post, context_evidence, config)
    evidence.update(
        {
            "topic_text": _truncate_text(topic_text, TOPIC_TEXT_MAX_CHARS),
            "image_urls": _unique_strings(image_urls, limit=8),
            "image_dimensions": _extract_image_dimensions(cooked),
            "link_titles": link_titles,
            "reference_url": reference_url,
            "topic_category": topic_category,
            "topic_tags": topic_tags,
            "public_site_signal": public_site_signal,
            "post_value_profile": post_value_profile,
            "low_value_context": low_value_context,
            "topic_metrics": {
                "views": topic.get("views"),
                "like_count": topic.get("like_count"),
                "reads": first_post.get("reads"),
                "score": first_post.get("score"),
            },
        }
    )
    merged = finalize_model_coverage(_merge_evidence(evidence), config)
    return finalize_post_value_profile(synthetic_post, merged, config)


def fetch_topic_evidence(post: LinuxDoPost, config: dict, *, deadline: float | None = None) -> dict:
    if not config.get("topic_json_enabled", True) or not post.topic_id:
        return _empty_evidence()
    timeout = int(config.get("fetch_timeout_seconds") or 8)
    if deadline_remaining(deadline) < min(timeout, 4):
        return _empty_evidence()
    try:
        topic = fetch_json(
            f"https://linux.do/t/topic/{post.topic_id}.json",
            timeout=timeout,
            attempts=max(1, min(int(config.get("fetch_attempts") or 3), 2)),
            phase="topic_json",
        )
        return parse_topic_json(topic, config)
    except Exception as exc:
        print(f"linuxdo_topic_enrich_failed topic_id={post.topic_id} error={type(exc).__name__}: {exc}", file=sys.stderr)
        return _empty_evidence()


def _compact_model_coverage_for_prompt(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, Any] = {}
    for key in ("present_top_families", "present_non_frontline_families", "source", "snippets"):
        items = value.get(key)
        if not isinstance(items, list):
            continue
        limit = 3 if key == "snippets" else 6
        compact[key] = [_truncate_text(str(item), 120) for item in items[:limit] if str(item or "").strip()]
    for key in ("domestic_only", "top_model_absent_explicit"):
        compact[key] = bool(value.get(key))
    confidence = str(value.get("confidence") or "").strip()
    if confidence:
        compact["confidence"] = confidence
    return {key: val for key, val in compact.items() if val not in ("", None, [], {})}


def _compact_model_resource_profile_for_prompt(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, Any] = {}
    for key in ("subject", "confidence"):
        text = str(value.get(key) or "").strip()
        if text:
            compact[key] = _truncate_text(text, 120)
    for key in ("is_model_resource", "domestic_or_non_top_subject", "top_model_is_core_resource", "unknown_non_top_plan"):
        compact[key] = bool(value.get(key))
    for key in ("subject_terms", "provider_terms", "top_model_resource_families", "top_model_non_resource_mentions", "snippets"):
        items = value.get(key)
        if not isinstance(items, list):
            continue
        limit = 3 if key in {"top_model_non_resource_mentions", "snippets"} else 6
        compact[key] = [_truncate_text(str(item), 120) for item in items[:limit] if str(item or "").strip()]
    return {key: val for key, val in compact.items() if val not in ("", None, [], {})}


def _compact_welfare_resource_profile_for_prompt(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    compact: dict[str, Any] = {}
    for key in (
        "offer_intent",
        "question_intent",
        "resource_is_image_only",
        "domain_context",
        "consumer_context",
        "explicit_consumer_value",
        "top_model_exception",
        "has_direct_resource",
        "model_resource_subject",
        "subscription_question_context",
    ):
        compact[key] = bool(value.get(key))
    signals = value.get("signals")
    if isinstance(signals, list):
        compact["signals"] = [_truncate_text(str(item), 80) for item in signals[:8] if str(item or "").strip()]
    return {key: val for key, val in compact.items() if val not in ("", None, [], {}) and val is not False}


def _compact_evidence_for_prompt(evidence: dict | None) -> dict:
    evidence = evidence if isinstance(evidence, dict) else {}
    allowed = [
        "topic_text",
        "resource_url",
        "reference_url",
        "access_method",
        "model_names",
        "provider",
        "limits",
        "price",
        "risk_notes",
        "why",
        "frontline_model_verdict",
        "model_coverage",
        "model_resource_profile",
        "welfare_resource_profile",
        "image_text_status",
        "link_titles",
        "topic_metrics",
        "topic_category",
        "topic_tags",
        "public_site_signal",
        "post_value_profile",
        "low_value_context",
    ]
    compact: dict[str, Any] = {}
    for key in allowed:
        value = evidence.get(key)
        if value in ("", None, [], {}):
            continue
        if key == "model_coverage":
            compact_value = _compact_model_coverage_for_prompt(value)
            if compact_value:
                compact[key] = compact_value
            continue
        if key == "model_resource_profile":
            compact_value = _compact_model_resource_profile_for_prompt(value)
            if compact_value:
                compact[key] = compact_value
            continue
        if key == "welfare_resource_profile":
            compact_value = _compact_welfare_resource_profile_for_prompt(value)
            if compact_value:
                compact[key] = compact_value
            continue
        if isinstance(value, str):
            compact[key] = _truncate_text(value, EVIDENCE_TEXT_MAX_CHARS)
        elif isinstance(value, list):
            compact[key] = [_truncate_text(str(item), 120) for item in value[:8] if str(item or "").strip()]
        elif isinstance(value, dict) and key == "public_site_signal":
            compact[key] = {
                str(signal_key): signal_value
                for signal_key, signal_value in value.items()
                if signal_key
                in {
                    "has_public_site",
                    "has_direct_resource",
                    "has_action_update",
                    "has_operator_notice",
                    "has_any_operator_notice",
                    "terminal_status",
                    "actionable_exception",
                    "terminal_actionable_exception",
                    "loss_only",
                    "access_method",
                }
                and signal_value not in ("", None, [], {})
            }
        elif isinstance(value, dict) and key == "post_value_profile":
            compact[key] = {
                str(profile_key): profile_value
                for profile_key, profile_value in value.items()
                if profile_key
                in {
                    "domain",
                    "actionability",
                    "benefit_type",
                    "resource_available",
                    "replacement_available",
                    "top_model_available",
                    "user_action_required",
                    "loss_only",
                    "value_tier",
                    "hard_silent_reason",
                }
                and profile_value not in ("", None, [], {})
            }
        else:
            compact[key] = value
    return compact


def _is_technical_reference_content(post: LinuxDoPost, evidence: dict, config: dict) -> bool:
    if _is_welfare_topic(post, evidence):
        return False
    blob = _compact_text(
        " ".join(
            [
                _post_blob(post),
                str(evidence.get("topic_text") or ""),
                " ".join(evidence.get("link_titles") or []),
                str(evidence.get("reference_url") or ""),
                str(evidence.get("resource_url") or ""),
            ]
        )
    )
    if _has_actionable_resource_terms(blob):
        return False
    has_reference_link = bool(evidence.get("reference_url")) or _looks_like_reference_url(
        str(evidence.get("topic_text") or ""),
        [str(item) for item in evidence.get("link_titles") or []],
        str(evidence.get("resource_url") or ""),
        config,
    )
    has_reference_language = _contains_any(blob, config.get("technical_reference_keywords", []))
    has_tool_context = _contains_any(blob, config.get("tool_keywords", [])) or bool(re.search(r"\b(?:agent|rg|ripgrep)\b", blob, flags=re.I))
    return bool(has_tool_context and (has_reference_link or has_reference_language))


def _is_ai_public_site_resource(post: LinuxDoPost, evidence: dict, config: dict) -> bool:
    blob = _compact_text(
        " ".join(
            [
                _post_blob(post),
                str(evidence.get("topic_text") or ""),
                str(evidence.get("resource_url") or ""),
                str(evidence.get("access_method") or ""),
                " ".join(str(item) for item in evidence.get("link_titles") or []),
            ]
        )
    )
    if not blob:
        return False
    public_site = bool(
        re.search(r"(?:AI\s*)?(?:公益站|公益中转|中转站)|api\s*公益|Base\s*URL|baseurl", blob, flags=re.I)
        or (evidence.get("resource_url") and _contains_any(blob, config.get("public_site_keywords", [])))
    )
    ai_context = bool(
        re.search(r"(?:AI|API|LLM|模型|model|GPT|Claude|Grok|Gemini|OpenAI|Codex|中转|公益站|key\s*填任意值)", blob, flags=re.I)
    )
    actionable = bool(evidence.get("resource_url") or evidence.get("access_method") or _has_direct_public_site_activity(blob))
    return bool(public_site and ai_context and actionable)


def _evidence_for_local_gate(post: LinuxDoPost, evidence: dict | None, config: dict) -> dict:
    if isinstance(evidence, dict) and evidence:
        return evidence
    return build_evidence_from_text(_post_blob(post), config)


def _is_model_resource_offer_context(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    blob = _evidence_context_blob(post, evidence) or _post_blob(post)
    if not blob:
        return False
    profile = evidence.get("model_resource_profile") if isinstance(evidence.get("model_resource_profile"), dict) else {}
    if not profile.get("is_model_resource"):
        return False
    if _is_welfare_topic(post, evidence):
        return True
    if evidence.get("resource_url") or evidence.get("access_method"):
        return True
    if _is_ai_public_site_resource(post, evidence, config):
        return True
    if _has_direct_public_site_activity(blob) or _has_actionable_resource_terms(blob):
        return True
    return bool(
        re.search(
            r"(?:coding\s*plan|token\s*plan|套餐|体验卡|订阅|账户|账号|key|token|api[-_ ]?key|Base\s*URL|baseurl|公益站|中转站|公益中转).{0,36}(?:分享|送|赠|领|领取|免费|剩余|还有|到期|过期|速登|速蹬|开放|可用|9\.9|优惠|福利)"
            r"|(?:分享|送|赠|领|领取|免费|剩余|还有|到期|过期|速登|速蹬|开放|可用|9\.9|优惠|福利).{0,36}(?:coding\s*plan|token\s*plan|套餐|体验卡|订阅|账户|账号|key|token|api[-_ ]?key|Base\s*URL|baseurl|公益站|中转站|公益中转)",
            blob,
            flags=re.I,
        )
    )


def _is_welfare_question_low_value(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if not _is_welfare_topic(post, evidence):
        return False
    profile = _low_value_intent_profile(post, evidence, config)
    if profile.get("question_or_help") and not profile.get("public_status_action") and not profile.get("has_top_model_api_resource"):
        return True
    blob = _evidence_context_blob(post, evidence)
    if not blob:
        blob = _post_blob(post)
    question_intent = bool(
        re.search(
            r"(?:是不是|是否|还有吗|还有.*吗|有吗|有没有|有无|为什么|为啥|怎么|如何|咋|请问|求问|咨询|一些问题|延迟|涨价|订阅问题|配置问题|没了|彻底没了|打不开|报错|失败)",
            blob,
            flags=re.I,
        )
    )
    if not question_intent:
        return False
    offering_resource = bool(evidence.get("resource_url") or evidence.get("access_method") or _has_actionable_resource_terms(blob))
    if offering_resource and not re.search(r"(?:是不是|是否|还有|为什么|为啥|怎么|如何|请问|求问|咨询|一些问题|延迟|涨价|没了|打不开|报错|失败)", blob, flags=re.I):
        return False
    return True


def _is_welfare_subscription_question_low_value(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if not _is_welfare_topic(post, evidence):
        return False
    profile = evidence.get("welfare_resource_profile") if isinstance(evidence.get("welfare_resource_profile"), dict) else {}
    if not profile.get("subscription_question_context"):
        profile = _welfare_resource_profile(post, evidence, config)
    if not profile.get("subscription_question_context"):
        return False
    blob = _welfare_low_value_blob(post, evidence)
    concrete_resource = bool(
        str(evidence.get("access_method") or "").strip()
        or re.search(
            r"(?:Base\s*URL|baseurl|api[-_ ]?key|token|key\s*填任意值|账号|子号|team|额度).{0,36}(?:发放|领取|领|送|赠|分享|可用|不限|补货|开放)"
            r"|(?:发放|领取|领|送|赠|分享|可用|不限|补货|开放).{0,36}(?:Base\s*URL|baseurl|api[-_ ]?key|token|key\s*填任意值|账号|子号|team|额度)",
            blob,
            flags=re.I,
        )
    )
    if concrete_resource:
        return False
    return True


def _is_domestic_or_non_top_model_welfare(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = _evidence_for_local_gate(post, evidence, config)
    if not _is_model_resource_offer_context(post, evidence, config):
        return False
    profile = evidence.get("model_resource_profile") if isinstance(evidence.get("model_resource_profile"), dict) else {}
    coverage = evidence.get("model_coverage") if isinstance(evidence.get("model_coverage"), dict) else {}
    if profile.get("top_model_is_core_resource") or profile.get("top_model_resource_families"):
        return False
    return bool(
        profile.get("is_model_resource")
        and (
            profile.get("domestic_or_non_top_subject")
            or coverage.get("domestic_only")
            or coverage.get("top_model_absent_explicit")
            or (coverage.get("present_non_frontline_families") and not coverage.get("present_top_families"))
        )
    )


def _is_non_beijing_local_welfare(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if not _is_welfare_topic(post, evidence):
        return False
    blob = _evidence_context_blob(post, evidence) or _post_blob(post)
    if not blob:
        return False
    allowed_regions = _config_list(config, "allowed_local_regions", ["北京"])
    if _contains_any(blob, allowed_regions):
        return False
    local_terms = _config_list(
        config,
        "local_welfare_keywords",
        ["地区", "线下", "门店", "到店", "核销", "自提", "同城", "咖啡", "移动", "联通", "电信", "流量"],
    )
    if not _contains_any(blob, local_terms):
        return False
    if _is_ai_public_site_resource(post, evidence, config):
        return False
    region_terms = _config_list(
        config,
        "non_beijing_local_regions",
        [
            "上海",
            "山西",
            "天津",
            "重庆",
            "河北",
            "河南",
            "山东",
            "江苏",
            "浙江",
            "安徽",
            "福建",
            "江西",
            "湖北",
            "湖南",
            "广东",
            "广西",
            "海南",
            "四川",
            "贵州",
            "云南",
            "陕西",
            "甘肃",
            "青海",
            "辽宁",
            "吉林",
            "黑龙江",
            "内蒙古",
            "宁夏",
            "新疆",
            "西藏",
            "香港",
            "澳门",
            "台湾",
        ],
    )
    matched_regions = [term for term in region_terms if str(term or "").strip() and str(term).strip().lower() in blob.lower()]
    if not matched_regions:
        return False
    action_context = bool(
        re.search(
            r"(?:地区|本地|当地|线下|门店|到店|核销|自提|同城|营业厅|移动|联通|电信|全家|咖啡|流量).{0,30}(?:免费|0元|领取|领|送|兑换|核销|自提|限|仅限|参加|活动)"
            r"|(?:免费|0元|领取|领|送|兑换|核销|自提|限|仅限|参加|活动).{0,30}(?:地区|本地|当地|线下|门店|到店|同城|营业厅|移动|联通|电信|全家|咖啡|流量)",
            blob,
            flags=re.I,
        )
    )
    region_context = any(
        re.search(
            rf"{re.escape(str(region))}.{{0,24}}(?:地区|本地|门店|到店|核销|自提|全家|咖啡|移动|联通|电信|流量|免费|0元|领取|领|送|活动)"
            rf"|(?:地区|本地|门店|到店|核销|自提|全家|咖啡|移动|联通|电信|流量|免费|0元|领取|领|送|活动).{{0,24}}{re.escape(str(region))}",
            blob,
            flags=re.I,
        )
        for region in matched_regions
    )
    return bool(action_context and region_context)


def _is_user_excluded_service_welfare(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if not _is_welfare_topic(post, evidence):
        return False
    blob = _evidence_context_blob(post, evidence) or _post_blob(post)
    if not blob:
        return False
    terms = _config_list(config, "excluded_service_welfare_terms", ["WPS"])
    if not _contains_any(blob, terms):
        return False
    if _has_developer_project_context(blob) or _is_ai_public_site_resource(post, evidence, config):
        return False
    benefit_context = bool(re.search(r"(?:会员|大会员|月卡|季卡|年卡|权益|订阅|兑换码|激活码|积分)", blob, flags=re.I))
    action_context = bool(re.search(r"(?:免费|领取|领|福利|兑换|赠送|送|白嫖|活动|限时)", blob, flags=re.I))
    service_context = any(
        re.search(
            rf"{re.escape(str(term))}.{{0,30}}(?:会员|大会员|月卡|季卡|年卡|权益|订阅|免费|领取|领|兑换|福利|活动)"
            rf"|(?:会员|大会员|月卡|季卡|年卡|权益|订阅|免费|领取|领|兑换|福利|活动).{{0,30}}{re.escape(str(term))}",
            blob,
            flags=re.I,
        )
        for term in terms
        if str(term or "").strip()
    )
    return bool(service_context and benefit_context and action_context)


def _is_domain_registration_welfare(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if not _is_welfare_topic(post, evidence) or _top_model_api_exception(post, evidence, config):
        return False
    profile = _welfare_resource_profile(post, evidence, config)
    if not profile.get("domain_context"):
        return False
    blob = _welfare_low_value_blob(post, evidence)
    if _has_model_or_api_resource_context(blob, evidence) and not re.search(r"(?:域名|domain|registrar|续费|注册|\.de|欧/年|元/年)", blob, flags=re.I):
        return False
    return bool(profile.get("offer_intent") or profile.get("has_direct_resource"))


def _is_ambiguous_consumer_welfare_low_value(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if not _is_welfare_topic(post, evidence) or _top_model_api_exception(post, evidence, config):
        return False
    profile = _welfare_resource_profile(post, evidence, config)
    if not profile.get("consumer_context"):
        return False
    if profile.get("explicit_consumer_value") or profile.get("has_direct_resource"):
        return False
    if profile.get("model_resource_subject"):
        return False
    return bool(profile.get("resource_is_image_only") or not profile.get("offer_intent") or profile.get("question_intent"))


def _top_model_api_exception(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    blob = _evidence_context_blob(post, evidence) or _post_blob(post)
    if not blob:
        return False
    profile = evidence.get("model_resource_profile") if isinstance(evidence.get("model_resource_profile"), dict) else {}
    coverage = evidence.get("model_coverage") if isinstance(evidence.get("model_coverage"), dict) else {}
    if profile.get("top_model_is_core_resource") or profile.get("top_model_resource_families"):
        return True
    if coverage.get("present_top_families") and _has_model_or_api_resource_context(blob, evidence) and _has_actionable_resource_terms(blob):
        return True
    return bool(_is_ai_public_site_resource(post, evidence, config) and coverage.get("present_top_families"))


def _welfare_low_value_blob(post: LinuxDoPost, evidence: dict | None) -> str:
    evidence = evidence if isinstance(evidence, dict) else {}
    return _evidence_context_blob(post, evidence) or _post_blob(post)


def _is_infrastructure_hosting_welfare(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if not _is_welfare_topic(post, evidence) or _top_model_api_exception(post, evidence, config):
        return False
    blob = _welfare_low_value_blob(post, evidence)
    if not _contains_any(blob, _config_list(config, "excluded_infrastructure_welfare_terms", DEFAULT_EXCLUDED_INFRASTRUCTURE_WELFARE_TERMS)):
        return False
    return bool(re.search(r"(?:流量|快捷指令|VPS|服务器|主机|到期|续费|套餐|带宽|traffic|hosting)", blob, flags=re.I))


def _is_media_subscription_welfare(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if not _is_welfare_topic(post, evidence) or _top_model_api_exception(post, evidence, config):
        return False
    blob = _welfare_low_value_blob(post, evidence)
    if not _contains_any(blob, _config_list(config, "excluded_media_subscription_terms", DEFAULT_EXCLUDED_MEDIA_SUBSCRIPTION_TERMS)):
        return False
    return bool(re.search(r"(?:会员|体验卡|月卡|季卡|年卡|权益|听书|音频|兑换|领取|赠送|免费|福利)", blob, flags=re.I))


def _is_private_tracker_welfare(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if not _is_welfare_topic(post, evidence) or _top_model_api_exception(post, evidence, config):
        return False
    blob = _welfare_low_value_blob(post, evidence)
    terms = _config_list(config, "excluded_private_tracker_terms", DEFAULT_EXCLUDED_PRIVATE_TRACKER_TERMS)
    if not _contains_any(blob, terms):
        return False
    return bool(re.search(r"(?:PTTIME|PT\s*(?:站|邀请|邀请码|馒头|猫站)|tracker|种子|保种|魔力|上传量|下载量)", blob, flags=re.I))


def _is_forum_currency_chatter(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    if not _is_welfare_topic(post, evidence) or _top_model_api_exception(post, evidence, config):
        return False
    blob = _welfare_low_value_blob(post, evidence)
    terms = _config_list(config, "forum_currency_chatter_terms", DEFAULT_FORUM_CURRENCY_CHATTER_TERMS)
    if not _contains_any(blob, terms):
        return False
    return bool(re.search(r"(?:工资|难赚|怎么赚|攒|打赏|悬赏|论坛币|站内币|LDC|纯水|闲聊|新人发现)", blob, flags=re.I))


def _is_question_discussion_low_value(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    blob = _welfare_low_value_blob(post, evidence)
    if not blob:
        return False
    low_value_profile = _low_value_intent_profile(post, evidence, config)
    if _top_model_api_exception(post, evidence, config) and not (
        low_value_profile.get("question_or_help")
        or low_value_profile.get("negative_only")
        or low_value_profile.get("water_or_screenshot")
    ):
        return False
    if _is_support_troubleshooting_help(post, evidence, config):
        return False
    if _is_generic_question(post, config, blob) and not _contains_any(blob, config.get("public_site_keywords", [])):
        return False
    question_intent = bool(
        re.search(
            r"(?:是不是|是否|为什么|为啥|怎么|如何|请问|求问|咨询|问题|有无|有没有|哪家|哪个|合算|还是|在干什么|干什么|做什么|拿来做什么|用来做什么|用什么方式|什么方式|投票|吗|\?|？)",
            blob,
            flags=re.I,
        )
    )
    discussion_context = bool(
        re.search(r"(?:降智|中转站|公益站|Claude\s+Max|GPT|Claude|Codex|token|api|订阅|额度|购买|拼车|签到|薅羊毛|可用|报错|失败|体验|讨论|大家|平时|投票|poll)", blob, flags=re.I)
    )
    offering_resource = bool(evidence.get("resource_url") or evidence.get("access_method") or _has_actionable_resource_terms(blob))
    assertive_resource_release = bool(
        re.search(
            r"(?:发布|上线|开业|开放|发放|送|赠送|分享|领取|补货|补了|恢复|扩容|评论即可|盖楼送).{0,48}(?:Base\s*URL|baseurl|api[-_ ]?key|apikey|token|key|额度|公益站|中转站|GPT|Claude|Codex)"
            r"|(?:Base\s*URL|baseurl|api[-_ ]?key|apikey|token|key|额度|公益站|中转站|GPT|Claude|Codex).{0,48}(?:发布|上线|开业|开放|发放|送|赠送|分享|领取|补货|补了|恢复|扩容|评论即可|盖楼送)",
            blob,
            flags=re.I,
        )
    )
    concrete_release = bool((evidence.get("resource_url") or evidence.get("access_method")) and assertive_resource_release)
    troubleshooting_question = bool(
        question_intent
        and re.search(
            r"(?:配置|Hub\s*站|public-sub2api|provider|model|/model|apikey|api[-_ ]?key|Base\s*URL|baseurl|报错|错误|失败|超时|timeout|APITimeoutError|疑惑|疑问)",
            blob,
            flags=re.I,
        )
    )
    usage_discussion = bool(
        question_intent
        and discussion_context
        and re.search(r"(?:token|api|订阅|公益站|中转站|额度|购买|拼车|签到|薅羊毛|GPT|Claude|Codex)", blob, flags=re.I)
    )
    return bool(
        question_intent
        and (discussion_context or troubleshooting_question or usage_discussion)
        and (troubleshooting_question or usage_discussion or not offering_resource or not concrete_release)
        and not assertive_resource_release
    )


def _is_low_value_welfare_intent(post: LinuxDoPost, evidence: dict | None, config: dict) -> tuple[bool, str]:
    profile = _low_value_intent_profile(post, evidence, config)
    if not profile.get("welfare"):
        return False, ""
    if profile.get("public_status_action") or profile.get("has_top_model_api_resource"):
        return False, ""
    if profile.get("water_or_screenshot") and (
        profile.get("resource_is_image_only") or not profile.get("assertive_offer")
    ):
        return True, "welfare_water_or_screenshot_low_value"
    if profile.get("resource_request_intent") or profile.get("availability_question"):
        return True, "welfare_question_low_value"
    if profile.get("question_or_help") and not profile.get("assertive_offer"):
        return True, "welfare_question_low_value"
    if profile.get("negative_only") and not profile.get("assertive_offer"):
        return True, "negative_welfare_status_low_value"
    if (
        profile.get("troubleshooting_or_availability")
        and not profile.get("assertive_offer")
        and not profile.get("has_direct_resource")
    ):
        return True, "welfare_question_low_value"
    return False, ""


def apply_enriched_gate(post: LinuxDoPost, decision: RouteDecision, evidence: dict, config: dict) -> RouteDecision:
    if decision.route == "silent":
        return decision
    finalize_model_coverage(evidence, config)
    finalize_welfare_resource_profile(post, evidence, config)
    finalize_post_value_profile(post, evidence, config)
    categories = list(decision.matched_categories)
    verdict = str(evidence.get("frontline_model_verdict") or "unknown")
    model_coverage = evidence.get("model_coverage") if isinstance(evidence.get("model_coverage"), dict) else {}
    model_profile = evidence.get("model_resource_profile") if isinstance(evidence.get("model_resource_profile"), dict) else {}
    if verdict == "non_frontline":
        categories.append("domestic_only_model" if model_coverage.get("domestic_only") else "non_frontline_model")
        categories.append("non_frontline_model")
        return RouteDecision("silent", "非一线/国产-only 模型资源默认静默", _unique_strings(categories))

    if _is_email_account_giveaway(_compact_text(" ".join([_post_blob(post), str(evidence.get("topic_text") or "")])), config):
        categories.append("email_account_giveaway")
        return RouteDecision("silent", "邮箱账号赠送默认静默", _unique_strings(categories))

    if _is_forum_chatter_low_value(post, evidence, config):
        categories.append("forum_chatter_low_value")
        return RouteDecision("silent", "论坛等级/邀请码/截图闲聊默认静默", _unique_strings(categories))

    if _is_welfare_subscription_question_low_value(post, evidence, config):
        categories.extend(["low_value_welfare_intent", "welfare_question_low_value", "welfare_subscription_question_low_value"])
        evidence["low_value_context"] = _unique_strings(list(evidence.get("low_value_context") or []) + ["welfare_subscription_question_low_value"], limit=6)
        return RouteDecision("silent", "福利羊毛订阅/价格/地区提问默认静默", _unique_strings(categories))

    if _is_domestic_or_non_top_model_welfare(post, evidence, config):
        categories.append("domestic_or_non_top_model_welfare")
        evidence["risk_notes"] = evidence.get("risk_notes") or "国产/非顶级模型福利，未确认 GPT/Claude/Grok/Gemini 可用资源"
        return RouteDecision("silent", "国产/非顶级模型福利默认静默", _unique_strings(categories))

    if _is_non_beijing_local_welfare(post, evidence, config):
        categories.append("non_beijing_local_welfare")
        return RouteDecision("silent", "非北京地区限定福利默认静默", _unique_strings(categories))

    if _is_user_excluded_service_welfare(post, evidence, config):
        categories.append("user_excluded_service_welfare")
        return RouteDecision("silent", "命中用户排除服务福利，默认静默", _unique_strings(categories))

    if _is_domain_registration_welfare(post, evidence, config):
        categories.append("domain_registration_welfare")
        evidence["low_value_context"] = _unique_strings(list(evidence.get("low_value_context") or []) + ["domain_registration_welfare"], limit=6)
        return RouteDecision("silent", "域名注册/续费促销福利默认静默", _unique_strings(categories))

    if _is_ambiguous_consumer_welfare_low_value(post, evidence, config):
        categories.append("ambiguous_consumer_welfare_low_value")
        evidence["low_value_context"] = _unique_strings(list(evidence.get("low_value_context") or []) + ["ambiguous_consumer_welfare_low_value"], limit=6)
        return RouteDecision("silent", "缺少明确价值的生活消费福利水帖默认静默", _unique_strings(categories))

    if _is_infrastructure_hosting_welfare(post, evidence, config):
        categories.append("infrastructure_hosting_welfare")
        return RouteDecision("silent", "主机/流量类福利默认静默", _unique_strings(categories))

    if _is_media_subscription_welfare(post, evidence, config):
        categories.append("media_subscription_welfare")
        return RouteDecision("silent", "音频/媒体会员福利默认静默", _unique_strings(categories))

    if _is_private_tracker_welfare(post, evidence, config):
        categories.append("private_tracker_welfare")
        return RouteDecision("silent", "PT/私有站福利默认静默", _unique_strings(categories))

    if _is_forum_currency_chatter(post, evidence, config):
        categories.append("forum_currency_chatter")
        return RouteDecision("silent", "论坛货币/等级闲聊默认静默", _unique_strings(categories))

    low_value_welfare, low_value_category = _is_low_value_welfare_intent(post, evidence, config)
    if low_value_welfare:
        categories.extend(["low_value_welfare_intent", low_value_category])
        _append_low_value_context(evidence, low_value_category)
        return RouteDecision("silent", "福利羊毛提问/负面/截图水帖默认静默", _unique_strings(categories))

    if _is_welfare_question_low_value(post, evidence, config):
        categories.extend(["low_value_welfare_intent", "welfare_question_low_value"])
        _append_low_value_context(evidence, "welfare_question_low_value")
        return RouteDecision("silent", "福利羊毛提问/咨询帖默认静默", _unique_strings(categories))

    if _is_question_discussion_low_value(post, evidence, config):
        categories.append("question_discussion_low_value")
        return RouteDecision("silent", "普通提问/讨论帖默认静默", _unique_strings(categories))

    if _is_design_resource_low_value(post, evidence, config):
        categories.append("design_resource_low_value")
        return RouteDecision("silent", "设计/创作工具福利资源默认静默", _unique_strings(categories))

    if _is_game_resource_low_value(post, evidence, config):
        categories.append("game_resource_low_value")
        return RouteDecision("silent", "游戏/娱乐福利资源默认静默", _unique_strings(categories))

    if _is_public_site_shutdown_low_value(post, evidence, config):
        categories.append("public_site_shutdown_low_value")
        _append_low_value_context(evidence, "public_site_shutdown_low_value")
        finalize_post_value_profile(post, evidence, config)
        return RouteDecision("silent", "公益站关闭/停运且无可执行替代资源默认静默", _unique_strings(categories))

    if _is_public_site_chatter_low_value(post, evidence, config):
        categories.append("public_site_chatter_low_value")
        evidence["low_value_context"] = _unique_strings(list(evidence.get("low_value_context") or []) + ["public_site_chatter_low_value"], limit=6)
        evidence["public_site_signal"] = _public_site_signal_facts(_evidence_context_blob(post, evidence), evidence, config)
        finalize_post_value_profile(post, evidence, config)
        return RouteDecision("silent", "公益站/中转站普通可用性闲聊默认静默", _unique_strings(categories))

    if _is_research_academic_low_value(post, evidence, config):
        categories.append("research_academic_low_value")
        evidence["low_value_context"] = _unique_strings(list(evidence.get("low_value_context") or []) + ["research_academic_low_value"], limit=6)
        return RouteDecision("silent", "科研/论文/学术资料类内容默认静默", _unique_strings(categories))

    if _is_support_troubleshooting_help(post, evidence, config):
        categories.append("support_troubleshooting_help")
        return RouteDecision("silent", "普通求助/配置排错默认静默", _unique_strings(categories))

    if _is_technical_reference_content(post, evidence, config):
        categories.append("technical_reference")
        return RouteDecision("silent", "普通技术教程/博客/参数经验帖默认静默", _unique_strings(categories))

    if _is_welfare_new_topic(post, config, evidence):
        if (
            verdict == "unknown"
            and _is_ai_public_site_resource(post, evidence, config)
        ):
            categories.append("unknown_ai_public_site")
            if not evidence.get("why"):
                evidence["why"] = "AI 公益站未列明 GPT/Claude/Grok/Gemini 支持，证据不足静默"
            return RouteDecision("silent", evidence["why"], _unique_strings(categories))
        categories.append("welfare_full_push")
        if not evidence.get("why"):
            evidence["why"] = "福利羊毛新主题，按当前策略先全量推送"
        return RouteDecision("push", "#福利羊毛新主题默认推送", _unique_strings(categories))

    blob = _compact_text(" ".join([_post_blob(post), str(evidence.get("topic_text") or ""), " ".join(evidence.get("link_titles") or [])]))
    public_site = _contains_any(blob, config.get("public_site_keywords", [])) or bool(evidence.get("resource_url"))
    direct_activity = _has_direct_public_site_activity(blob) or bool(evidence.get("resource_url") and evidence.get("access_method"))
    strong_limits = _contains_any(blob, ["不限额度", "不限并发", "不限速", "火力全开", "兑换码"])
    top_model_core = bool(model_profile.get("top_model_is_core_resource") or (verdict == "frontline" and model_profile.get("top_model_resource_families")))
    if top_model_core and public_site and (direct_activity or strong_limits):
        categories.append("frontline_public_resource")
        return RouteDecision("push", "一线模型公益资源富化后确认", _unique_strings(categories))
    if "priority_author" in categories or public_site or decision.route == "candidate":
        return RouteDecision("candidate", "富化后核验确定性推送条件", _unique_strings(categories))
    return decision


def enrich_post_for_routing(
    post: LinuxDoPost,
    decision: RouteDecision,
    config: dict,
    state: dict,
    *,
    deadline: float | None = None,
) -> tuple[RouteDecision, dict]:
    if decision.route == "silent":
        evidence = _empty_evidence()
        if set(decision.matched_categories) & DECISION_LOG_HIGH_SIGNAL_SILENT_CATEGORIES:
            evidence = build_evidence_from_text(_post_blob(post), config)
            finalize_model_coverage(evidence, config)
            finalize_welfare_resource_profile(post, evidence, config)
            for category in decision.matched_categories:
                if category in DECISION_LOG_HIGH_SIGNAL_SILENT_CATEGORIES:
                    _append_low_value_context(evidence, category)
            finalize_post_value_profile(post, evidence, config)
        return decision, evidence
    base_text = _compact_text(" ".join([post.title, post.summary]))
    base_evidence = build_evidence_from_text(base_text, config)
    topic_evidence = fetch_topic_evidence(post, config, deadline=deadline)
    image_urls = _unique_strings(list(topic_evidence.get("image_urls") or []) + list(post.photo_urls or []), limit=8)
    image_evidence = {"image_text_status": "text_only"} if image_urls else {}
    evidence = _merge_evidence(base_evidence, topic_evidence, image_evidence, {"image_urls": image_urls})
    finalize_model_coverage(evidence, config)
    finalize_welfare_resource_profile(post, evidence, config)
    return apply_enriched_gate(post, decision, evidence, config), evidence


def evaluate_post(post: LinuxDoPost, *, config: dict | None = None) -> RouteDecision:
    config = config or default_config()
    blob = _post_blob(post)
    categories: list[str] = []

    if _is_email_account_giveaway(blob, config):
        return RouteDecision("silent", "邮箱账号赠送默认静默", ["email_account_giveaway"])

    if _is_forum_chatter_low_value(post, None, config):
        return RouteDecision("silent", "论坛等级/邀请码/截图闲聊默认静默", ["forum_chatter_low_value"])

    base_evidence = build_evidence_from_text(blob, config)
    finalize_welfare_resource_profile(post, base_evidence, config)
    finalize_post_value_profile(post, base_evidence, config)

    if _is_welfare_subscription_question_low_value(post, base_evidence, config):
        return RouteDecision(
            "silent",
            "福利羊毛订阅/价格/地区提问默认静默",
            ["low_value_welfare_intent", "welfare_question_low_value", "welfare_subscription_question_low_value"],
        )

    if _is_domestic_or_non_top_model_welfare(post, base_evidence, config):
        categories = ["domestic_or_non_top_model_welfare"]
        model_coverage = base_evidence.get("model_coverage") if isinstance(base_evidence.get("model_coverage"), dict) else {}
        if str(base_evidence.get("frontline_model_verdict") or "") == "non_frontline" or (
            model_coverage.get("present_non_frontline_families") and not model_coverage.get("present_top_families")
        ):
            categories.append("domestic_only_model" if model_coverage.get("domestic_only") else "non_frontline_model")
        return RouteDecision("silent", "国产/非顶级模型福利默认静默", _unique_strings(categories))

    if _is_non_beijing_local_welfare(post, base_evidence, config):
        return RouteDecision("silent", "非北京地区限定福利默认静默", ["non_beijing_local_welfare"])

    if _is_user_excluded_service_welfare(post, base_evidence, config):
        return RouteDecision("silent", "命中用户排除服务福利，默认静默", ["user_excluded_service_welfare"])

    if _is_domain_registration_welfare(post, base_evidence, config):
        return RouteDecision("silent", "域名注册/续费促销福利默认静默", ["domain_registration_welfare"])

    if _is_ambiguous_consumer_welfare_low_value(post, base_evidence, config):
        return RouteDecision("silent", "缺少明确价值的生活消费福利水帖默认静默", ["ambiguous_consumer_welfare_low_value"])

    if _is_infrastructure_hosting_welfare(post, base_evidence, config):
        return RouteDecision("silent", "主机/流量类福利默认静默", ["infrastructure_hosting_welfare"])

    if _is_media_subscription_welfare(post, base_evidence, config):
        return RouteDecision("silent", "音频/媒体会员福利默认静默", ["media_subscription_welfare"])

    if _is_private_tracker_welfare(post, base_evidence, config):
        return RouteDecision("silent", "PT/私有站福利默认静默", ["private_tracker_welfare"])

    if _is_forum_currency_chatter(post, base_evidence, config):
        return RouteDecision("silent", "论坛货币/等级闲聊默认静默", ["forum_currency_chatter"])

    low_value_welfare, low_value_category = _is_low_value_welfare_intent(post, base_evidence, config)
    if low_value_welfare:
        return RouteDecision(
            "silent",
            "福利羊毛提问/负面/截图水帖默认静默",
            _unique_strings(["low_value_welfare_intent", low_value_category]),
        )

    if _is_welfare_question_low_value(post, base_evidence, config):
        return RouteDecision("silent", "福利羊毛提问/咨询帖默认静默", ["low_value_welfare_intent", "welfare_question_low_value"])

    if _is_question_discussion_low_value(post, base_evidence, config):
        return RouteDecision("silent", "普通提问/讨论帖默认静默", ["question_discussion_low_value"])

    if _is_design_resource_low_value(post, None, config):
        return RouteDecision("silent", "设计/创作工具福利资源默认静默", ["design_resource_low_value"])

    if _is_game_resource_low_value(post, None, config):
        return RouteDecision("silent", "游戏/娱乐福利资源默认静默", ["game_resource_low_value"])

    if _is_public_site_shutdown_low_value(post, base_evidence, config):
        _append_low_value_context(base_evidence, "public_site_shutdown_low_value")
        finalize_post_value_profile(post, base_evidence, config)
        return RouteDecision("silent", "公益站关闭/停运且无可执行替代资源默认静默", ["public_site_shutdown_low_value"])

    if _is_public_site_chatter_low_value(post, None, config):
        return RouteDecision("silent", "公益站/中转站普通可用性闲聊默认静默", ["public_site_chatter_low_value"])

    if _is_research_academic_low_value(post, None, config):
        return RouteDecision("silent", "科研/论文/学术资料类内容默认静默", ["research_academic_low_value"])

    if _is_support_troubleshooting_help(post, None, config):
        return RouteDecision("silent", "普通求助/配置排错默认静默", ["support_troubleshooting_help"])

    public_site = _contains_any(blob, config.get("public_site_keywords", []))
    status_signal = _contains_any(blob, config.get("status_keywords", []))
    tool_signal = _contains_any(blob, config.get("tool_keywords", []))
    question_signal = _has_question_signal(blob, config)
    direct_activity = public_site and _has_direct_public_site_activity(blob)

    if post.source_role == "supplement" and post.is_reply and not _is_priority_author(post, config):
        refresh_signal = _has_reply_refresh_signal(blob, config)
        if not refresh_signal and not (public_site and status_signal):
            return RouteDecision("silent", "补充源旧帖回复默认静默", ["supplement_reply"])
        categories.append("supplement_refresh_reply" if refresh_signal else "supplement_status_reply")

    if _is_welfare_new_topic(post, config):
        categories.append("welfare_full_push")
        return RouteDecision("push", "#福利羊毛新主题默认推送", categories)

    if post.source_role == "supplement" and post.is_reply and _is_welfare_topic(post) and _has_reply_refresh_signal(blob, config):
        categories.extend(["welfare_refresh_reply"])
        return RouteDecision("push", "#福利羊毛旧帖回复出现补货/额度/状态刷新", _unique_strings(categories))

    if _is_priority_author(post, config):
        categories.append("priority_author")
        if public_site and (direct_activity or status_signal):
            categories.append("priority_author_high_signal")
            return RouteDecision("candidate", "重点用户发布高信号内容，需富化确认", categories)
        return RouteDecision("candidate", "重点用户发布新内容", categories)

    if direct_activity:
        categories.append("public_site_activity")
        return RouteDecision("candidate", "公益站/中转站活动需富化确认", categories)

    if public_site and status_signal:
        categories.extend(["public_site_status", "public_site"])
        return RouteDecision("candidate", "公益站/中转站状态变化需要模型判断", categories)

    if question_signal or _is_generic_question(post, config, blob):
        return RouteDecision("silent", "普通求助默认静默", ["generic_help_question"])

    if public_site:
        categories.append("public_site")
        return RouteDecision("candidate", "公益站/中转站相关内容需要模型判断", categories)

    if tool_signal and _has_tool_action_signal(blob):
        categories.append("tool_or_ai_api")
        return RouteDecision("candidate", "AI/API/工具内容需要模型判断", categories)

    return RouteDecision("silent", "未命中 Linux.do 高价值边界", ["low_signal"])


def post_payload(post: LinuxDoPost, decision: RouteDecision | None = None, evidence: dict | None = None) -> dict:
    decision = decision or evaluate_post(post)
    return sanitize_for_prompt(
        {
            "channel": post.channel,
            "message_id": post.message_id,
            "telegram_url": post.telegram_url,
            "posted_at": post.posted_at,
            "topic_id": post.topic_id,
            "topic_url": post.topic_url,
            "topic_post_no": post.topic_post_no,
            "is_reply": post.is_reply,
            "title": post.title,
            "author": post.author,
            "section": post.section,
            "topic_created_at": post.topic_created_at,
            "summary": post.summary,
            "source_role": post.source_role,
            "photo_count": len(post.photo_urls or []),
            "evidence_facts": _compact_evidence_for_prompt(evidence),
            "routing_context": {
                "route": decision.route,
                "reason": decision.reason,
                "matched_categories": decision.matched_categories,
            },
        }
    )


def _decision_log_categories(payload: dict) -> list[str]:
    routing = payload.get("routing_context") if isinstance(payload.get("routing_context"), dict) else {}
    categories = routing.get("matched_categories") if isinstance(routing.get("matched_categories"), list) else []
    evidence = payload.get("evidence_facts") if isinstance(payload.get("evidence_facts"), dict) else {}
    low_value_context = evidence.get("low_value_context") if isinstance(evidence.get("low_value_context"), list) else []
    return _unique_strings([str(item) for item in list(categories) + list(low_value_context)], limit=16)


def _compact_decision_log_text(text: Any, max_chars: int = DECISION_LOG_TEXT_MAX_CHARS) -> str:
    compacted = _truncate_text(str(text or ""), max_chars)
    compacted = re.sub(r"data:image/[^,\s]+;base64,[A-Za-z0-9+/=]+", "[image-base64-omitted]", compacted, flags=re.I)
    compacted = re.sub(r"<[^>]+>", " ", compacted)
    compacted = _compact_text(compacted)
    return compacted


def _compact_decision_log_evidence(evidence: dict) -> dict:
    compact: dict[str, Any] = {}
    for key in (
        "resource_url",
        "reference_url",
        "access_method",
        "model_names",
        "provider",
        "limits",
        "price",
        "risk_notes",
        "why",
        "frontline_model_verdict",
        "model_coverage",
        "model_resource_profile",
        "welfare_resource_profile",
        "image_text_status",
        "topic_category",
        "topic_tags",
        "public_site_signal",
        "post_value_profile",
        "low_value_context",
    ):
        value = evidence.get(key)
        if value in ("", None, [], {}):
            continue
        if isinstance(value, str):
            compact[key] = _compact_decision_log_text(value, DECISION_LOG_EVIDENCE_MAX_CHARS)
        elif isinstance(value, list):
            compact[key] = [_compact_decision_log_text(item, 140) for item in value[:8] if str(item or "").strip()]
        elif isinstance(value, dict):
            compact[key] = sanitize_for_prompt(value)
        else:
            compact[key] = value
    return compact


def build_decision_log_entry(payload: dict, decision: str, *, delivered: bool = False) -> dict:
    routing = payload.get("routing_context") if isinstance(payload.get("routing_context"), dict) else {}
    evidence = payload.get("evidence_facts") if isinstance(payload.get("evidence_facts"), dict) else {}
    categories = _decision_log_categories(payload)
    entry = {
        "logged_at": utc_now_iso(),
        "posted_at": payload.get("posted_at", ""),
        "topic_id": str(payload.get("topic_id") or ""),
        "topic_url": payload.get("topic_url", ""),
        "topic_post_no": payload.get("topic_post_no", ""),
        "title": _compact_decision_log_text(payload.get("title", "")),
        "author": _compact_decision_log_text(payload.get("author", ""), 120),
        "section": _compact_decision_log_text(payload.get("section", ""), 120),
        "channel": payload.get("channel", ""),
        "message_id": payload.get("message_id", ""),
        "source_role": payload.get("source_role", ""),
        "decision": str(decision or routing.get("route") or ""),
        "delivered": bool(delivered),
        "rule_version": RULE_VERSION,
        "reason": _compact_decision_log_text(routing.get("reason", "")),
        "matched_categories": categories,
        "routing_context": {
            "route": routing.get("route", ""),
            "reason": _compact_decision_log_text(routing.get("reason", "")),
            "matched_categories": categories,
        },
        "evidence_facts": _compact_decision_log_evidence(evidence),
    }
    return sanitize_for_prompt(entry)


def append_decision_log_entries(
    audit: list[dict],
    *,
    log_path: Path | None = None,
    delivered_topic_ids: set[str] | None = None,
) -> int:
    log_path = log_path or DECISION_LOG_FILE
    delivered_topic_ids = delivered_topic_ids or set()
    lines: list[str] = []
    for item in audit:
        if not isinstance(item, dict):
            continue
        payload = item.get("post")
        if not isinstance(payload, dict):
            continue
        decision = str(item.get("decision") or ((payload.get("routing_context") or {}).get("route") if isinstance(payload.get("routing_context"), dict) else "") or "")
        if set(_decision_log_categories(payload)) & DECISION_LOG_SKIP_CATEGORIES:
            continue
        delivered = str(payload.get("topic_id") or "") in delivered_topic_ids
        entry = build_decision_log_entry(payload, decision, delivered=delivered)
        lines.append(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    if not lines:
        return 0
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")
    return len(lines)


def format_push_message(post: LinuxDoPost, categories: list[str], evidence: dict | None = None) -> str:
    evidence = evidence or {}
    category_note = "、".join(categories[:3]) if categories else "high_signal"
    section = f" {post.section}" if post.section else ""
    author = f" / {post.author}" if post.author else ""
    lines = [f"[{post.title}]({post.topic_url}) — Linux.do{section}{author}；{category_note}"]
    details: list[str] = []
    if evidence.get("why"):
        details.append(f"价值：{_truncate_text(str(evidence.get('why')), 120)}")
    models = evidence.get("model_names") if isinstance(evidence.get("model_names"), list) else []
    if models:
        details.append(f"模型：{'、'.join(str(model) for model in models[:4])}")
    if evidence.get("limits"):
        details.append(f"限制：{_truncate_text(str(evidence.get('limits')), 120)}")
    if evidence.get("resource_url"):
        details.append(f"资源：{evidence.get('resource_url')}")
    if evidence.get("risk_notes"):
        details.append(f"注意：{_truncate_text(str(evidence.get('risk_notes')), 120)}")
    lines.extend(details[:4])
    return "\n".join(lines)


def load_state() -> dict:
    data = state_json.load(STATE_FILE, default={}, strict=True)
    if not isinstance(data, dict):
        raise ValueError(f"invalid monitor state: {STATE_FILE}")
    return data


def save_state(state: dict) -> None:
    state_json.save_atomic(STATE_FILE, state)


def _parse_state_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _delivered_topics(state: dict) -> dict:
    delivered = state.get("delivered_topics")
    if not isinstance(delivered, dict):
        delivered = {}
        state["delivered_topics"] = delivered
    return delivered


def _prune_delivered_topics(state: dict, config: dict) -> None:
    delivered = _delivered_topics(state)
    ttl_hours = int(config.get("delivered_topic_ttl_hours") or 168)
    if ttl_hours <= 0:
        return
    now = datetime.now(timezone.utc)
    cutoff_seconds = ttl_hours * 3600
    for topic_id, entry in list(delivered.items()):
        if not isinstance(entry, dict):
            delivered.pop(topic_id, None)
            continue
        delivered_at = _parse_state_datetime(entry.get("delivered_at"))
        if delivered_at is None or (now - delivered_at).total_seconds() > cutoff_seconds:
            delivered.pop(topic_id, None)


def _is_topic_delivered(state: dict, topic_id: str, config: dict) -> bool:
    topic = str(topic_id or "").strip()
    if not topic:
        return False
    _prune_delivered_topics(state, config)
    return topic in _delivered_topics(state)


def _mark_topic_delivered(state: dict, topic_id: str, *, source: str = "", message_id: int | str = "") -> None:
    topic = str(topic_id or "").strip()
    if not topic:
        return
    entry: dict[str, Any] = {"delivered_at": utc_now_iso()}
    if source:
        entry["source"] = source
    if message_id not in ("", None):
        entry["message_id"] = message_id
    _delivered_topics(state)[topic] = entry


def _topic_id_from_message(message: str) -> str:
    match = LINUXDO_TOPIC_RE.search(str(message or ""))
    return match.group(1) if match else ""


def _delivery_queue(state: dict) -> list[dict]:
    queue = state.get(DELIVERY_QUEUE_STATE_KEY)
    if not isinstance(queue, list):
        queue = []
        state[DELIVERY_QUEUE_STATE_KEY] = queue
    cleaned: list[dict] = []
    for entry in queue:
        if isinstance(entry, dict) and str(entry.get("message") or "").strip():
            cleaned.append(entry)
    if len(cleaned) != len(queue):
        state[DELIVERY_QUEUE_STATE_KEY] = cleaned
    return cleaned


def _queued_topic_ids(state: dict) -> set[str]:
    return {str(entry.get("topic_id") or "").strip() for entry in _delivery_queue(state) if str(entry.get("topic_id") or "").strip()}


def _allow_repeat_delivery(post: LinuxDoPost, evidence: dict | None, config: dict) -> bool:
    evidence = evidence if isinstance(evidence, dict) else {}
    blob = _compact_text(" ".join([_post_blob(post), str(evidence.get("topic_text") or ""), str(evidence.get("limits") or "")]))
    return bool(post.is_reply and _has_reply_refresh_signal(blob, config))


def _delivery_suppression_decision(post: LinuxDoPost, decision: RouteDecision, evidence: dict | None, state: dict, config: dict) -> RouteDecision | None:
    if decision.route == "silent" or not post.topic_id:
        return None
    if _allow_repeat_delivery(post, evidence, config):
        return None
    categories = list(decision.matched_categories)
    if _is_topic_delivered(state, post.topic_id, config):
        categories.append("delivered_topic")
        return RouteDecision("silent", "同一 topic 已投递过，跨频道/跨轮去重", _unique_strings(categories))
    if post.topic_id in _queued_topic_ids(state):
        categories.append("queued_topic")
        return RouteDecision("silent", "同一 topic 已在待投递队列中", _unique_strings(categories))
    return None


def _enqueue_push_messages(state: dict, messages: list[str], config: dict) -> None:
    queue = _delivery_queue(state)
    queued_topics = _queued_topic_ids(state)
    for message in messages:
        text = str(message or "").strip()
        if not text:
            continue
        topic_id = _topic_id_from_message(text)
        if topic_id and (_is_topic_delivered(state, topic_id, config) or topic_id in queued_topics):
            continue
        queue.append({"message": text, "topic_id": topic_id, "created_at": utc_now_iso()})
        if topic_id:
            queued_topics.add(topic_id)
    state[DELIVERY_QUEUE_STATE_KEY] = queue


def _pop_next_queued_delivery(state: dict, config: dict) -> str:
    queue = _delivery_queue(state)
    while queue:
        entry = queue.pop(0)
        message = str(entry.get("message") or "").strip()
        topic_id = str(entry.get("topic_id") or "").strip() or _topic_id_from_message(message)
        if not message:
            continue
        if topic_id and _is_topic_delivered(state, topic_id, config):
            continue
        if topic_id:
            _mark_topic_delivered(state, topic_id, source="queue")
        state[DELIVERY_QUEUE_STATE_KEY] = queue
        return message
    state[DELIVERY_QUEUE_STATE_KEY] = queue
    return ""


def collect_channel_posts(
    channel: str,
    source_role: str,
    *,
    last_seen_id: int | None,
    config: dict,
    deadline: float | None = None,
) -> tuple[list[LinuxDoPost], dict]:
    seen: dict[int, LinuxDoPost] = {}
    before: int | None = None
    pages_fetched = 0
    hit_last_seen = False
    fetch_error = ""
    max_pages = int(config.get("max_pages") or 6)
    timeout = int(config.get("fetch_timeout_seconds") or 8)
    attempts = int(config.get("fetch_attempts") or 3)

    for _ in range(max_pages):
        if deadline_remaining(deadline) < timeout + 1:
            break
        url = f"https://t.me/s/{channel}" if before is None else f"https://t.me/s/{channel}?before={before}"
        try:
            html_text = fetch(url, timeout=timeout, attempts=attempts, phase=f"telegram_{channel}")
        except Exception as exc:
            fetch_error = f"{type(exc).__name__}: {exc}"
            print(f"linuxdo_collect_page_failed channel={channel} page={pages_fetched + 1} error={fetch_error}", file=sys.stderr)
            break
        pages_fetched += 1
        posts = parse_channel_posts(html_text, channel, source_role)
        if not posts:
            break
        for post in posts:
            seen[post.message_id] = post
        min_id = min(post.message_id for post in posts)
        if last_seen_id is not None and any(post.message_id <= last_seen_id for post in posts):
            hit_last_seen = True
            break
        if before is not None and min_id >= before:
            break
        before = min_id

    ordered = sorted(seen.values(), key=lambda post: post.message_id, reverse=True)
    return ordered, {"pages_fetched": pages_fetched, "hit_last_seen": hit_last_seen, "fetch_error": fetch_error}


def collect_recent_posts(state: dict, config: dict, *, deadline: float | None = None) -> tuple[list[LinuxDoPost], dict]:
    primary = str(config.get("primary_channel") or PRIMARY_CHANNEL)
    supplement = str(config.get("supplement_channel") or SUPPLEMENT_CHANNEL)
    channel_state = state.get("channels", {})
    if not isinstance(channel_state, dict):
        channel_state = {}

    posts: list[LinuxDoPost] = []
    meta: dict[str, dict] = {}
    primary_posts, primary_meta = collect_channel_posts(
        primary,
        "primary",
        last_seen_id=channel_state.get(primary, {}).get("last_seen_id") if isinstance(channel_state.get(primary), dict) else None,
        config=config,
        deadline=deadline,
    )
    posts.extend(primary_posts)
    meta[primary] = primary_meta

    if config.get("supplement_enabled", True):
        supplement_posts, supplement_meta = collect_channel_posts(
            supplement,
            "supplement",
            last_seen_id=channel_state.get(supplement, {}).get("last_seen_id") if isinstance(channel_state.get(supplement), dict) else None,
            config=config,
            deadline=deadline,
        )
        posts.extend(supplement_posts)
        meta[supplement] = supplement_meta

    return dedupe_posts(posts), meta


def _channel_new_posts(posts: list[LinuxDoPost], state: dict, config: dict) -> list[LinuxDoPost]:
    channel_state = state.get("channels", {})
    if not isinstance(channel_state, dict):
        channel_state = {}
    is_bootstrap = not channel_state
    if is_bootstrap:
        return posts[: int(config.get("bootstrap_limit") or 60)]
    result: list[LinuxDoPost] = []
    for post in posts:
        last_seen = None
        current = channel_state.get(post.channel)
        if isinstance(current, dict):
            last_seen = current.get("last_seen_id")
        try:
            if last_seen is None or post.message_id > int(last_seen):
                result.append(post)
        except Exception:
            result.append(post)
    return result


def _update_seen_state(state: dict, posts: list[LinuxDoPost], meta: dict) -> None:
    channels = state.get("channels")
    if not isinstance(channels, dict):
        channels = {}
    by_channel: dict[str, list[LinuxDoPost]] = {}
    for post in posts:
        by_channel.setdefault(post.channel, []).append(post)
    for channel, channel_posts in by_channel.items():
        current = channels.get(channel)
        if not isinstance(current, dict):
            current = {}
        current["last_seen_id"] = max(post.message_id for post in channel_posts)
        current["last_seen_at"] = utc_now_iso()
        current["last_poll_meta"] = meta.get(channel, {})
        channels[channel] = current
    state["channels"] = channels


def finalize_rule_decision(post: LinuxDoPost, decision: RouteDecision, evidence: dict, config: dict) -> RouteDecision:
    if decision.route != "candidate":
        return decision
    profile = finalize_post_value_profile(post, evidence, config)
    categories = list(decision.matched_categories)
    coverage = evidence.get("model_coverage") or {}
    resource = evidence.get("model_resource_profile") or {}
    has_top_resource = bool(resource.get("top_model_is_core_resource") or resource.get("top_model_resource_families"))
    actionable = profile.get("actionability") in {"direct_use", "positive_change", "replacement_available"}
    if (actionable and has_top_resource and not coverage.get("domestic_only")
            and not profile.get("hard_silent_reason") and not profile.get("loss_only")):
        categories.append("confirmed_actionable_resource")
        evidence["why"] = "文本明确列出可用模型资源及领取、访问或变更信息"
        return RouteDecision("push", evidence["why"], _unique_strings(categories))
    categories.append("insufficient_actionable_evidence")
    return RouteDecision("silent", "未满足确定性推送条件，证据不足静默", _unique_strings(categories))


def _candidate_items(
    posts: list[LinuxDoPost],
    config: dict,
    state: dict,
    *,
    deadline: float | None = None,
    dry_run: bool = False,
) -> tuple[list[str], list[dict]]:
    push_messages: list[str] = []
    audit: list[dict] = []
    for post in posts:
        decision = evaluate_post(post, config=config)
        suppressed = _delivery_suppression_decision(post, decision, None, state, config)
        if suppressed is not None:
            audit.append({"post": post_payload(post, suppressed, _empty_evidence()), "decision": suppressed.route})
            continue
        decision, evidence = enrich_post_for_routing(post, decision, config, state, deadline=deadline)
        decision = finalize_rule_decision(post, decision, evidence, config)
        suppressed = _delivery_suppression_decision(post, decision, evidence, state, config)
        if suppressed is not None:
            audit.append({"post": post_payload(post, suppressed, evidence), "decision": suppressed.route})
            continue
        audit.append({"post": post_payload(post, decision, evidence), "decision": decision.route})
        if decision.route == "push":
            push_messages.append(format_push_message(post, decision.matched_categories, evidence))
    return push_messages, audit


def run_once(*, dry_run: bool = False) -> str:
    config = load_config()
    deadline = deadline_after(int(config.get("run_budget_seconds") or RUN_BUDGET_SECONDS))
    state = load_state()
    if not dry_run:
        queued_message = _pop_next_queued_delivery(state, config)
        if queued_message:
            save_state(state)
            return queued_message
    posts, meta = collect_recent_posts(state, config, deadline=deadline)
    candidates = _channel_new_posts(posts, state, config) if posts else []
    push_messages, audit = _candidate_items(candidates, config, state, deadline=deadline, dry_run=dry_run)
    if dry_run:
        return output.json_dumps_pretty({
            "wakeAgent": False, "dry_run": True, "rule_version": RULE_VERSION,
            "collected": len(posts), "candidates": len(candidates),
            "push_messages": push_messages, "audit": audit[:80], "meta": meta,
        }, sort_keys=True)
    _update_seen_state(state, posts, meta)
    state["rule_version"] = RULE_VERSION
    _enqueue_push_messages(state, push_messages, config)
    message = _pop_next_queued_delivery(state, config)
    topic_id = _topic_id_from_message(message)
    append_decision_log_entries(audit, delivered_topic_ids={topic_id} if topic_id else set())
    save_state(state)
    return message or output.wake_agent_false()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Monitor Linux.do Telegram public channels.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and classify without writing state.")
    args = parser.parse_args(argv)
    with locks.nonblocking_file_lock(paths.CRON_STATE_DIR / "linuxdo_notice.lock") as lock:
        print(run_once(dry_run=args.dry_run) if lock.acquired else output.wake_agent_false())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
