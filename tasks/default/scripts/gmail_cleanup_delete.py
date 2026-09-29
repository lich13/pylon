#!/srv/hermes/venvs/google-workspace/bin/python
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
from email.utils import parseaddr
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

HERMES_HOME = Path(os.getenv("HERMES_HOME", "/srv/hermes"))
TOKEN_PATH = HERMES_HOME / "google_token.json"
DATA_DIR = HERMES_HOME / "data" / "gmail_cleanup"
EXTRA_RULES_PATH = DATA_DIR / "extra_rules.json"
NEW_TYPE_STATE_PATH = DATA_DIR / "new_type_candidates.json"
# Keep this aligned with gmail_cleanup_oauth.py and the stored token.
# Using narrower Gmail scopes here can make refresh fail with invalid_scope
# when the token was originally granted for https://mail.google.com/.
SCOPES = ["https://mail.google.com/"]
PROTECTION = "-is:starred"


def gmail_quote(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def query_or(*terms: str) -> str:
    cleaned = []
    seen = set()
    for term in terms:
        term = str(term or "").strip()
        if not term or term in seen:
            continue
        seen.add(term)
        cleaned.append(term)
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    return "(" + " OR ".join(cleaned) + ")"


def subject_phrase(value: str) -> str:
    return f'subject:"{gmail_quote(value)}"'


def body_phrase(value: str) -> str:
    return f'"{gmail_quote(value)}"'


def from_sender(value: str) -> str:
    return f"from:{str(value or '').strip().lower()}"


def subject_terms(*terms: str) -> list[str]:
    return [subject_phrase(term) for term in terms]


def body_terms(*terms: str) -> list[str]:
    return [body_phrase(term) for term in terms]


def sender_terms(*senders: str) -> list[str]:
    return [from_sender(sender) for sender in senders]


def exclude_terms(*terms: str) -> str:
    return " ".join(f'-{term}' for term in terms if term)


def cleanup_rule(name: str, description: str, *clauses: str, exclude: str = "") -> dict[str, str]:
    body = " ".join(clause for clause in clauses if clause)
    query = f"{PROTECTION} {body}".strip()
    if exclude:
        query = f"{query} {exclude}".strip()
    return {"name": name, "query": query, "description": description}


def build_builtin_rules() -> list[dict[str, str]]:
    verification_security_exclusions = exclude_terms(
        subject_phrase("安全提醒"),
        subject_phrase("两步验证"),
        subject_phrase("security alert"),
        body_phrase("new app(s) connected"),
        body_phrase("security activity"),
        body_phrase("安全性活动"),
    )

    low_value_login_exclusions = exclude_terms(
        subject_phrase("suspicious sign-in"),
        subject_phrase("suspicious login"),
        subject_phrase("unusual activity"),
        subject_phrase("blocked sign-in"),
        subject_phrase("password changed"),
        subject_phrase("password reset"),
        subject_phrase("2FA disabled"),
        subject_phrase("two-factor disabled"),
        subject_phrase("recovery email changed"),
        subject_phrase("account locked"),
        subject_phrase("payment method changed"),
        subject_phrase("可疑登录"),
        subject_phrase("异常登录"),
        subject_phrase("密码重置"),
        subject_phrase("密码已更改"),
        subject_phrase("不審なログイン"),
        subject_phrase("不審なアクティビティ"),
        subject_phrase("パスワードのリセット"),
        subject_phrase("パスワードが変更"),
        subject_phrase("セキュリティ"),
    )

    return [
        cleanup_rule(
            "promotions_social",
            "促销 / 社交通知",
            query_or("category:promotions", "category:social"),
        ),
        cleanup_rule(
            "verification_codes",
            "验证码邮件",
            query_or(
                *subject_terms(
                    "verification code",
                    "验证码",
                    "动态码",
                    "校验码",
                    "OTP",
                    "one-time password",
                    "security code",
                    "login code",
                    "authentication code",
                    "确认码",
                    "登录代码",
                ),
                *body_terms(
                    "your verification code is",
                    "your code is",
                    "verification code",
                    "security code",
                    "one-time password",
                    "验证码是",
                    "验证码为",
                    "动态码",
                    "校验码",
                    "确认码",
                ),
            ),
            exclude=verification_security_exclusions,
        ),
        cleanup_rule(
            "account_verification",
            "账号/邮箱验证",
            query_or(
                *subject_terms(
                    "账号验证",
                    "账户验证",
                    "验证账户",
                    "验证账号",
                    "验证你的",
                    "验证您的",
                    "验证你的邮箱",
                    "验证您的邮箱",
                    "邮箱账户",
                    "邮箱验证",
                    "verify your account",
                    "verify your email",
                    "email verification",
                    "account verification",
                    "confirm your email",
                    "confirm your account",
                ),
                *body_terms(
                    "请验证账户",
                    "验证账户",
                    "验证您的账户",
                    "验证你的邮箱账户",
                    "验证您的邮箱账户",
                    "验证你的滴答清单邮箱账户",
                    "verify account",
                    "verify your account",
                    "verify your email",
                    "account verification",
                    "email verification",
                    "confirm your email",
                ),
            ),
        ),
        cleanup_rule(
            "welcome_emails",
            "迎新/注册完成邮件",
            query_or(
                *subject_terms("welcome", "welcome to", "欢迎", "欢迎注册", "欢迎使用", "注册成功"),
                *body_terms("welcome to", "welcome aboard", "thanks for signing up", "欢迎注册", "欢迎使用"),
            ),
        ),
        cleanup_rule(
            "maintenance_notices",
            "维护/服务公告",
            query_or(
                *subject_terms(
                    "维护通知",
                    "维护公告",
                    "系统维护",
                    "运营维护",
                    "maintenance",
                    "scheduled maintenance",
                    "maintenance notice",
                    "maintenance window",
                    "service maintenance",
                    "downtime notice",
                ),
                *body_terms("服务器运营维护", "scheduled maintenance", "maintenance window", "service maintenance"),
            ),
        ),
        cleanup_rule(
            "mileage_change_notifications",
            "里程/积分变动",
            query_or(
                *sender_terms("skypearl@edm.csair.com"),
                *subject_terms("里程变动", "积分变动", "points update", "mileage update"),
                *body_terms("里程变动通知", "积分变动通知", "会员积分变动", "points balance update"),
            ),
        ),
        cleanup_rule("linuxdo_notifications", "linux.do 通知", from_sender("noreply@linux.do")),
        cleanup_rule(
            "exness_notifications",
            "Exness 通知",
            query_or(*sender_terms("no-reply@exness.com", "exness.com")),
        ),
        cleanup_rule(
            "trading_notifications",
            "Trading/交易通知",
            query_or(
                *subject_terms(
                    "Trading",
                    "trading",
                    "Trade",
                    "交易",
                    "trading alert",
                    "trading opportunity",
                    "交易提醒",
                    "交易机会",
                    "交易信号",
                ),
                *body_terms("trade the world", "trading alert", "trading opportunity", "交易提醒", "交易机会", "交易信号"),
            ),
        ),
        cleanup_rule(
            "okx_deposit_receive_notifications",
            "OKX 充币/收币通知",
            query_or(*sender_terms("noreply@service3.okx.com", "noreply@mailer2.okx.com", "okx.com")),
            query_or(
                *subject_terms(
                    "充币处理中",
                    "您收到了",
                    "您收到了 USDT",
                    "已存入您的账户",
                    "deposit processing",
                    "deposit received",
                    "you received",
                ),
                *body_terms("充币处理中", "您收到了", "已存入您的账户", "deposit processing", "deposit received", "you received"),
            ),
        ),
        cleanup_rule(
            "website_notifications",
            "网站/社区通知",
            "category:updates",
            query_or(
                *subject_terms(
                    "notification",
                    "notifications",
                    "notice",
                    "提醒",
                    "通知",
                    "更新",
                    "回复",
                    "评论",
                    "mention",
                    "提及",
                ),
                *body_terms("notification", "notifications", "notice", "提醒", "通知", "更新", "回复", "评论", "mention", "提及"),
            ),
            exclude=exclude_terms(
                body_phrase("credit card statement"),
                body_phrase("credit card bill"),
                "信用卡",
                "账单",
                "对账单",
                body_phrase("verification code"),
                "otp",
                "验证码",
                "动态码",
                "校验码",
                body_phrase("security alert"),
                body_phrase("安全提醒"),
                "login",
                "登录",
                "password",
                "密码",
                body_phrase("new app"),
                body_phrase("sign-in"),
                body_phrase("account security"),
                "风险",
                "异常",
                "GitHub",
                "github.com",
            ),
        ),
        cleanup_rule(
            "low_value_service_login_notices",
            "低价值服务登录通知",
            query_or(*sender_terms("no-reply@spotify.com")),
            query_or(
                *subject_terms(
                    "new login",
                    "new sign-in",
                    "new sign in",
                    "login activity",
                    "new device login",
                    "新的登录活动",
                    "新登录通知",
                    "新登录提醒",
                    "新设备登录",
                    "新しいログイン",
                    "新規ログイン",
                    "ログインがありました",
                    "アカウントに新しいログイン",
                ),
                *body_terms(
                    "new login",
                    "new sign-in",
                    "new sign in",
                    "login activity",
                    "new device login",
                    "新的登录活动",
                    "新登录通知",
                    "新登录提醒",
                    "新设备登录",
                    "新しいログイン",
                    "新規ログイン",
                    "ログインがありました",
                    "アカウントに新しいログイン",
                ),
            ),
            exclude=low_value_login_exclusions,
        ),
        cleanup_rule(
            "quota_limit_notifications",
            "额度/配额/用量提醒",
            query_or(
                *subject_terms(
                    "额度即将用尽",
                    "额度将用尽",
                    "额度要用尽",
                    "额度快用完",
                    "额度不足",
                    "额度已用尽",
                    "额度即将耗尽",
                    "配额即将用尽",
                    "配额将用尽",
                    "配额快用完",
                    "配额不足",
                    "配额已用尽",
                    "配额即将耗尽",
                    "用量即将达到",
                    "用量已达到",
                    "接近使用上限",
                    "达到使用上限",
                    "quota almost",
                    "quota exhausted",
                    "quota exceeded",
                    "usage limit",
                    "usage threshold",
                    "approaching your limit",
                    "credits are running low",
                ),
                *body_terms(
                    "quota is almost",
                    "quota almost used up",
                    "quota almost exhausted",
                    "quota exceeded",
                    "quota exhausted",
                    "usage limit",
                    "usage threshold",
                    "usage is nearing",
                    "approaching your limit",
                    "approaching the limit",
                    "credits are running low",
                    "额度即将用尽",
                    "额度将用尽",
                    "额度要用尽",
                    "额度快用完",
                    "额度不足",
                    "额度已用尽",
                    "配额即将用尽",
                    "配额将用尽",
                    "配额快用完",
                    "配额不足",
                    "配额已用尽",
                    "配额即将耗尽",
                    "用量即将达到",
                    "用量已达到",
                    "接近使用上限",
                    "达到使用上限",
                ),
            ),
        ),
        cleanup_rule(
            "payment_invoice_reminders",
            "支付/发票付款提醒",
            query_or(
                *subject_terms(
                    "Invoice Payment Reminder",
                    "First Invoice Overdue Notice",
                    "payment reminder",
                    "invoice payment reminder",
                    "invoice reminder",
                    "payment due",
                    "past due",
                    "overdue invoice",
                    "付款提醒",
                    "支付提醒",
                    "缴费提醒",
                    "账单提醒",
                    "逾期提醒",
                    "发票付款提醒",
                ),
                *body_terms(
                    "Invoice Payment Reminder",
                    "First Invoice Overdue Notice",
                    "payment reminder",
                    "invoice payment reminder",
                    "invoice reminder",
                    "payment is due",
                    "invoice is due",
                    "past due invoice",
                    "overdue invoice",
                    "付款提醒",
                    "支付提醒",
                    "缴费提醒",
                    "逾期提醒",
                ),
            ),
        ),
        cleanup_rule(
            "invoice_receipt_documents",
            "发票/收据/水单/票据",
            query_or(
                *sender_terms("hotel-noreply@notice.alitrip.com", "yun2@vip.baiwang.com", "no-reply@ytoo.li"),
                *subject_terms(
                    "电子发票",
                    "电子发票下载",
                    "发票下载",
                    "发票开具",
                    "开票通知",
                    "水单",
                    "飞猪水单开具通知",
                    "行程单",
                    "收据",
                    "receipt",
                    "invoice",
                    "tax invoice",
                    "e-invoice",
                    "invoice download",
                    "receipt available",
                    "First Invoice Overdue Notice",
                ),
                *body_terms(
                    "电子发票",
                    "发票下载",
                    "水单",
                    "开票通知",
                    "receipt",
                    "invoice",
                    "tax invoice",
                    "invoice download",
                    "receipt available",
                ),
            ),
        ),
        cleanup_rule(
            "ecommerce_order_fulfillment",
            "电商订单/发货/购买凭证",
            query_or(
                *sender_terms("tz3@email.ldxp.cn"),
                *subject_terms(
                    "您下单的商品已发货",
                    "您下单的商品已发货，点击查看详情",
                    "订单已发货",
                    "商品已发货",
                    "发货通知",
                    "购买凭证",
                    "订单凭证",
                    "order shipped",
                    "your order has shipped",
                    "shipment",
                    "shipping confirmation",
                    "order receipt",
                    "purchase receipt",
                ),
                *body_terms(
                    "您下单的商品已发货",
                    "订单已发货",
                    "商品已发货",
                    "感谢购买商品",
                    "购买凭证",
                    "实付",
                    "付款时间",
                    "order shipped",
                    "your order has shipped",
                    "shipment",
                    "shipping confirmation",
                    "order receipt",
                    "purchase receipt",
                ),
            ),
        ),
        cleanup_rule(
            "account_settings_change_notices",
            "账号设置/转发/配置变更通知",
            query_or(
                *sender_terms("noreply@email.apple.com"),
                *subject_terms(
                    "邮件现被转发至",
                    "邮箱现被转发至",
                    "邮件转发",
                    "转发至",
                    "mail is being forwarded",
                    "email is being forwarded",
                    "email forwarding",
                    "mail forwarding",
                    "forwarding enabled",
                    "account settings changed",
                    "settings changed",
                ),
                *body_terms(
                    "邮件现被转发至",
                    "要更改此设置",
                    "偏好设置",
                    "mail is being forwarded",
                    "email forwarding",
                    "mail forwarding",
                    "forwarding enabled",
                    "account settings changed",
                ),
            ),
        ),
        cleanup_rule(
            "digests_newsletters_summaries",
            "摘要/总结/简报/Newsletter",
            query_or(
                *sender_terms("noreply@nodeseek.cc", "nodeseek.cc"),
                *subject_terms(
                    "daily digest",
                    "weekly digest",
                    "daily summary",
                    "weekly summary",
                    "newsletter",
                    "digest",
                    "summary",
                    "总结",
                    "摘要",
                    "简报",
                    "网站总结",
                    "社区总结",
                    "站点总结",
                    "日报",
                    "周报",
                    "月报",
                    "NodeSeek] 总结",
                ),
                *body_terms(
                    "daily digest",
                    "weekly digest",
                    "daily summary",
                    "weekly summary",
                    "newsletter",
                    "digest",
                    "summary",
                    "总结",
                    "摘要",
                    "简报",
                    "日报",
                    "周报",
                    "月报",
                ),
            ),
        ),
        cleanup_rule(
            "financial_notifications",
            "金融/银行/证券通知",
            query_or(
                *sender_terms(
                    "ebcmarketing.cloud",
                    "tmgm.com",
                    "message.cib.com.cn",
                    "creditcard.abchina.com.cn",
                    "message.cmbchina.com",
                    "cardcenter.cebbank.com",
                    "message.njcb.com.cn",
                    "cgbchina.com.cn",
                    "bill.citiccard.com",
                    "sc.com",
                    "csc108.com",
                ),
                *subject_terms(
                    "银行",
                    "证券",
                    "电子账单",
                    "电子对账单",
                    "电子月结单",
                    "月结单",
                    "还款提醒",
                    "保证金",
                    "期权",
                    "交易提醒",
                    "账户变动",
                    "资金变动",
                    "MT4",
                    "数字货币渠道",
                    "statement",
                    "account statement",
                    "transaction alert",
                ),
            ),
        ),
        cleanup_rule("github_notifications", "GitHub 邮件", from_sender("github.com")),
        cleanup_rule(
            "credit_card_statements",
            "信用卡账单",
            query_or(
                body_phrase("credit card statement"),
                body_phrase("credit card bill"),
                "(信用卡 账单)",
                "(信用卡 对账单)",
                subject_phrase("信用卡账单"),
                subject_phrase("信用卡对账单"),
            ),
        ),
    ]


RULES = build_builtin_rules()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_extra_rules(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or EXTRA_RULES_PATH
    data = load_json(path, {"version": 1, "rules": []})
    rules = data.get("rules") if isinstance(data, dict) else []
    if not isinstance(rules, list):
        return []
    out = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name") or "").strip()
        query = str(rule.get("query") or "").strip()
        if not name or not query:
            continue
        out.append({
            "name": name,
            "query": query,
            "description": str(rule.get("description") or name).strip(),
            "source": rule.get("source") or "manual_candidate",
            "source_candidate_id": rule.get("source_candidate_id") or "",
        })
    return out


def all_rules(extra_rules_path: Path | None = None) -> list[dict[str, Any]]:
    extra_rules_path = extra_rules_path or EXTRA_RULES_PATH
    return [*RULES, *load_extra_rules(extra_rules_path)]


def sender_email(value: str) -> str:
    _, addr = parseaddr(str(value or ""))
    return addr.lower().strip() or str(value or "").lower().strip()


def subject_family(subject: str) -> str:
    text = str(subject or "")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\b[0-9a-fA-F]{8,}\b", " ", text)
    text = re.sub(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}\b", " ", text)
    text = re.sub(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b", " ", text)
    text = re.sub(r"#[0-9]+", " ", text)
    text = re.sub(r"\b\d+\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_:|[]()（）【】")
    if not text:
        text = str(subject or "").strip()
    return text[:96]


def build_candidate_from_message(message: dict[str, Any]) -> dict[str, Any]:
    sender = sender_email(str(message.get("from") or ""))
    family = subject_family(str(message.get("subject") or ""))
    signature = f"{sender}\n{family}".lower()
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:10]
    query = f"{PROTECTION} from:{sender}" if sender else PROTECTION
    if family:
        query += f' subject:"{gmail_quote(family)}"'
    return {
        "id": f"gmail-nt-{digest}",
        "from": sender,
        "subject_family": family,
        "suggested_query": query,
        "sample_count": 1,
        "sample": {
            "id": message.get("id") or "",
            "from": message.get("from") or "",
            "subject": message.get("subject") or "",
            "date": message.get("date") or "",
            "snippet": message.get("snippet") or "",
            "labels": message.get("labels") or [],
        },
    }


def load_candidate_state(path: Path | None = None) -> dict[str, Any]:
    path = path or NEW_TYPE_STATE_PATH
    data = load_json(path, {"version": 1, "candidates": {}})
    if not isinstance(data, dict):
        data = {"version": 1, "candidates": {}}
    if not isinstance(data.get("candidates"), dict):
        data["candidates"] = {}
    data.setdefault("version", 1)
    return data


def candidate_rule_tokens(candidate: dict[str, Any]) -> list[str]:
    tokens = []
    sender = str(candidate.get("from") or "").strip().lower()
    family = str(candidate.get("subject_family") or "").strip()
    if sender:
        tokens.append(from_sender(sender))
    if family:
        tokens.append(subject_phrase(family))
    return tokens


def extract_rule_subject_phrases(query: str) -> list[str]:
    phrases = re.findall(r'(?<!-)subject:"((?:\\.|[^"\\])*)"', query)
    return [phrase.replace('\\"', '"').replace("\\\\", "\\") for phrase in phrases]


def extract_rule_excluded_subject_phrases(query: str) -> list[str]:
    phrases = re.findall(r'-subject:"((?:\\.|[^"\\])*)"', query)
    return [phrase.replace('\\"', '"').replace("\\\\", "\\") for phrase in phrases]


def subject_phrase_overlaps(phrase: str, subject: str) -> bool:
    phrase_folded = str(phrase or "").casefold()
    subject_folded = str(subject or "").casefold()
    return bool(phrase_folded and subject_folded and (phrase_folded in subject_folded or subject_folded in phrase_folded))


def is_candidate_covered_by_builtin_rules(candidate: dict[str, Any]) -> bool:
    tokens = candidate_rule_tokens(candidate)
    if not tokens:
        return False
    sender = str(candidate.get("from") or "").strip().lower()
    family = str(candidate.get("subject_family") or "").strip()
    for rule in RULES:
        query = rule["query"]
        if family and any(subject_phrase_overlaps(phrase, family) for phrase in extract_rule_excluded_subject_phrases(query)):
            continue
        if all(token in query for token in tokens):
            return True
        if sender and family and from_sender(sender) in query:
            for phrase in extract_rule_subject_phrases(query):
                if subject_phrase_overlaps(phrase, family):
                    return True
    return False


def pending_candidates_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = state.get("candidates") if isinstance(state, dict) else {}
    if not isinstance(candidates, dict):
        return []
    pending = []
    for candidate in candidates.values():
        if candidate.get("status") != "pending":
            continue
        if is_candidate_covered_by_builtin_rules(candidate):
            candidate["status"] = "covered_builtin"
            candidate["covered_builtin_at_utc"] = utc_now()
            continue
        pending.append(candidate)
    pending.sort(key=lambda c: (str(c.get("last_seen") or ""), str(c.get("id") or "")), reverse=True)
    return pending


def update_candidate_state(
    state_path: Path,
    messages: list[dict[str, Any]],
    *,
    run_date: str,
    now: str | None = None,
) -> dict[str, Any]:
    now = now or utc_now()
    state = load_candidate_state(state_path)
    candidates = state["candidates"]
    grouped: dict[str, dict[str, Any]] = {}
    for message in messages:
        candidate = build_candidate_from_message(message)
        existing = grouped.get(candidate["id"])
        if existing:
            existing["sample_count"] += 1
            continue
        grouped[candidate["id"]] = candidate

    new_candidates = []
    for cid, candidate in grouped.items():
        if cid not in candidates:
            candidate.update({
                "status": "pending",
                "first_seen": run_date,
                "last_seen": run_date,
                "first_reported": run_date,
                "last_reported": run_date,
                "created_at_utc": now,
                "updated_at_utc": now,
            })
            candidates[cid] = candidate
            new_candidates.append(candidate)
            continue
        current = candidates[cid]
        current["last_seen"] = run_date
        current["updated_at_utc"] = now
        current["sample_count"] = int(current.get("sample_count") or 0) + int(candidate.get("sample_count") or 1)
        current.setdefault("sample", candidate.get("sample") or {})
        current.setdefault("suggested_query", candidate.get("suggested_query") or "")
        current.setdefault("from", candidate.get("from") or "")
        current.setdefault("subject_family", candidate.get("subject_family") or "")
        current.setdefault("status", "pending")

    pending = pending_candidates_from_state(state)
    new_candidates = [candidate for candidate in new_candidates if candidate.get("status") == "pending"]
    save_json(state_path, state)
    return {
        "state_path": str(state_path),
        "new_candidates": new_candidates,
        "pending_candidates": pending,
    }


def accept_candidate(
    candidate_id: str,
    *,
    state_path: Path | None = None,
    extra_rules_path: Path | None = None,
    created_at_utc: str | None = None,
) -> dict[str, Any]:
    created_at_utc = created_at_utc or utc_now()
    state_path = state_path or NEW_TYPE_STATE_PATH
    extra_rules_path = extra_rules_path or EXTRA_RULES_PATH
    state = load_candidate_state(state_path)
    candidate = state["candidates"].get(candidate_id)
    if not candidate:
        raise KeyError(f"Unknown Gmail cleanup candidate: {candidate_id}")
    if candidate.get("status") == "ignored":
        raise ValueError(f"Candidate {candidate_id} was ignored; unignore it before accepting.")
    data = load_json(extra_rules_path, {"version": 1, "rules": []})
    if not isinstance(data, dict):
        data = {"version": 1, "rules": []}
    if not isinstance(data.get("rules"), list):
        data["rules"] = []
    existing = [r for r in data["rules"] if r.get("source_candidate_id") == candidate_id]
    if existing:
        rule = existing[0]
    else:
        suffix = candidate_id.replace("gmail-nt-", "")
        family = candidate.get("subject_family") or candidate.get("from") or candidate_id
        rule = {
            "name": f"manual_{suffix}",
            "description": f"手动确认：{family}",
            "query": candidate.get("suggested_query") or PROTECTION,
            "source": "gmail_new_type_candidate",
            "source_candidate_id": candidate_id,
            "created_at_utc": created_at_utc,
        }
        data["rules"].append(rule)
    candidate["status"] = "accepted"
    candidate["accepted_at_utc"] = created_at_utc
    candidate["accepted_rule_name"] = rule["name"]
    save_json(extra_rules_path, data)
    save_json(state_path, state)
    return rule


def ignore_candidate(
    candidate_id: str,
    *,
    state_path: Path | None = None,
    ignored_at_utc: str | None = None,
) -> dict[str, Any]:
    ignored_at_utc = ignored_at_utc or utc_now()
    state_path = state_path or NEW_TYPE_STATE_PATH
    state = load_candidate_state(state_path)
    candidate = state["candidates"].get(candidate_id)
    if not candidate:
        raise KeyError(f"Unknown Gmail cleanup candidate: {candidate_id}")
    candidate["status"] = "ignored"
    candidate["ignored_at_utc"] = ignored_at_utc
    save_json(state_path, state)
    return candidate


def list_recent_unmatched_messages(
    service,
    *,
    matched_ids: set[str],
    window: str,
    max_scan: int,
) -> list[dict[str, Any]]:
    query = f"{PROTECTION} newer_than:{window}"
    ids = list_message_ids(service, query, max_scan)
    messages = []
    for mid in ids:
        if mid in matched_ids:
            continue
        try:
            messages.append(get_message_metadata(service, mid))
        except Exception:
            continue
    return messages


def format_candidate_line(candidate: dict[str, Any], *, include_query: bool = False) -> str:
    cid = candidate.get("id") or ""
    sender = candidate.get("from") or "unknown"
    family = candidate.get("subject_family") or "(无主题族)"
    count = candidate.get("sample_count") or 0
    line = f"- {cid}｜{sender}｜{family}｜样本 {count}"
    if include_query:
        query = candidate.get("suggested_query") or ""
        line += f"｜建议查询：`{query}`"
    return line


def format_text_report(
    *,
    dry_run: bool,
    total_unique_matches: int,
    deleted_count: int,
    per_rule: list[dict[str, Any]],
    candidate_report: dict[str, Any] | None = None,
    max_candidates: int | None = None,
) -> str:
    if dry_run:
        summary = f"Gmail 自动清理试运行完成：若执行将永久删除 {total_unique_matches} 封。"
    else:
        summary = f"Gmail 自动清理已执行：已永久删除 {deleted_count} 封（匹配 {total_unique_matches} 封）。"
    lines = [summary, "保护规则：仅保留星标邮件。"]
    if candidate_report:
        new_candidates = list(candidate_report.get("new_candidates") or [])
        pending_candidates = list(candidate_report.get("pending_candidates") or [])
        if new_candidates:
            lines.extend(["", f"新类型邮件待判断：新增 {len(new_candidates)} 个。"])
            if max_candidates is None or max_candidates < 0:
                shown_candidates = new_candidates
            else:
                shown_candidates = new_candidates[:max_candidates]
            for candidate in shown_candidates:
                lines.append(format_candidate_line(candidate))
            if max_candidates is not None and max_candidates >= 0 and len(new_candidates) > max_candidates:
                lines.append(f"- 另有 {len(new_candidates) - max_candidates} 个新增候选未展开。")
            new_ids = {candidate.get("id") for candidate in new_candidates}
            older_pending = [candidate for candidate in pending_candidates if candidate.get("id") not in new_ids]
            if older_pending:
                lines.append(f"此前仍待确认：{len(older_pending)} 个。")
        elif pending_candidates:
            lines.extend(["", f"新类型邮件待判断：无新增，累计 {len(pending_candidates)} 个。"])
    return "\n".join(lines)


def build_new_type_candidate_report(
    service,
    *,
    matched_ids: set[str],
    state_path: Path,
    window: str,
    max_scan: int,
    run_date: str,
) -> dict[str, Any]:
    messages = list_recent_unmatched_messages(
        service,
        matched_ids=matched_ids,
        window=window,
        max_scan=max_scan,
    )
    return update_candidate_state(state_path, messages, run_date=run_date)


def load_credentials() -> Credentials:
    if not TOKEN_PATH.exists():
        raise FileNotFoundError(f"Google token not found: {TOKEN_PATH}")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    if not creds.valid:
        raise RuntimeError("Google token is invalid; re-run OAuth setup.")
    return creds


def build_gmail_service():
    creds = load_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_message_ids(service, query: str, max_messages: int | None = None) -> list[str]:
    ids: list[str] = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(
                userId="me",
                q=query,
                includeSpamTrash=False,
                maxResults=min(500, max_messages - len(ids)) if max_messages else 500,
                pageToken=page_token,
            )
            .execute()
        )
        for item in resp.get("messages", []):
            ids.append(item["id"])
            if max_messages and len(ids) >= max_messages:
                return ids
        page_token = resp.get("nextPageToken")
        if not page_token:
            return ids


def get_message_metadata(service, message_id: str) -> dict[str, Any]:
    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )
    header_map = {h.get("name", ""): h.get("value", "") for h in msg.get("payload", {}).get("headers", [])}
    return {
        "id": message_id,
        "from": header_map.get("From", ""),
        "subject": header_map.get("Subject", ""),
        "date": header_map.get("Date", ""),
        "snippet": msg.get("snippet", ""),
        "labels": msg.get("labelIds", []),
    }


