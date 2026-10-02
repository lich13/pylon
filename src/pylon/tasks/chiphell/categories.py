"""Deterministic Chiphell product taxonomy and notification policy.

The classifier is deliberately conservative.  A category is only muted when a
post is an unambiguous, single-category item (or when a body contains clearly
separated item lines).  Mixed and unknown posts stay visible so a category
preference cannot hide useful information.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from pylon.config import profile_env
from pylon.shared import paths, state_json


CATEGORY_VERSION = 1
POLICY_PATH = paths.CHIPHELL_DATA_ROOT / "category-policy.json"
QUEUE_PATH = paths.CHIPHELL_DATA_ROOT / "delivery.sqlite3"
POLICY_LOCK_PATH = paths.CHIPHELL_DATA_ROOT / ".category-policy.lock"
CALLBACK_PREFIX = "ch1"

# Stable ids are used in callback data and the policy file; labels are the
# Chinese names shown to the user.
CATEGORY_LABELS: dict[str, str] = {
    "phone_apple": "手机·苹果",
    "phone_oppo_vivo": "手机·OPPO/vivo",
    "phone_huawei": "手机·华为",
    "phone_xiaomi": "手机·小米/红米",
    "phone_samsung": "手机·三星及其他",
    "phone_honor": "手机·荣耀",
    "computer_cpu": "电脑·CPU",
    "computer_laptop": "电脑·笔记本",
    "computer_motherboard": "电脑·主板",
    "computer_memory": "电脑·内存",
    "computer_gpu": "电脑·显卡",
    "computer_storage": "电脑·硬盘/存储",
    "computer_power_case_cooling": "电脑·电源/机箱/散热",
    "computer_monitor": "电脑·显示器",
    "computer_peripherals": "电脑·键鼠/外设",
    "computer_mini_pc": "电脑·台式/小主机",
    "network": "网络设备",
    "audio_headphones": "音频·耳机",
    "audio_speakers": "音频·音箱",
    "game_console": "游戏·主机/掌机",
    "camera_drone": "相机·无人机/运动相机",
    "camera_lens": "相机·相机/镜头",
    "tablet_ipad": "平板·iPad",
    "tablet_other": "平板·其他",
    "watch_smart": "手表·智能",
    "watch_mechanical": "手表·机械",
    "accessory_cable": "配件·充电/线材",
    "accessory_case_stand": "配件·壳膜/支架",
    "life_clothing": "生活·服饰鞋包",
    "life_vehicle": "生活·车/骑行",
    "life_home": "生活·家居/家电",
    "life_collectible": "生活·收藏/玩具",
    "life_other": "生活·其他",
}

MIXED_KEY = "mixed"
UNKNOWN_KEY = "unknown"
MIXED_LABEL = "标题明确涉及多类商品"
UNKNOWN_LABEL = "暂未归类"

# Ordered from specific to broad to keep iPad/MacBook/Apple Watch out of the
# generic Apple phone category.  Each entry is intentionally a small set of
# product evidence rather than a broad keyword OR query.
_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tablet_ipad", (r"\bipad\b", r"ipad", r"苹果平板")),
    ("tablet_other", (r"平板", r"matepad", r"surface pro", r"小米平板", r"联想小新 pad")),
    ("watch_smart", (r"apple watch", r"华为手表", r"智能手表", r"智能手环", r"galaxy watch", r"手环")),
    ("watch_mechanical", (r"机械表", r"机械手表", r"seiko", r"精工", r"劳力士", r"欧米茄")),
    ("camera_drone", (r"无人机", r"大疆", r"\bdji\b", r"gopro", r"运动相机", r"osmo", r"pocket\s*[234]")),
    ("camera_lens", (r"相机", r"镜头", r"单反", r"微单", r"\blens\b", r"eos", r"a7[mr]?\d")),
    ("computer_laptop", (r"笔记本", r"macbook", r"thinkpad", r"xps", r"matebook", r"magicbook", r"轻薄本", r"游戏本")),
    ("computer_mini_pc", (r"小主机", r"迷你主机", r"mini\s*pc", r"\bnuc\b", r"sff")),
    ("computer_gpu", (r"显卡", r"\brtx\s*\d", r"\bgtx\s*\d", r"\brx\s*\d", r"radeon")),
    ("computer_cpu", (r"cpu", r"处理器", r"intel\s*(core)?", r"core\s*i[3579]", r"\bi[3579]-?\d", r"ryzen", r"锐龙", r"\d{4,5}x3d\b")),
    ("computer_motherboard", (r"主板", r"\b[zhb]\d{3}\b", r"x\d{3}\s*(主板)?", r"amd\s*主板")),
    ("computer_memory", (r"内存", r"内存条", r"ddr[345]", r"\bram\b")),
    ("computer_storage", (r"硬盘", r"固态", r"ssd", r"nvme", r"机械盘", r"hdd", r"u盘", r"存储卡")),
    ("computer_power_case_cooling", (r"电源", r"机箱", r"散热", r"水冷", r"风扇", r"电源仓")),
    ("computer_monitor", (r"显示器", r"\bmonitor\b", r"曲面屏", r"4k屏", r"aoc", r"lg\s*显示")),
    ("computer_peripherals", (r"键盘", r"鼠标", r"键鼠", r"鼠标垫", r"手柄", r"外设", r"机械键盘")),
    ("network", (r"路由器", r"交换机", r"wifi", r"mesh", r"光猫", r"网络设备", r"旁路由")),
    ("audio_headphones", (r"耳机", r"airpods", r"headphone", r"headset", r"入耳", r"降噪")),
    ("audio_speakers", (r"音箱", r"音响", r"soundbar", r"speaker")),
    ("game_console", (r"ps[45]", r"ps5", r"xbox", r"switch", r"steam\s*deck", r"掌机", r"游戏机", r"rg\w+")),
    ("accessory_cable", (r"充电器", r"充电线", r"数据线", r"线材", r"电源适配器", r"充电头", r"移动电源")),
    ("accessory_case_stand", (r"手机壳", r"保护壳", r"贴膜", r"钢化膜", r"支架", r"壳膜")),
    ("phone_apple", (r"iphone", r"苹果手机", r"\bapple\s*手机\b")),
    ("phone_oppo_vivo", (r"oppo", r"vivo", r"一加", r"oneplus", r"iqoo", r"find\s*[xn]")),
    ("phone_huawei", (r"华为(?!平板|手表|matebook)", r"\bmate\s*\d", r"\bp\d{2}\s*(pro|plus)?\b")),
    ("phone_xiaomi", (r"小米手机", r"红米", r"redmi", r"\bmi\s*\d", r"\bk\d{2}\b")),
    ("phone_samsung", (r"三星", r"galaxy\s*s", r"galaxy\s*note")),
    ("phone_honor", (r"荣耀(?!手表|平板)", r"honor\s*(magic|x|play)")),
    ("life_vehicle", (r"自行车", r"单车", r"公路车", r"折叠车", r"摩托", r"汽车", r"凯迪拉克", r"小鹏", r"行车记录仪")),
    ("life_clothing", (r"鞋", r"衣服", r"服饰", r"外套", r"裤", r"背包", r"羽绒", r"帽")),
    ("life_home", (r"家电", r"冰箱", r"洗衣机", r"空调", r"吸尘器", r"台灯", r"家居", r"homeassistant")),
    ("life_collectible", (r"玩具", r"手办", r"收藏", r"lego", r"模型")),
    ("life_other", (r"投影", r"kindle", r"电子书", r"书籍", r"\bnas\b", r"qnap", r"办公椅", r"家具")),
)


def _compiled_patterns() -> tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]:
    return tuple((key, tuple(re.compile(pattern, re.I) for pattern in patterns)) for key, patterns in _PATTERNS)


COMPILED_PATTERNS = _compiled_patterns()


def _text(post: dict[str, Any] | str) -> str:
    if isinstance(post, str):
        return post
    return "\n".join(str(post.get(key) or "") for key in ("title", "body_full", "body_excerpt"))


def _labels_for_text(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text or "").strip().lower()
    found: list[str] = []
    for key, patterns in COMPILED_PATTERNS:
        if any(pattern.search(normalized) for pattern in patterns):
            if key not in found:
                found.append(key)
    return found


def _segments(post: dict[str, Any]) -> list[str]:
    """Return only clearly separated item lines; otherwise return one item."""
    title = str(post.get("title") or "").strip()
    body = str(post.get("body_full") or post.get("body_excerpt") or "").strip()
    raw = "\n".join(item for item in (title, body) if item)
    body_lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)、])\s*", "", line).strip()
                  for line in re.split(r"[\r\n]+", body)]
    body_lines = [line for line in body_lines if len(line) >= 3 and len(line) <= 240]
    # A title plus ordinary price/contact lines is one product.  Only split
    # when at least two body lines independently identify a concrete product.
    if len(body_lines) < 2:
        return [raw] if raw else []
    known_lines = [line for line in body_lines if len(_labels_for_text(line)) == 1]
    if len(known_lines) < 2:
        return [raw] if raw else []
    return body_lines


def classify_post(post: dict[str, Any]) -> dict[str, Any]:
    segments = _segments(post)
    classified: list[dict[str, Any]] = []
    all_labels: list[str] = []
    for segment in segments:
        labels = _labels_for_text(segment)
        for label in labels:
            if label not in all_labels:
                all_labels.append(label)
        classified.append({"text": segment, "categories": labels or [UNKNOWN_KEY]})
    if not all_labels:
        all_labels = [UNKNOWN_KEY]
    ambiguous_segment = any(
        len(item["categories"]) != 1 or item["categories"][0] == UNKNOWN_KEY
        for item in classified
    )
    splittable = len(classified) > 1 and not ambiguous_segment
    mixed = any(len(item["categories"]) > 1 for item in classified) or (len(all_labels) > 1 and not splittable)
    return {
        "version": CATEGORY_VERSION,
        "categories": all_labels,
        "mixed": mixed,
        "splittable": splittable,
        "segments": classified,
    }


def _default_policy() -> dict[str, Any]:
    return {"version": CATEGORY_VERSION, "silent_categories": [], "updated_at": None, "updated_by": None}


def load_policy(path: Path | None = None) -> dict[str, Any]:
    path = path or POLICY_PATH
    data = state_json.load(path, default=_default_policy())
    if not isinstance(data, dict):
        data = _default_policy()
    values = data.get("silent_categories")
    if not isinstance(values, list):
        values = []
    valid = [str(value) for value in values if str(value) in CATEGORY_LABELS]
    return {"version": CATEGORY_VERSION, "silent_categories": list(dict.fromkeys(valid)),
            "updated_at": data.get("updated_at"), "updated_by": data.get("updated_by")}


@contextlib.contextmanager
def _policy_lock(path: Path):
    lock_path = path.with_name("." + path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def save_policy(policy: dict[str, Any], path: Path | None = None, *, actor: str = "local") -> dict[str, Any]:
    path = path or POLICY_PATH
    with _policy_lock(path):
        clean = load_policy(path)
        clean["silent_categories"] = [key for key in policy.get("silent_categories", []) if key in CATEGORY_LABELS]
        clean["silent_categories"] = list(dict.fromkeys(clean["silent_categories"]))
        clean["updated_at"] = int(__import__("time").time())
        clean["updated_by"] = str(actor)[:80]
        state_json.save_atomic(path, clean)
    return clean


def resolve_category(value: str) -> str:
    raw = re.sub(r"\s+", "", str(value or "")).lower()
    if raw in CATEGORY_LABELS:
        return raw
    aliases = {re.sub(r"\s+", "", label).lower(): key for key, label in CATEGORY_LABELS.items()}
    if raw in aliases:
        return aliases[raw]
    raise ValueError("分类不存在；请先使用 /chiphell_categories 查看分类名称")


def set_category_silence(category: str, silent: bool, *, actor: str = "local", path: Path | None = None) -> dict[str, Any]:
    key = resolve_category(category)
    path = path or POLICY_PATH
    with _policy_lock(path):
        policy = load_policy(path)
        values = set(policy["silent_categories"])
        if silent:
            values.add(key)
        else:
            values.discard(key)
        policy["silent_categories"] = sorted(values)
        policy["updated_at"] = int(__import__("time").time())
        policy["updated_by"] = str(actor)[:80]
        state_json.save_atomic(path, policy)
    return policy


def filter_normal_posts(posts: Iterable[dict[str, Any]], policy: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    policy = policy or load_policy()
    silent = set(policy.get("silent_categories") or [])
    output: list[dict[str, Any]] = []
    for post in posts:
        if not post.get("normal_category"):
            post = dict(post)
            post["category"] = classify_post(post)
        classification = post.get("category") if isinstance(post.get("category"), dict) else classify_post(post)
        labels = set(classification.get("categories") or [UNKNOWN_KEY])
        if not silent or classification.get("mixed") or UNKNOWN_KEY in labels:
            output.append(post)
            continue
        if classification.get("splittable"):
            kept = [item for item in classification.get("segments", [])
                    if not set(item.get("categories") or [UNKNOWN_KEY]) <= silent]
            if kept:
                item = dict(post)
                item["category_segments"] = kept
                output.append(item)
            continue
        if not labels <= silent:
            output.append(post)
    return output


def arbitrage_posts(posts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the existing device exclusion boundary for quote matching only."""
    from pylon.tasks.chiphell import monitor
    return [post for post in posts if monitor.should_notify_post(post)]


