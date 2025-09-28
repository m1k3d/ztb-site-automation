#!/usr/bin/env python3
# load_env.py — load .env and print BEARER info

import os, pathlib, base64, json, time

def load_env(path=".env"):
    p = pathlib.Path(path)
    if not p.exists():
        print(f"No {path} found")
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        os.environ[k] = v

def check_bearer():
    tok = os.environ.get("BEARER")
    if not tok:
        print("❌ No BEARER in env")
        return
    parts = tok.split(".")
    if len(parts) < 2:
        print("❌ BEARER is not a valid JWT")
        return
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode())
    exp = payload.get("exp")
    if exp:
        mins = (exp - time.time()) / 60
        print("✅ BEARER expires at:", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(exp)))
        print(f"   Minutes left: {mins:.1f}")
    else:
        print("⚠️  No exp claim in BEARER")

if __name__ == "__main__":
    load_env(".env")
    check_bearer()