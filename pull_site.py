#!/usr/bin/env python3
# pull_site.py — list sites OR pull one site by name, save VLAN JSON+CSV, and upsert sites.csv
# Author: Mike Dechow (@m1k3d)
# Repo: github.com/m1k3d/ztb-site-automation
# License: MIT
# version: 1.4.0
#
# Usage:
#   python3 pull_site.py                                     # lists sites
#   python3 pull_site.py --site-name "Utrecht-Branch"
#   python3 pull_site.py --site-name "Utrecht-Branch" --include-wan
#   python3 pull_site.py --site-name "Utrecht-Branch" --json-only
#   python3 pull_site.py --site-name "Utrecht-Branch" --include-ha
#   python3 pull_site.py --list-templates [--template-search "zt800"]
#
# Notes:
#   - Saves VLAN definitions to vlans/<site>.json and vlans/<site>.csv
#   - CSV includes "share_over_vpn" (TRUE/FALSE) and "dhcp_service" (on/off/non-airgapped)
#   - CSV "enabled" = TRUE iff status == "provisioned"
#   - By default, WAN VLANs are excluded from the CSV (use --include-wan to include them)
#   - **HA VLANs:** By default, HA internal VLANs (e.g., zone "HA Zone") are excluded from the CSV
#                   because they’re auto-provisioned during site creation and not editable.
#   - Updates or inserts site row into sites.csv for bulk_create.py
#   - Supports HA by adding optional *_b / wan1_* fields when a second gateway is present
#   - **Templates:** `--list-templates` shows name/deployment_type/platform_type/id. Keep template_id blank;
#                   bulk_create resolves ID from template_name at runtime.
#
#   - **Auth QoL (single-run)**:
#       · If BEARER is missing, we call `ztb_login.py`, reload .env, and build the session with the new token.
#       · If any request returns 401 once, we call `ztb_login.py`, update the session header, and retry ONCE.
#       · Messages are visible (no hidden background behavior).

import os, sys, json, csv, argparse, pathlib, subprocess
from typing import Any, Dict, List, Optional, Tuple
import requests

# ------------------------
# Paths
# ------------------------
ROOT = pathlib.Path(__file__).resolve().parent
OUT_VLANS_DIR = ROOT / "vlans"
OUT_VLANS_DIR.mkdir(exist_ok=True)
CSV_PATH = ROOT / "sites.csv"
LOGIN_SCRIPT = ROOT / "ztb_login.py"

# ------------------------
# tiny .env loader (no extra deps)
# ------------------------
def load_env_file(path: str = ".env"):
    p = pathlib.Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        # allow refresh/overwrite after ztb_login.py runs
        os.environ[k] = v

load_env_file(".env")

# ------------------------
# Env / session
# ------------------------
def _normalize_base_root(raw: str) -> str:
    """Accept root or /api/v3|v2 and return clean ROOT (no trailing slash, no /api/*)."""
    base = (raw or "").strip().rstrip("/")
    if base.endswith("/api/v3") or base.endswith("/api/v2"):
        base = base.rsplit("/api/", 1)[0]
    return base

def _invoke_login() -> bool:
    """Run ztb_login.py and reload .env. Return True if BEARER is now set."""
    if not LOGIN_SCRIPT.exists():
        print("ERROR: ztb_login.py not found; cannot auto-fetch token.", file=sys.stderr)
        return False
    print("🔐 BEARER missing — invoking ztb_login.py to obtain a fresh token…")
    try:
        # visible to user
        subprocess.run([sys.executable, str(LOGIN_SCRIPT)], check=True)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: ztb_login.py failed with exit code {e.returncode}", file=sys.stderr)
        return False
    # reload env so we get the new token immediately
    load_env_file(".env")
    return bool((os.environ.get("BEARER") or "").strip())

