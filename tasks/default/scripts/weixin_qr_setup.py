#!/usr/bin/env python3
"""One-shot Weixin QR setup for the private Hermes profile.

Fetches an iLink QR code, saves it as a PNG for Telegram delivery, waits for
scan/confirmation, then writes WEIXIN_* settings into this profile's .env.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

PROFILE_HOME = Path("/srv/hermes")
os.environ["HERMES_HOME"] = str(PROFILE_HOME)
PROJECT_ROOT = Path("/srv/hermes/hermes-agent")
sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli.config import save_env_value  # noqa: E402
from gateway.platforms.weixin import (  # noqa: E402
    EP_GET_BOT_QR,
    EP_GET_QR_STATUS,
    ILINK_BASE_URL,
    QR_TIMEOUT_MS,
    WEIXIN_CDN_BASE_URL,
    _api_get,
    _make_ssl_connector,
    save_weixin_account,
)


def emit(tag: str, payload) -> None:
    print(f"__WEIXIN_{tag}__{json.dumps(payload, ensure_ascii=False)}", flush=True)


async def main() -> int:
    import aiohttp
    import qrcode

    status_path = PROFILE_HOME / "weixin_setup_status.json"
    qr_png = PROFILE_HOME / "weixin_qr.png"

    def write_status(data: dict) -> None:
        status_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        try:
            qr_resp = await _api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type=3",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except Exception as exc:
            write_status({"state": "error", "error": f"fetch_qr_failed: {exc}"})
            emit("ERROR", {"step": "fetch_qr", "error": str(exc)})
            return 2

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            write_status({"state": "error", "error": "QR response missing qrcode"})
            emit("ERROR", {"step": "fetch_qr", "error": "QR response missing qrcode"})
            return 2

        qr_scan_data = qrcode_url if qrcode_url else qrcode_value
        img = qrcode.make(qr_scan_data)
        img.save(qr_png)
        try:
            qr_png.chmod(0o600)
        except OSError:
            pass

        write_status({
            "state": "waiting_scan",
            "qr_png": str(qr_png),
            "qr_url": qr_scan_data,
            "created_at": time.time(),
        })
        emit("QR", {"png": str(qr_png), "url": qr_scan_data})

        deadline = time.time() + 600
        current_base_url = ILINK_BASE_URL
        last_status = None
        while time.time() < deadline:
            try:
                status_resp = await _api_get(
                    session,
                    base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout_ms=QR_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                write_status({"state": "poll_warning", "error": str(exc), "updated_at": time.time()})
                emit("POLL_WARNING", {"error": str(exc)})
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")
            if status != last_status:
                last_status = status
                emit("STATUS", {"status": status})
                write_status({
                    "state": status,
                    "qr_png": str(qr_png),
                    "qr_url": qr_scan_data,
                    "updated_at": time.time(),
                })

            if status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
            elif status == "expired":
                write_status({"state": "expired", "qr_png": str(qr_png), "updated_at": time.time()})
                emit("EXPIRED", {"message": "QR expired"})
                return 3
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    write_status({"state": "error", "error": "confirmed but account/token missing"})
                    emit("ERROR", {"step": "confirmed", "error": "account/token missing"})
                    return 4

                save_weixin_account(
                    str(PROFILE_HOME),
                    account_id=account_id,
                    token=token,
                    base_url=base_url,
                    user_id=user_id,
                )
                save_env_value("WEIXIN_ACCOUNT_ID", account_id)
                save_env_value("WEIXIN_TOKEN", token)
                save_env_value("WEIXIN_BASE_URL", base_url)
                save_env_value("WEIXIN_CDN_BASE_URL", WEIXIN_CDN_BASE_URL)

                # Safe defaults: unknown DMs must be paired/approved, groups disabled.
                save_env_value("WEIXIN_DM_POLICY", "pairing")
                save_env_value("WEIXIN_ALLOW_ALL_USERS", "false")
                save_env_value("WEIXIN_ALLOWED_USERS", "")
                save_env_value("WEIXIN_GROUP_POLICY", "disabled")
                save_env_value("WEIXIN_GROUP_ALLOWED_USERS", "")
                if user_id:
                    save_env_value("WEIXIN_HOME_CHANNEL", user_id)
                    save_env_value("WEIXIN_HOME_CHANNEL_NAME", "Home")

                result = {
                    "state": "confirmed",
                    "account_id": account_id,
                    "user_id": user_id,
                    "base_url": base_url,
                    "env_path": str(PROFILE_HOME / ".env"),
                    "account_file": str(PROFILE_HOME / "weixin" / "accounts" / f"{account_id}.json"),
                    "updated_at": time.time(),
                }
                write_status(result)
                emit("CONFIRMED", result)
                return 0

            await asyncio.sleep(1)

    write_status({"state": "timeout", "updated_at": time.time()})
    emit("TIMEOUT", {"message": "login timed out"})
    return 5


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
