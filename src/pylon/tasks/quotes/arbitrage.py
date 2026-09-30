from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import html
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from pylon.shared import paths
from pylon.shared.bark import BarkResult, send_critical_push

DATA_ROOT = paths.DATA_DIR / "electronics_quotes"
DB_PATH = DATA_ROOT / "electronics_quotes.sqlite3"
SOURCE_URL = "https://www.kdocs.cn/l/ccXDbEMZ5Ejp"
SVG_ENDPOINT = "https://www.kdocs.cn/api/v3/office/file/ccXDbEMZ5Ejp/svg/et"
OPEN_ET_ENDPOINT = "https://www.kdocs.cn/api/v3/office/file/ccXDbEMZ5Ejp/open/et"

BRANDS = {"苹果", "华为", "小米", "荣耀", "OPPO", "VIVO", "三星", "摩托罗拉", "一加", "8848", "红米", "魅族", "大疆", "影石", "任天堂", "索尼"}
BRAND_ALIASES = {
    "苹果": ["苹果", "IPHONE", "APPLE", "AIRPODS", "WATCH", "PENCIL"],
    "华为": ["华为", "HUAWEI"],
    "小米": ["小米", "XIAOMI", "MI ", "REDMI", "红米"],
    "红米": ["红米", "REDMI"],
    "荣耀": ["荣耀", "HONOR"],
    "OPPO": ["OPPO", "FIND", "RENO"],
    "VIVO": ["VIVO", "IQOO"],
    "三星": ["三星", "SAMSUNG"],
    "摩托罗拉": ["摩托罗拉", "MOTOROLA", "MOTO"],
    "一加": ["一加", "ONEPLUS"],
    "8848": ["8848"],
    "魅族": ["魅族", "MEIZU"],
    "大疆": ["大疆", "DJI"],
    "影石": ["影石", "INSTA360"],
    "任天堂": ["任天堂", "NINTENDO", "SWITCH", "NS2"],
    "索尼": ["索尼", "SONY", "PS5", "PLAYSTATION"],
}
PRICE_RE = re.compile(r"^\d{3,6}$")
NEW_RE = re.compile(r"(全新|未拆封|未开封|未激活|未使用|原封|仅拆封未用|只拆封未用)", re.I)
# Accessory/package scoped newness must not qualify the *device* for quote
# arbitrage. Example false positive: a used, activated iPhone whose only
# new/sealed item is ``数据线全新未拆``.
ACCESSORY_NEW_CONTEXT_RE = re.compile(
    r"(?:数据线|充电线|充电器|充电头|线材)"
    r"[^。；;，,\n]{0,30}(?:全新|未拆封|未拆|未开封|未使用|原封)",
    re.I,
)
USED_SIGNAL_NEGATION_RE = re.compile(r"(?:未|无|没(?:有)?|没有)\s*(?:激活|使用|拆封|开封)|(?:仅|只)拆封未用", re.I)
USED_DEVICE_RE = re.compile(
    r"(?:已|已经|刚|\d{1,2}\s*月|[一二三四五六七八九十]+月)?\s*激活"
    r"|激活(?:了|过|后|时间|日期)"
    r"|用[了过]?\s*\d+\s*(?:天|日|周|个月|月|年)"
    r"|使用痕迹|轻微使用"
    r"|成色\s*[:：]?\s*(?:\d(?:\.\d)?\s*成新|\d{2}\s*新|[九八七六五四三二一]\s*成新)"
    r"|(?:已|已经)\s*拆封",
    re.I,
)
SALE_RE = re.compile(r"(二手交易|出售|出个|出一|\b出\b|转让|卖|明盘|挂价|闲置|可刀|走鱼|闲鱼)", re.I)
NEGATIVE_BUY_RE = re.compile(r"(求购|收一个|收个|想收|求收|预算)")
# User-excluded device deal classes. Keep this deterministic so marketplace and
# arbitrage alerts do not wake the model for known noisy/risky inventory.
EXCLUDED_DEVICE_DEAL_RE = re.compile(
    r"(有锁机|有锁版|网络锁|卡贴机|监管机|监管锁|租赁机|租借机|官翻机|官翻|官方翻新|Apple\s*Certified\s*Refurbished|资源机|权益机|演示机)",
    re.I,
)
MDM_DEVICE_RE = re.compile(r"\bMDM\b|移动设备管理")
MDM_NEGATION_RE = re.compile(r"(非|无|没有|不是|不存在|无任何)\s*(?:MDM|移动设备管理)", re.I)
COLOR_TOKENS = {"黑", "白", "蓝", "紫", "绿", "金", "橙", "银", "粉", "青", "灰", "红", "黄"}
QUOTE_COLOR_CHARS = "黑白蓝紫绿金橙银粉青灰红黄色沙钛原棕杏岩板石午夜淡桃雾浅海洋铁锚木炭珍珠冰川陶瓷玄曜丹曦彩"
NONPHONE_QUOTE_RE = re.compile(
    r"(平板|MATEPAD|IPAD|PAD|迷你\d|MINI\d|AIR\d+[-－]\d{1,2}寸|手表|WATCH|手环|BAND|耳机|AIRPODS|FREEBUDS|BUDS|PENCIL|键盘|鼠标|充电|电源|数据线|保护壳|壳膜|配件|MACBOOK|MATEBOOK|MAGICBOOK|REDMIBOOK|THINKPAD|SURFACE|NOTEBOOK|LAPTOP|笔记本|电脑|无人机|大疆|DJI|影石|INSTA360|任天堂|SWITCH|PS5|PLAYSTATION)",
    re.I,
)
POTENTIAL_QUOTE_CANDIDATE_RE = re.compile(
    r"(平板|MATEPAD|IPAD|PAD|迷你\d|MINI\d|AIR\d+[-－]\d{1,2}寸|手表|WATCH|手环|BAND|耳机|AIRPODS|FREEBUDS|BUDS|PENCIL|键盘|鼠标|充电|电源|数据线|保护壳|壳膜|配件|MACBOOK|MATEBOOK|MAGICBOOK|REDMIBOOK|THINKPAD|SURFACE|NOTEBOOK|LAPTOP|笔记本|电脑|无人机|大疆|DJI|GOPRO|相机|影石|INSTA360|任天堂|SWITCH|PS5|PLAYSTATION|AR眼镜|智能眼镜|眼镜)",
    re.I,
)
WPS_INLINE_ALLOWED_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff\s+\-./（）()_:：,，*]+")
INLINE_COLOR_PRICE_RE = re.compile(rf"(?P<color>[{QUOTE_COLOR_CHARS}]{{1,16}})\s*[-*]?\s*(?P<price>\d{{2,5}})(?!\d|\s*[GgTt款年代])")
# Full-sheet clipboard TSV contains compact non-phone quote cells that are not
# present in `/open/et`, e.g. AirPods, Apple Watch split tables, Huawei FreeBuds
# and Huawei WATCH/FIT rows. Keep this parser independent from phone parsing so
# the noisy copied grid cannot overwrite the structured SVG phone rows.
TSV_COLOR_PRICE_RE = re.compile(
    r"(?P<color>[黑白蓝紫绿金橙银粉青灰红黄棕褐杏原沙钛石板午夜星光亮深空雾岩桃色兰暖瑰全陶瓷冰川木炭海洋高山回环野径米尼斯]{1,18})\s*[-—–*]?\s*(?P<price>\d{2,5})(?!\d)",
    re.I,
)
TSV_PRICE_RE = re.compile(r"\d{2,5}")
TSV_SECTION_RE = re.compile(r"(苹果配件|苹果手表|华为耳机|华为手表|手表手环)", re.I)
BASE64_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9+/=_-])([A-Za-z0-9+/_-]{8,}(?:={0,2}))(?![A-Za-z0-9+/=_-])")
CONTACT_CONTEXT_RE = re.compile(r"(联系方式|联系|微信|VX|WX|WeChat|QQ|加Q|加V|base64|解码|有意|购买)", re.I)
WECHAT_CONTEXT_RE = re.compile(r"(微信|VX|WX|WeChat|加V)", re.I)
QQ_CONTEXT_RE = re.compile(r"(QQ|加Q)", re.I)
DEFAULT_BARK_BURST_COUNT = 4
DEFAULT_BARK_BURST_INTERVAL_SECONDS = 5.0
MAX_BARK_BURST_COUNT = 8
MAX_BARK_BURST_INTERVAL_SECONDS = 30.0


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS quotes (
            sku_key TEXT PRIMARY KEY,
            brand TEXT NOT NULL,
            model_spec TEXT NOT NULL,
            color TEXT NOT NULL,
            product_name TEXT NOT NULL,
            quote_price INTEGER NOT NULL,
            normalized TEXT NOT NULL,
            source_url TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arbitrage_alerts (
            alert_key TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            url TEXT NOT NULL,
            title TEXT NOT NULL,
            product_name TEXT NOT NULL,
            quote_price INTEGER NOT NULL,
            sale_price INTEGER NOT NULL,
            profit INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_sent_at TEXT,
            repeat_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            message TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_arbitrage_active ON arbitrage_alerts(status, repeat_count, last_sent_at);
        """
    )
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else default


def extract_svg_texts(svg_bytes: bytes) -> list[tuple[float, float, str]]:
    s = svg_bytes.decode("utf-8", "replace")
    out: list[tuple[float, float, str]] = []
    for m in re.finditer(r"<text\b([^>]*)>(.*?)</text>", s, re.S):
        attrs = m.group(1)
        inner = m.group(2)
        content = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if not content:
            continue
        mx = re.search(r"\bx=\"?([^\"\s]+)", attrs)
        my = re.search(r"\by=\"?([^\"\s]+)", attrs)
        if mx and my:
            try:
                out.append((float(mx.group(1)), float(my.group(1)), content))
            except ValueError:
                continue
    return out


def group_rows(texts: list[tuple[float, float, str]], tolerance: float = 35.0) -> list[tuple[float, list[tuple[float, str]]]]:
    groups: list[list[Any]] = []
    for x, y, text in sorted(texts, key=lambda z: (z[1], z[0])):
        if not groups or abs(float(groups[-1][0]) - y) > tolerance:
            groups.append([y, []])
        groups[-1][1].append((x, text))
    return [(float(y), sorted(cells)) for y, cells in groups]


def normalize_text(s: str) -> str:
    s = (s or "").upper()
    s = s.replace("１", "1").replace("２", "2").replace("３", "3").replace("４", "4").replace("５", "5")
    s = s.replace("６", "6").replace("７", "7").replace("８", "8").replace("９", "9").replace("０", "0")
    s = s.replace("＋", "+").replace("Ｔ", "T").replace("Ｇ", "G")
    return re.sub(r"[^0-9A-Z\u4e00-\u9fff+]+", "", s)


def normalize_match_text(s: str) -> str:
    """Normalize text for quote/post matching, including memory/storage units.

    Marketplace posts often write configs as ``16G+512G`` while KDocs rows use
    ``16+512``. Convert these equivalent forms before token matching.
    """
    norm = normalize_text(s)
    # Official/full-name aliases seen in the quote DB and vendor pages.
    # Examples: Huawei RS uses "ULTIMATE DESIGN 非凡大师"; marketplace posts
    # often misspell it as DEDIGN. Xiaomi/Apple sellers commonly abbreviate
    # Pro Max / Pro / Ultra as PM / P / U after the generation number.
    for alias in ("ULTIMATEDEDIGN", "ULTIMATEDESIGN", "ULTIMATEDES", "UD"):
        norm = norm.replace(alias, "非凡大师")
    norm = norm.replace("典藏版", "典藏")
    # Wearables: keep watch/band names comparable across English product names
    # and Chinese marketplace shorthand. Examples: Apple Watch Series 11/S11,
    # Huawei WATCH GT 5/华为手表 GT5, Xiaomi Smart Band/小米手环.
    norm = norm.replace("智能手表", "WATCH").replace("手表", "WATCH")
    norm = norm.replace("SMARTBAND", "BAND").replace("智能手环", "BAND").replace("手环", "BAND").replace("米环", "BAND")
    norm = norm.replace("MATEPAD", "PAD").replace("IPAD", "PAD").replace("平板电脑", "PAD").replace("平板", "PAD")
    norm = norm.replace("迷你", "MINI")
    norm = norm.replace("AIRPODS", "BUDS").replace("FREEBUDS", "BUDS").replace("蓝牙耳机", "BUDS").replace("耳机", "BUDS").replace("耳塞", "BUDS")
    norm = norm.replace("触控笔", "PENCIL").replace("手写笔", "PENCIL").replace("妙控键盘", "KEYBOARD").replace("键盘", "KEYBOARD")
    norm = norm.replace("保护壳", "CASE").replace("壳膜", "CASE").replace("手机壳", "CASE").replace("平板壳", "CASE")
    norm = norm.replace("一代", "1").replace("二代", "2").replace("三代", "3").replace("四代", "4")
    norm = norm.replace("TYPEC", "USBC")
    norm = norm.replace("SERIES", "S")
    norm = norm.replace("蜂窝网络", "CELLULAR").replace("蜂窝版", "CELLULAR").replace("蜂窝", "CELLULAR")
    norm = norm.replace("移动网络", "CELLULAR").replace("ESIM", "CELLULAR").replace("LTE", "CELLULAR")
    norm = re.sub(r"(\d{2})毫米", r"\1MM", norm)
    norm = re.sub(r"(\d{1,2})(?:英寸|寸)", r"\1INCH", norm)
    norm = re.sub(r"(PAD(?:PRO|AIR|MINI))(\d{2})(?=\d{3}G|\dT|WIFI|CELLULAR|$)", r"\1\2INCH", norm)
    norm = re.sub(r"(?<!\d)4G版", "CELLULAR", norm)
    norm = re.sub(r"(\d{1,3})PM", r"\1PROMAX", norm)
    norm = re.sub(r"(\d{1,3})P(?!RO|LUS)", r"\1PRO", norm)
    norm = re.sub(r"(\d{1,3})U(?![A-Z])", r"\1ULTRA", norm)
    # Normalize RAM/storage units. KDocs rows mix 16+512, 16+512G, 16+1T,
    # while forum posts use 16G+512G / 16GB+1TB. Some parsed rows contain
    # odd artifacts like 1TBG or 1TTB; collapse those to 1T.
    norm = re.sub(r"(\d+)T+BG?", r"\1T", norm)
    norm = re.sub(r"(\d+)TB", r"\1T", norm)
    norm = re.sub(r"(\d+)GB", r"\1G", norm)
    norm = re.sub(r"(\d+)G\+(\d+)G", r"\1+\2", norm)
    norm = re.sub(r"(\d+)G\+(\d+)T", r"\1+\2T", norm)
    norm = re.sub(r"(\d+)\+(\d+)G", r"\1+\2", norm)
    return norm


def is_excluded_device_deal(text: str) -> bool:
    """Return True for device classes the user never wants pushed.

    Exact terms such as 官翻/有锁机/监管机 are noisy even when the price looks
    attractive. MDM is treated as excluded unless locally negated (e.g. 非 MDM).
    """
    hay = text or ""
    if EXCLUDED_DEVICE_DEAL_RE.search(hay):
        return True
    if MDM_DEVICE_RE.search(hay) and not MDM_NEGATION_RE.search(hay):
        return True
    return False


def _strip_accessory_new_context(text: str) -> str:
    """Remove new/sealed wording that clearly describes only bundled cables."""
    return ACCESSORY_NEW_CONTEXT_RE.sub(" ", text or "")


def has_device_newness(text: str) -> bool:
    """Return True only when new/sealed wording plausibly applies to the device.

    Marketplace excerpts often mix device condition with package/accessory notes.
    A line like ``2月激活，用了3个月，数据线全新未拆`` contains ``全新/未拆``
    but the newness belongs to the cable, not the phone. Treat explicit used
    device signals as a blocker unless there is still an independent device-level
    newness phrase after accessory/package-scoped phrases are removed.
    """
    from .matching import ACCESSORY, USED
    hay = text or ""
    if USED.search(hay):
        return False
    hay = ACCESSORY.sub(" ", hay)
    cleaned = _strip_accessory_new_context(hay)
    if not NEW_RE.search(cleaned):
        return False
    if USED_DEVICE_RE.search(hay):
        # Avoid false positives from negated phrases that are themselves valid
        # newness signals, e.g. ``未激活`` / ``没有使用``.
        used_context = USED_SIGNAL_NEGATION_RE.sub(" ", hay)
        if USED_DEVICE_RE.search(used_context):
            return False
    return True


def sku_key(brand: str, model_spec: str, color: str) -> str:
    raw = f"{brand}|{model_spec}|{color}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _row_get(row: Any, key: str, default: str = "") -> Any:
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        if isinstance(row, dict):
            return row.get(key, default)
    return default


def quote_category_from_values(brand: str, model_spec: str, product_name: str = "") -> str:
    """Classify a quote row into a durable device category.

    The category snapshot is used to detect new source sections and to guard
    against accidental whole-category deletions when KDocs rendering/parsing
    changes. Keep category names stable; add new ones before auto-ingesting a
    new device family.
    """
    raw = f"{brand} {model_spec} {product_name}".upper()
    norm = normalize_match_text(raw)
    if re.search(r"无人机|大疆|DJI", raw + norm, re.I):
        return "drone"
    if re.search(r"影石|INSTA360|GOPRO|相机", raw + norm, re.I):
        return "camera"
    if re.search(r"任天堂|SWITCH|NS2|PS5|PLAYSTATION", raw + norm, re.I):
        return "console"
    if re.search(r"PENCIL|KEYBOARD|CASE|保护壳|壳膜|配件|充电|电源|数据线|鼠标", raw + norm, re.I):
        return "accessory"
    if re.search(r"BUDS|AIRPODS|FREEBUDS|耳机|耳塞", raw + norm, re.I):
        return "earbuds"
    if re.search(r"WATCH|手表", raw + norm, re.I):
        return "watch"
    if str(brand) == "苹果" and re.search(r"(?:^|\s)(?:S\d{1,2}|SE|ULTRA|WATCH).*?(?:\d{2}|MM|GPS|蜂窝|运动|米兰|回环|野径|海洋)|\d{2}[-－].*(?:S/M|M/L)|钛金属蜂窝", raw + norm, re.I):
        return "watch"
    if re.search(r"\bBAND\b|BAND\d|手环|米环", raw + norm, re.I):
        return "band"
    if re.search(r"PAD|IPAD|MATEPAD|平板|AIR\d+[-－]\d{1,2}寸|迷你\d|MINI\d", raw + norm, re.I):
        return "tablet"
    if re.search(r"MACBOOK|MATEBOOK|MAGICBOOK|REDMIBOOK|THINKPAD|SURFACE|NOTEBOOK|LAPTOP|笔记本|电脑", raw + norm, re.I):
        return "laptop"
    if str(brand) in BRANDS:
        return "phone"
    return "unknown"


def quote_category(row: Any) -> str:
    return quote_category_from_values(
        str(_row_get(row, "brand", "")),
        str(_row_get(row, "model_spec", "")),
        str(_row_get(row, "product_name", "")),
    )


def quote_category_snapshot(rows: list[Any]) -> dict[str, Any]:
    categories: dict[str, dict[str, Any]] = {}
    for row in rows:
        cat = quote_category(row)
        bucket = categories.setdefault(cat, {"count": 0, "brands": {}, "model_specs": []})
        bucket["count"] += 1
        brand = str(_row_get(row, "brand", ""))
        bucket["brands"][brand] = int(bucket["brands"].get(brand, 0)) + 1
        model_spec = str(_row_get(row, "model_spec", ""))
        if model_spec and model_spec not in bucket["model_specs"] and len(bucket["model_specs"]) < 40:
            bucket["model_specs"].append(model_spec)
    return {"categories": categories, "total": sum(v["count"] for v in categories.values())}


def parse_embedded_brand_model_row(tokens: list[str]) -> list[dict[str, Any]]:
    """Parse rows like ``华为Mate XTS非凡大师16+512 玄黑 17700 ...``.

    Most quote sections are parsed by :func:`parse_block`, but some KDocs rows
    put the brand+model+configuration in one cell followed by color/price pairs.
    """
    if not tokens:
        return []
    first = (tokens[0] or "").strip()
    brand = next((b for b in sorted(BRANDS, key=len, reverse=True) if first.startswith(b) and first != b), "")
    if not brand:
        return []
    model_spec = first[len(brand) :].strip()
    model_norm = normalize_match_text(model_spec)
    has_phone_config = bool(re.search(r"\d+\s*\+\s*\d", model_spec))
    has_wearable_model = bool(re.search(r"WATCH|BAND|PAD|BUDS|PENCIL|KEYBOARD|CASE|GT\d*|FIT|S\d+|D\d+|SE|MINI|\d{2}MM|\d{1,2}寸", model_norm))
    if not model_spec or not (has_phone_config or has_wearable_model):
        return []

    entries: list[dict[str, Any]] = []
    rest = tokens[1:]
    last_price_idx = -1
    for i, token in enumerate(rest):
        if not PRICE_RE.match(token):
            continue
        seg = rest[last_price_idx + 1 : i]
        if not seg:
            continue
        color = seg[-1].strip()
        if not color or color in {"（", "）", "官网价", "官方价", "-", "/"}:
            continue
        price = int(token)
        if price < 100 or price > 50000:
            continue
        product_name = f"{brand} {model_spec} {color}".strip()
        entries.append(
            {
                "sku_key": sku_key(brand, model_spec, color),
                "brand": brand,
                "model_spec": model_spec,
                "color": color,
                "product_name": product_name,
                "quote_price": price,
                "normalized": normalize_text(f"{brand}{model_spec}{color}"),
                "source_url": SOURCE_URL,
            }
        )
        last_price_idx = i
    return entries


def parse_block(tokens: list[str]) -> list[dict[str, Any]]:
    if not tokens:
        return []
    if tokens[0] not in BRANDS:
        return parse_embedded_brand_model_row(tokens)
    brand = tokens[0]
    rest = tokens[1:]
    entries: list[dict[str, Any]] = []
    model_tokens: list[str] | None = None
    last_price_idx = -1
    for i, token in enumerate(rest):
        if not PRICE_RE.match(token):
            continue
        prev = rest[i - 1] if i > 0 else ""
        if prev in {"官网价", "官方价"}:
            continue
        seg = rest[last_price_idx + 1 : i]
        if not seg:
            continue
        color = seg[-1]
        if color in {"（", "）", "官网价", "官方价", "-", "/"}:
            continue
        if model_tokens is None:
            model_tokens = seg[:-1]
        if not model_tokens:
            continue
        price = int(token)
        if price < 100 or price > 50000:
            continue
        model_spec = " ".join(model_tokens).strip()
        product_name = f"{brand} {model_spec} {color}".strip()
        entries.append(
            {
                "sku_key": sku_key(brand, model_spec, color),
                "brand": brand,
                "model_spec": model_spec,
                "color": color,
                "product_name": product_name,
                "quote_price": price,
                "normalized": normalize_text(f"{brand}{model_spec}{color}"),
                "source_url": SOURCE_URL,
            }
        )
        last_price_idx = i
    return entries


def dedupe_quote_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """De-duplicate by SKU, keeping the last parsed price for that SKU."""
    dedup: dict[str, dict[str, Any]] = {}
    for e in entries:
        dedup[e["sku_key"]] = e
    return sorted(dedup.values(), key=lambda e: (e.get("row_y", 0), e["brand"], e["model_spec"], e["color"]))


def parse_quotes_from_svg(svg_bytes: bytes) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row_y, cells in group_rows(extract_svg_texts(svg_bytes)):
        tokens = [t for _, t in cells]
        indices = [i for i, t in enumerate(tokens) if t in BRANDS]
        if not indices:
            for entry in parse_block(tokens):
                entry["row_y"] = row_y
                entries.append(entry)
            continue
        for a, b in zip(indices, indices[1:] + [len(tokens)]):
            for entry in parse_block(tokens[a:b]):
                entry["row_y"] = row_y
                entries.append(entry)
    return dedupe_quote_entries(entries)


def _clean_wps_inline_text(raw: str) -> str:
    """Trim control/format bytes from UTF-16 strings embedded in KDocs open/et."""
    s = (raw or "").strip()
    m = WPS_INLINE_ALLOWED_RE.match(s)
    return (m.group(0) if m else "").strip()


def extract_wps_utf16_strings(blob: bytes) -> list[str]:
    """Extract likely UTF-16LE cell strings from KDocs `/open/et` binary data.

    The public KDocs SVG endpoint only renders the current visible range; the
    workbook stream contains the full sheet, including far-right tablet rows.
    This scanner intentionally returns cleaned printable strings only; parsing
    remains conservative and brand/pattern gated below.
    """
    out: list[str] = []
    i = 0
    while i < len(blob) - 4:
        start = i
        chars: list[str] = []
        while i < len(blob) - 1:
            code = blob[i] | (blob[i + 1] << 8)
            if code in (9, 10, 13) or 32 <= code <= 0x9FFF:
                chars.append(chr(code))
                i += 2
            else:
                break
        if len(chars) >= 3:
            cleaned = _clean_wps_inline_text("".join(chars))
            if len(cleaned) >= 3:
                out.append(cleaned)
        i = max(i + 2, start + 1)
    seen: set[str] = set()
    uniq: list[str] = []
    for s in out:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq


def _detect_brand_prefix(line: str) -> tuple[str, str]:
    clean = (line or "").strip()
    for prefix in ("原封", "新品", "新款"):
        if clean.startswith(prefix):
            clean = clean[len(prefix) :].strip()
            break
    for brand in sorted(BRANDS, key=len, reverse=True):
        if clean.startswith(brand):
            return brand, clean[len(brand) :].strip()
    norm = normalize_match_text(clean)
    if re.search(r"AIRPODS|BUDS|头戴BUDS|PENCIL|KEYBOARD|妙控|手写笔|耳机", clean + norm, re.I):
        return "苹果", clean
    if re.search(r"^(?:S\d{1,2}|SE|WATCH|ULTRA|\d{2}[-－])", norm, re.I):
        return "苹果", clean
    if re.search(r"^(?:大疆|DJI|OSMO|ACTION|POCKET|MAVIC|MIC)", clean, re.I):
        return "大疆", clean
    if re.search(r"^(?:影石|INSTA360|ACEPRO|GO\s*ULTRA|GO\d)", clean, re.I):
        return "影石", clean
    if re.search(r"^(?:任天堂|OLED|NS2|SWITCH)", clean, re.I):
        return "任天堂", clean
    if re.search(r"^P?S5", clean, re.I):
        return "索尼", clean
    return "", line


def _make_quote_entry(brand: str, model_spec: str, color: str, price: int, *, row_y: float | None = None) -> dict[str, Any]:
    product_name = f"{brand} {model_spec} {color}".strip()
    entry = {
        "sku_key": sku_key(brand, model_spec, color),
        "brand": brand,
        "model_spec": model_spec,
        "color": color,
        "product_name": product_name,
        "quote_price": int(price),
        "normalized": normalize_text(f"{brand}{model_spec}{color}"),
        "source_url": SOURCE_URL,
    }
    if row_y is not None:
        entry["row_y"] = row_y
    return entry


def parse_inline_quote_line(line: str, *, nonphone_only: bool = False) -> list[dict[str, Any]]:
    """Parse compact workbook cell strings such as tablet rows.

    Examples from `/open/et` include
    `华为平板原封MatePad-11.5S寸2025款活力版12+256灰1780银紫1800`
    and `苹果2025款iPad11 256白2960蓝2980粉2960黄2900`.
    """
    clean = _clean_wps_inline_text(line)
    if not clean:
        return []
    brand, rest = _detect_brand_prefix(clean)
    if not brand:
        return []
    matches = [m for m in INLINE_COLOR_PRICE_RE.finditer(rest) if 50 <= int(m.group("price")) <= 50000]
    if matches:
        first = matches[0]
        model_spec = rest[: first.start("color")].strip(" -_/，,")
    else:
        # Accessories/earbuds often have a single no-color price, e.g.
        # `AirPods4标准版(P63) 720` or `大疆Mobi1e7标准270`.
        masked_rest = re.sub(r"[（(][^）)]*[）)]", lambda m: " " * (m.end() - m.start()), rest)
        price_matches = []
        for m in re.finditer(r"(?<!\d)(\d{2,5})(?!\d|\s*[GgTt款年代])", masked_rest):
            price = int(m.group(1))
            if price < 50 or price > 50000:
                continue
            prev = masked_rest[m.start(1) - 1] if m.start(1) > 0 else ""
            nxt = masked_rest[m.end(1)] if m.end(1) < len(masked_rest) else ""
            # Do not treat model-generation digits such as S1142 / HP4 as prices.
            if (prev.isascii() and prev.isalnum()) or (nxt.isascii() and nxt.isalnum()):
                continue
            price_matches.append(m)
        if not price_matches:
            return []
        first_price = price_matches[0]
        raw_model = rest[: first_price.start(1)]
        # Single-price Apple Watch cells in the copied sheet are usually MSRP
        # headers (e.g. `Watch Ultra3 官网6499元`, `S11 42...官5799`,
        # `2025款 SE 40mmGPS运动版1999`). The real quote rows follow as
        # descriptor / SKU / price triples and are parsed from TSV below.
        raw_norm = normalize_match_text(raw_model)
        if re.search(r"官网|官方价|官\s*$", raw_model, re.I):
            return []
        if brand == "苹果" and re.search(r"WATCH|S\d{1,2}|SE|\d{2}MM", raw_norm) and re.search(r"运动版|蜂窝|GPS", raw_model, re.I):
            return []
        model_spec = raw_model.strip(" -*_/，,")
        model_spec = re.sub(r"\b(?:官网价?|官方价?)\s*$", "", model_spec).strip(" -*_/，,")
        if not model_spec or re.search(r"官网|官方价", model_spec):
            return []
        entries = [_make_quote_entry(brand, model_spec, "默认", int(first_price.group(1)))]
        if nonphone_only:
            entries = [entry for entry in entries if quote_category(entry) not in {"phone", "unknown"}]
        return entries
    model_spec = re.sub(r"^(?:平板)?原封", "", model_spec)
    model_spec = re.sub(r"^平板", "", model_spec).strip(" -_/，,")
    if not model_spec:
        return []
    # Avoid internal workbook enum strings that happen to contain color chars and digits.
    model_norm = normalize_match_text(model_spec)
    if nonphone_only and not (NONPHONE_QUOTE_RE.search(model_spec) or NONPHONE_QUOTE_RE.search(model_norm)):
        return []
    if not re.search(r"\d|MATE|PURA|IPAD|PAD|AIR|GT|PRO|MAX|WATCH|BAND|BUDS|PENCIL|KEYBOARD|CASE|X|K|MINI|迷你|耳机|手表|手环|头戴|妙控|手写笔|键盘", model_spec, re.I):
        return []

    entries: list[dict[str, Any]] = []
    for m in matches:
        color = m.group("color").strip()
        price = int(m.group("price"))
        if not color or color in {"原"}:
            continue
        entries.append(_make_quote_entry(brand, model_spec, color, price))
    if nonphone_only:
        entries = [entry for entry in entries if quote_category(entry) not in {"phone", "unknown"}]
    return entries


def parse_quotes_from_open_et(open_et_bytes: bytes, *, nonphone_only: bool = False) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for idx, line in enumerate(extract_wps_utf16_strings(open_et_bytes)):
        for entry in parse_inline_quote_line(line, nonphone_only=nonphone_only):
            entry["row_y"] = 1_000_000 + idx
            entries.append(entry)
    return dedupe_quote_entries(entries)


def _header_brand(cell: str) -> str:
    s = cell or ""
    upper = s.upper()
    if "OPPO" in upper or "FIND" in upper or "RENO" in upper:
        return "OPPO"
    if "VIVO" in upper or "IQOO" in upper:
        return "VIVO"
    for brand in ("苹果", "华为", "小米", "红米", "荣耀", "三星", "一加", "大疆", "影石", "任天堂", "索尼"):
        if brand in s:
            return brand
    return ""


def _clean_watch_header(header: str) -> str:
    h = (header or "").strip()
    h = re.sub(r"官网\s*\d+\s*元?", "", h, flags=re.I)
    h = re.sub(r"官\s*\d+", "", h, flags=re.I)
    h = re.sub(r"((?:运动版|蜂窝))\s*\d{3,5}\s*$", r"\1", h, flags=re.I)
    h = re.sub(r"^S(\d{2})(\d{2})(?=铁|钛|金属|米兰)", r"S\1 \2", h, flags=re.I)
    return h.strip(" -_/，,")


def _looks_like_watch_header(cell: str) -> bool:
    norm = normalize_match_text(cell or "")
    if "官网" in (cell or "") or re.search(r"官\d{3,5}", cell or ""):
        return bool(re.search(r"WATCH|ULTRA|S\d{1,2}|SE|钛金属|米兰尼斯", norm, re.I))
    return bool(re.search(r"^(?:WATCHULTRA\d*|\d{4}款SE|S\d{1,2}\d{2})", norm, re.I))


def parse_watch_matrix_from_tsv_rows(rows: list[list[str]]) -> list[dict[str, Any]]:
    """Parse Apple Watch sections copied from KDocs TSV.

    KDocs renders Apple Watch as a matrix: a header cell (`S11 42 GPS...`)
    followed by rows of `descriptor / SKU code / price`. The binary workbook
    stream does not expose these as compact single-cell strings, so TSV copy is
    the reliable source.
    """
    entries: list[dict[str, Any]] = []
    contexts: dict[int, str] = {}
    for row_idx, row in enumerate(rows):
        for group_col in (0, 4):
            cell = row[group_col].strip() if group_col < len(row) else ""
            if cell and _looks_like_watch_header(cell):
                contexts[group_col] = _clean_watch_header(cell)
                if group_col == 0 and (4 >= len(row) or not (row[4] or "").strip()):
                    contexts[4] = contexts[group_col]
        for group_col in (0, 4):
            header = contexts.get(group_col, "")
            if not header or group_col + 2 >= len(row):
                continue
            desc = (row[group_col] or "").strip()
            code = (row[group_col + 1] or "").strip()
            price_text = (row[group_col + 2] or "").strip()
            if not desc or _looks_like_watch_header(desc) or not re.fullmatch(r"\d{2,5}", price_text):
                continue
            price = int(price_text)
            if price < 50 or price > 50000:
                continue
            if not re.search(r"\d{2}|色|米兰|回环|野径|海洋|S/M|M/L|GPS|蜂窝", desc, re.I):
                continue
            model_spec = f"{header} {desc}".strip()
            color = f"{desc} {code}".strip() if code else desc
            entries.append(_make_quote_entry("苹果", model_spec, color, price, row_y=2_000_000 + row_idx * 100 + group_col))
    return entries


def parse_quotes_from_copied_tsv(tsv: str, *, nonphone_only: bool = False) -> list[dict[str, Any]]:
    rows = [line.split("\t") for line in (tsv or "").splitlines()]
    entries: list[dict[str, Any]] = []
    col_brands: dict[int, str] = {}
    for row_idx, row in enumerate(rows):
        for col_idx, cell in enumerate(row):
            clean = (cell or "").strip()
            if not clean:
                continue
            brand_header = _header_brand(clean)
            if brand_header and re.search(r"系列|更新时间|线上", clean, re.I):
                col_brands[col_idx] = brand_header
                continue
            parsed = parse_inline_quote_line(clean, nonphone_only=nonphone_only)
            if not parsed and col_idx in col_brands:
                parsed = parse_inline_quote_line(f"{col_brands[col_idx]}{clean}", nonphone_only=nonphone_only)
            for entry in parsed:
                entry["row_y"] = 3_000_000 + row_idx * 1000 + col_idx
                entries.append(entry)
    watch_entries = parse_watch_matrix_from_tsv_rows(rows)
    if nonphone_only:
        watch_entries = [entry for entry in watch_entries if quote_category(entry) not in {"phone", "unknown"}]
    return dedupe_quote_entries([*entries, *watch_entries])


def discover_unknown_quote_candidates(open_et_bytes: bytes, *, limit: int = 20) -> list[dict[str, Any]]:
    """Return candidate quote lines that look device-related but are not ingested.

    This is the early-warning path for new KDocs categories/brands. It does not
    write synthetic quotes; it stores compact samples so the parser/catalog can
    be extended intentionally when the source adds a new section.
    """
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in extract_wps_utf16_strings(open_et_bytes):
        clean = _clean_wps_inline_text(line)
        if not clean or clean in seen:
            continue
        if not any(50 <= int(m['price']) <= 50000 for m in INLINE_COLOR_PRICE_RE.finditer(clean)):
            continue
        if not (POTENTIAL_QUOTE_CANDIDATE_RE.search(clean) or POTENTIAL_QUOTE_CANDIDATE_RE.search(normalize_match_text(clean))):
            continue
        parsed = parse_inline_quote_line(clean, nonphone_only=False)
        cats = sorted({quote_category(entry) for entry in parsed})
        if parsed and any(cat not in {"phone", "unknown"} for cat in cats):
            # Known non-phone category; it will be ingested by parse_quotes_from_open_et(..., nonphone_only=True).
            continue
        reason = "unknown_brand_or_layout" if not parsed else "unknown_category"
        candidates.append({"line": clean[:160], "reason": reason, "parsed_categories": cats})
        seen.add(clean)
        if len(candidates) >= limit:
            break
    return candidates


def replace_quotes(
    conn: sqlite3.Connection,
    entries: list[dict[str, Any]],
    source_url: str = SOURCE_URL,
    *,
    snapshot_hash: str | None = None,
    protect_removed_categories: bool = False,
    category_absence_confirmations: int = 2,
    before_commit=None,
) -> dict[str, Any]:
    ts = now_iso()
    existing_rows = list(
        conn.execute(
            "SELECT sku_key, brand, model_spec, color, product_name, quote_price, normalized, source_url, updated_at FROM quotes"
        )
    )
    existing = {row["sku_key"]: row for row in existing_rows}
    existing_category_by_sku = {row["sku_key"]: quote_category(row) for row in existing_rows}
    existing_snapshot = quote_category_snapshot(existing_rows)
    incoming_snapshot = quote_category_snapshot(entries)
    existing_categories = set(existing_snapshot["categories"].keys())
    incoming_categories = set(incoming_snapshot["categories"].keys())
    added_categories = sorted(cat for cat in incoming_categories if cat not in existing_categories)
    missing_categories = sorted(cat for cat in existing_categories if cat not in incoming_categories)

    try:
        missing_streaks = json.loads(get_meta(conn, "quotes_category_missing_streaks", "{}") or "{}")
        if not isinstance(missing_streaks, dict):
            missing_streaks = {}
    except json.JSONDecodeError:
        missing_streaks = {}
    category_absence_confirmations = max(1, int(category_absence_confirmations or 1))
    preserved_categories: set[str] = set()
    pending_removed_categories: dict[str, int] = {}
    confirmed_removed_categories: list[str] = []
    for cat in missing_categories:
        streak = int(missing_streaks.get(cat, 0) or 0) + 1
        if protect_removed_categories and streak < category_absence_confirmations:
            preserved_categories.add(cat)
            pending_removed_categories[cat] = streak
            missing_streaks[cat] = streak
        else:
            confirmed_removed_categories.append(cat)
            missing_streaks.pop(cat, None)
    for cat in incoming_categories:
        missing_streaks.pop(cat, None)

    seen: set[str] = set()
    inserted = 0
    updated = 0
    unchanged = 0

    def needs_update(old: sqlite3.Row | None, new: dict[str, Any]) -> bool:
        if old is None:
            return True
        comparable = ("brand", "model_spec", "color", "product_name", "quote_price", "normalized", "source_url")
        for key in comparable:
            old_val = old[key]
            new_val = new[key]
            if key == "quote_price":
                if int(old_val) != int(new_val):
                    return True
            else:
                if str(old_val) != str(new_val):
                    return True
        return False

    with conn:
        for entry in entries:
            row = {**entry, "source_url": source_url}
            sku = row["sku_key"]
            seen.add(sku)
            old = existing.get(sku)
            if needs_update(old, row):
                conn.execute(
                    """
                    INSERT INTO quotes(sku_key,brand,model_spec,color,product_name,quote_price,normalized,source_url,updated_at)
                    VALUES(:sku_key,:brand,:model_spec,:color,:product_name,:quote_price,:normalized,:source_url,:updated_at)
                    ON CONFLICT(sku_key) DO UPDATE SET
                        brand=excluded.brand,
                        model_spec=excluded.model_spec,
                        color=excluded.color,
                        product_name=excluded.product_name,
                        quote_price=excluded.quote_price,
                        normalized=excluded.normalized,
                        source_url=excluded.source_url,
                        updated_at=excluded.updated_at
                    """,
                    {**row, "updated_at": ts},
                )
                if old is None:
                    inserted += 1
                else:
                    updated += 1
            else:
                unchanged += 1
        missing_delete: list[str] = []
        preserved_missing = 0
        for sku in existing.keys():
            if sku in seen:
                continue
            if existing_category_by_sku.get(sku) in preserved_categories:
                preserved_missing += 1
                continue
            missing_delete.append(sku)
        if missing_delete:
            conn.executemany("DELETE FROM quotes WHERE sku_key=?", [(sku,) for sku in missing_delete])
        actual_rows = list(
            conn.execute(
                "SELECT sku_key, brand, model_spec, color, product_name, quote_price, normalized, source_url, updated_at FROM quotes"
            )
        )
        actual_snapshot = quote_category_snapshot(actual_rows)
        actual_count = int(actual_snapshot["total"])
        stats: dict[str, Any] = {
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "deleted": len(missing_delete),
            "preserved_missing": preserved_missing,
            "added_categories": added_categories,
            "pending_removed_categories": pending_removed_categories,
            "confirmed_removed_categories": confirmed_removed_categories,
            "category_absence_confirmations": category_absence_confirmations,
            "category_counts": {cat: data["count"] for cat, data in actual_snapshot["categories"].items()},
            "should_notify": bool(
                added_categories
                or confirmed_removed_categories
                or any(streak == 1 for streak in pending_removed_categories.values())
            ),
        }
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('quotes_updated_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (ts,),
        )
        conn.execute("INSERT OR REPLACE INTO meta VALUES('quotes_validated_at',?)", (ts,))
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('quotes_count',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(actual_count),),
        )
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('quotes_sync_stats',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(stats, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('quotes_category_snapshot',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(actual_snapshot, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('quotes_category_missing_streaks',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(missing_streaks, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('quotes_category_change',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(stats, ensure_ascii=False),),
        )
        if snapshot_hash is not None:
            conn.execute(
                "INSERT INTO meta(key,value) VALUES('quotes_snapshot_hash',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (snapshot_hash,),
            )
        if before_commit is not None:
            before_commit(stats)
    return stats


def load_quotes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM quotes ORDER BY quote_price DESC"))


def strip_non_price_numbers(text: str) -> str:
    """Mask dates/times and forum metadata before extracting prices.

    Chiphell appends snippets such as ``补充内容 (2026-5-6 22:30)`` to post
    excerpts. Without masking, the bare-number fallback can treat the year 2026
    as a sale price and inflate the apparent profit.
    """
    s = text or ""
    s = re.sub(r"补充内容\s*\([^)]{0,80}\)\s*:?", " ", s)
    s = re.sub(r"(?:发表于|编辑于|发布于|更新时间)\s*\d{4}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?(?:\s+\d{1,2}:\d{2})?", " ", s)
    s = re.sub(r"\b20\d{2}[-/.年]\d{1,2}(?:[-/.月]\d{1,2}日?)?(?:\s+\d{1,2}:\d{2})?", " ", s)
    s = re.sub(r"\b\d{1,2}:\d{2}\b", " ", s)
    return s


def money_candidates(text: str) -> list[int]:
    s = strip_non_price_numbers(text)
    values: list[int] = []
    for m in re.finditer(r"[¥￥]\s*(\d+(?:\.\d+)?)\s*([wW万kK])?", s):
        n = float(m.group(1)); unit = (m.group(2) or "").lower()
        values.append(int(n * (10000 if unit in {"w", "万"} else 1000 if unit == "k" else 1)))
    for m in re.finditer(r"(?<![\d+])(\d+(?:\.\d+)?)\s*([wW万])", s):
        values.append(int(float(m.group(1)) * 10000))
    for m in re.finditer(r"(?<![\d+])(\d+(?:\.\d+)?)\s*([kK])", s):
        values.append(int(float(m.group(1)) * 1000))
    for m in re.finditer(r"(?<![\d+])(\d{3,6})(?:\s*(?:元|块|RMB|rmb|人民币))", s):
        values.append(int(m.group(1)))
    for m in re.finditer(r"(?:明盘|售价|价格|挂价|挂|出|卖|可刀|自刀|包邮|闲鱼)[^\d¥￥]{0,40}(\d{4,5})(?!\d)", s):
        values.append(int(m.group(1)))
    # In marketplace posts the asking price is often a bare 4-5 digit number
    # near the model/spec. Capacities like 256G/1T are excluded by adjacency.
    for m in re.finditer(r"(?<![\d+])(\d{4,5})(?!\s*[GgTt款年代]|\d|\+)", s):
        values.append(int(m.group(1)))
    return sorted({v for v in values if 100 <= v <= 50000})


def text_has_brand(text_norm: str, brand: str) -> bool:
    for alias in BRAND_ALIASES.get(brand, [brand]):
        if normalize_text(alias) in text_norm or normalize_match_text(alias) in text_norm:
            return True
    return False


NOISY_QUOTE_SPEC_RE = re.compile(r"(收货地址|地址：|联系人|联系电话|电话|微信|QQ|赛格|珠宝城)", re.I)
INCOMPLETE_CONFIG_QUOTE_RE = re.compile(r"(?:^|[^\d])\d{1,2}\+\s*(?:$|[^\d])")
WEAK_MATCH_TOKENS = {"标准", "默认", "套装", "GPS", "WIFI", "CELLULAR", "USBC", "ANC"}
REQUIRED_EDITION_TOKENS = {"非凡大师", "典藏", "柔光", "活力", "插卡"}


def _unique_ordered(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


def capacity_tokens(text: str) -> list[str]:
    """Extract storage/config tokens without being fooled by collapsed model text."""
    raw = (text or "").upper()
    raw = raw.replace("１", "1").replace("２", "2").replace("３", "3").replace("４", "4").replace("５", "5")
    raw = raw.replace("６", "6").replace("７", "7").replace("８", "8").replace("９", "9").replace("０", "0")
    raw = raw.replace("＋", "+").replace("Ｔ", "T").replace("Ｇ", "G")
    raw = re.sub(r"(\d+)T+BG?", r"\1T", raw)
    raw = re.sub(r"(\d+)TB", r"\1T", raw)
    raw = re.sub(r"(\d+)GB", r"\1G", raw)
    combos: list[str] = []
    for m in re.finditer(r"(?<!\d)(\d{1,2})\s*G?\s*\+\s*(\d{1,4})\s*([TG])?", raw):
        ram = m.group(1)
        storage = m.group(2)
        unit = "T" if (m.group(3) == "T" or (not m.group(3) and storage in {"1", "2", "4"})) else ""
        combos.append(f"{ram}+{storage}{unit}")
    if combos:
        return _unique_ordered(combos)
    storage_tokens: list[str] = []
    for m in re.finditer(r"(?<![A-Z0-9])((?:128|256|512|1024)G?|[124]T)(?![A-Z0-9])", raw):
        token = m.group(1)
        if token == "1024G":
            token = "1T"
        elif token.endswith("G"):
            token = token[:-1]
        storage_tokens.append(token)
    return _unique_ordered(storage_tokens)


def model_anchor_tokens(text: str) -> list[str]:
    """Return model identity anchors that must match exactly across quote/post text.

    These anchors intentionally keep short alphanumeric model names intact
    (X5, NS2, PS5, S25PLUS, MATEX5, TURBO6V) so bare digits from dates,
    prices, addresses or capacities cannot satisfy a model match.
    """
    norm = normalize_match_text(text)
    patterns = [
        r"MATEXTS",
        r"MATEXT\d*(?![S\d])",
        r"MATEX\d+[A-Z]*?(?=\d{1,2}\+|$|典藏|标准|普通|全能|黑|白|蓝|紫|绿|金|银|灰|红|青)",
        r"MATE\d{2,3}(?:RS|PROMAX|PRO|ULTRA)?(?=\d{1,2}\+|\d{3,4}G?|\dT|$|典藏|标准|普通|非凡大师|黑|白|蓝|紫|绿|金|银|灰|红|青)",
        r"PURA(?:XMAX|X|\d{1,3}(?:ULTRA|PROMAX|PRO|MAX)?)(?=\d{1,2}\+|\d{3,4}G?|\dT|$|典藏|标准|普通|黑|白|蓝|紫|绿|金|银|灰|红|青)",
        r"\d{1,3}(?:PROMAX|PLUS|PRO|MAX|ULTRA|E)(?=\d{1,2}\+|\d{3,4}G?|\dT|$|黑|白|蓝|紫|绿|金|银|灰|红|青)",
        r"S\d{1,2}(?=\d{2}|MM|GPS|运动|米兰|回环|野径|海洋)",
        r"S\d{1,3}(?:ULTRA|PLUS|\+)?(?=\d{1,2}\+|\d{2}MM|\d{2}GPS|\d{3,4}G?|\dT|$|运动|米兰|回环|野径|海洋|黑|白|蓝|紫|绿|金|银|灰|红|青)",
        r"(?:ACEPRO|ACE|TURBO|MAGICV|MAGIC|RENO|POWER|NOTE|WINRT|WIN|POCKET|MINI|K)\d+[A-Z]*?(?=\d{1,2}\+|\d{3,4}G?|\dT|$|至尊|竞速|标准|普通|黑|白|蓝|紫|绿|金|银|灰|红|青)",
        r"(?:(?<![A-Z0-9])|(?<=INSTA360))X\d+[A-Z0-9]*?(?=\d{1,2}\+|\d{3,4}G?|\dT|$|标准|全能|普通|黑|白|蓝|紫|绿|金|银|灰|红|青)",
        r"(?:PS|NS)\d+[A-Z]*",
        r"GO\d+[A-Z0-9]*",
        r"GT\d+[A-Z]*",
        r"WATCH(?:GT|FIT)?\d*[A-Z]*",
        r"BAND\d+[A-Z]*",
        r"BUDS\d*[A-Z]*",
        r"PAD(?:PRO|AIR|MINI)?\d{1,2}(?:INCH)?",
    ]
    anchors: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, norm):
            token = m.group(0).replace("+", "PLUS")
            if re.fullmatch(r"\d{1,3}MAX", token):
                token = token.replace("MAX", "PROMAX")
            anchors.append(token)
    return _unique_ordered(anchors)


def model_tokens(model_spec: str) -> list[str]:
    norm = normalize_match_text(model_spec)
    tokens = re.findall(r"\d+\+\d+T?|\d+PROMAX|\d+PLUS|\d+PRO|\d+ULTRA|\d+E|\d+MM|\d+INCH|\d+G|\d+T|PAD\d{1,2}|ACEPRO\d*|GO\d+[A-Z]*|X\d+[A-Z0-9]*|S\d+(?:ULTRA|PLUS|\+)?|D\d+|M\d+|GT\d*|MATE[A-Z0-9]*|XTS|PURA|WATCH|BAND|PAD|BUDS|PENCIL|KEYBOARD|CASE|FIT|MINI|GPS|CELLULAR|WIFI|USBC|ANC|PROMAX|PLUS|PRO|MAX|ULTRA|AIR|SE|RS|非凡大师|典藏|柔光|灵动|活力|标准|套装|插卡|X|\d+", norm)
    tokens = model_anchor_tokens(model_spec) + [t.replace("+", "PLUS") for t in tokens]
    # Keep stable order and drop weak standalone X unless accompanied by Pura.
    out: list[str] = []
    for t in tokens:
        if t == "X" and "PURA" not in tokens:
            continue
        if t not in out:
            out.append(t)
    return out


def _mate_signature(norm: str) -> tuple[str, str] | None:
    m = re.search(r"MATE(\d+)(RS|PROMAX|PRO)?", norm)
    if not m:
        return None
    gen = m.group(1)
    suffix = m.group(2) or "BASE"
    return gen, suffix


def _numeric_spec_signature(tokens: list[str]) -> tuple[str, str] | None:
    gen = ""
    variant = "BASE"
    for token in tokens:
        m = re.fullmatch(r"(\d+)(PROMAX|PRO|ULTRA|E)", token)
        if m:
            return m.group(1), m.group(2)
        if re.fullmatch(r"\d+", token) and not gen:
            gen = token
    if gen:
        if "MAX" in tokens:
            variant = "PROMAX"
        elif "ULTRA" in tokens:
            variant = "ULTRA"
        elif "PRO" in tokens:
            variant = "PRO"
        elif "AIR" in tokens:
            variant = "AIR"
        return gen, variant
    if "AIR" in tokens:
        return "AIR", "AIR"
    return None


def _numeric_text_variant(text_norm: str, gen: str) -> str | None:
    if gen == "AIR":
        return "AIR" if "AIR" in text_norm else None
    for suffix, variant in (("PROMAX", "PROMAX"), ("MAX", "PROMAX"), ("ULTRA", "ULTRA"), ("PRO", "PRO"), ("E", "E")):
        if re.search(rf"(?<!\d){re.escape(gen + suffix)}", text_norm):
            return variant
    return None


def model_variant_conflicts(brand: str, spec: str, spec_tokens: list[str], text_norm: str) -> bool:
    spec_norm = normalize_match_text(spec)
    if brand == "华为":
        spec_sig = _mate_signature(spec_norm)
        text_sig = _mate_signature(text_norm)
        if spec_sig and text_sig and spec_sig[0] == text_sig[0] and spec_sig[1] != text_sig[1]:
            return True
    if brand in {"苹果", "小米"}:
        spec_sig = _numeric_spec_signature(spec_tokens)
        if not spec_sig:
            return False
        text_variant = _numeric_text_variant(text_norm, spec_sig[0])
        if text_variant and text_variant != spec_sig[1]:
            return True
    return False


def quote_matches_text(q: sqlite3.Row, text_norm: str, raw_text: str | None = None) -> bool:
    brand = str(q["brand"])
    if not text_has_brand(text_norm, brand):
        return False
    # Color detection is intentionally lightweight, but avoid treating material
    # words such as `金属` as a color constraint. Rows with color=`默认` have no
    # reliable color dimension, so color words appearing in the model/spec should
    # not reject them.
    color_text_norm = re.sub(r"金属", "", text_norm)
    text_colors = {c for c in COLOR_TOKENS if c in color_text_norm}
    quote_color = normalize_text(str(q["color"]))
    if quote_color not in {"", "默认"} and text_colors and not any(c in quote_color for c in text_colors):
        return False
    spec = str(q["model_spec"])
    product_name = str(_row_get(q, "product_name", ""))
    if NOISY_QUOTE_SPEC_RE.search(f"{spec} {product_name}"):
        return False
    if INCOMPLETE_CONFIG_QUOTE_RE.search(f"{spec} {product_name}"):
        return False
    text_source = raw_text if raw_text is not None else text_norm
    quote_anchors = set(model_anchor_tokens(spec))
    text_anchors = set(model_anchor_tokens(text_source))
    if quote_anchors and not quote_anchors.intersection(text_anchors):
        return False
    quote_caps = set(capacity_tokens(spec))
    if quote_caps and not quote_caps.intersection(capacity_tokens(text_source)):
        return False
    compact_spec = normalize_match_text(spec)
    toks = model_tokens(spec)
    if not toks:
        return False
    if model_variant_conflicts(brand, spec, toks, text_norm):
        return False
    text_toks = set(model_tokens(text_source))
    required_editions = {t for t in toks if t in REQUIRED_EDITION_TOKENS}
    if required_editions and not required_editions.issubset(text_toks):
        return False
    text_required_editions = {t for t in text_toks if t in REQUIRED_EDITION_TOKENS}
    if text_required_editions and not text_required_editions.issubset(set(toks)):
        return False
    if quote_anchors:
        return True
    if compact_spec and len(compact_spec) >= 4 and compact_spec in text_norm:
        return True
    # Capacity/config token is required when present; otherwise model-only matches are too loose.
    cap_tokens = [t for t in toks if re.search(r"\d+\+\d+|\d+G|\d+T", t)]
    if cap_tokens and not any(t.replace("TB", "T") in text_norm for t in cap_tokens):
        return False
    strong = [t for t in toks if t not in cap_tokens and t not in WEAK_MATCH_TOKENS and not re.fullmatch(r"\d{1,2}", t)]
    if not strong:
        return False
    if brand == "苹果" and "AIR" in strong and "PAD" not in strong and "IPHONE" not in text_norm and "手机" not in text_norm:
        return False
    return all(t in text_norm for t in strong)


def best_arbitrage_match_from_quotes(quotes: list[sqlite3.Row], title: str, body: str, *, assume_sale: bool = False) -> dict[str, Any] | None:
    from .matching import select_match
    return select_match(quotes, title, body, assume_sale=assume_sale)


def best_arbitrage_match(conn: sqlite3.Connection, title: str, body: str, *, assume_sale: bool = False) -> dict[str, Any] | None:
    return best_arbitrage_match_from_quotes(load_quotes(conn), title, body, assume_sale=assume_sale)


def format_money(n: int) -> str:
    return f"¥{n:,}".replace(",", "")


def _normalize_contact_label(label: str) -> str:
    raw = (label or "").strip()
    if re.search(r"QQ|加Q", raw, re.I):
        return "QQ"
    if re.search(r"微信|VX|WX|WeChat|加V", raw, re.I):
        return "微信"
    return "联系方式"


def extract_plaintext_contacts(text: str, limit: int = 3) -> list[dict[str, str]]:
    contacts: list[dict[str, str]] = []
    seen: set[str] = set()
    hay = text or ""
    for match in re.finditer(
        r"(?P<label>微信|VX|WX|WeChat|QQ|加Q|加V|联系方式|联系)\s*[:：]?\s*(?P<value>[A-Za-z0-9_@.+-]{3,64})",
        hay,
        re.I,
    ):
        value = match.group("value").strip()
        if not value or value in seen:
            continue
        if _decode_base64_candidate(value) is not None:
            continue
        contacts.append({"label": _normalize_contact_label(match.group("label")), "value": value})
        seen.add(value)
        if len(contacts) >= limit:
            break
    return contacts


def _decode_base64_candidate(token: str) -> str | None:
    raw = (token or "").strip().strip("，。；;：:,.()[]{}<>")
    if len(raw) < 8 or len(raw) > 120:
        return None
    if len(raw) % 4 == 1:
        return None
    if raw.isdigit():
        return None
    padded = raw + "=" * ((4 - len(raw) % 4) % 4)
    try:
        decoded_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return None
    if not decoded_bytes or len(decoded_bytes) > 96:
        return None
    try:
        decoded = decoded_bytes.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not (3 <= len(decoded) <= 64):
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in decoded):
        return None
    # Contacts hidden with base64 in these posts are usually WeChat/QQ handles.
    # Keep this strict so product specs or random ASCII are not surfaced.
    if not re.fullmatch(r"[A-Za-z0-9_@.+-]{3,64}", decoded):
        return None
    return decoded


def extract_decoded_base64_contacts(text: str, limit: int = 3) -> list[dict[str, str]]:
    contacts: list[dict[str, str]] = []
    seen: set[str] = set()
    hay = text or ""
    for match in BASE64_TOKEN_RE.finditer(hay):
        token = match.group(1)
        decoded = _decode_base64_candidate(token)
        if not decoded or decoded in seen:
            continue
        before = hay[max(0, match.start() - 50) : match.start()]
        after = hay[match.end() : min(len(hay), match.end() + 50)]
        window = f"{before} {after}"
        if WECHAT_CONTEXT_RE.search(window):
            label = "微信"
        elif QQ_CONTEXT_RE.search(window):
            label = "QQ"
        elif CONTACT_CONTEXT_RE.search(window):
            label = "联系方式"
        else:
            label = "疑似联系方式"
        contacts.append({"label": label, "encoded": token, "decoded": decoded})
        seen.add(decoded)
        if len(contacts) >= limit:
            break
    return contacts


def format_contact_note(text: str) -> str:
    existing = _contact_note_from_alert_message(text)
    if existing:
        return existing

    plain_contacts = extract_plaintext_contacts(text)
    if plain_contacts:
        parts = [f"{item['label']}：{item['value']}（明文）" for item in plain_contacts]
        return " / ".join(parts)

    contacts = extract_decoded_base64_contacts(text)
    if not contacts:
        return ""
    parts = []
    for item in contacts:
        label = item["label"]
        decoded = item["decoded"]
        parts.append(f"{label}：{decoded}")
    return f"{' / '.join(parts)}（Base64 解码）"


def format_decoded_base64_contact_note(text: str) -> str:
    return format_contact_note(text)


def _contact_note_from_alert_message(message: str) -> str:
    match = re.search(r"((?:疑似联系方式|联系方式|微信|QQ)(?::|：)[^。\n]+(?:（明文）|（Base64 解码）))", message or "")
    return match.group(1) if match else ""


def _as_repeat_alert_message(message: str) -> str:
    lines = (message or "").splitlines()
    if not lines:
        return ""
    if lines[0].startswith("## 重点比价预警"):
        lines[0] = "## 重点比价预警（重复提醒）"
    return "\n".join(lines).strip()


def make_alert_message(title: str, url: str, match: dict[str, Any], repeat: bool = False, contact_note: str = "") -> str:
    head = "## 重点比价预警（重复提醒）" if repeat else "## 重点比价预警"
    contact_suffix = f"{contact_note}。" if contact_note else ""
    return (
        f"{head}\n"
        f"[{title}]({url}) — 匹配：{match['product_name']}；帖子价 {format_money(match['sale_price'])}，"
        f"报价 {format_money(match['quote_price'])}，差价 {format_money(match['profit'])}。依据：{match.get('condition_evidence', '全新状态已核对')}。{contact_suffix}"
    )


def make_bark_arbitrage_payload(title: str, url: str, match: dict[str, Any], repeat: bool = False, contact_note: str = "") -> tuple[str, str]:
    prefix = "重复提醒" if repeat else "重点比价预警"
    push_title = f"{prefix}：差价 {format_money(int(match['profit']))}"
    body = (
        f"匹配：{match['product_name']}\n"
        f"帖子价：{format_money(int(match['sale_price']))}\n"
        f"报价：{format_money(int(match['quote_price']))}\n"
        f"差价：{format_money(int(match['profit']))}\n"
        f"帖子：{title}\n"
        f"依据：{match.get('condition_evidence', '全新状态已核对')}"
    )
    if contact_note:
        body += f"\n{contact_note}"
    if url:
        body += f"\n链接：{url}"
    return push_title, body


def log_bark_result(context: dict[str, Any], result: BarkResult) -> None:
    if result.ok:
        return
    try:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        with (DATA_ROOT / "bark_failures.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": now_iso(),
                        "context": context,
                        "http_status": result.http_status,
                        "code": result.code,
                        "message": result.message,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    except Exception:
        pass


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1, maximum: int = 100) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float = 0.0, maximum: float = 60.0) -> float:
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def bark_burst_settings() -> tuple[int, float]:
    # Dry-run tests should not sleep or simulate repeated pushes unless explicitly requested.
    default_count = 1 if _env_truthy("BARK_DRY_RUN") else DEFAULT_BARK_BURST_COUNT
    count = _env_int("ARBITRAGE_BARK_BURST_COUNT", default_count, 1, MAX_BARK_BURST_COUNT)
    interval = _env_float("ARBITRAGE_BARK_BURST_INTERVAL_SECONDS", DEFAULT_BARK_BURST_INTERVAL_SECONDS, 0.0, MAX_BARK_BURST_INTERVAL_SECONDS)
    return count, interval


def send_bark_arbitrage_alert(title: str, url: str, match: dict[str, Any], repeat: bool = False, contact_note: str = "") -> BarkResult:
    push_title, body = make_bark_arbitrage_payload(title, url, match, repeat=repeat, contact_note=contact_note)
    result = send_critical_push(
        title=push_title,
        body=body,
        url=url,
        group="electronics-arbitrage",
    )
    log_bark_result(
        {
            "repeat": repeat,
            "url": url,
            "title": title,
            "product_name": str(match.get("product_name") or ""),
            "profit": int(match.get("profit") or 0),
        },
        result,
    )
    return result


def send_bark_arbitrage_burst_alert(title: str, url: str, match: dict[str, Any], contact_note: str = "") -> BarkResult:
    count, interval = bark_burst_settings()
    last_result: BarkResult | None = None
    for i in range(count):
        previous_key = os.environ.get("PYLON_BARK_KEY")
        os.environ["PYLON_BARK_KEY"] = f"arbitrage:{url}:{match.get('product_name')}:{match.get('sale_price')}:{match.get('quote_price')}:{i}"
        try:
            last_result = send_bark_arbitrage_alert(title, url, match, repeat=i > 0, contact_note=contact_note)
        finally:
            if previous_key is None:
                os.environ.pop("PYLON_BARK_KEY", None)
            else:
                os.environ["PYLON_BARK_KEY"] = previous_key
        if i < count - 1 and interval > 0:
            time.sleep(interval)
    assert last_result is not None
    return last_result


def _record_arbitrage_if_match_conn(
    conn: sqlite3.Connection,
    source: str,
    source_id: str,
    url: str,
    title: str,
    body: str,
    quotes: list[sqlite3.Row],
) -> str | None:
    if not quotes:
        return None
    assume_sale = source.lower().startswith("chiphell")
    match = best_arbitrage_match_from_quotes(quotes, title, body, assume_sale=assume_sale)
    if not match:
        return None
    alert_key = hashlib.sha1(f"{source}|{source_id}|{match['sku_key']}".encode("utf-8")).hexdigest()[:24]
    contact_note = format_contact_note(f"{title}\n{body}")
    message = make_alert_message(title, url, match, repeat=False, contact_note=contact_note)
    ts = now_iso()
    send_bark_arbitrage_burst_alert(title, url, match, contact_note=contact_note)
    with conn:
        conn.execute(
            """
            INSERT INTO arbitrage_alerts(alert_key,source,source_id,url,title,product_name,quote_price,sale_price,profit,first_seen_at,last_sent_at,repeat_count,status,message)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(alert_key) DO UPDATE SET
                last_sent_at=excluded.last_sent_at,
                repeat_count=arbitrage_alerts.repeat_count+1,
                status='active',
                message=excluded.message
            """,
            (
                alert_key,
                source,
                source_id,
                url,
                title,
                match["product_name"],
                int(match["quote_price"]),
                int(match["sale_price"]),
                int(match["profit"]),
                ts,
                ts,
                1,
                "active",
                message,
            ),
        )
    return message


def record_arbitrage_if_match(
    source: str,
    source_id: str,
    url: str,
    title: str,
    body: str,
    db_path: Path = DB_PATH,
) -> str | None:
    if db_path == DB_PATH:
        from .health import inspect
        if not inspect(db_path)['available']:
            return None
    with connect(db_path) as conn:
        if int(get_meta(conn, "quotes_count", "0") or "0") <= 0:
            return None
        return _record_arbitrage_if_match_conn(conn, source, source_id, url, title, body, load_quotes(conn))


def due_repeat_alerts(db_path: Path = DB_PATH, max_repeats: int = 8, min_minutes: int = 15) -> list[str]:
    with connect(db_path) as conn:
        now = dt.datetime.now(dt.timezone.utc)
        rows = conn.execute(
            "SELECT * FROM arbitrage_alerts WHERE status='active' AND repeat_count < ? ORDER BY profit DESC, first_seen_at ASC LIMIT 5",
            (max_repeats,),
        ).fetchall()
        messages: list[str] = []
        with conn:
            for row in rows:
                last_s = row["last_sent_at"]
                due = True
                if last_s:
                    try:
                        last = dt.datetime.fromisoformat(str(last_s).replace("Z", "+00:00"))
                        due = (now - last).total_seconds() >= min_minutes * 60
                    except Exception:
                        due = True
                if not due:
                    continue
                match = {"product_name": row["product_name"], "sale_price": row["sale_price"], "quote_price": row["quote_price"], "profit": row["profit"]}
                stored_message = str(row["message"] or "")
                msg = _as_repeat_alert_message(stored_message) or make_alert_message(row["title"], row["url"], match, repeat=True)
                messages.append(msg)
                send_bark_arbitrage_alert(
                    row["title"],
                    row["url"],
                    match,
                    repeat=True,
                    contact_note=_contact_note_from_alert_message(stored_message),
                )
                conn.execute(
                    "UPDATE arbitrage_alerts SET repeat_count=repeat_count+1,last_sent_at=? WHERE alert_key=?",
                    (now_iso(), row["alert_key"]),
                )
        return messages


def collect_arbitrage_messages(source: str, posts: list[dict[str, Any]], db_path: Path = DB_PATH) -> list[str]:
    if db_path == DB_PATH:
        from .health import inspect
        if not inspect(db_path)['available']:
            return []
    if not posts:
        return []
    messages: list[str] = []
    with connect(db_path) as conn:
        if int(get_meta(conn, "quotes_count", "0") or "0") <= 0:
            return []
        quotes = load_quotes(conn)
        for post in posts:
            if not post.get('body_complete') or not post.get('body_full'):
                continue
            msg = _record_arbitrage_if_match_conn(
                conn=conn,
                source=source,
                source_id=str(post.get("id") or post.get("message_id") or post.get("url") or post.get("v2ex_url") or ""),
                url=str(post.get("url") or post.get("v2ex_url") or ""),
                title=str(post.get("title") or ""),
                body=str(post['body_full']),
                quotes=quotes,
            )
            if msg:
                messages.append(msg)
    return messages


def format_alerts(messages: list[str]) -> str:
    if not messages:
        return ""
    if len(messages) == 1:
        return messages[0]
    lines = ["## 重点比价预警"]
    for msg in messages:
        body = "\n".join(msg.splitlines()[1:]).strip()
        if body:
            lines.append(body)
    return "\n".join(lines).strip()
