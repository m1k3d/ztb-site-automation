# ztb_api.py
# Single-file client for ZTB/ZIA APIs:
# - Logs into ZIdentity with username/password -> authnToken
# - Exchanges authnToken for a Bearer access token (tries several JSON variants)
# - Calls ZIA /api/v3 endpoints with the Bearer
#
# Env vars expected (keep .env slim):
#   ZID_AUTHN_URL   e.g. https://<tenant>.zslogin.net/authn/api/v1/authn
#   ZIA_TOKEN_URL   e.g. https://<internal>-api.goairgap.com/api/v2/auth/zid/token
#   ZIA_API_BASE    e.g. https://<internal>-api.goairgap.com/api/v3
#   ZID_USER        service account (no MFA)
#   ZID_PASS        service account password
# Optional:
#   TENANT_ID       (only if your tenant requires it)
#   BEARER          (manual override for quick testing; if set we’ll use it)

from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urljoin, urlsplit

import requests

# Load .env if python-dotenv is available (ok if not)
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass


class ZTBAuthError(RuntimeError):
    pass


def _env(name: str, required: bool = True) -> Optional[str]:
    val = os.getenv(name)
    if required and not val:
        raise ZTBAuthError(f"Missing required environment variable: {name}")
    return val


# ---- Required env
ZID_AUTHN_URL = _env("ZID_AUTHN_URL")
ZIA_TOKEN_URL = _env("ZIA_TOKEN_URL")
ZIA_API_BASE = _env("ZIA_API_BASE")
ZID_USER = _env("ZID_USER")
ZID_PASS = _env("ZID_PASS")

# ---- Optional env
TENANT_ID = os.getenv("TENANT_ID") or os.getenv("ZTB_TENANT_ID")  # allow old name
MANUAL_BEARER = os.getenv("BEARER")


@dataclass
class _TokenCache:
    bearer: Optional[str] = None
    expires_at: Optional[float] = None  # epoch seconds (if we ever parse exp)


