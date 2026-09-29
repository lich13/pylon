#!/srv/pylon/venv/bin/python
import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

PYLON_PROFILE_ROOT = Path(os.getenv("PYLON_PROFILE_ROOT", "/srv/pylon/profiles/default"))
CLIENT_SECRET_PATH = PYLON_PROFILE_ROOT / "google_client_secret.json"
TOKEN_PATH = PYLON_PROFILE_ROOT / "google_token.json"
PENDING_AUTH_PATH = PYLON_PROFILE_ROOT / "gmail_cleanup_oauth_pending.json"
LAST_URL_PATH = PYLON_PROFILE_ROOT / "gmail_cleanup_oauth_last_url.txt"
# Request full Gmail scope for permanent deletion, plus the existing Workspace
# scopes so this flow does not clobber Calendar/Drive/Sheets/Docs access when
# it overwrites the shared profile token at google_token.json.
SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/contacts.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents.readonly",
]
REDIRECT_URI = "http://localhost:1"


def _normalize_authorized_user_payload(payload: dict) -> dict:
    payload = dict(payload)
    payload.setdefault("type", "authorized_user")
    return payload


def _save_pending_auth(state: str, code_verifier: str) -> None:
    PENDING_AUTH_PATH.write_text(
        json.dumps(
            {
                "state": state,
                "code_verifier": code_verifier,
                "redirect_uri": REDIRECT_URI,
            },
            indent=2,
        )
    )


def _load_pending_auth() -> dict:
    if not PENDING_AUTH_PATH.exists():
        raise RuntimeError("No pending OAuth session found. Run --auth-url first.")
    return json.loads(PENDING_AUTH_PATH.read_text())


def _parse_code_or_url(code_or_url: str):
    if not code_or_url.startswith("http"):
        return code_or_url, None
    parsed = urlparse(code_or_url)
    params = parse_qs(parsed.query)
    if "code" not in params:
        raise RuntimeError("No code parameter found in redirect URL.")
    return params["code"][0], params.get("state", [None])[0]


def store_client_secret(path: str) -> None:
    src = Path(path).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Client secret file not found: {src}")
    data = json.loads(src.read_text())
    if "installed" not in data and "web" not in data:
        raise RuntimeError("This file is not a Google OAuth client secret JSON.")
    CLIENT_SECRET_PATH.write_text(json.dumps(data, indent=2))
    print(f"OK: Client secret saved to {CLIENT_SECRET_PATH}")


def get_auth_url() -> None:
    if not CLIENT_SECRET_PATH.exists():
        raise FileNotFoundError(f"Missing client secret at {CLIENT_SECRET_PATH}")
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
        autogenerate_code_verifier=True,
    )
    auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
    _save_pending_auth(state=state, code_verifier=flow.code_verifier)
    LAST_URL_PATH.write_text(auth_url)
    print(auth_url)


def exchange_auth_code(code_or_url: str) -> None:
    if not CLIENT_SECRET_PATH.exists():
        raise FileNotFoundError(f"Missing client secret at {CLIENT_SECRET_PATH}")
    pending = _load_pending_auth()
    code, returned_state = _parse_code_or_url(code_or_url)
    if returned_state and returned_state != pending["state"]:
        raise RuntimeError("OAuth state mismatch. Run --auth-url again to start a fresh session.")
    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRET_PATH),
        scopes=SCOPES,
        redirect_uri=pending.get("redirect_uri", REDIRECT_URI),
        state=pending["state"],
        code_verifier=pending["code_verifier"],
    )
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_payload = _normalize_authorized_user_payload(json.loads(creds.to_json()))
    granted_scopes = list(getattr(creds, "granted_scopes", []) or SCOPES)
    token_payload["scopes"] = granted_scopes
    TOKEN_PATH.write_text(json.dumps(token_payload, indent=2))
    PENDING_AUTH_PATH.unlink(missing_ok=True)
    print(f"OK: Authenticated. Token saved to {TOKEN_PATH}")


def check_auth() -> bool:
    if not TOKEN_PATH.exists():
        print(f"NOT_AUTHENTICATED: No token at {TOKEN_PATH}")
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    except Exception as exc:
        print(f"TOKEN_CORRUPT: {exc}")
        return False
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(json.dumps(_normalize_authorized_user_payload(json.loads(creds.to_json())), indent=2))
        except Exception as exc:
            print(f"REFRESH_FAILED: {exc}")
            return False
    if creds.valid:
        print(f"AUTHENTICATED: Token valid at {TOKEN_PATH}")
        return True
    print("TOKEN_INVALID: Re-run OAuth setup.")
    return False


def revoke() -> None:
    TOKEN_PATH.unlink(missing_ok=True)
    PENDING_AUTH_PATH.unlink(missing_ok=True)
    print("Deleted local Gmail cleanup OAuth token/session files.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Minimal OAuth helper for Gmail cleanup automation")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--client-secret", metavar="PATH")
    group.add_argument("--auth-url", action="store_true")
    group.add_argument("--auth-code", metavar="CODE_OR_URL")
    group.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    try:
        if args.check:
            return 0 if check_auth() else 1
        if args.client_secret:
            store_client_secret(args.client_secret)
            return 0
        if args.auth_url:
            get_auth_url()
            return 0
        if args.auth_code:
            exchange_auth_code(args.auth_code)
            return 0
        if args.revoke:
            revoke()
            return 0
    except Exception as exc:
        print(f"ERROR: {exc}")
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
