#!/usr/bin/env python3
# bulk_create.py — Deploys sites + VLANs to ZIA/ZTB from sites.csv and VLAN CSVs
#
# Author: Mike Dechow (@m1k3d)
# Repo: github.com/m1k3d/ztb-site-automation
# License: MIT
# Version: 1.0.0
# Usage:
#   python3 bulk_create.py --dry-run    # Validate payloads without pushing
#   python3 bulk_create.py              # Create sites and VLANs
#
# Notes:
#   - Reads site definitions from sites.csv
#   - Attaches VLAN definitions from vlans/<site>.csv (CSV or JSON)
#   - CSV columns expected for VLANs:
#       name, tag, subnet, default_gateway, dhcp_start, dhcp_end,
#       dhcp_service, interface, zone, enabled, share_over_vpn
#     · dhcp_service accepts: on, inherit, non-airgapped, no_dhcp
#       (we send API values: inherit | non_airgapped | no_dhcp)
#   - Waits for gateway/cluster readiness before pushing VLANs
#   - After creation: PUT status=provisioned (enable), PATCH share_over_vpn

import os, sys, csv, json, time, ipaddress, argparse, pathlib
from typing import Any, Dict, List, Tuple, Optional
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ------------------------
# tiny .env loader (keeps your flow portable)
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
        "User-Agent": "bulk_create.py",
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

def put_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
    data = json.dumps(payload)
    return session.put(url, data=data, headers=headers, timeout=90)

def patch_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
    data = json.dumps(payload)
    return session.patch(url, data=data, headers=headers, timeout=90)

# -------- value normalization --------
def _clean_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("1","true","yes","y")

def _split_dhcp(start: str, end: str) -> Optional[str]:
    s = (start or "").strip(); e = (end or "").strip()
    if not s and not e: return None
    if s and e: return f"{s}-{e}"
    return None

def norm_dhcp_service(val: str, has_range: bool) -> str:
    """
    Accept human inputs from CSV and map to API values:
      on -> inherit
      inherit -> inherit
      non-airgapped / non_airgapped -> non_airgapped
      no_dhcp / off -> no_dhcp
    If blank: default to 'inherit' when we have a range, else 'no_dhcp'.
    """
    v = (val or "").strip().lower().replace("-", "_")
    if v == "on": return "inherit"
    if v in ("inherit", "non_airgapped", "no_dhcp"): return v
    if v == "off": return "no_dhcp"
    return "inherit" if has_range else "no_dhcp"

# -------- VLAN loading (CSV or JSON) --------
def _vlan_from_csv_row(r: Dict[str, str]) -> Dict[str, Any]:
    dhcp_range = _split_dhcp(r.get("dhcp_start"), r.get("dhcp_end"))
    svc = norm_dhcp_service(r.get("dhcp_service", ""), bool(dhcp_range))
    vlan = {
        "name": (r.get("name") or "").strip(),
        "display_name": (r.get("name") or "").strip(),
        "interface": (r.get("interface") or "").strip(),
        "subnet": str(r.get("subnet") or "").strip(),
        "tag": str(r.get("tag") or "").strip(),
        # CSV uses default_gateway (we also mirror to start_ip for safety)
        "default_gateway": (r.get("default_gateway") or "").strip(),
        "start_ip": (r.get("default_gateway") or "").strip(),
        "zone": (r.get("zone") or "").strip() or "LAN Zone",
        "enabled": _clean_bool(r.get("enabled","true")),
        "share_over_vpn": _clean_bool(r.get("share_over_vpn","false")),
        "dhcp_service": svc,
        **({"dhcp_range": dhcp_range} if dhcp_range else {})
    }
    return vlan

def load_vlans(vlans_file: str) -> List[Dict[str, Any]]:
    p = pathlib.Path(vlans_file)
    if not p.exists():
        raise FileNotFoundError(f"vlans_file not found: {vlans_file}")

    if p.suffix.lower() == ".csv":
        rows = read_csv_rows(p)
        return [_vlan_from_csv_row(r) for r in rows]

    # JSON fallback (kept for convenience)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("rows") or data.get("result",{}).get("rows") or data.get("vlans") or []
    if not isinstance(data, list): data = []
    out = []
    for v in data:
        # Try to reconstruct a CSV-like vlan
        dhcp_range = v.get("dhcp_range")
        out.append({
            "name": (v.get("display_name") or v.get("name") or "").strip(),
            "display_name": (v.get("display_name") or v.get("name") or "").strip(),
            "interface": (v.get("interface") or "").strip(),
            "subnet": str(v.get("subnet") or "").strip(),
            "tag": str(v.get("tag") or "").strip(),
            "default_gateway": (v.get("start_ip") or v.get("default_gateway") or "").strip(),
            "start_ip": (v.get("start_ip") or v.get("default_gateway") or "").strip(),
            "zone": (v.get("zone") or "").strip() or "LAN Zone",
            "enabled": True,
            "share_over_vpn": bool(v.get("share_over_vpn", False)),
            "dhcp_service": norm_dhcp_service(v.get("dhcp_service",""), bool(dhcp_range)),
            **({"dhcp_range": dhcp_range} if dhcp_range else {})
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
    """Poll until both gateway_id and cluster_id are available after site creation."""
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
    start_ip = vlan.get("start_ip") or vlan.get("default_gateway") or ""
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
        "dhcp_service": norm_dhcp_service(vlan.get("dhcp_service",""), bool(vlan.get("dhcp_range"))),
        "share_over_vpn": bool(vlan.get("share_over_vpn", False)),
    }
    return payload

def post_vlan(vlan_payload: Dict[str, Any]) -> Tuple[bool, str]:
    # NOTE trailing slash is required on many tenants
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

# --- helpers: fetch VLANs for site and build a map so we can PATCH/PUT by id ---
def list_site_vlans_v2(site_id: str) -> List[Dict[str, Any]]:
    url = f"{API_V2}/Network/"
    params = {"siteId": site_id, "refresh_token": "enabled"}
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
    }
    data = get_json(url, params=params, headers=headers)
    if isinstance(data, dict):
        rows = data.get("rows") or data.get("result", {}).get("rows")
        return rows or []
    if isinstance(data, list):
        return data
    return []