class ZTBClient:
    def __init__(self, timeout: int = 30):
        self.timeout = timeout
        self.session = requests.Session()
        self._cache = _TokenCache()

        # If user already exported BEARER for this shell, honor it.
        if MANUAL_BEARER:
            self._cache.bearer = MANUAL_BEARER

        # Otherwise try full auth flow automatically.
        if not self._cache.bearer:
            authn = self._auth_via_identity()
            bearer = self._try_token_exchange(authn)
            self._cache.bearer = bearer

    # ---------- Public API ----------

    def releases(self) -> Dict[str, Any]:
        url = urljoin(ZIA_API_BASE.rstrip("/") + "/", "Gateway/releases?refresh_token=enabled")
        return self._get(url)

    def gateways(
        self,
        gateway_type: str = "isolation",
        template_id: str = "",
        sortdir: str = "asc",
        sort: str = "location",
        search: str = "",
        page: int = 0,
        limit: int = 100,
        refresh_token: str = "enabled",
    ) -> Dict[str, Any]:
        path = (
            f"Gateway/?gateway_type={gateway_type}"
            f"&template_id={template_id}"
            f"&sortdir={sortdir}"
            f"&sort={sort}"
            f"&search={search}"
            f"&page={page}"
            f"&limit={limit}"
            f"&refresh_token={refresh_token}"
        )
        url = urljoin(ZIA_API_BASE.rstrip("/") + "/", path)
        return self._get(url)

    # ---------- Internals ----------

    def _headers(self) -> Dict[str, str]:
        if not self._cache.bearer:
            raise ZTBAuthError("No bearer token on client.")
        return {
            "Authorization": f"Bearer {self._cache.bearer}",
            "Accept": "application/json",
        }

    def _get(self, url: str) -> Dict[str, Any]:
        r = self.session.get(url, headers=self._headers(), timeout=self.timeout)
        if r.status_code != 200:
            raise ZTBAuthError(f"GET {url} failed: {r.status_code} {r.text[:400]}")
        try:
            return r.json()
        except Exception:
            raise ZTBAuthError(f"GET {url} did not return JSON")

    def _auth_via_identity(self) -> str:
        """POST username/password to ZIdentity -> authnToken"""
        payload = {"username": ZID_USER, "password": ZID_PASS}
        r = self.session.post(
            ZID_AUTHN_URL, headers={"Content-Type": "application/json"}, json=payload, timeout=self.timeout
        )
        if r.status_code != 200:
            raise ZTBAuthError(f"ZIdentity auth failed: {r.status_code} {r.text[:300]}")
        try:
            j = r.json()
            token = j.get("authnToken")
            if not token:
                raise ValueError("missing authnToken")
            return token
        except Exception:
            raise ZTBAuthError(f"Could not parse authnToken from ZIdentity: {r.text[:400]}")

    def _try_token_exchange(self, authn: str) -> str:
        """
        Try several JSON payload shapes + headers (some tenants enforce Origin/Referer).
        Return the first access token we can parse.
        """
        if not ZIA_TOKEN_URL:
            raise ZTBAuthError("Missing ZIA_TOKEN_URL in env")

        p = urlsplit(ZIA_TOKEN_URL)
        ORIGIN = f"{p.scheme}://{p.netloc}"
        REFERER = ORIGIN

        base_json_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": ORIGIN,
            "Referer": REFERER,
        }
        base_form_headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": ORIGIN,
            "Referer": REFERER,
        }

        attempts = [
            # JSON variants first (most common)
            ("json:minimal", base_json_headers, {"authnToken": authn}, True),
            ("json:underscore", base_json_headers, {"authn_token": authn}, True),
            ("json:token+type", base_json_headers, {"token": authn, "tokenType": "authn"}, True),
            ("json:audience", base_json_headers, {"authnToken": authn, "audience": "segment"}, True),
        ]
        if TENANT_ID:
            attempts += [
                ("json:tenantId", base_json_headers, {"authnToken": authn, "tenantId": TENANT_ID}, True),
                (
                    "json:audience+tenantId",
                    base_json_headers,
                    {"authnToken": authn, "audience": "segment", "tenantId": TENANT_ID},
                    True,
                ),
            ]
        # Header-only variant
        attempts.append(("json:X-Authn-Token", {**base_json_headers, "X-Authn-Token": authn}, {}, True))

        # Keep form OAuth exchange LAST; some environments disable it.
        attempts.append(
            (
                "form:oauth-token-exchange",
                base_form_headers,
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                    "subject_token": authn,
                    "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                    "client_id": "ztds.client",
                    "audience": "segment",
                },
                False,
            )
        )
        if TENANT_ID:
            attempts.append(
                (
                    "form:oauth-token-exchange+tenantId",
                    base_form_headers,
                    {
                        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                        "subject_token": authn,
                        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
                        "client_id": "ztds.client",
                        "audience": "segment",
                        "tenantId": TENANT_ID,
                    },
                    False,
                )
            )

        last: Dict[str, Any] | None = None
        for kind, headers, data, as_json in attempts:
            try:
                if as_json:
                    r = self.session.post(ZIA_TOKEN_URL, headers=headers, json=data, timeout=self.timeout)
                else:
                    r = self.session.post(ZIA_TOKEN_URL, headers=headers, data=data, timeout=self.timeout)
            except requests.RequestException as e:
                last = {"attempt": kind, "error": str(e)}
                continue

            if r.status_code == 200:
                # Response can be JSON or raw JWT
                try:
                    j = r.json()
                    token = j.get("access_token") or j.get("token") or j.get("bearer")
                    if token:
                        return token
                    last = {"attempt": kind, "status": 200, "json_keys": list(j.keys())}
                except Exception:
                    txt = r.text.strip()
                    if txt.count(".") == 2:
                        return txt
                    last = {"attempt": kind, "status": 200, "body": txt[:500]}
            else:
                last = {"attempt": kind, "status": r.status_code, "body": r.text[:500]}

        # Persist last failure for debugging
        try:
            pathlib.Path("/tmp/ztb_token_fail.json").write_text(json.dumps(last or {}, indent=2))
        except Exception:
            pass
        raise ZTBAuthError(f"Token exchange failed for all payloads. Last: {last}")

    # Optional helper to expose the bearer if you need it elsewhere
    def bearer(self) -> str:
        if not self._cache.bearer:
            raise ZTBAuthError("No bearer set")
        return self._cache.bearer