"""Explicit, shared configuration. Importing this module never reads credentials."""

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values, set_key


def credential(value: str) -> str:
    value = str(value or "").strip()
    if value.upper() == "AUTO_POPULATED" or value.upper().startswith("YOUR_"):
        return ""
    return value


def normalize_base(raw: str, *, zpa: bool = False) -> str:
    value = str(raw or "").strip().rstrip("/")
    if zpa and value and "://" not in value:
        value = "https://" + value
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("must be an HTTPS URL without embedded credentials")
    if any(c in parsed.netloc for c in "<> \t\r\n") or parsed.query or parsed.fragment:
        raise ValueError("must be a tenant URL without placeholders, query, or fragment")
    if parsed.path.rstrip("/") not in ("", "/api/v2", "/api/v3"):
        raise ValueError("must be the tenant root URL (optionally ending in /api/v2 or /api/v3)")
    host = parsed.netloc
    if zpa:
        if host.lower().startswith("api."):
            host = "config." + host[4:]
        elif not host.lower().startswith("config."):
            host = "config." + host
    return urlunsplit(("https", host, "", "", ""))


@dataclass(frozen=True)
class Settings:
    ztb_api_base: str = ""
    api_key: str = field(default="", repr=False)
    bearer: str = field(default="", repr=False)
    zpa_base_url: str = ""
    zpa_client_id: str = field(default="", repr=False)
    zpa_client_secret: str = field(default="", repr=False)
    zpa_customer_id: str = ""
    zpa_enabled: str = ""
    zpa_enrollment_cert_name: str = "Connector"
    referer_path: str = "/"
    env_path: Optional[Path] = field(default=None, repr=False)

    @classmethod
    def load(cls, env_file=".env", *, environ: Optional[Mapping[str, str]] = None):
        """Process environment overrides the explicitly selected dotenv file."""
        path = Path(env_file).resolve()
        values = dict(dotenv_values(path, interpolate=False)) if path.exists() else {}
        values.update(os.environ if environ is None else environ)
        def get(name, default=""):
            return str(values.get(name) or default).strip()
        return cls(
            ztb_api_base=get("ZTB_API_BASE") or get("ZIA_API_BASE"),
            api_key=credential(get("API_KEY")), bearer=credential(get("BEARER")),
            zpa_base_url=get("ZPA_BASE_URL"), zpa_client_id=credential(get("ZPA_CLIENT_ID")),
            zpa_client_secret=credential(get("ZPA_CLIENT_SECRET")),
            zpa_customer_id=credential(get("ZPA_CUSTOMER_ID")), zpa_enabled=get("ZPA_ENABLED"),
            zpa_enrollment_cert_name=get("ZPA_ENROLLMENT_CERT_NAME", "Connector"),
            referer_path=get("ZTB_REFERER_PATH", "/"), env_path=path,
        )

    def errors(self, *, require_ztb=True, require_zpa=False):
        errors = []
        def check_url(name, value, zpa=False):
            try:
                normalize_base(value, zpa=zpa)
            except ValueError as exc:
                errors.append(f"{name}: {exc}")
        if require_ztb:
            check_url("ZTB_API_BASE", self.ztb_api_base)
            if not credential(self.bearer) and not credential(self.api_key):
                errors.append("API_KEY: required when BEARER is empty or a placeholder")
        if require_zpa:
            enabled = self.zpa_enabled.lower()
            if enabled in ("false", "0", "no", "n"):
                errors.append("ZPA_ENABLED: false conflicts with a selected appc_provision row")
            elif enabled not in ("", "true", "1", "yes", "y"):
                errors.append("ZPA_ENABLED: expected true or false")
            check_url("ZPA_BASE_URL", self.zpa_base_url, zpa=True)
            for name, value in (("ZPA_CLIENT_ID", self.zpa_client_id), ("ZPA_CLIENT_SECRET", self.zpa_client_secret)):
                if not credential(value):
                    errors.append(f"{name}: required for appc_provision")
        return errors


def write_tokens(path: Optional[Path], values: Mapping[str, str]) -> None:
    """Only explicit login/refresh persists tokens; normal configuration loading is read-only."""
    if path is not None:
        for key, value in values.items():
            if value:
                set_key(str(path), key, str(value), quote_mode="always")
