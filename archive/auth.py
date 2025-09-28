#!/usr/bin/env python3
"""
auth.py
Two-step auth helper for ZIA/ZTB:
  1) POST to ZSLogin /authn to get authnToken (or sessionToken)
  2) Exchange that for a bearer access_token on your tenant's token endpoint

Env (.env):
  ZTB_AUTHN_URL   = https://<yourcompany>.zslogin.net/authn/api/v1/authn
  ZTB_TOKEN_URL   = https://<tenant>-api.goairgap.com/api/v2/auth/zid/token
  ZTB_TENANT_ID   = ghvgpguqn06qa           (optional but recommended)
  ZTB_USER        = servicemiked@...        (non-MFA account)
  ZTB_PASS        = <password>
  ZTB_BASE_URL    = https://<tenant>-api.goairgap.com/api/v3  (for smoke test)

Usage (smoke test):
  python3 auth.py
"""

import os
import sys
import json
import time
from pathlib import Path
from typing import Dict, Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

AUTHN_URL   = os.getenv("ZTB_AUTHN_URL", "").strip()
TOKEN_URL   = os.getenv("ZTB_TOKEN_URL", "").strip()
TENANT_ID   = os.getenv("ZTB_TENANT_ID", "").strip()
USERNAME    = os.getenv("ZTB_USER", "").strip()
PASSWORD    = os.getenv("ZTB_PASS", "").strip()
BASE_URL    = os.getenv("ZTB_BASE_URL", "").strip()  # only used for optional smoke test

CACHE_PATH  = Path(".ztb_token.json")  # local cache for the exchanged bearer
TIMEOUT     = 30

def need_env(*names: str) -> None:
    missing = [n for n in names if not globals()[n]]
    if missing:
        print(f"ERROR: Missing {', '.join(missing)} in .env")
        sys.exit(1)

def http_err(prefix: str, r: requests.Response) -> RuntimeError:
    body = r.text
    if len(body) > 400:
        body = body[:400] + "...(truncated)"
    return RuntimeError(f"{prefix}: HTTP {r.status_code} {body}")

def get_authn_token() -> str:
    """Step 1: call ZSLogin /authn to obtain an authnToken (or sessionToken)."""
    need_env("AUTHN_URL", "USERNAME", "PASSWORD")
    # Most deployments require tenantId in the body; include it if present.
    payload: Dict[str, Any] = {"username": USERNAME, "password": PASSWORD}
    if TENANT_ID:
        payload["tenantId"] = TENANT_ID

    r = requests.post(
        AUTHN_URL,
        json=payload,
        headers={"Content-Type": "application/json"},
        timeout=TIMEOUT,
    )
    if r.status_code != 200:
        raise http_err("Authn step failed", r)

    data = r.json()
    # Prefer 'authnToken', fall back to 'sessionToken'
    token = data.get("authnToken") or data.get("sessionToken")
    if not isinstance(token, str) or not token:
        raise RuntimeError(f"Authn response missing token: {json.dumps(data)[:300]}")
    return token

def exchange_token(authn_token: str) -> Dict[str, Any]:
    """
    Step 2: For some tenants the key must be 'token'; some accept 'authnToken';
            some require tenantId in the exchange body. Try the common permutations.
    """
    need_env("TOKEN_URL")

    body_variants = [
        {"token": authn_token},                          # most common
        {"authnToken": authn_token},                     # fallback
        {"tenantId": TENANT_ID, "token": authn_token}    # some tenants require tenantId here
        if TENANT_ID else None,
    ]
    last_resp: Optional[requests.Response] = None

    for body in [b for b in body_variants if b]:
        r = requests.post(
            TOKEN_URL,
            json=body,
            headers={"Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        last_resp = r
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                raise http_err("Token exchange returned non-JSON", r)

    assert last_resp is not None
    raise http_err("Token exchange failed", last_resp)

def cache_write(obj: Dict[str, Any]) -> None:
    obj["_cached_at"] = int(time.time())
    CACHE_PATH.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8")

def cache_read() -> Optional[Dict[str, Any]]:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        # If the server provides expiry, honor it; otherwise assume ~25 minutes
        exp = data.get("expires_at") or data.get("expires_in")
        cached = data.get("_cached_at", 0)
        if isinstance(exp, int):
            # If expires_in was provided, compute absolute expiry
            if "expires_in" in data and "expires_at" not in data:
                # convert seconds to absolute epoch
                data["expires_at"] = cached + int(exp * 0.9)  # safety margin
            # fall through to check absolute
        if isinstance(data.get("expires_at"), int) and time.time() < data["expires_at"]:
            return data
        # Otherwise, keep a soft 20m window
        if time.time() - cached < 1200:
            return data
    except Exception:
        return None
    return None

def get_bearer(force_refresh: bool = False) -> Dict[str, Any]:
    """Return a dict including at least 'access_token'. Uses on-disk cache."""
    if not force_refresh:
        cached = cache_read()
        if cached and isinstance(cached.get("access_token", ""), str):
            return cached

    authn = get_authn_token()
    exchanged = exchange_token(authn)

    # Normalize keys for convenience
    token = (
        exchanged.get("access_token")
        or exchanged.get("token")
        or exchanged.get("accessToken")
    )
    if not token:
        raise RuntimeError(f"Exchange success but no access token field found: {json.dumps(exchanged)[:300]}")

    normalized = {
        "access_token": token,
        "token_type": exchanged.get("token_type", "Bearer"),
        "expires_in": exchanged.get("expires_in"),
        # If the API provides an absolute expiry, copy it through
        "expires_at": exchanged.get("expires_at"),
        # Keep original payload too
        "_raw": exchanged,
    }
    cache_write(normalized)
    return normalized

def get_authed_session() -> requests.Session:
    """Convenience: return a requests.Session with Authorization header set."""
    tok = get_bearer()
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {tok['access_token']}"})
    return s

def _smoke_test() -> None:
    """Optional smoke test: GET one Gateway row if ZTB_BASE_URL is set."""
    print("Auth: acquiring bearer…")
    bearer = get_bearer(force_refresh=True)
    print("OK. Token (first 24 chars):", bearer["access_token"][:24] + "…")

    if not BASE_URL:
        print("Skip smoke test (ZTB_BASE_URL not set).")
        return

    s = get_authed_session()
    url = f"{BASE_URL}/Gateway"
    params = {"limit": "1", "size": "1"}
    r = s.get(url, params=params, timeout=TIMEOUT)
    print("Smoke GET", url, "->", r.status_code)
    try:
        print(json.dumps(r.json(), indent=2)[:800])
    except Exception:
        print(r.text[:800])

if __name__ == "__main__":
    try:
        _smoke_test()
    except Exception as e:
        print("Auth error:", e)
        sys.exit(1)