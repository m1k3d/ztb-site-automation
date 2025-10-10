#!/usr/bin/env python3
"""
ztb_login.py
- Reads API base + API key from .env or env vars
- Prefers ZTB_API_BASE (falls back to legacy ZIA_API_BASE)
- Calls /api/v3/api-key-auth/login
- Writes BEARER=<delegate_token> (raw token only) into your .env
- Prints an `export BEARER=...` you can eval to load your shell

New in this version:
- Auto-creates/updates .env safely (idempotent upsert)
- Auto-loads .env via python-dotenv (no need for set -a/source unless you want it)
- Captures token expiry when available (writes BEARER_EXPIRES_AT)
- Quiet mode and no-write mode for CI/pipelines
"""

from __future__ import annotations
import argparse
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
    Accepts either:
      https://<tenant>-api.goairgap.com
      https://<tenant>-api.goairgap.com/
      https://<tenant>-api.goairgap.com/api/v3
      https://<tenant>-api.goairgap.com/api/v3/
    Returns: https://<tenant>-api.goairgap.com  (no trailing slash)
    """
    base = (raw or "").strip().rstrip("/")
    if base.endswith("/api/v3"):
        base = base[: -len("/api/v3")]
    return base


def parse_expiry_fields(result: dict) -> Tuple[Optional[str], Optional[int]]:
    """
    Try to extract expiry info from various shapes the API might return.

    Returns:
      (iso_expiry_str, seconds_remaining) — either or both may be None if unavailable.
    """
    # Common possibilities:
    # - result["expires_at"] (ISO timestamp)
    # - result["expires_in"] (seconds)
    # - result["ttl"] / result["ttl_seconds"] (seconds)
    iso = None
    seconds = None

    if isinstance(result.get("expires_at"), str):
        iso = result["expires_at"]
        try:
            # try to compute seconds remaining if it's parseable
            # assume UTC/Z if 'Z' present; otherwise treat naive as UTC
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
                # compute ISO if we have seconds
                exp_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
                iso = exp_dt.isoformat().replace("+00:00", "Z")
            break

    return iso, seconds


def main() -> None:
    ap = argparse.ArgumentParser(description="Obtain a ZTB delegate token and write it into .env")
    ap.add_argument("--quiet", action="store_true", help="Only print the export line (suppress status text)")
    ap.add_argument("--no-write", action="store_true", help="Do not write .env; only print export line")
    args = ap.parse_args()

    # Prefer ZTB_API_BASE, fall back to legacy ZIA_API_BASE
    base_raw = (os.getenv("ZTB_API_BASE") or os.getenv("ZIA_API_BASE") or "").strip()
    if not base_raw:
        raise SystemExit("❌ Missing ZTB_API_BASE (or legacy ZIA_API_BASE). Set it in .env")
    api_key = get_env("API_KEY") or ""

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
        # API doc shows: { "result": { "delegate_token": "..." } }
        result = data["result"]
        token = result["delegate_token"]
        if not token:
            raise KeyError("empty token")
    except Exception:
        # Show whatever we got back to help debugging
        raise SystemExit(f"❌ Unexpected JSON shape:\n{json.dumps(resp.json(), indent=2)}")

    # Optional expiry capture
    iso_exp, seconds = parse_expiry_fields(result)

    if not args.no_write:
        # Save only the RAW token (no "Bearer " prefix)
        upsert_env_var("BEARER", token)
        if iso_exp:
            upsert_env_var("BEARER_EXPIRES_AT", iso_exp)

    # Status lines
    if not args.quiet:
        where = str(ENV_PATH.resolve()) if not args.no_write else "(not written; --no-write)"
        print(f"✅ Token retrieved")
        print(f"   • API base  : {base}")
        print(f"   • .env file : {where}")
        if iso_exp or seconds is not None:
            human = f"{seconds//3600}h{(seconds%3600)//60:02d}m" if seconds is not None else "unknown"
            print(f"   • Expires   : {iso_exp or 'unknown'} (~{human})")

    # Always print a last line that can be eval'd in POSIX shells
    # Keep RAW token (your scripts prepend 'Bearer ' themselves)
    print(f'export BEARER="{token}"')


if __name__ == "__main__":
    main()