def _refresh_bearer_and_update_session(session: requests.Session) -> bool:
    """On 401: run login, reload env, update session header."""
    print("🔄 401 Unauthorized — refreshing token via ztb_login.py and retrying once…")
    if not _invoke_login():
        print("ERROR: token refresh failed.", file=sys.stderr)
        return False
    new_bearer = (os.environ.get("BEARER") or "").strip()
    if not new_bearer:
        print("ERROR: ztb_login.py ran but BEARER is still empty.", file=sys.stderr)
        return False
    session.headers["Authorization"] = f"Bearer {new_bearer}"
    return True

def get_session_and_bases():
    # Prefer ZTB_API_BASE, fallback to legacy ZIA_API_BASE
    base_env = os.environ.get("ZTB_API_BASE") or os.environ.get("ZIA_API_BASE") or ""
    base_root = _normalize_base_root(base_env)
    if not base_root:
        print("ERROR: Missing ZTB_API_BASE (or legacy ZIA_API_BASE) in environment (.env).", file=sys.stderr)
        sys.exit(1)

    # Ensure a bearer exists BEFORE creating the session header
    if not (os.environ.get("BEARER") or "").strip():
        if not _invoke_login():
            print("ERROR: BEARER still missing after ztb_login.py.", file=sys.stderr)
            sys.exit(1)

    bearer = (os.environ.get("BEARER") or "").strip()

    base_v3 = f"{base_root}/api/v3"
    base_v2 = f"{base_root}/api/v2"

    # Origin/Referer like the UI (root without "-api.")
    origin_host = base_root.replace("-api.", ".")
    referer_path = os.getenv("ZTB_REFERER_PATH", "/").lstrip("/")
    referer = origin_host + (("" if referer_path == "" else f"{referer_path}/"))

    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {bearer}",  # BEARER here is already ensured/fresh
        "Accept": "application/json",
        "User-Agent": "pull_site.py",
    })

    return s, base_v3, base_v2, origin_host, referer

session, BASE_V3, BASE_V2, ORIGIN, REFERER = get_session_and_bases()

# ------------------------
# HTTP helpers (with single-run 401 refresh)
# ------------------------
def _request_with_auto_refresh(method: str, url: str, *, params=None, headers=None, timeout=60, json=None, data=None):
    r = session.request(method, url, params=params, headers=headers, timeout=timeout, json=json, data=data)
    if r.status_code == 401:
        if _refresh_bearer_and_update_session(session):
            r = session.request(method, url, params=params, headers=headers, timeout=timeout, json=json, data=data)
    return r