def _safe_payload_text(payload: str) -> str:
    return str(payload or "").replace("\x00", "")


_POST_LINE = re.compile(r"^\s*\d+\.\s+\[([^\]]+)\]\((https?://[^)]+)\)(?:\s+摘要：)?(.*)$")


def history_posts(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or QUEUE_PATH
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        uri = f"file:{path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as db:
            for (payload,) in db.execute("SELECT payload FROM delivery_queue ORDER BY created_at, dedupe_key"):
                for line in _safe_payload_text(payload).splitlines():
                    match = _POST_LINE.match(line)
                    if not match:
                        continue
                    rows.append({"title": match.group(1), "url": match.group(2), "body_excerpt": match.group(3)})
    except (OSError, sqlite3.Error):
        return []
    return rows


def category_counts(posts: Iterable[dict[str, Any]]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for post in posts:
        result = classify_post(post)
        labels = result["categories"]
        if result.get("mixed"):
            counts[MIXED_KEY] += 1
        for label in labels:
            counts[label] += 1
    return counts


def _signature(profile: str, action: str, category: str) -> str:
    secret = str(profile_env(profile).get("TELEGRAM_BOT_TOKEN") or "pylon").encode()
    return hmac.new(secret, f"{profile}|{action}|{category}".encode(), hashlib.sha256).hexdigest()[:12]


def callback_data(profile: str, action: str, category: str) -> str:
    return f"{CALLBACK_PREFIX}|{action}|{category}|{_signature(profile, action, category)}"


def _authorized_callback(profile: str, payload: dict[str, Any]) -> bool:
    message = payload.get("message") or {}
    sender = payload.get("from") or {}
    owner = str(profile_env(profile).get("TELEGRAM_HOME_CHANNEL") or "")
    chat = message.get("chat") or {}
    return bool(owner and chat.get("type") == "private" and str(chat.get("id")) == owner
                and str(sender.get("id")) == owner and not sender.get("is_bot"))


def _parse_callback(profile: str, data: str) -> tuple[str, str]:
    parts = str(data or "").split("|")
    if len(parts) != 4 or parts[0] != CALLBACK_PREFIX:
        raise ValueError("分类按钮已失效，请重新使用 /chiphell_categories")
    action, category, signature = parts[1:]
    if action not in {"mute", "unmute"} or category not in CATEGORY_LABELS:
        raise ValueError("分类按钮参数无效，请重新使用 /chiphell_categories")
    if not hmac.compare_digest(signature, _signature(profile, action, category)):
        raise ValueError("分类按钮签名无效，请重新使用 /chiphell_categories")
    return action, category


def _label(key: str) -> str:
    return CATEGORY_LABELS.get(key, MIXED_LABEL if key == MIXED_KEY else UNKNOWN_LABEL)


def category_report(profile: str = "default", *, path: Path | None = None, queue_path: Path | None = None) -> tuple[str, dict[str, Any]]:
    policy = load_policy(path)
    posts = history_posts(queue_path)
    counts = category_counts(posts)
    lines = [f"CHH 历史分类（分类器 v{CATEGORY_VERSION}）", f"已解析商品帖：{len(posts)} 条",
             f"当前静默：{ '、'.join(_label(key) for key in policy['silent_categories']) or '无' }", ""]
    for key, label in CATEGORY_LABELS.items():
        if counts.get(key):
            state = "静默" if key in policy["silent_categories"] else "推送"
            lines.append(f"{label}：{counts[key]}（{state}）")
    if counts.get(MIXED_KEY):
        lines.append(f"{MIXED_LABEL}：{counts[MIXED_KEY]}（始终保留）")
    if counts.get(UNKNOWN_KEY):
        lines.append(f"{UNKNOWN_LABEL}：{counts[UNKNOWN_KEY]}（始终保留）")
    lines.append("未列入静默的类别继续推送；重点比价不受此策略影响。")
    if profile == "cli":
        return "\n".join(lines), {}
    rows: list[list[dict[str, str]]] = []
    for key, label in CATEGORY_LABELS.items():
        if not counts.get(key):
            continue
        muted = key in policy["silent_categories"]
        action = "unmute" if muted else "mute"
        rows.append([{"text": ("恢复 " if muted else "静默 ") + label, "callback_data": callback_data(profile, action, key)}])
    return "\n".join(lines), {"inline_keyboard": rows}


def policy_status_line(path: Path | None = None) -> str:
    policy = load_policy(path)
    muted = "、".join(_label(key) for key in policy["silent_categories"]) or "无"
    return "CHH 普通新帖静默：" + muted + "；混合/未归类保留；重点比价独立"


def callback(profile: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not _authorized_callback(profile, payload):
        return {"text": "无权操作此分类按钮。"}
    try:
        action, category = _parse_callback(profile, str(payload.get("data") or ""))
        policy = set_category_silence(category, action == "mute", actor=f"telegram:{profile}")
        text, markup = category_report(profile)
        result = "已静默" if action == "mute" else "已恢复推送"
        return {"text": result + "：" + _label(category) + "\n\n" + text, "reply_markup": markup}
    except ValueError as error:
        return {"text": str(error)}


def command(action: str, argument: str = "", profile: str = "default") -> tuple[str, dict[str, Any] | None]:
    actor = "cli" if profile == "cli" else f"telegram:{profile}"
    if action == "categories":
        return category_report(profile)
    if action in {"silence", "unsilence"}:
        if not argument.strip():
            raise ValueError("请提供分类名称，例如：/chiphell_silence 生活·服饰鞋包")
        policy = set_category_silence(argument, action == "silence", actor=actor)
        key = resolve_category(argument)
        return (("已静默：" if action == "silence" else "已恢复推送：") + _label(key)
                + "\n当前静默：" + ("、".join(_label(x) for x in policy["silent_categories"]) or "无"), None)
    raise ValueError("不支持的分类操作")


def cli(action: str, argument: str = "") -> str:
    text, _ = command(action, argument, "cli")
    return text


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Chiphell 分类策略")
    parser.add_argument("action", choices=["categories", "silence", "unsilence"])
    parser.add_argument("category", nargs="?", default="")
    args = parser.parse_args(argv)
    try:
        print(cli(args.action, args.category))
        return 0
    except Exception as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
