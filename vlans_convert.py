#!/usr/bin/env python3
# vlans_convert.py — Utility to convert pulled VLAN JSON into CSV format
#
# Author: Mike Dechow (@m1k3d)
# Repo: github.com/m1k3d/ztb-site-automation
# License: MIT
#version: 1.0.0
# Usage (either form works):
#   # Use a site name (reads vlans/<site>.json, writes vlans/<site>.csv)
#   python3 vlans_convert.py --site-name "Utrecht-Branch"
#
#   # Or point directly at a JSON file and choose an output CSV
#   python3 vlans_convert.py --from-json vlans/Utrecht-Branch.json --to-csv vlans/Utrecht-Branch.csv
#
#   # Include the raw VLAN id column as well
#   python3 vlans_convert.py --site-name "Utrecht-Branch" --include-id
#
# Notes:
#   - CSV columns are aligned with bulk_create.py expectations:
#       name, tag, subnet, default_gateway, dhcp_start, dhcp_end, dhcp_service, interface, zone, enabled, share_over_vpn
#   - "enabled" in CSV reflects UI status:
#       status == "provisioned"  -> enabled = TRUE
#       otherwise                -> enabled = FALSE
#   - default_gateway prefers start_ip, then default_gateway from JSON.

import argparse, json, csv
from pathlib import Path
from typing import Any, Dict, List, Tuple

def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return s in ("1", "true", "yes", "y")

def norm_dhcp_service(val: str) -> str:
    v = (val or "").strip().lower().replace("-", "_")
    if v == "on":
        return "inherit"
    if v in ("inherit", "non_airgapped", "no_dhcp"):
        return v
    # sensible default: if caller omitted, we'll let bulk_create decide based on dhcp_range,
    # but for display here we return "inherit"
    return "inherit"

def display_dhcp_service(val: str) -> str:
    v = norm_dhcp_service(val)
    return "on" if v == "inherit" else v  # show "on" in CSV for readability

def parse_args():
    ap = argparse.ArgumentParser(description="Convert VLAN JSON -> CSV")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--site-name", help="Read vlans/<site>.json, write vlans/<site>.csv")
    g.add_argument("--from-json", help="Path to VLAN JSON")
    ap.add_argument("--to-csv", help="Output CSV path")
    ap.add_argument("--include-id", action="store_true", help="Include 'vlan_id' column")
    return ap.parse_args()

def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise SystemExit(f"ERROR: failed to read/parse JSON {path}: {e}")

def normalize_rows(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            return data["rows"]
        if isinstance(data.get("result"), dict) and isinstance(data["result"].get("rows"), list):
            return data["result"]["rows"]
    if isinstance(data, list):
        return data
    return []

def parse_dhcp_range(v: Any) -> Tuple[str,str]:
    if isinstance(v, str) and "-" in v:
        a, b = v.split("-", 1)
        return a.strip(), b.strip()
    if isinstance(v, list) and v and isinstance(v[0], list) and len(v[0]) == 2:
        return str(v[0][0]), str(v[0][1])
    return "", ""

def to_out_row(vlan: Dict[str, Any], include_id: bool) -> Dict[str, Any]:
    # DHCP range (accept dhcp_range "a-b" or range_list [[a,b],...])
    dhcp_start, dhcp_end = parse_dhcp_range(vlan.get("dhcp_range") or vlan.get("range_list"))

    # dhcp_service for display: "on" for inherit, else "non_airgapped"/"no_dhcp"
    raw_service = vlan.get("dhcp_service")
    # If API omitted it, infer from presence of a DHCP range
    if not raw_service:
        raw_service = "inherit" if (dhcp_start or dhcp_end) else "no_dhcp"
    display_service = display_dhcp_service(raw_service)

    # enabled follows UI state (status)
    enabled = "TRUE" if vlan.get("status") == "provisioned" else "FALSE"

    row = {
        "tag":            str(vlan.get("tag", "")).strip(),
        "name":           (vlan.get("name") or vlan.get("display_name") or "").strip(),
        "display_name":   (vlan.get("display_name") or vlan.get("name") or "").strip(),
        "subnet":         str(vlan.get("subnet", "")).strip(),
        "default_gateway": (vlan.get("start_ip") or vlan.get("default_gateway") or "").strip(),
        "dhcp_start":     dhcp_start,
        "dhcp_end":       dhcp_end,
        "dhcp_service":   display_service,   # <-- for the CSV we show "on"/"non_airgapped"/"no_dhcp"
        "interface":      (vlan.get("interface") or "").strip(),
        "zone":           (vlan.get("zone") or "").strip(),
        "enabled":        enabled,
        "share_over_vpn": "TRUE" if _as_bool(vlan.get("share_over_vpn")) else "FALSE",
    }
    if include_id:
        row = {"vlan_id": vlan.get("id", "")} | row
    return row

def write_csv(rows: List[Dict[str, Any]], path: Path, include_id: bool):
    headers = (["vlan_id"] if include_id else []) + [
        "tag","name","display_name","subnet","default_gateway",
        "dhcp_start","dhcp_end","dhcp_service",
        "interface","zone","enabled","share_over_vpn"
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)

def main():
    a = parse_args()
    if a.site_name:
        j = Path("vlans") / f"{a.site_name}.json"
        c = Path("vlans") / f"{a.site_name}.csv" if not a.to_csv else Path(a.to_csv)
    else:
        j = Path(a.from_json)
        c = Path(a.to_csv) if a.to_csv else j.with_suffix(".csv")

    rows = [to_out_row(v, a.include_id) for v in normalize_rows(read_json(j))]
    write_csv(rows, c, a.include_id)
    print(f"Wrote {c} ({len(rows)} rows)")

if __name__ == "__main__":
    main()