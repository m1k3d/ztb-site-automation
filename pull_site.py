#!/usr/bin/env python3
# pull_site.py — list sites OR pull one site by name, save VLAN JSON+CSV, and upsert sites.csv
# Author: Mike Dechow (@m1k3d)
# Repo: github.com/m1k3d/ztb-site-automation
# License: MIT
# version: 1.6.1
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
#   - **Templates:** `--list-templates` shows name/deployment_type/platform_type/id.
#                   Exports use template_name; bulk_create resolves its ID at runtime.
#
#   - **Auth QoL (single-run)**:
#       · The shared client obtains a missing BEARER through the login function.
#       · On HTTP 401, it refreshes the session token and retries ONCE.
#       · Messages are visible (no hidden background behavior).

import os, sys, json, csv, argparse, pathlib, tempfile
from typing import Any, Dict, List, Optional, Tuple
import requests
import ipaddress

# ------------------------
# Paths
# ------------------------
ROOT = pathlib.Path(__file__).resolve().parent
OUT_VLANS_DIR = ROOT / "vlans"
CSV_PATH = ROOT / "sites.csv"
# Shared settings are loaded only when main() runs, never during import.
from automation_config import Settings
from api_client import ZTBClient

client = None
BASE_V3 = BASE_V2 = ORIGIN = REFERER = ""


def _request_with_auto_refresh(method: str, url: str, **kwargs):
    if client is None:
        raise RuntimeError("Initialize the CLI before making API requests")
    return client.request(method, url, **kwargs)

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

# ---- Locations API (settings/locations) ----
def fetch_locations() -> List[Dict[str, Any]]:
    # Based on user screenshot: GET /api/v3/settings/locations?no_cache=true&refresh_token=enabled
    params = {
        "no_cache": "true",
        "refresh_token": "enabled",
    }
    data = get_json_v3_no_trailing("settings/locations", params=params)
    if isinstance(data, dict):
        return data.get("locations", [])
    return []

def print_locations(locations: List[Dict[str, Any]]):
    if not locations:
        print("No locations found.")
        return
    hdr = f"{'name':40}  {'id'}"
    print(hdr)
    print("-" * len(hdr))
    # Sort by name for nicer output
    for loc in sorted(locations, key=lambda x: (x.get("name") or "").lower()):
        name = (loc.get("name") or "")[:40]
        lid  = loc.get("id") or ""
        print(f"{name:40}  {lid}")

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
# ------------------------
# sites.csv helpers (HA columns supported, no LAN column)
# ------------------------
CSV_HEADER = (
    "site_name,gateway_name,gateway_name_b,city,country,"
    "wan0_ip,wan0_mask,wan0_gw,wan1_ip,wan1_mask,wan1_gw,"
    "template_name,wan_dns,private_dns,dhcp_server_ip,zia_location_name,"
    "location_type,location_template_name,"
    "wan_interface_name,wan1_interface_name,vlans_file,post,appc_provision\n"
)

def upsert_sites_csv_row(row: Dict[str, str]):
    existing: List[Dict[str, str]] = []
    fieldnames = CSV_HEADER.strip().split(",")
    if CSV_PATH.exists():
        with open(CSV_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or fieldnames
            existing = list(reader)

    out: List[Dict[str, str]] = []
    seen = False
    for r in existing:
        if r.get("site_name", "").strip().lower() == row["site_name"].strip().lower():
            merged = {**r, **row}
            # These are deployment choices, not values recovered by the site API.
            for key in ("location_type", "location_template_name", "location_template_id"):
                if str(r.get(key) or "").strip():
                    merged[key] = r[key]
            out.append(merged); seen = True
        else:
            out.append(r)
    if not seen:
        out.append(row)

    # Keep old/custom columns and add new columns regardless of row order.
    for item in out:
        if None in item:
            raise ValueError("sites.csv contains a row with more values than headers; file unchanged")
        for key in item:
            if key not in fieldnames:
                fieldnames.append(key)

    # Replace only after the entire CSV has been written successfully.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", newline="", encoding="utf-8", dir=CSV_PATH.parent,
            prefix=CSV_PATH.name + ".", suffix=".tmp", delete=False,
        ) as f:
            temp_path = pathlib.Path(f.name)
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(out)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, CSV_PATH)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()

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

