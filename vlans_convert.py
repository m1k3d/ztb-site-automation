#!/usr/bin/env python3
# vlans_convert.py — Utility to convert pulled VLAN JSON into CSV format
#
# Author: Mike Dechow (@m1k3d)
# Repo: github.com/m1k3d/ztb-site-automation
# License: MIT
#
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
#       name, tag, subnet, start_ip, dhcp_start, dhcp_end, interface, zone, enabled, share_over_vpn
#   - "enabled" in CSV reflects UI status:
#       status == "provisioned"  -> enabled = TRUE
#       otherwise                -> enabled = FALSE
#   - start_ip falls back to default_gateway if start_ip missing in JSON.

import argparse
import json
import csv
from pathlib import Path
from typing import Any, Dict, List, Tuple


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Convert VLAN JSON -> CSV (for bulk_create)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--site-name", help="Human site name (reads vlans/<site>.json, writes vlans/<site>.csv)")
    g.add_argument("--from-json", help="Path to a VLAN JSON file")
    ap.add_argument("--to-csv", help="Output CSV path (optional; defaults beside input)")
    ap.add_argument("--include-id", action="store_true", help="Add 'vlan_id' column (raw API id) to the CSV")
    return ap.parse_args()


def read_json(path: Path) -> Any:
    if not path.exists():
        raise FileNotFoundError(f"JSON file not found: {path}")
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except Exception as e:
        raise ValueError(f"Failed to parse JSON from {path}: {e}") from e


def normalize_rows(data: Any) -> List[Dict[str, Any]]:
    """
    Handle the common shapes we’ve seen:
      - { "rows": [ ... ] }
      - { "result": { "rows": [ ... ] } }
      - [ ... ]  (already an array)
    """
    if isinstance(data, dict):
        if isinstance(data.get("rows"), list):
            return data["rows"]
        if isinstance(data.get("result"), dict) and isinstance(data["result"].get("rows"), list):
            return data["result"]["rows"]
        return []
    if isinstance(data, list):
        return data
    return []


def parse_dhcp_range(v: Any) -> Tuple[str, str]:
    """
    Accepts:
      - "172.16.67.1-172.16.67.254"
      - [["172.16.67.1","172.16.67.254"], ...]
      - None / ""
    Returns (start, end) or ("","") if missing.
    """
    if isinstance(v, str) and "-" in v:
        a, b = v.split("-", 1)
        return a.strip(), b.strip()
    if isinstance(v, list) and v and isinstance(v[0], list) and len(v[0]) == 2:
        return str(v[0][0]), str(v[0][1])
    return "", ""


def to_out_row(vlan: Dict[str, Any], include_id: bool) -> Dict[str, Any]:
    # DHCP parse (accepts either dhcp_range string or range_list array)
    dhcp_start, dhcp_end = parse_dhcp_range(vlan.get("dhcp_range") or vlan.get("range_list"))

    # Enabled follows UI status (provisioned == enabled)
    enabled = "TRUE" if vlan.get("status") == "provisioned" else "FALSE"

    # start_ip: prefer explicit start_ip, else default_gateway, else blank
    start_ip = (vlan.get("start_ip") or vlan.get("default_gateway") or "").strip()

    row = {
        "name":           (vlan.get("display_name") or vlan.get("name") or "").strip(),
        "tag":            str(vlan.get("tag") or "").strip(),
        "subnet":         str(vlan.get("subnet") or "").strip(),
        "start_ip":       start_ip,
        "dhcp_start":     dhcp_start,
        "dhcp_end":       dhcp_end,
        "interface":      (vlan.get("interface") or "").strip(),
        "zone":           (vlan.get("zone") or "").strip(),
        "enabled":        enabled,
        "share_over_vpn": "TRUE" if bool(vlan.get("share_over_vpn", False)) else "FALSE",
    }
    if include_id:
        row = {"vlan_id": vlan.get("id", "")} | row
    return row


def write_csv(rows: List[Dict[str, Any]], out_path: Path, include_id: bool) -> None:
    headers = (
        ["vlan_id", "name", "tag", "subnet", "start_ip", "dhcp_start", "dhcp_end", "interface", "zone", "enabled", "share_over_vpn"]
        if include_id else
        ["name", "tag", "subnet", "start_ip", "dhcp_start", "dhcp_end", "interface", "zone", "enabled", "share_over_vpn"]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(rows)


def main():
    args = parse_args()

    # Resolve in/out paths
    if args.site_name:
        json_path = Path("vlans") / f"{args.site_name}.json"
        csv_path = Path("vlans") / f"{args.site_name}.csv" if not args.to_csv else Path(args.to_csv)
    else:
        json_path = Path(args.from_json)
        csv_path = Path(args.to_csv) if args.to_csv else json_path.with_suffix(".csv")

    # Load + normalize
    data = read_json(json_path)
    src_rows = normalize_rows(data)

    # Transform
    out_rows = [to_out_row(v, args.include_id) for v in src_rows]

    # Write
    write_csv(out_rows, csv_path, include_id=args.include_id)
    print(f"Wrote VLAN CSV  →  {csv_path}")
    print(f"Rows: {len(out_rows)}")


if __name__ == "__main__":
    main()