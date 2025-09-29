#!/usr/bin/env python3
# pull_site.py — list sites OR pull one site by name, save VLAN JSON+CSV, and upsert sites.csv
# Author: Mike Dechow (@m1k3d)
# Repo: github.com/m1k3d/ztb-site-automation
# License: MIT
#
# Usage:
#   python3 pull_site.py                   # lists sites
#   python3 pull_site.py --site-name "Utrecht-Branch"
#   python3 pull_site.py --site-name "Utrecht-Branch" --include-wan
#   python3 pull_site.py --site-name "Utrecht-Branch" --json-only
#
# Notes:
#   - Saves VLAN definitions to vlans/<site>.json and vlans/<site>.csv
#   - CSV now includes a "share_over_vpn" column (TRUE/FALSE)
#   - By default, WAN VLANs are excluded from the CSV (use --include-wan to keep them)
#   - Updates or inserts site row into sites.csv for bulk_create.py

import os, sys, json, csv, argparse, pathlib
from typing import Any, Dict, List, Optional, Tuple
import requests

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
        if k and k not in os.environ:
            os.environ[k] = v

load_env_file(".env")

# ------------------------
# Env / session
# ------------------------
def get_session_and_bases() -> Tuple[requests.Session, str, str, str, str]:
    base_v3 = os.environ.get("ZIA_API_BASE", "").rstrip("/")
    bearer  = os.environ.get("BEARER", "").strip()
    if not base_v3 or not bearer:
        print("ERROR: Missing ZIA_API_BASE or BEARER in environment (check .env).", file=sys.stderr)
        sys.exit(1)
    if "/api/" not in base_v3:
        print("ERROR: ZIA_API_BASE should look like https://<tenant>-api.../api/v3", file=sys.stderr)
        sys.exit(1)

    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json",
        "User-Agent": "pull_site.py",
    })

    base_v2 = base_v3.replace("/api/v3", "/api/v2")
    origin_host = base_v3.split("/api/")[0].replace("-api.", ".")
    referer = origin_host + "/"
    return s, base_v3, base_v2, origin_host, referer

session, BASE_V3, BASE_V2, ORIGIN, REFERER = get_session_and_bases()

# ------------------------
# Paths
# ------------------------
ROOT = pathlib.Path(__file__).resolve().parent
OUT_VLANS_DIR = ROOT / "vlans"
OUT_VLANS_DIR.mkdir(exist_ok=True)
CSV_PATH = ROOT / "sites.csv"

