#!/usr/bin/env python3
# pull_site.py — minimal, bearer-based pull that matches your tenant's working endpoints

import os, sys, json, csv, argparse, pathlib
from typing import Any, Dict, List, Optional
import requests

# ------------------------
# Env / session
# ------------------------
def get_session_and_bases():
    zia_api_base = os.environ.get("ZIA_API_BASE", "").rstrip("/")
    bearer = os.environ.get("BEARER", "").strip()
    if not zia_api_base or not bearer:
        print("ERROR: Missing ZIA_API_BASE or BEARER in .env", file=sys.stderr)
        sys.exit(1)

    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json",
    })

    # Derive v2 base and browser-y Origin/Referer host (swap -api. → .)
    if "/api/" not in zia_api_base:
        print("ERROR: ZIA_API_BASE should look like https://<tenant>-api.../api/v3", file=sys.stderr)
        sys.exit(1)

    v2_base = zia_api_base.replace("/api/v3", "/api/v2")
    origin_host = zia_api_base.split("/api/")[0].replace("-api.", ".")
    referer = origin_host + "/"

    return s, zia_api_base, v2_base, origin_host, referer

session, BASE_V3, BASE_V2, ORIGIN, REFERER = get_session_and_bases()

# ------------------------
# Files / paths
# ------------------------
OUT_VLANS_DIR = pathlib.Path("vlans"); OUT_VLANS_DIR.mkdir(exist_ok=True)
CSV_PATH = pathlib.Path("sites.csv")

# ------------------------
# HTTP helpers
# ------------------------
def get_json_v3(path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    url = f"{BASE_V3}/{path.lstrip('/')}"
    r = session.get(url, params=params, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code} {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        raise ValueError(f"Non-JSON response from {url}: {r.text[:300]}")

def get_vlans_v2_network(site_id: str) -> Any:
    """
    Your tenant exposes VLANs via /api/v2/Network/?siteId=...
    This function mirrors the browser headers you saw in DevTools.
    """
    url = f"{BASE_V2}/Network/"
    params = {"siteId": site_id, "refresh_token": "enabled"}
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
    }
    r = session.get(url, params=params, headers=headers, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code} {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        raise ValueError(f"Non-JSON response from {url}: {r.text[:300]}")

# ------------------------
# Data access
# ------------------------
def list_gateways_rows() -> List[Dict[str, Any]]:
    data = get_json_v3("Gateway/", params={
        "gateway_type": "isolation",
        "sortdir": "asc",
        "limit": "100",
        "page": "0",
        "refresh_token": "enabled",
    })
    # normalize possible shapes
    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            return data["rows"]
        if isinstance(data.get("result"), dict) and isinstance(data["result"].get("rows"), list):
            return data["result"]["rows"]
    raise ValueError("Unexpected /Gateway/ response; could not find rows list.")

def match_row_by_name(rows: List[Dict[str, Any]], site_name: str) -> Optional[Dict[str, Any]]:
    wanted = site_name.strip().lower()
    fields = ["location_display_name", "site_name", "zia_location_name", "name", "location"]
    for r in rows:
        for f in fields:
            v = r.get(f)
            if isinstance(v, str) and v.strip().lower() == wanted:
                return r
    return None

def try_get_vlans(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Strategy:
      A) v2 Network by siteId (matches your working DevTools call)
      B) (fallbacks) embedded or v3 detail if needed
    """
    # A) v2 network by siteId (best match for your tenant)
    site_id = (row.get("cluster_info") or {}).get("site_id") or row.get("site_id") or row.get("id")
    if site_id:
        try:
            net = get_vlans_v2_network(str(site_id))
            # Normalize shapes: {result:{rows:[...]}} OR {rows:[...]} OR [...]
            if isinstance(net, dict):
                if isinstance(net.get("result"), dict) and isinstance(net["result"].get("rows"), list):
                    return net["result"]["rows"]
                if isinstance(net.get("rows"), list):
                    return net["rows"]
            if isinstance(net, list):
                return net
        except Exception:
            pass  # fall through

    # B1) embedded
    if isinstance(row.get("vlans"), list):
        return row["vlans"]

    # B2) v3 detail by gateway_id (sometimes has lan.vlans)
    gws = row.get("gateways") or []
    gw_id = (gws[0] or {}).get("gateway_id") if gws else row.get("gateway_id")
    if gw_id:
        try:
            detail = get_json_v3(f"Gateway/{gw_id}")
            if isinstance(detail, dict):
                lan = detail.get("lan")
                if isinstance(lan, dict) and isinstance(lan.get("vlans"), list):
                    return lan["vlans"]
                if isinstance(detail.get("vlans"), list):
                    return detail["vlans"]
        except Exception:
            pass

    return []

# ------------------------
# CSV helpers
# ------------------------
CSV_HEADER = (
    "site_name,gateway_name,city,country,wan0_ip,wan0_mask,wan0_gw,"
    "template_name,per_site_dns,dhcp_server_ip,zia_location_name,"
    "lan_interface_name,wan_interface_name,vlans_file,post\n"
)

def ensure_csv_header():
    if not CSV_PATH.exists():
        CSV_PATH.write_text(CSV_HEADER, encoding="utf-8")

def upsert_csv_row(row: Dict[str, str]):
    ensure_csv_header()
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
# Main
# ------------------------
def main():
    ap = argparse.ArgumentParser(description="Pull a site by name; save VLANs and update sites.csv")
    ap.add_argument("--site-name", required=True, help="Human site name from the UI (e.g. 'Utrecht-Branch')")
    args = ap.parse_args()

    rows = list_gateways_rows()
    row = match_row_by_name(rows, args.site_name)
    if not row:
        print(f"ERROR: site not found: {args.site_name}")
        sys.exit(1)

    # VLANs
    vlans = try_get_vlans(row)
    vlan_path = OUT_VLANS_DIR / f"{args.site_name}.json"
    vlan_path.write_text(json.dumps(vlans, indent=2) + "\n", encoding="utf-8")
    print(f"Saved VLANs: {vlan_path} (count={len(vlans) if isinstance(vlans, list) else 'n/a'})")

    # CSV row (defaults you can edit later)
    csv_row = {
        "site_name":           args.site_name,
        "gateway_name":        row.get("gateway_name") or row.get("name") or "",
        "city":                (row.get("location") or {}).get("city","") if isinstance(row.get("location"), dict) else row.get("city",""),
        "country":             (row.get("location") or {}).get("country","") if isinstance(row.get("location"), dict) else row.get("country",""),
        "wan0_ip":             "",
        "wan0_mask":           "",
        "wan0_gw":             "",
        "template_name":       row.get("template_name",""),
        "per_site_dns":        (row.get("cluster_info") or {}).get("per_site_dns","") or row.get("per_site_dns",""),
        "dhcp_server_ip":      (row.get("cluster_info") or {}).get("dhcp_server_ip","") or row.get("dhcp_server_ip",""),
        "zia_location_name":   row.get("zia_location_name","") or row.get("location_display_name",""),
        "lan_interface_name":  row.get("lan_interface_name","ge2"),
        "wan_interface_name":  row.get("wan_interface_name","ge5"),
        "vlans_file":          vlan_path.as_posix(),
        "post":                "0",
    }

    upsert_csv_row(csv_row)
    print(f"Upserted row in {CSV_PATH}: {csv_row}")

if __name__ == "__main__":
    main()