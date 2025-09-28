#!/usr/bin/env python3
# bulk_create.py — create sites (v3) then add VLANs (v2) using vlans_file (via gateway_id + cluster_id)

import os, sys, csv, json, pathlib, argparse, time, ipaddress
from typing import Any, Dict, List, Tuple, Optional
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

# -------- env / session --------
def get_sessions_and_bases() -> Tuple[requests.Session, str, str, str, str]:
    base_v3 = os.environ.get("ZIA_API_BASE", "").rstrip("/")
    bearer  = os.environ.get("BEARER", "").strip()
    if not base_v3 or not bearer:
        print("ERROR: Missing ZIA_API_BASE or BEARER in .env", file=sys.stderr)
        sys.exit(1)

    if "/api/" not in base_v3:
        print("ERROR: ZIA_API_BASE should look like https://<tenant>-api.../api/v3", file=sys.stderr)
        sys.exit(1)

    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })

    base_v2 = base_v3.replace("/api/v3", "/api/v2")
    origin_host = base_v3.split("/api/")[0].replace("-api.", ".")
    referer = origin_host + "/"
    return s, base_v3, base_v2, origin_host, referer

session, API_V3, API_V2, ORIGIN, REFERER = get_sessions_and_bases()

ROOT = pathlib.Path(__file__).resolve().parent
TEMPLATE_PATH = ROOT / "site_template.json.j2"

# -------- utils --------
def read_csv_rows(p: pathlib.Path) -> List[Dict[str, str]]:
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def render_template(ctx: Dict[str, Any]) -> str:
    env = Environment(
        loader=FileSystemLoader(str(ROOT)),
        autoescape=select_autoescape(enabled_extensions=("j2",))
    )
    tmpl = env.get_template(TEMPLATE_PATH.name)
    return tmpl.render(**ctx)