def batch_delete(service, message_ids: list[str]) -> int:
    deleted = 0
    for i in range(0, len(message_ids), 1000):
        batch = message_ids[i : i + 1000]
        service.users().messages().batchDelete(userId="me", body={"ids": batch}).execute()
        deleted += len(batch)
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Permanently delete selected Gmail categories.")
    parser.add_argument("--dry-run", action="store_true", help="Only count matches, do not delete.")
    parser.add_argument("--sample-size", type=int, default=3, help="How many sample messages to fetch per rule.")
    parser.add_argument("--max-per-rule", type=int, default=None, help="Optional cap on messages scanned per rule.")
    parser.add_argument("--compact", action="store_true", help="Omit queries/samples from JSON output to reduce cron token usage.")
    parser.add_argument("--format", choices=("json", "text"), default="json", help="Output machine JSON or concise Chinese text.")
    parser.add_argument("--no-new-type-report", action="store_true", help="Disable recent outside-rule candidate detection.")
    parser.add_argument("--new-type-window", default="14d", help="Gmail newer_than window for outside-rule candidates, e.g. 14d.")
    parser.add_argument("--new-type-max-scan", type=int, default=200, help="Max recent non-starred messages to scan for new type candidates.")
    parser.add_argument("--new-type-max-report", type=int, default=-1, help="Max new candidates to show in text output; negative means show all.")
    parser.add_argument("--quiet-noop", action="store_true", help="With text output, print nothing when a successful real run has no deletions and no new candidates.")
    parser.add_argument("--list-candidates", action="store_true", help="List pending Gmail cleanup candidates and exit.")
    parser.add_argument("--accept-candidate", help="Accept a candidate ID and add its suggested query as an extra cleanup rule.")
    parser.add_argument("--ignore-candidate", help="Ignore a candidate ID and stop reporting it.")
    args = parser.parse_args(argv)

    json_indent = None if args.compact else 2

    if args.list_candidates:
        state = load_candidate_state(NEW_TYPE_STATE_PATH)
        pending = pending_candidates_from_state(state)
        save_json(NEW_TYPE_STATE_PATH, state)
        if args.format == "text":
            if not pending:
                print("Gmail 新类型邮件候选：暂无待确认。")
            else:
                print("\n".join(["Gmail 新类型邮件候选：", *[format_candidate_line(c) for c in pending]]))
        else:
            print(json.dumps({"status": "ok", "pending_candidates": pending}, ensure_ascii=False, indent=json_indent))
        return 0

    if args.accept_candidate:
        try:
            rule = accept_candidate(args.accept_candidate)
        except Exception as exc:
            print(f"Gmail 新类型候选接受失败：{exc}" if args.format == "text" else json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=json_indent))
            return 1
        if args.format == "text":
            print(f"已加入 Gmail 自动清理规则：{rule['description']}；查询：{rule['query']}")
        else:
            print(json.dumps({"status": "accepted", "rule": rule}, ensure_ascii=False, indent=json_indent))
        return 0

    if args.ignore_candidate:
        try:
            candidate = ignore_candidate(args.ignore_candidate)
        except Exception as exc:
            print(f"Gmail 新类型候选忽略失败：{exc}" if args.format == "text" else json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=json_indent))
            return 1
        if args.format == "text":
            print(f"已忽略 Gmail 新类型候选：{candidate.get('id')}")
        else:
            print(json.dumps({"status": "ignored", "candidate": candidate}, ensure_ascii=False, indent=json_indent))
        return 0

    try:
        service = build_gmail_service()
    except Exception as exc:
        if args.format == "text":
            print(f"Gmail 自动清理失败：{exc}。如果是 token 缺失/失效，请重新 OAuth 授权。token_path={TOKEN_PATH}")
        else:
            print(json.dumps({"status": "error", "error": str(exc), "token_path": str(TOKEN_PATH)}, ensure_ascii=False, indent=json_indent))
        return 1

    per_rule = []
    ordered_unique_ids: list[str] = []
    seen_ids: set[str] = set()
    rules = all_rules()

    for rule in rules:
        ids = list_message_ids(service, rule["query"], args.max_per_rule)
        new_ids = [mid for mid in ids if mid not in seen_ids]
        for mid in new_ids:
            seen_ids.add(mid)
            ordered_unique_ids.append(mid)
        samples = []
        if args.format == "json" and not args.compact and args.sample_size > 0:
            for mid in ids[: args.sample_size]:
                try:
                    samples.append(get_message_metadata(service, mid))
                except Exception as exc:
                    samples.append({"id": mid, "error": str(exc)})
        rule_result = {
            "name": rule["name"],
            "description": rule["description"],
            "matched_count": len(ids),
            "new_unique_count": len(new_ids),
        }
        if args.format == "json" and not args.compact:
            rule_result["query"] = rule["query"]
            rule_result["samples"] = samples
        per_rule.append(rule_result)

    candidate_report = None
    if not args.no_new_type_report:
        run_date = dt.datetime.now().date().isoformat()
        candidate_report = build_new_type_candidate_report(
            service,
            matched_ids=seen_ids,
            state_path=NEW_TYPE_STATE_PATH,
            window=args.new_type_window,
            max_scan=max(0, args.new_type_max_scan),
            run_date=run_date,
        )

    deleted_count = 0
    if not args.dry_run and ordered_unique_ids:
        deleted_count = batch_delete(service, ordered_unique_ids)

    if args.format == "text":
        new_candidates = list((candidate_report or {}).get("new_candidates") or [])
        if args.quiet_noop and not args.dry_run and not ordered_unique_ids and deleted_count == 0 and not new_candidates:
            return 0
        print(format_text_report(
            dry_run=args.dry_run,
            total_unique_matches=len(ordered_unique_ids),
            deleted_count=deleted_count,
            per_rule=per_rule,
            candidate_report=candidate_report,
            max_candidates=args.new_type_max_report,
        ))
        return 0

    result = {
        "status": "dry_run" if args.dry_run else "deleted",
        "token_path": str(TOKEN_PATH),
        "protected_filters": ["is:starred"],
        "total_unique_matches": len(ordered_unique_ids),
        "deleted_count": deleted_count,
        "extra_rules_path": str(EXTRA_RULES_PATH),
        "new_type_state_path": str(NEW_TYPE_STATE_PATH),
        "new_type_candidates": candidate_report,
        "rules": per_rule,
    }
    print(json.dumps(result, ensure_ascii=False, indent=json_indent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
