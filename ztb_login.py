#!/usr/bin/env python3
"""
ztb_login.py
- Reads API base + API key from .env or environment
- Calls /api/v3/api-key-auth/login to obtain a delegate token
- Writes BEARER=<delegate_token> (raw token) and BEARER_EXPIRES_AT to .env
- Prints `export BEARER=...` line for shell use

Supports both:
  • CLI use (manual login)
  • Programmatic use via `ztb_login(write_env=True, quiet=False)`
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


def normalize_base(raw: str) -> str:
    """
    Normalize ZTB API base:
      https://<tenant>-api.goairgap.com[/api/v3[/]]
    → https://<tenant>-api.goairgap.com
    """
    base = (raw or "").strip().rstrip("/")
    if base.endswith("/api/v3"):
        base = base[: -len("/api/v3")]
    return base


def parse_expiry_fields(result: dict) -> Tuple[Optional[str], Optional[int]]:
    """
    Extract expiry info from the API response.
    Returns: (iso_expiry_str, seconds_remaining)
    """
    iso, seconds = None, None

    if isinstance(result.get("expires_at"), str):
        iso = result["expires_at"]
        try:
            s = result["expires_at"]
            if s.endswith("Z"):
                exp_dt = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            else:
                exp_dt = dt.datetime.fromisoformat(s)
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=dt.timezone.utc)
            now = dt.datetime.now(dt.timezone.utc)
            seconds = int(max(0, (exp_dt - now).total_seconds()))
        except Exception:
            pass

    for k in ("expires_in", "ttl", "ttl_seconds"):
        if isinstance(result.get(k), (int, float)):
            seconds = int(result[k])
            if iso is None:
                exp_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
                iso = exp_dt.isoformat().replace("+00:00", "Z")
            break

    return iso, seconds


def ztb_login(write_env: bool = True, quiet: bool = False) -> Tuple[str, Optional[str]]:
    """
    Authenticate to ZTB API and return (token, iso_expiry).

    If write_env=True, updates .env with BEARER and BEARER_EXPIRES_AT.
    If quiet=True, suppresses console output.
    """
    base_raw = (os.getenv("ZTB_API_BASE") or os.getenv("ZIA_API_BASE") or "").strip()
    if not base_raw:
        raise SystemExit("❌ Missing ZTB_API_BASE (or legacy ZIA_API_BASE). Set it in .env")
    api_key = get_env("API_KEY")

    base = normalize_base(base_raw)
    url = f"{base}/api/v3/api-key-auth/login"

    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"api_key": api_key},
            timeout=30,
        )
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
        result = data["result"]
        token = result["delegate_token"]
        if not token:
            raise KeyError("empty token")
    except Exception:
        raise SystemExit(f"❌ Unexpected JSON shape:\n{json.dumps(resp.json(), indent=2)}")

    iso_exp, seconds = parse_expiry_fields(result)

    if write_env:
        upsert_env_var("BEARER", token)
        if iso_exp:
            upsert_env_var("BEARER_EXPIRES_AT", iso_exp)

    if not quiet:
        where = str(ENV_PATH.resolve()) if write_env else "(not written)"
        print(f"✅ ZTB token retrieved")
        print(f"   • API base  : {base}")
        print(f"   • .env file : {where}")
        if iso_exp or seconds is not None:
            human = f"{seconds//3600}h{(seconds%3600)//60:02d}m" if seconds is not None else "unknown"
            print(f"   • Expires   : {iso_exp or 'unknown'} (~{human})")

    return token, iso_exp


if __name__ == "__main__":
    token, exp = ztb_login(write_env=True, quiet=False)
    print(f'export BEARER="{token}"')
