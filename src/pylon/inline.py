"""Require a signed action to originate on a card delivered by this bot."""
import json

from .config import profile_env
from .outbox import Outbox


def owns_callback(profile, payload, *, box=None):
    message = payload.get('message') or {}
    sender, chat = payload.get('from') or {}, message.get('chat') or {}
    owner = str(profile_env(profile).get('TELEGRAM_HOME_CHANNEL') or '')
    mid = message.get('message_id')
    if (not owner or chat.get('type') != 'private' or str(chat.get('id')) != owner
            or str(sender.get('id')) != owner or sender.get('is_bot') or type(mid) is not int):
        return False
    box = box or Outbox()
    with box.connect() as db:
        rows = db.execute("SELECT payload,receipt FROM events WHERE profile=? AND receipt IS NOT NULL "
                          "AND (json_extract(receipt,'$[#-1].message_id')=? OR json_extract(receipt,'$.message_id')=?)",
                          (profile, mid, mid)).fetchall()
    for row in rows:
        receipts = json.loads(row['receipt'])
        receipt = receipts[-1] if isinstance(receipts, list) and receipts else receipts
        if not isinstance(receipt, dict) or str(receipt.get('chat_id')) != owner:
            continue
        buttons = json.loads(row['payload']).get('reply_markup', {}).get('inline_keyboard', [])
        if any(button.get('callback_data') == payload.get('data') for line in buttons for button in line):
            return True
    return False


def signature(profile: str, action: str, target: str) -> str:
    """Stable callback signing, independent of any business extension."""
    import hashlib
    import hmac
    secret = str(profile_env(profile).get('TELEGRAM_BOT_TOKEN') or 'pylon').encode()
    return hmac.new(secret, f'{profile}|{action}|{target}'.encode(), hashlib.sha256).hexdigest()[:12]