# ------------------------
# HTTP helpers
# ------------------------
def get_json(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
    r = session.get(url, params=params, headers=headers, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code} {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        raise ValueError(f"Non-JSON response from {url}: {r.text[:300]}")

def get_json_v3(path: str, params: Optional[Dict[str, Any]] = None) -> Any:
    return get_json(f"{BASE_V3}/{path.lstrip('/')}", params=params)

def get_vlans_v2_network(site_id: str) -> List[Dict[str, Any]]:
    """
    Tenant exposes VLANs via /api/v2/Network/?siteId=...
    We mirror the browser Origin/Referer.
    Returns a list of vlan dicts.
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
        if isinstance(rows, list):
            return rows
        return []
    if isinstance(data, list):
        return data
    return []

# ------------------------
# Data access
# ------------------------
def list_gateways_rows() -> List[Dict[str, Any]]:
    data = get_json_v3("Gateway/", params={
        "gateway_type": "isolation",
        "sortdir": "asc",
        "limit": "100",
        "page": 0,
        "refresh_token": "enabled",
    })
    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            return data["rows"]
        if isinstance(data.get("result"), dict) and isinstance(data["result"].get("rows"), list):
            return data["result"]["rows"]
    raise ValueError("Unexpected /Gateway/ response; could not find rows list.")

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
    "name", "tag", "subnet", "start_ip", "dhcp_start", "dhcp_end",
    "interface", "zone", "enabled", "share_over_vpn"
]

def _split_range(d: Dict[str, Any]) -> Tuple[str, str]:
    # Prefer range_list [[a,b]] then dhcp_range "a-b"
    r = d.get("range_list")
    if isinstance(r, list) and r and isinstance(r[0], list) and len(r[0]) == 2:
        a, b = r[0][0] or "", r[0][1] or ""
        return (a, b)
    dr = d.get("dhcp_range")
    if isinstance(dr, str) and "-" in dr:
        a, b = dr.split("-", 1)
        return (a.strip(), b.strip())
    return ("", "")

def vlans_to_csv_rows(vlans: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for v in vlans:
        name   = (v.get("display_name") or v.get("name") or "").strip()
        tag    = str(v.get("tag") or "").strip()
        subnet = str(v.get("subnet") or "").strip()
        iface  = (v.get("interface") or "").strip()
        zone   = (v.get("zone") or "").strip()
        start_ip = (v.get("start_ip") or "").strip()
        if not start_ip:
            # fall back to first of range if present
            a, _ = _split_range(v)
            start_ip = a
        dhcp_start, dhcp_end = _split_range(v)
        enabled = "TRUE" if bool(v.get("enforcement_on", True)) else "FALSE"
        share_over_vpn = "TRUE" if bool(v.get("share_over_vpn", False)) else "FALSE"

        out.append({
            "name": name,
            "tag": tag,
            "subnet": subnet,
            "start_ip": start_ip,
            "dhcp_start": dhcp_start,
            "dhcp_end": dhcp_end,
            "interface": iface,
            "zone": zone,
            "enabled": enabled,
            "share_over_vpn": share_over_vpn,
        })
    return out

def write_vlans_csv(vlans: List[Dict[str, Any]], path: pathlib.Path):
    rows = vlans_to_csv_rows(vlans)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=VLAN_CSV_FIELDS)
        w.writeheader()
        w.writerows(rows)

# ------------------------
# sites.csv helpers
# ------------------------
CSV_HEADER = (
    "site_name,gateway_name,city,country,wan0_ip,wan0_mask,wan0_gw,"
    "template_name,template_id,per_site_dns,dhcp_server_ip,zia_location_name,"
    "lan_interface_name,wan_interface_name,vlans_file,post\n"
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
    """Heuristic to identify WAN VLANs: prefer zone label; fall back to name hint."""
    zone = (v.get("zone") or "").strip().lower()
    if zone.startswith("wan"):
        return True
    name = (v.get("display_name") or v.get("name") or "").strip().lower()
    if name.startswith("wan"):
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
    args = ap.parse_args()

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

    vlans = vlans_all if args.include_wan else [v for v in vlans_all if not is_wan_vlan(v)]
    if not args.include_wan:
        print(f"Filtered out WAN VLANs. CSV count={len(vlans)}")

    if not args.json_only:
        vlan_csv_path = OUT_VLANS_DIR / f"{args.site_name}.csv"
        write_vlans_csv(vlans, vlan_csv_path)
        print(f"Saved VLANs CSV : {vlan_csv_path}")

    # sites.csv row (defaults you can edit before bulk_create)
    csv_row = {
        "site_name":           args.site_name,
        "gateway_name":        row.get("gateway_name") or row.get("name") or "",
        "city":                (row.get("location") or {}).get("city","") if isinstance(row.get("location"), dict) else row.get("city",""),
        "country":             (row.get("location") or {}).get("country","") if isinstance(row.get("location"), dict) else row.get("country",""),
        "wan0_ip":             "",
        "wan0_mask":           "",
        "wan0_gw":             "",
        "template_name":       row.get("template_name","") or ci.get("template_name",""),
        "template_id":         row.get("template_id","")   or ci.get("template_id",""),
        "per_site_dns":        ci.get("per_site_dns","") or row.get("per_site_dns",""),
        "dhcp_server_ip":      ci.get("dhcp_server_ip","") or row.get("dhcp_server_ip",""),
        "zia_location_name":   row.get("zia_location_name","") or row.get("location_display_name","") or args.site_name,
        "lan_interface_name":  row.get("lan_interface_name","ge2"),
        "wan_interface_name":  row.get("wan_interface_name","ge5"),
        "vlans_file":          (OUT_VLANS_DIR / f"{args.site_name}.csv").as_posix(),
        "post":                "0",
    }

    upsert_sites_csv_row(csv_row)
    print(f"Upserted row in {CSV_PATH}: {csv_row}")

if __name__ == "__main__":
    main()