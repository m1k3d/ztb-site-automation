#!/usr/bin/env python3
"""
zpa_login.py
- Authenticates to the Zscaler Private Access (ZPA) API using the legacy /signin endpoint
- Reads credentials from .env or environment variables
- Writes only the access_token (ZPA_BEARER) and expiry timestamp (ZPA_BEARER_EXPIRES_AT) to .env
- Prints `export ZPA_BEARER=...` for shell use

Compatible with the legacy API framework on api.zpatwo.net
"""

from __future__ import annotations
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional, Tuple
import requests

# Optional: load from .env if present (non-fatal if missing)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(override=False)
except Exception:
    pass

ENV_PATH = Path(".env")


def get_env(name: str, *, required: bool = True) -> Optional[str]:
    """Fetch env var, optionally requiring it."""
    v = os.getenv(name)
    if required and not v:
        print(f"❌ Missing {name} (set it in .env or your environment)", file=sys.stderr)
        sys.exit(2)
    return v


def upsert_env_var(key: str, value: str) -> None:
    """Upsert KEY="value" in .env (create file if missing)."""
    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f'{key}="{value}"'
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    ENV_PATH.write_text(text, encoding="utf-8")


def compute_expiry_iso(seconds: int) -> str:
    """Return ISO 8601 UTC timestamp (Z format) given duration in seconds."""
    exp_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return exp_dt.isoformat().replace("+00:00", "Z")


def zpa_login(write_env: bool = True, quiet: bool = False) -> Tuple[str, Optional[str]]:
    """
    Authenticate to ZPA API (legacy /signin endpoint) and return (token, iso_expiry).

    If write_env=True, updates .env with ZPA_BEARER and ZPA_BEARER_EXPIRES_AT.
    If quiet=True, suppresses console output.
    """
    base = os.getenv("ZPA_BASE_URL", "").rstrip("/")
    if not base:
        raise SystemExit("❌ Missing ZPA_BASE_URL in .env")

    client_id = get_env("ZPA_CLIENT_ID")
    client_secret = get_env("ZPA_CLIENT_SECRET")

    # Legacy ZPA API endpoint (form-encoded)
    url = f"{base}/signin"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {"client_id": client_id, "client_secret": client_secret}

    try:
        resp = requests.post(url, headers=headers, data=payload, timeout=30)
        resp.raise_for_status()
    except requests.HTTPError as e:
        try:
            body = json.dumps(resp.json(), indent=2)
        except Exception:
            body = (getattr(resp, "text", "") or "")[:800]
        raise SystemExit(f"❌ Auth failed ({resp.status_code}) at {url}\nResponse:\n{body}") from e
    except Exception as e:
        raise SystemExit(f"❌ Request error calling {url}: {e}") from e

    try:
        data = resp.json()
        token = data.get("access_token") or resp.text.strip()
        expires_in = int(data.get("expires_in", 3600))
        iso_exp = compute_expiry_iso(expires_in)
    except Exception:
        raise SystemExit(f"❌ Unexpected JSON shape:\n{resp.text}")

    if write_env:
        upsert_env_var("ZPA_BEARER", token)
        if iso_exp:
            upsert_env_var("ZPA_BEARER_EXPIRES_AT", iso_exp)

    if not quiet:
        where = str(ENV_PATH.resolve()) if write_env else "(not written)"
        print(f"✅ ZPA token retrieved")
        print(f"   • API base  : {base}")
        print(f"   • .env file : {where}")
        print(f"   • Expires   : {iso_exp or 'unknown'} (~{expires_in//60}m)")

    return token, iso_exp


if __name__ == "__main__":
    token, exp = zpa_login(write_env=True, quiet=False)
    print(f'export ZPA_BEARER="{token}"')
