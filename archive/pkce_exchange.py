#!/usr/bin/env python3
"""
PKCE exchange → Bearer (ZIdentity → ZIA API)

Usage:
  python3 pkce_exchange.py \
    --code "..." \
    --code-verifier "..." \
    --state "..."

Requires:
  - .env in the current folder with ZIA_TOKEN_URL
    (e.g. ZIA_TOKEN_URL=https://<tenant>-api.goairgap.com/api/v2/auth/zid/token)
"""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv


def die(msg: str, status: int = 1):
    print(msg, file=sys.stderr)
    sys.exit(status)


def load_env():
    # look for .env in current working dir
    env_ok = load_dotenv(dotenv_path=Path(".env"))
    if not env_ok:
        print("⚠️  .env not found in cwd — make sure ZIA_TOKEN_URL is provided some other way")
    token_url = os.environ.get("ZIA_TOKEN_URL")
    if not token_url:
        die("❌ ZIA_TOKEN_URL not set. Put it in .env or export it before running.")
    return token_url


def api_origin_from(token_url: str) -> str:
    """
    Convert https://<tenant>-api.goairgap.com/...  →  https://<tenant>.goairgap.com
    That matches what your browser sends as Origin/Referer.
    """
    p = urlsplit(token_url)
    host = p.netloc  # e.g. ziainternalmike-api.goairgap.com
    host = host.replace("-api.", ".")  # -> ziainternalmike.goairgap.com
    return f"{p.scheme}://{host}"


def build_headers(origin: str) -> dict:
    # Mirror the browser-y headers that worked for you
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": origin,
        "referer": origin + "/",
        # UA is often checked by stricter tenants
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
    }


def is_jwt(s: str) -> bool:
    return s.count(".") == 2 and 30 < len(s) < 8192


def main():
    parser = argparse.ArgumentParser(description="PKCE code → Bearer exchange")
    parser.add_argument("--code", required=True, help="PKCE authorization code")
    parser.add_argument("--code-verifier", required=True, dest="code_verifier", help="PKCE code_verifier")
    parser.add_argument("--state", required=True, help="state value captured with the code")
    parser.add_argument(
        "--out",
        default="/tmp/ztb_bearer.txt",
        help="Where to save the Bearer token (default: /tmp/ztb_bearer.txt)",
    )
    args = parser.parse_args()

    token_url = load_env()
    origin = api_origin_from(token_url)
    headers = build_headers(origin)

    payload = {"code": args.code, "code_verifier": args.code_verifier, "state": args.state}

    print(f"→ POST {token_url}")
    try:
        r = requests.post(token_url, headers=headers, json=payload, timeout=20)
    except requests.RequestException as e:
        die(f"❌ Network error during exchange: {e}")

    ctype = r.headers.get("content-type", "")
    print(f"← status: {r.status_code} | content-type: {ctype}")

    body_text = r.text or ""
    if r.status_code != 200:
        # Save the failure body to help troubleshoot quickly
        fail_path = Path("/tmp/ztb_token_fail.json")
        try:
            # Try to parse as JSON; fall back to plain text
            body_obj = r.json()
        except Exception:
            body_obj = {"raw": body_text[:1000]}
        fail_path.write_text(json.dumps({"status": r.status_code, "body": body_obj}, indent=2))
        die("❌ Exchange failed. Details saved to /tmp/ztb_token_fail.json")

    # Some tenants return the Bearer as raw text, others wrap in JSON — handle both
    bearer = None
    if ctype.startswith("application/json"):
        try:
            j = r.json()
        except Exception:
            die("❌ Response said JSON but could not be parsed.")
        bearer = j.get("access_token") or j.get("bearer") or j.get("accessToken")
        if not bearer and is_jwt(body_text.strip()):
            bearer = body_text.strip()
    else:
        # text/plain; charsets — browser-like path typically returns the JWT string
        bearer = body_text.strip()

    if not bearer or not is_jwt(bearer):
        # Save body for inspection
        Path("/tmp/ztb_token_body.txt").write_text(body_text)
        die("❌ Did not find a JWT in the response. Saved body to /tmp/ztb_token_body.txt")

    # Persist & show a quick preview
    Path(args.out).write_text(bearer)
    print(f"✅ Bearer saved to {args.out}")
    print(f"BEARER length: {len(bearer)} | starts: {bearer[:12]}...")


if __name__ == "__main__":
    main()