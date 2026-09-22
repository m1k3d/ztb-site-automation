#!/usr/bin/env python3
"""
zpa_login.py
- Authenticates to the Zscaler Private Access (ZPA) API using the legacy /signin endpoint
- Reads credentials from .env or environment variables
- Writes only the access_token (ZPA_BEARER) and expiry timestamp (ZPA_BEARER_EXPIRES_AT) to .env
- Prints `export ZPA_BEARER=...` for shell use

Compatible with the legacy API framework on api.zpatwo.net
"""

import datetime as dt
from typing import Optional, Tuple
import requests
from automation_config import Settings, normalize_base, write_tokens

def compute_expiry_iso(seconds: int) -> str:
    """Return ISO 8601 UTC timestamp (Z format) given duration in seconds."""
    exp_dt = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)
    return exp_dt.isoformat().replace("+00:00", "Z")


def normalize_zpa_base_url(raw: str) -> str:
    return normalize_base(raw, zpa=True)

def zpa_login(write_env: bool = True, quiet: bool = False, *, config: Optional[Settings] = None) -> Tuple[str, Optional[str]]:
    """
    Authenticate to ZPA API (legacy /signin endpoint) and return (token, iso_expiry).

    If write_env=True, updates .env with ZPA_BEARER and ZPA_BEARER_EXPIRES_AT.
    If quiet=True, suppresses console output.
    """
    config = config or Settings.load()
    errors = config.errors(require_ztb=False, require_zpa=True)
    if errors:
        raise ValueError("; ".join(errors))
    base = normalize_zpa_base_url(config.zpa_base_url)

    # Legacy ZPA API endpoint (form-encoded)
    url = f"{base}/signin"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {"client_id": config.zpa_client_id, "client_secret": config.zpa_client_secret}

    try:
        resp = requests.post(url, headers=headers, data=payload, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise RuntimeError(f"ZPA authentication failed at {url}; check ZPA credentials and cloud URL") from e

    try:
        data = resp.json()
        token = data["access_token"]
        if not isinstance(token, str) or not token:
            raise ValueError("missing token")
        expires_in = int(data.get("expires_in", 3600))
        iso_exp = compute_expiry_iso(expires_in)
    except (ValueError, KeyError, TypeError):
        raise RuntimeError("ZPA authentication response is missing a valid access_token or expiry") from None

    if write_env:
        write_tokens(config.env_path, {"ZPA_BEARER": token, "ZPA_BEARER_EXPIRES_AT": iso_exp})

    if not quiet:
        where = str(config.env_path) if write_env and config.env_path else "(not written)"
        print(f"✅ ZPA token retrieved")
        print(f"   • API base  : {base}")
        print(f"   • .env file : {where}")
        print(f"   • Expires   : {iso_exp or 'unknown'} (~{expires_in//60}m)")

    return token, iso_exp


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Obtain a ZPA bearer token")
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args(argv)
    try:
        token, _ = zpa_login(config=Settings.load(args.env_file))
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f'export ZPA_BEARER="{token}"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
