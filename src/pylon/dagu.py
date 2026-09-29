from __future__ import annotations

import json
from urllib.parse import quote

import httpx

from .config import home, read_env


def request(method: str, endpoint: str, value=None):
    env = read_env(home() / "secrets/dagu-api.env")
    token = env.get("DAGU_API_TOKEN", "")
    url = env.get("DAGU_API_URL", "http://127.0.0.1:18090/pylon/api/v1") + endpoint
    response = httpx.request(method, url, json=value, headers={"Authorization": f"Bearer {token}"}, timeout=15, trust_env=False)
    response.raise_for_status()
    return response.json() if response.content else {}


def suspend(job_id: str, paused: bool):
    return request("POST", f"/dags/{quote(job_id, safe='')}/suspend", {"suspend": paused})