def parse_private_dns_members(data: Any, site_id: str) -> str:
    """Read confirmed membership responses; unknown shapes are not empty DNS."""
    if not isinstance(data, dict):
        raise ValueError("expected a private-DNS response object")
    if "result" in data:
        members = data["result"]
        if not isinstance(members, list):
            raise ValueError("private-DNS result must be a list")
        attributes = []
        for member in members:
            if not isinstance(member, dict):
                raise ValueError("private-DNS membership must be an object")
            if "site_id" in member and str(member["site_id"]) != str(site_id):
                raise ValueError("private-DNS membership belongs to a different site")
            attributes.append(member.get("membership_info"))
    elif "member_attributes" in data:
        attributes = [data["member_attributes"]]
    else:
        raise ValueError("unrecognized private-DNS response structure")

    ips = []
    for attrs in attributes:
        if not isinstance(attrs, dict) or not isinstance(attrs.get("ip_prefix"), list):
            raise ValueError("private-DNS membership requires an ip_prefix list")
        for prefix in attrs["ip_prefix"]:
            if not isinstance(prefix, str) or not prefix.strip():
                raise ValueError("private-DNS prefixes must be nonempty strings")
            try:
                address = ipaddress.IPv4Interface(prefix.strip())
            except ValueError:
                raise ValueError("private-DNS response contains an invalid or unsupported IPv4 prefix") from None
            # Only /32 denotes a single host. Keep other prefix lengths intact.
            value = str(address.ip) if address.network.prefixlen == 32 else str(address)
            if value not in ips:
                ips.append(value)
    return ",".join(ips)


def get_private_dns_members(site_id: str) -> str:
    """
    Fetch Private DNS members for the site from System-Private-DNS-Servers-Group.
    Returns comma-separated list of IPs (without /32).
    """
    url = f"{BASE_V2}/group-membership"
    params = {
        "site_id": site_id,
        "group_name": "System-Private-DNS-Servers-Group",
        "refresh_token": "enabled"
    }
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
    }
    
    try:
        data = get_json(url, params=params, headers=headers)
    except (RuntimeError, ValueError, requests.RequestException) as exc:
        raise RuntimeError("Private DNS lookup failed; export stopped before writing files. Check access/connectivity and retry; use --debug for HTTP status.") from exc
    try:
        return parse_private_dns_members(data, site_id)
    except ValueError as exc:
        raise ValueError(f"{exc}; export stopped before writing files. Check the private-DNS response format.") from None

# ------------------------
# Main
# ------------------------
def main(argv=None):
    global client, BASE_V3, BASE_V2, ORIGIN, REFERER
    ap = argparse.ArgumentParser(
        description="List sites OR pull one by name; saves VLANs (JSON+CSV) and updates sites.csv"
    )
    ap.add_argument("--site-name", help="Human site name from the UI (e.g. 'Utrecht-Branch')")
    ap.add_argument("--json-only", action="store_true", help="Skip writing VLAN CSV")
    ap.add_argument("--include-wan", action="store_true", help="Include WAN VLANs in the CSV (default: excluded)")
    ap.add_argument("--include-ha", action="store_true", help="Include HA internal VLAN(s) in the CSV (default: excluded)")
    ap.add_argument("--list-templates", action="store_true", help="List templates (name, deployment_type, platform_type, id)")
    ap.add_argument("--template-search", default="", help="Optional name filter for --list-templates (uses API 'search' param)")
    ap.add_argument("--list-locations", action="store_true", help="List ZIA locations (name, id)")
    ap.add_argument("--env-file", default=".env", help="Credential file; process environment takes precedence")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(argv)
    try:
        config = Settings.load(args.env_file)
        errors = config.errors()
        if errors:
            raise ValueError("; ".join(errors))
        client = ZTBClient(config, debug=args.debug)
        BASE_V3, BASE_V2, ORIGIN, REFERER = client.api_v3, client.api_v2, client.origin, client.referer
        return export_or_list(args) or 0
    except (ValueError, RuntimeError, OSError, requests.RequestException) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if client is not None:
            client.close()
            client = None


def export_or_list(args):

    # Handle location listing early-out
    if args.list_locations:
        locs = fetch_locations()
        print_locations(locs)
        return

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
    if pathlib.Path(args.site_name).name != args.site_name or args.site_name in (".", ".."):
        raise ValueError("site name must not contain path separators")
    # Resolve DNS before writing any exports; failed reads must not erase saved values.
    private_dns_ips = get_private_dns_members(str(site_id))
    OUT_VLANS_DIR.mkdir(exist_ok=True)
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

    # Export the human-readable template name; the deployment engine resolves its ID.
    # Omit template ID columns so re-export preserves overrides without adding columns.
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
        "wan_dns":             ci.get("per_site_dns","") or row.get("per_site_dns",""),
        "private_dns":         private_dns_ips,
        "dhcp_server_ip":      ci.get("dhcp_server_ip","") or row.get("dhcp_server_ip",""),
        "zia_location_name":   row.get("zia_location_name","") or row.get("location_display_name","") or args.site_name,
        "location_type":       "auto",
        "location_template_name": "Default Location Template",

        "wan_interface_name":  wan0_if,
        "wan1_interface_name": wan1_if,

        "vlans_file":          (OUT_VLANS_DIR / f"{args.site_name}.{'json' if args.json_only else 'csv'}").relative_to(CSV_PATH.parent).as_posix(),
        "post":                "0",
        "appc_provision":      "0",
    }

    upsert_sites_csv_row(csv_row)
    print(f"Upserted row in {CSV_PATH}: {csv_row}")

if __name__ == "__main__":
    raise SystemExit(main())
