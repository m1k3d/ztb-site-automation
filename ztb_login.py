#!/usr/bin/env python3
"""
ztb_login.py
- Reads API base + API key from .env or env vars
- Calls /api/v3/api-key-auth/login
- Writes BEARER=<delegate_token> (raw token only) into your .env
- Prints an `export BEARER=...` you can eval to load your shell
"""

from __future__ import annotations
import json, os, re, sys
from pathlib import Path
from typing import Optional

import requests

# Optional: load from .env if present
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

def put_env_var(key: str, value: str) -> None:
    """Upsert KEY="value" in .env (create if missing)."""
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    line = f'{key}="{value}"'
    if pattern.search(text):
        text = pattern.sub(line, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"
    ENV_PATH.write_text(text)

def normalize_base(raw: str) -> str:
    """
    Accepts either:
      https://<tenant>-api.goairgap.com
      https://<tenant>-api.goairgap.com/
      https://<tenant>-api.goairgap.com/api/v3
      https://<tenant>-api.goairgap.com/api/v3/
    Returns: https://<tenant>-api.goairgap.com  (no trailing slash)
    """
    base = raw.strip().rstrip("/")
    # If someone pasted a full /api/v3 base, strip it
    if base.endswith("/api/v3"):
        base = base[: -len("/api/v3")]
    return base

def main() -> None:
    base_raw = get_env("ZIA_API_BASE") or ""   # e.g. https://<tenant>-api.goairgap.com
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
            body = (resp.text or "")[:800]
        raise SystemExit(f"❌ Auth failed ({resp.status_code}) at {url}\nResponse:\n{body}") from e
    except Exception as e:
        raise SystemExit(f"❌ Request error calling {url}: {e}") from e

    try:
        data = resp.json()
        # API doc shows: { "result": { "delegate_token": "..." } }
        token = data["result"]["delegate_token"]
    except Exception:
        raise SystemExit(f"❌ Unexpected JSON shape:\n{json.dumps(resp.json(), indent=2)}")

    # Save only the RAW token (no "Bearer " prefix)
    put_env_var("BEARER", token)

    print(f"✅ Wrote BEARER to {ENV_PATH.resolve()}")
    print(f'export BEARER="{token}"')

if __name__ == "__main__":
    main()