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

import datetime as dt
from typing import Optional, Tuple
import requests
from automation_config import Settings, normalize_base, write_tokens

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


def ztb_login(write_env: bool = True, quiet: bool = False, *, config: Optional[Settings] = None) -> Tuple[str, Optional[str]]:
    """
    Authenticate to ZTB API and return (token, iso_expiry).

    If write_env=True, updates .env with BEARER and BEARER_EXPIRES_AT.
    If quiet=True, suppresses console output.
    """
    config = config or Settings.load()
    if not config.api_key:
        raise ValueError("API_KEY: required to obtain or refresh a ZTB token")
    base = normalize_base(config.ztb_api_base)
    url = f"{base}/api/v3/api-key-auth/login"

    try:
        resp = requests.post(
            url,
            headers={"Content-Type": "application/json"},
            json={"api_key": config.api_key},
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"ZTB authentication failed at {url}; check API_KEY and tenant URL") from e

    try:
        data = resp.json()
        result = data["result"]
        token = result["delegate_token"]
        if not isinstance(token, str) or not token.strip():
            raise KeyError("empty token")
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("ZTB authentication response is missing result.delegate_token") from None

    iso_exp, seconds = parse_expiry_fields(result)

    if write_env:
        write_tokens(config.env_path, {"BEARER": token, "BEARER_EXPIRES_AT": iso_exp})

    if not quiet:
        where = str(config.env_path) if write_env and config.env_path else "(not written)"
        print(f"✅ ZTB token retrieved")
        print(f"   • API base  : {base}")
        print(f"   • .env file : {where}")
        if iso_exp or seconds is not None:
            human = f"{seconds//3600}h{(seconds%3600)//60:02d}m" if seconds is not None else "unknown"
            print(f"   • Expires   : {iso_exp or 'unknown'} (~{human})")

    return token, iso_exp


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Obtain a ZTB bearer token")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)
    try:
        token, _ = ztb_login(config=Settings.load(args.env_file))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f'export BEARER="{token}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
