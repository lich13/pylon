"""One validity boundary for persisted quotes, health and notifications."""
import re

PLACEHOLDER = re.compile(r'^(?:0+|[-—–*/\s]+|无|空|暂无|缺货|无货|售罄|待定|待报价|N/?A|null|none)$', re.I)


def valid_quote(row) -> bool:
    row = dict(row)
    for field in ('sku_key', 'brand', 'model_spec', 'product_name', 'normalized'):
        value = str(row.get(field) or '').strip()
        if not value or PLACEHOLDER.fullmatch(value):
            return False
    for field in ('status', 'availability'):
        if re.search(r'缺货|无货|售罄|out.of.stock|unavailable', str(row.get(field, '')), re.I):
            return False
    price = row.get('quote_price')
    if isinstance(price, bool) or not re.fullmatch(r'[1-9]\d*', str(price or '')):
        return False
    return 50 <= int(price) <= 50000


def valid_match(match) -> bool:
    name = str((match or {}).get('product_name') or '').strip()
    if not name or PLACEHOLDER.fullmatch(name):
        return False
    try:
        sale, quote = int(match['sale_price']), int(match['quote_price'])
        return 0 < sale < quote <= 50000 and int(match['profit']) == quote - sale
    except (ValueError, TypeError, KeyError):
        return False