def _vlan_key(v: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """Key by (name/display_name lower, tag, interface lower, default_gateway/start_ip)."""
    nm = (v.get("display_name") or v.get("name") or "").strip().lower()
    tg = str(v.get("tag") or "").strip()
    iface = (v.get("interface") or "").strip().lower()
    gw = (v.get("default_gateway") or v.get("start_ip") or "").strip()
    return (nm, tg, iface, gw)

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

        print(f"OK  : {site_name}: VLANs POSTed OK={vlan_ok} ERR={vlan_fail}")

        # 4) Enable + share_over_vpn + (re)apply dhcp_service — requires VLAN ids
        #    Resolve site_id to list vlans
        site_row = find_site_row_by_name(site_name) or {}
        ci = site_row.get("cluster_info") or {}
        site_id = ci.get("site_id") or site_row.get("site_id") or site_row.get("id")
        if not site_id:
            print(f"ERR : {site_name}: cannot resolve site_id for post-patch actions")
            fail += 1; continue

        current = list_site_vlans_v2(str(site_id))
        id_map: Dict[Tuple[str,str,str,str], Dict[str,Any]] = {_vlan_key(v): v for v in current}

        def find_id_for(csv_vlan: Dict[str,Any]) -> Optional[str]:
            k = _vlan_key(csv_vlan)
            hit = id_map.get(k)
            if hit and hit.get("id"):
                return hit["id"]
            # Relaxed fallback: try by (name, tag)
            nm = (csv_vlan.get("display_name") or csv_vlan.get("name") or "").strip().lower()
            tg = str(csv_vlan.get("tag") or "").strip()
            for v in current:
                if (v.get("display_name") or v.get("name") or "").strip().lower() == nm and str(v.get("tag") or "") == tg:
                    if v.get("id"):
                        return v["id"]
            return None

        v2_hdrs = {
            "Accept": "application/json, text/plain, */*",
            "Origin": ORIGIN,
            "Referer": REFERER,
            "Content-Type": "application/json",
        }

        # a) Enable (status=provisioned) where CSV says enabled=True
        for v in vlans:
            if not v.get("enabled", True):
                continue
            vid = find_id_for(v)
            if not vid:
                print(f"    WARN enable: could not match VLAN id for {v.get('name')}/{v.get('tag')}")
                continue
            url = f"{API_V2}/Network/update/{vid}?refresh_token=enabled"
            payload = {
                "name": v.get("display_name") or v.get("name") or "",
                "subnet": str(v.get("subnet") or ""),
                "per_network_dns": "",
                "status": "provisioned",
            }
            r = put_json(url, payload, headers=v2_hdrs)
            if r.status_code not in (200,204):
                print(f"    WARN enable PUT {vid}: {r.status_code} {r.text[:180]}")

        # b) Patch share_over_vpn where requested TRUE
        for v in vlans:
            if not v.get("share_over_vpn", False):
                continue
            vid = find_id_for(v)
            if not vid:
                print(f"    WARN share_over_vpn: could not match VLAN id for {v.get('name')}/{v.get('tag')}")
                continue
            url = f"{API_V2}/Network/share-over-vpn?refresh_token=enabled"
            payload = {"id": vid, "share_over_vpn": True}
            r = patch_json(url, payload, headers=v2_hdrs)
            if r.status_code not in (200,204):
                print(f"    WARN share_over_vpn PATCH {vid}: {r.status_code} {r.text[:180]}")

        # c) (Re)apply dhcp_service if CSV specified something explicit
        for v in vlans:
            desired = norm_dhcp_service(v.get("dhcp_service",""), bool(v.get("dhcp_range")))
            # If CSV was blank, we already chose a default during POST; skip.
            if (v.get("dhcp_service") or "") == "":
                continue
            vid = find_id_for(v)
            if not vid:
                print(f"    WARN dhcp_service: could not match VLAN id for {v.get('name')}/{v.get('tag')}")
                continue
            url = f"{API_V2}/Network/update/{vid}?refresh_token=enabled"
            payload = {
                "name": v.get("display_name") or v.get("name") or "",
                "subnet": str(v.get("subnet") or ""),
                "per_network_dns": "",
                "dhcp_service": desired,
            }
            r = put_json(url, payload, headers=v2_hdrs)
            if r.status_code not in (200,204):
                print(f"    WARN dhcp_service PUT {vid}: {r.status_code} {r.text[:180]}")

        ok += 1

    print(f"\nDone. OK={ok}  ERR={fail}")

if __name__ == "__main__":
    main()