def get_json(url: str, params: Optional[Dict[str, str]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
    r = session.get(url, params=params, headers=headers, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        raise ValueError(f"Non-JSON response from {url}: {r.text[:300]}")

def post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
    data = json.dumps(payload)
    return session.post(url, data=data, headers=headers, timeout=90)

# -------- VLAN loading (CSV or JSON) --------
def _clean_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1","true","yes","y")

def _split_dhcp(start: str, end: str) -> Optional[str]:
    s = (start or "").strip(); e = (end or "").strip()
    if not s and not e: return None
    if s and e: return f"{s}-{e}"
    return None

def _vlan_from_csv_row(r: Dict[str, str]) -> Dict[str, Any]:
    vlan = {
        "name": (r.get("name") or "").strip(),
        "display_name": (r.get("name") or "").strip(),
        "interface": (r.get("interface") or "").strip(),      # ge2 / ge1 / ge5
        "subnet": str(r.get("subnet") or "").strip(),         # "24"
        "tag": str(r.get("tag") or "").strip(),
        "start_ip": (r.get("start_ip") or "").strip(),        # VLAN GW IP
        "zone": (r.get("zone") or "").strip() or "LAN Zone",
        "enforcement_on": _clean_bool(r.get("enabled","true")),
    }
    dr = _split_dhcp(r.get("dhcp_start"), r.get("dhcp_end"))
    if dr:
        vlan["dhcp_range"] = dr
    return vlan

def load_vlans(vlans_file: str) -> List[Dict[str, Any]]:
    p = pathlib.Path(vlans_file)
    if not p.exists():
        raise FileNotFoundError(f"vlans_file not found: {vlans_file}")

    if p.suffix.lower() == ".csv":
        rows = read_csv_rows(p)
        return [_vlan_from_csv_row(r) for r in rows]

    # JSON fallback
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("rows") or data.get("result",{}).get("rows") or data.get("vlans") or []
    if not isinstance(data, list): data = []
    out = []
    for v in data:
        out.append({
            "name": (v.get("display_name") or v.get("name") or "").strip(),
            "display_name": (v.get("display_name") or v.get("name") or "").strip(),
            "interface": (v.get("interface") or "").strip(),
            "subnet": str(v.get("subnet") or "").strip(),
            "tag": str(v.get("tag") or "").strip(),
            "start_ip": (v.get("start_ip") or (v.get("default_gateway") or "")).strip(),
            "zone": (v.get("zone") or "").strip() or "LAN Zone",
            "enforcement_on": bool(v.get("enforcement_on", True)),
            **({"dhcp_range": v["dhcp_range"]} if v.get("dhcp_range") else {})
        })
    return out

# -------- lookups --------
def find_site_row_by_name(site_name: str) -> Optional[Dict[str, Any]]:
    data = get_json(f"{API_V3}/Gateway/", params={
        "gateway_type": "isolation", "limit": "100", "page": 0,
        "sortdir": "asc", "search": site_name, "refresh_token": "enabled"
    })
    rows = data.get("rows") or data.get("result",{}).get("rows",[]) or []
    wanted = site_name.strip().lower()
    for r in rows:
        nm = (r.get("location_display_name") or r.get("site_name") or r.get("location") or "").strip().lower()
        if nm == wanted:
            return r
    return None

def get_gateway_detail_v3(gateway_id: str) -> Dict[str, Any]:
    url = f"{API_V3}/Gateway/{gateway_id}"
    return get_json(url)

def resolve_gateway_and_cluster(site_name: str, retries: int = 40, delay: float = 3.0) -> Tuple[Optional[str], Optional[int]]:
    """
    Poll until both gateway_id and cluster_id are available after site creation.
    ~2 minutes total (40 * 3s).
    """
    for _ in range(retries):
        row = find_site_row_by_name(site_name)
        gw_id = None
        cl_id = None

        if row:
            gws = row.get("gateways") or []
            if gws and isinstance(gws, list):
                gw_id = gws[0].get("gateway_id")
            ci = row.get("cluster_info") or {}
            cl_id = ci.get("cluster_id")

        if gw_id and not cl_id:
            try:
                det = get_gateway_detail_v3(gw_id)
                cl_id = det.get("cluster_id") or (det.get("cluster") or {}).get("cluster_id")
            except Exception:
                pass

        if gw_id and cl_id:
            return gw_id, int(cl_id)

        time.sleep(delay)

    return None, None

# -------- v3/v2 API calls --------
def create_site(template_id: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    url = f"{API_V3}/templates/{template_id}/deploy_site?refresh_token=enabled"
    r = post_json(url, payload)
    if r.status_code in (200,201,202):
        return True, r.text
    return False, f"{r.status_code} {r.text[:300]}"

def _network_base_from_start(start_ip: str, subnet_bits: str) -> Optional[str]:
    s = (start_ip or "").strip()
    b = (subnet_bits or "").strip()
    if not s or not b:
        return None
    try:
        net = ipaddress.ip_network(f"{s}/{b}", strict=False)
        return str(net.network_address)
    except Exception:
        return None

def _short_name(name: str, maxlen: int = 16) -> str:
    n = (name or "").strip()
    if len(n) <= maxlen:
        return n
    return n[:maxlen]

def vlan_to_v2_payload(vlan: Dict[str, Any], gateway_id: str, cluster_id: int) -> Dict[str, Any]:
    start_ip = vlan.get("start_ip") or ""
    subnet   = str(vlan.get("subnet") or "").strip()
    ip_range = _network_base_from_start(start_ip, subnet) or vlan.get("ip_range") or ""

    display = vlan.get("display_name") or vlan.get("name") or ""
    safe_name = _short_name(vlan.get("name") or display, 16)

    payload = {
        "subnet": subnet,
        "tag": str(vlan.get("tag") or "").strip(),
        "display_name": display,
        "ip_range": ip_range,
        "zone": vlan.get("zone") or "LAN Zone",
        "per_network_dns": "",
        "dns_forwarding": False,
        "dhcp_range": vlan.get("dhcp_range", ""),
        "slash30_range": "",
        "airgap_plus_mask": 30,
        "default_gateway": start_ip,
        "gateways": gateway_id,
        "interface": vlan.get("interface") or "",
        "name": safe_name,
        "cluster_id": int(cluster_id),
        "event_type": "addnetwork",
        "dhcp_service": "inherit" if vlan.get("dhcp_range") else "no_dhcp",
    }
    return payload

def post_vlan(vlan_payload: Dict[str, Any]) -> Tuple[bool, str]:
    # NOTE trailing slash is important on many tenants
    url = f"{API_V2}/Network/?refresh_token=enabled"
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "Content-Type": "application/json",
    }
    r = post_json(url, vlan_payload, headers=headers)
    if r.status_code in (200,201,202):
        return True, r.text
    return False, f"{r.status_code} {r.text[:300]}"

# -------- main --------
def main():
    ap = argparse.ArgumentParser(description="Bulk create sites then add VLANs from vlans_file (via gateway_id + cluster_id)")
    ap.add_argument("--csv", default="sites.csv", help="Path to sites.csv")
    ap.add_argument("--dry-run", action="store_true", help="Render site payloads only; do not POST")
    args = ap.parse_args()

    sites_path = pathlib.Path(args.csv)
    if not sites_path.exists():
        print(f"ERROR: {sites_path} not found", file=sys.stderr); sys.exit(1)

    rows = read_csv_rows(sites_path)
    todo = [r for r in rows if (r.get("post") or "").strip() == "1"]
    if not todo:
        print("Nothing to do. Mark rows with post=1 in sites.csv."); return

    print(f"Posting {len(todo)} site(s)…\n")
    ok = 0; fail = 0

    for r in todo:
        site_name   = (r.get("site_name") or "").strip()
        template_id = (r.get("template_id") or "").strip()
        vlans_file  = (r.get("vlans_file") or "").strip()

        if not site_name:
            print("SKIP: row missing site_name"); fail += 1; continue
        if not template_id:
            print(f"SKIP: {site_name}: missing template_id"); fail += 1; continue

        ctx = dict(r)

        # Render site payload
        try:
            rendered = render_template(ctx)
            payload  = json.loads(rendered)
        except Exception as e:
            print(f"ERR : {site_name}: template render failed: {e}")
            fail += 1; continue

        if args.dry_run:
            print(f"DRY: {site_name}: site-payload bytes={len(rendered)}")
            try:
                if vlans_file:
                    vlans = load_vlans(vlans_file)
                    print(f"    VLANs: {len(vlans)} (sample: {[v.get('display_name') or v.get('name') for v in vlans[:3]]})")
            except Exception as e:
                print(f"    VLAN load warn: {e}")
            ok += 1; continue

        # 1) Create site (v3)
        ok_site, msg = create_site(template_id, payload)
        if not ok_site:
            print(f"ERR : {site_name}: site create failed: {msg}")
            fail += 1; continue
        print(f"OK  : {site_name}: site create → {msg[:160]}")

        # 2) Resolve gateway + cluster (wait until both are ready)
        gateway_id, cluster_id = resolve_gateway_and_cluster(site_name, retries=40, delay=3.0)
        if not gateway_id or not cluster_id:
            print(f"ERR : {site_name}: gateway/cluster not ready (gw={gateway_id}, cluster={cluster_id})")
            fail += 1; continue

        # 3) Load VLANs & POST each (v2)
        try:
            vlans = load_vlans(vlans_file) if vlans_file else []
        except Exception as e:
            print(f"ERR : {site_name}: failed to load VLANs from {vlans_file}: {e}")
            fail += 1; continue

        vlan_ok = 0; vlan_fail = 0
        for v in vlans:
            v2_payload = vlan_to_v2_payload(v, gateway_id, cluster_id)
            okv, m = post_vlan(v2_payload)
            if okv:
                vlan_ok += 1
            else:
                vlan_fail += 1
                print(f"    VLAN ERR: {m}")

        print(f"OK  : {site_name}: VLANs posted OK={vlan_ok} ERR={vlan_fail}")
        ok += 1

    print(f"\nDone. OK={ok}  ERR={fail}")

if __name__ == "__main__":
    main()