def get_json(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
    r = _request_with_auto_refresh("GET", url, params=params, headers=headers, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code} {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        raise ValueError(f"Non-JSON response from {url}: {r.text[:300]}")

def get_json_v3(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    # Many v3 endpoints prefer the trailing slash (Gateway/ vs Gateway)
    p = path.lstrip("/")
    if not p.endswith("/"):
        p += "/"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
    }
    return get_json(f"{BASE_V3}/{p}", params=params, headers=headers)

def get_json_v3_no_trailing(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    # Some endpoints (e.g., /templates) are 404-sensitive to a trailing slash.
    p = path.lstrip("/")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
    }
    return get_json(f"{BASE_V3}/{p}", params=params, headers=headers)

def get_vlans_v2_network(site_id: str) -> List[Dict[str, Any]]:
    """
    VLANs via /api/v2/Network/?siteId=...
    """
    url = f"{BASE_V2}/Network/"
    params = {"siteId": site_id, "refresh_token": "enabled"}
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
    }
    data = get_json(url, params=params, headers=headers)
    # Normalize: {result:{rows:[...]}} OR {rows:[...]} OR [...]
    if isinstance(data, dict):
        rows = data.get("rows") or data.get("result", {}).get("rows")
        return rows or []
    if isinstance(data, list):
        return data
    return []

# ------------------------
# Data access
# ------------------------
def list_gateways_rows() -> List[Dict[str, Any]]:
    data = get_json_v3("Gateway", params={
        "gateway_type": "isolation",
        "sortdir": "asc",
        "sort": "location",
        "search": "",
        "page": 0,
        "limit": "100",
        "refresh_token": "enabled",
    })
    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            return data["rows"]
        if isinstance(data.get("result"), dict) and isinstance(data["result"].get("rows"), list):
            return data["result"]["rows"]
    raise ValueError("Unexpected /Gateway response; could not find rows list.")

# ---- Templates API (no trailing slash) ----
def fetch_templates(search: str = "") -> List[Dict[str, Any]]:
    params = {
        "sortdir": "asc",
        "sort": "name",
        "size": "100",
        "search": search or "",
        "page": 0,
        "refresh_token": "enabled",
    }
    data = get_json_v3_no_trailing("templates", params=params)
    if isinstance(data, dict):
        if isinstance(data.get("result"), list):
            return data["result"]
        if isinstance(data.get("rows"), list):
            return data["rows"]
    if isinstance(data, list):
        return data
    return []

def print_templates(templates: List[Dict[str, Any]]):
    if not templates:
        print("No templates found.")
        return
    hdr = f"{'name':40}  {'deployment_type':18}  {'platform_type':10}  {'id'}"
    print(hdr)
    print("-" * len(hdr))
    for t in templates:
        name = (t.get("name") or "")[:40]
        dep  = (t.get("deployment_type") or "")[:18]
        plat = (t.get("platform_type") or "")[:10]
        tid  = t.get("id") or ""
        print(f"{name:40}  {dep:18}  {plat:10}  {tid}")

def print_site_list(rows: List[Dict[str, Any]]):
    print("Available sites:")
    print("-" * 60)
    for r in rows:
        name = r.get("location_display_name") or r.get("site_name") or r.get("location") or "-"
        site_id = (r.get("cluster_info") or {}).get("site_id") or r.get("site_id") or r.get("id") or "-"
        print(f"{name:30}  site_id={site_id}")
    print("-" * 60)
    print('Run: python3 pull_site.py --site-name "Utrecht-Branch"')

def match_row_by_name(rows: List[Dict[str, Any]], site_name: str) -> Optional[Dict[str, Any]]:
    wanted = site_name.strip().lower()
    fields = ["location_display_name", "site_name", "zia_location_name", "name", "location"]
    for r in rows:
        for f in fields:
            v = r.get(f)
            if isinstance(v, str) and v.strip().lower() == wanted:
                return r
    return None

# ------------------------
# VLAN CSV conversion
# ------------------------
VLAN_CSV_FIELDS = [
    "name", "tag", "subnet", "default_gateway", "dhcp_start", "dhcp_end",
    "interface", "zone", "enabled", "share_over_vpn", "dhcp_service"
]

def _split_range(d: Dict[str, Any]) -> Tuple[str, str]:
    r = d.get("range_list")
    if isinstance(r, list) and r and isinstance(r[0], list) and len(r[0]) == 2:
        a, b = r[0][0] or "", r[0][1] or ""
        return (a, b)
    dr = d.get("dhcp_range")
    if isinstance(dr, str) and "-" in dr:
        a, b = dr.split("-", 1)
        return (a.strip(), b.strip())
    return ("", "")

def _map_dhcp_service_for_csv(raw: Optional[str]) -> str:
    if not raw:
        return ""
    raw = str(raw).strip().lower()
    if raw == "inherit":
        return "on"
    if raw == "no_dhcp":
        return "off"
    if raw == "non-airgapped":
        return "non-airgapped"
    return raw

def vlans_to_csv_rows(vlans: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for v in vlans:
        name   = (v.get("display_name") or v.get("name") or "").strip()
        tag    = str(v.get("tag") or "").strip()
        subnet = str(v.get("subnet") or "").strip()
        iface  = (v.get("interface") or "").strip()
        zone   = (v.get("zone") or "").strip()

        gw = (v.get("default_gateway") or "").strip()
        if not gw:
            gw = (v.get("start_ip") or "").strip()
            if not gw:
                a, _ = _split_range(v)
                gw = a

        dhcp_start, dhcp_end = _split_range(v)
        status = (v.get("status") or "").strip().lower()
        enabled = "TRUE" if status == "provisioned" else "FALSE"
        share_over_vpn = "TRUE" if bool(v.get("share_over_vpn", False)) else "FALSE"
        dhcp_service_disp = _map_dhcp_service_for_csv(v.get("dhcp_service"))

        out.append({
            "name": name,
            "tag": tag,
            "subnet": subnet,
            "default_gateway": gw,
            "dhcp_start": dhcp_start,
            "dhcp_end": dhcp_end,
            "interface": iface,
            "zone": zone,
            "enabled": enabled,
            "share_over_vpn": share_over_vpn,
            "dhcp_service": dhcp_service_disp,
        })
    return out

def write_vlans_csv(vlans: List[Dict[str, Any]], path: pathlib.Path):
    rows = vlans_to_csv_rows(vlans)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=VLAN_CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)

# ------------------------
# sites.csv helpers (HA columns supported, no LAN column)
# ------------------------
CSV_HEADER = (
    "site_name,gateway_name,gateway_name_b,city,country,"
    "wan0_ip,wan0_mask,wan0_gw,wan1_ip,wan1_mask,wan1_gw,"
    "template_name,template_id,per_site_dns,dhcp_server_ip,zia_location_name,"
    "wan_interface_name,wan1_interface_name,vlans_file,post\n"
)

def ensure_sites_csv_header():
    if not CSV_PATH.exists():
        CSV_PATH.write_text(CSV_HEADER, encoding="utf-8")

def upsert_sites_csv_row(row: Dict[str, str]):
    ensure_sites_csv_header()
    existing: List[Dict[str, str]] = []
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            existing = list(csv.DictReader(f))

    out: List[Dict[str, str]] = []
    seen = False
    for r in existing:
        if r.get("site_name", "").strip().lower() == row["site_name"].strip().lower():
            out.append(row); seen = True
        else:
            out.append(r)
    if not seen:
        out.append(row)

    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        writer.writeheader()
        writer.writerows(out)

# ------------------------
# helpers
# ------------------------
def is_wan_vlan(v: Dict[str, Any]) -> bool:
    zone = (v.get("zone") or "").strip().lower()
    if zone.startswith("wan"):
        return True
    name = (v.get("display_name") or v.get("name") or "").strip().lower()
    if name.startswith("wan"):
        return True
    return False

def is_ha_internal_vlan(v: Dict[str, Any]) -> bool:
    zone = (v.get("zone") or "").strip().lower()
    if zone.startswith("ha"):
        return True
    name = (v.get("display_name") or v.get("name") or "").strip().lower()
    tag  = str(v.get("tag") or "").strip()
    if name.startswith("ha-") and tag == "1":
        return True
    return False

# ------------------------
# Main
# ------------------------
def main():
    ap = argparse.ArgumentParser(
        description="List sites OR pull one by name; saves VLANs (JSON+CSV) and updates sites.csv"
    )
    ap.add_argument("--site-name", help="Human site name from the UI (e.g. 'Utrecht-Branch')")
    ap.add_argument("--json-only", action="store_true", help="Skip writing VLAN CSV")
    ap.add_argument("--include-wan", action="store_true", help="Include WAN VLANs in the CSV (default: excluded)")
    ap.add_argument("--include-ha", action="store_true", help="Include HA internal VLAN(s) in the CSV (default: excluded)")
    ap.add_argument("--list-templates", action="store_true", help="List templates (name, deployment_type, platform_type, id)")
    ap.add_argument("--template-search", default="", help="Optional name filter for --list-templates (uses API 'search' param)")
    args = ap.parse_args()

    # Handle template listing early-out
    if args.list_templates:
        tpls = fetch_templates(args.template_search)
        print_templates(tpls)
        return

    rows = list_gateways_rows()

    # If no site name, list sites and exit
    if not args.site_name:
        print_site_list(rows)
        return

    row = match_row_by_name(rows, args.site_name)
    if not row:
        print(f"ERROR: site not found: {args.site_name}", file=sys.stderr)
        sys.exit(1)

    # Resolve site_id for v2/Network
    ci = row.get("cluster_info") or {}
    site_id = ci.get("site_id") or row.get("site_id") or row.get("id")
    if not site_id:
        print("ERROR: Unable to resolve site_id for v2/Network.", file=sys.stderr)
        sys.exit(1)

    # VLANs
    vlans_all = get_vlans_v2_network(str(site_id))
    vlan_json_path = OUT_VLANS_DIR / f"{args.site_name}.json"
    vlan_json_path.write_text(json.dumps(vlans_all, indent=2) + "\n", encoding="utf-8")
    print(f"Saved VLANs JSON: {vlan_json_path} (count={len(vlans_all)})")

    # CSV is filtered view:
    vlans = vlans_all
    filtered = False
    if not args.include_wan:
        vlans = [v for v in vlans if not is_wan_vlan(v)]
        filtered = True
    if not args.include_ha:
        before = len(vlans)
        vlans = [v for v in vlans if not is_ha_internal_vlan(v)]
        if len(vlans) != before:
            filtered = True

    if filtered:
        print(f"Filtered CSV view. WAN included={args.include_wan}, HA included={args.include_ha}. CSV count={len(vlans)}")

    if not args.json_only:
        vlan_csv_path = OUT_VLANS_DIR / f"{args.site_name}.csv"
        write_vlans_csv(vlans, vlan_csv_path)
        print(f"Saved VLANs CSV : {vlan_csv_path}")

    # --- Extract per-node WAN fields (supports standalone or HA) ---
    gws = row.get("gateways") or ci.get("gateways") or []
    gw_a = gws[0] if isinstance(gws, list) and len(gws) >= 1 else {}
    gw_b = gws[1] if isinstance(gws, list) and len(gws) >= 2 else {}

    gateway_name_a = row.get("gateway_name") or gw_a.get("gateway_name") or row.get("name") or ""
    gateway_name_b = gw_b.get("gateway_name", "")

    wan0_ip   = gw_a.get("wan_ip_address", "")
    wan0_mask = gw_a.get("wan_subnet_mask", "")
    wan0_gw   = gw_a.get("default_gw_ip", "")
    wan0_if   = gw_a.get("wan_interface", "") or row.get("wan_interface_name","ge5")

    wan1_ip   = gw_b.get("wan_ip_address", "")
    wan1_mask = gw_b.get("wan_subnet_mask", "")
    wan1_gw   = gw_b.get("default_gw_ip", "")
    wan1_if   = gw_b.get("wan_interface", "")

    # sites.csv row (defaults you can edit before bulk_create) — leave template_id BLANK on purpose
    csv_row = {
        "site_name":           args.site_name,
        "gateway_name":        gateway_name_a,
        "gateway_name_b":      gateway_name_b,

        "city":                (row.get("location") or {}).get("city","") if isinstance(row.get("location"), dict) else row.get("city",""),
        "country":             (row.get("location") or {}).get("country","") if isinstance(row.get("location"), dict) else row.get("country",""),

        "wan0_ip":             wan0_ip,
        "wan0_mask":           wan0_mask,
        "wan0_gw":             wan0_gw,
        "wan1_ip":             wan1_ip,
        "wan1_mask":           wan1_mask,
        "wan1_gw":             wan1_gw,

        "template_name":       row.get("template_name","") or ci.get("template_name",""),
        "template_id":         "",  # intentionally blank; resolve by name at runtime in bulk_create.py
        "per_site_dns":        ci.get("per_site_dns","") or row.get("per_site_dns",""),
        "dhcp_server_ip":      ci.get("dhcp_server_ip","") or row.get("dhcp_server_ip",""),
        "zia_location_name":   row.get("zia_location_name","") or row.get("location_display_name","") or args.site_name,

        "wan_interface_name":  wan0_if,
        "wan1_interface_name": wan1_if,

        "vlans_file":          (OUT_VLANS_DIR / f"{args.site_name}.csv").as_posix(),
        "post":                "0",
    }

    upsert_sites_csv_row(csv_row)
    print(f"Upserted row in {CSV_PATH}: {csv_row}")

if __name__ == "__main__":
    main()