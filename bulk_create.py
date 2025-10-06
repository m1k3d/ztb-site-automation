#!/usr/bin/env python3
# bulk_create.py — Deploys sites + VLANs to ZTB from sites.csv and VLAN CSVs
#
# Author: Mike Dechow (@m1k3d)
# Repo: github.com/m1k3d/ztb-site-automation
# License: MIT
# Version: 1.3.4 (HA + name→id template resolution + DHCP relay inference + UI-accurate endpoints)
#
# Usage:
#   python3 bulk_create.py --dry-run           # Validate payloads, do not POST
#   python3 bulk_create.py                     # Create sites and VLANs
#   python3 bulk_create.py --debug             # Verbose HTTP (slash/no-slash differences)
#   python3 bulk_create.py --csv other.csv     # Use a different sites.csv
#
# Notes:
#   - Reads rows from sites.csv where post=1
#   - Renders site payload from site_template.json.j2
#   - Creates site (v3): POST /api/v3/templates/{template_id}/deploy_site?refresh_token=enabled
#   - Waits for gateway id(s) + cluster id to appear in /api/v3/Gateway results
#   - Posts VLANs (v2): POST /api/v2/Network/?refresh_token=enabled
#     • Standalone → "gateways": "<gwA>",       "interface": "ge5"
#     • HA         → "gateways": "<gwA>,<gwB>", "interface": "ge5,ge5" (auto-duplicates if CSV has one)
#   - After creation: PUT status=provisioned (enable), PATCH share_over_vpn, and PUT dhcp_service (if specified)
#   - VLAN CSV format unchanged:
#       name,tag,subnet,default_gateway,dhcp_start,dhcp_end,interface,zone,enabled,share_over_vpn,dhcp_service
#     · dhcp_service (CSV): on | inherit | non-airgapped | no_dhcp | off
#       (API sent: inherit | non_airgapped | no_dhcp)
#
# Environment (.env):
#   ZTB_API_BASE=https://<tenant>-api.goairgap.com      # or legacy ZIA_API_BASE
#   BEARER=<raw-token>                                  # raw token; we add 'Bearer ' automatically
#   ZTB_REFERER_PATH=/ztb/sites                         # optional; helps some tenants
#
# What changed from your last 1.3.x:
#   • Template name→id resolution: if template_id is blank but template_name is set, we resolve it at runtime
#   • HA VLAN post matches the UI: comma-joined gateway ids and (if needed) duplicated interface "geX,geX"
#   • v3 “Gateway” and “templates” endpoints try no-slash first, then fallback to trailing slash (tenant quirks)
#   • Optional per_site_dns in sites.csv flows into VLAN per_network_dns (safe if empty)
#   • NEW: DHCP relay inference & validation:
#       - If sites.csv has dhcp_server_ip (non-empty), we assume relay and inject
#         {"dhcp_service": "relay", "dhcp_server_ip": "<ip>"} into the site payload.
#       - If relay is requested but no IP is present, the row hard-fails with a clear error.
#       - If blank, template defaults apply and we don’t send DHCP keys.

import os, sys, csv, json, time, ipaddress, argparse, pathlib
from typing import Any, Dict, List, Tuple, Optional
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

# ------------------------
# tiny .env loader
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

DEBUG = False

# -------- env / session --------
def _normalize_base_root(raw: str) -> str:
    """Accept root or /api/v3|v2 and return a clean ROOT (no trailing slash, no /api/*)."""
    base = (raw or "").strip().rstrip("/")
    if base.endswith("/api/v3") or base.endswith("/api/v2"):
        base = base.rsplit("/api/", 1)[0]
    return base

def get_sessions_and_bases() -> Tuple[requests.Session, str, str, str, str]:
    # Prefer ZTB_API_BASE, fallback to legacy ZIA_API_BASE (compatible)
    base_env = os.environ.get("ZTB_API_BASE") or os.environ.get("ZIA_API_BASE") or ""
    bearer   = (os.environ.get("BEARER") or "").strip()
    base_root = _normalize_base_root(base_env)
    if not base_root or not bearer:
        print("ERROR: Missing ZTB_API_BASE (or ZIA_API_BASE) and/or BEARER in .env", file=sys.stderr)
        sys.exit(1)

    base_v3 = f"{base_root}/api/v3"
    base_v2 = f"{base_root}/api/v2"

    origin_host = base_root.replace("-api.", ".")
    referer_path = os.getenv("ZTB_REFERER_PATH", "/").lstrip("/")
    referer = origin_host + (("" if referer_path == "" else f"{referer_path}/"))

    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "bulk_create.py",
    })
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

def _d(method: str, url: str, status: int):
    if DEBUG:
        print(f"* {method} {url}\n  -> {status}")

def get_json(url: str, params: Optional[Dict[str, str]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
    r = session.get(url, params=params, headers=headers, timeout=60)
    _d("GET", r.url, r.status_code)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        raise ValueError(f"Non-JSON response from {url}: {r.text[:300]}")

def post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
    data = json.dumps(payload)
    r = session.post(url, data=data, headers=headers, timeout=90)
    _d("POST", url, r.status_code)
    return r

def put_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
    data = json.dumps(payload)
    r = session.put(url, data=data, headers=headers, timeout=90)
    _d("PUT", url, r.status_code)
    return r

def patch_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
    data = json.dumps(payload)
    r = session.patch(url, data=data, headers=headers, timeout=90)
    _d("PATCH", url, r.status_code)
    return r

# ---------- v3 helpers (UI-accurate; try no-slash then slash) ----------
def _v3_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
    }

def get_json_v3_gateway(params: Dict[str, str]) -> Any:
    """Some tenants want /Gateway, others /Gateway/. Try no-slash first, then fallback."""
    headers = _v3_headers()
    primary = f"{API_V3}/Gateway"
    try:
        return get_json(primary, params=params, headers=headers)
    except RuntimeError as e:
        if "404" in str(e) or "405" in str(e):
            return get_json(f"{API_V3}/Gateway/", params=params, headers=headers)
        raise

def get_json_v3_detail(path: str) -> Any:
    return get_json(f"{API_V3}/{path.lstrip('/')}", headers=_v3_headers())

# --- templates list (for name→id resolution) ---
def get_json_v3_templates() -> List[Dict[str, Any]]:
    """Fetch /api/v3/templates; normalize to a list of template dicts."""
    headers = _v3_headers()
    base = f"{API_V3}/templates"
    try:
        data = get_json(base, headers=headers)
    except RuntimeError as e:
        if "404" in str(e) or "405" in str(e):
            data = get_json(base + "/", headers=headers)
        else:
            raise
    if isinstance(data, dict):
        if isinstance(data.get("result"), list):
            return data["result"]
        if isinstance(data.get("result"), dict) and isinstance(data["result"].get("rows"), list):
            return data["result"]["rows"]
    if isinstance(data, list):
        return data
    return []

class TemplateResolver:
    """Caches template list for this run and resolves exact (case-insensitive) name → id."""
    def __init__(self):
        self._by_lower_name: Dict[str, List[Dict[str, Any]]] = {}
        self._loaded = False
    def _load(self):
        if self._loaded:
            return
        items = get_json_v3_templates()
        for t in items:
            nm = str(t.get("name","")).strip()
            if nm:
                self._by_lower_name.setdefault(nm.lower(), []).append(t)
        self._loaded = True
        if DEBUG:
            print(f"Loaded {sum(len(v) for v in self._by_lower_name.values())} templates")
    def resolve(self, name: str) -> Optional[str]:
        self._load()
        hits = self._by_lower_name.get(name.strip().lower(), [])
        if len(hits) == 1:
            return hits[0].get("id")
        return None  # ambiguous or not found

TEMPLATES = TemplateResolver()

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
    CSV → API mapping:
      on -> inherit
      inherit -> inherit
      non-airgapped/non_airgapped -> non_airgapped
      off -> no_dhcp
      no_dhcp -> no_dhcp
    Default: if blank, inherit when a range is present else no_dhcp.
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
    return {
        "name": (r.get("name") or "").strip(),
        "display_name": (r.get("name") or "").strip(),
        "interface": (r.get("interface") or "").strip(),
        "subnet": str(r.get("subnet") or "").strip(),
        "tag": str(r.get("tag") or "").strip(),
        "default_gateway": (r.get("default_gateway") or "").strip(),
        "start_ip": (r.get("default_gateway") or "").strip(),
        "zone": (r.get("zone") or "").strip() or "LAN Zone",
        "enabled": _clean_bool(r.get("enabled","true")),
        "share_over_vpn": _clean_bool(r.get("share_over_vpn","false")),
        "dhcp_service": svc,
        **({"dhcp_range": dhcp_range} if dhcp_range else {})
    }

def load_vlans(vlans_file: str) -> List[Dict[str, Any]]:
    p = pathlib.Path(vlans_file)
    if not p.exists():
        raise FileNotFoundError(f"vlans_file not found: {vlans_file}")
    if p.suffix.lower() == ".csv":
        rows = read_csv_rows(p)
        return [_vlan_from_csv_row(r) for r in rows]
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("rows") or data.get("result",{}).get("rows") or data.get("vlans") or []
    if not isinstance(data, list): data = []
    out = []
    for v in data:
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

# -------- HA validation (sites.csv sanity) --------
def validate_row_is_ha_consistent(row: Dict[str, str]) -> None:
    """
    If any WAN1_* values are present, gateway_name_b must be present, and vice-versa.
    This helps avoid partial HA rows.
    """
    b_name = (row.get("gateway_name_b") or "").strip()
    b_fields = [
        (row.get("wan1_ip") or "").strip(),
        (row.get("wan1_mask") or "").strip(),
        (row.get("wan1_gw") or "").strip(),
        (row.get("wan1_interface_name") or "").strip(),
    ]
    any_b = any(bool(x) for x in b_fields)
    if any_b and not b_name:
        raise SystemExit(f"❌ Row '{row.get('site_name')}' has WAN1 values but no gateway_name_b.")
    if b_name and not all(bool(x) for x in b_fields):
        raise SystemExit(f"❌ Row '{row.get('site_name')}' missing one or more WAN1 fields for HA site.")

# -------- lookups (gateways / templates) --------
def get_json_v3_gateway_list(site_name: str) -> Dict[str, Any]:
    params = {
        "gateway_type": "isolation",
        "template_id": "",
        "sortdir": "asc",
        "sort": "location",
        "search": site_name,
        "page": 0,
        "limit": 100,
        "refresh_token": "enabled",
    }
    return get_json_v3_gateway(params)

def find_site_row_by_name(site_name: str) -> Optional[Dict[str, Any]]:
    data = get_json_v3_gateway_list(site_name)
    rows = data.get("rows") or data.get("result",{}).get("rows",[]) or []
    wanted = site_name.strip().lower()
    for r in rows:
        nm = (r.get("location_display_name") or r.get("site_name") or r.get("location") or "").strip().lower()
        if nm == wanted:
            return r
    return None

def get_gateway_detail_v3(gateway_id: str) -> Dict[str, Any]:
    return get_json_v3_detail(f"Gateway/{gateway_id}")

def resolve_gateway_ids_and_cluster(site_name: str, retries: int = 40, delay: float = 3.0) -> Tuple[Optional[str], Optional[int]]:
    """
    Poll for gateway ids (standalone: 'idA', HA: 'idA,idB') and cluster_id after site creation.
    """
    for _ in range(retries):
        row = find_site_row_by_name(site_name)
        gw_ids_str = None
        cl_id = None

        if row:
            gws = row.get("gateways") or []
            if isinstance(gws, list) and gws:
                ids = [g.get("gateway_id") for g in gws if g.get("gateway_id")]
                if ids:
                    gw_ids_str = ",".join(ids)
            ci = row.get("cluster_info") or {}
            cl_id = ci.get("cluster_id")

        if gw_ids_str and not cl_id:
            try:
                first_id = gw_ids_str.split(",")[0]
                det = get_gateway_detail_v3(first_id)
                cl_id = det.get("cluster_id") or (det.get("cluster") or {}).get("cluster_id")
            except Exception:
                pass

        if gw_ids_str and cl_id:
            return gw_ids_str, int(cl_id)

        time.sleep(delay)

    return None, None

# --- template id resolution for each row ---
def ensure_template_id_for_row(row: Dict[str, str]) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Return (ok, template_id, err_msg). If csv has template_id -> return it.
    Else, resolve from template_name via /api/v3/templates (exact, case-insensitive).
    """
    tid = (row.get("template_id") or "").strip()
    if tid:
        return True, tid, None
    tname = (row.get("template_name") or "").strip()
    if not tname:
        return False, None, "missing template_id and template_name"
    resolved = TEMPLATES.resolve(tname)
    if resolved:
        row["template_id"] = resolved  # make available to Jinja
        return True, resolved, None
    all_items = get_json_v3_templates()
    names_hint = ", ".join(sorted({it.get("name","") for it in all_items if it.get("name")}))
    return False, None, f"could not resolve template_id from template_name='{tname}'. Available names: {names_hint}"

# -------- v3/v2 API calls --------
def create_site(template_id: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
    url = f"{API_V3}/templates/{template_id}/deploy_site?refresh_token=enabled"
    r = post_json(url, payload, headers=_v3_headers())
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
    return n if len(n) <= maxlen else n[:maxlen]

def _maybe_dup_interface_for_ha(interface: str, gateways_str: str) -> str:
    """
    If HA (gateways_str has a comma) and CSV provides a single interface ('ge5'),
    duplicate as 'ge5,ge5'. If CSV already has 'ge5,ge6', keep as-is.
    """
    if "," in gateways_str:
        if interface and "," not in interface:
            return f"{interface},{interface}"
    return interface

def vlan_to_v2_payload(vlan: Dict[str, Any], gateways_str: str, cluster_id: int, per_network_dns: str = "") -> Dict[str, Any]:
    """Build the v2 Network payload. Handles standalone vs HA for gateways/interface."""
    start_ip = vlan.get("start_ip") or vlan.get("default_gateway") or ""
    subnet   = str(vlan.get("subnet") or "").strip()
    ip_range = _network_base_from_start(start_ip, subnet) or vlan.get("ip_range") or ""
    display   = vlan.get("display_name") or vlan.get("name") or ""
    safe_name = _short_name(vlan.get("name") or display, 16)
    interface = _maybe_dup_interface_for_ha(vlan.get("interface") or "", gateways_str)
    return {
        "subnet": subnet,
        "tag": str(vlan.get("tag") or "").strip(),
        "display_name": display,
        "ip_range": ip_range,
        "zone": vlan.get("zone") or "LAN Zone",
        "per_network_dns": (per_network_dns or "").strip(),
        "dns_forwarding": False,
        "dhcp_range": vlan.get("dhcp_range", ""),
        "slash30_range": "",
        "airgap_plus_mask": 30,
        "default_gateway": start_ip,
        "gateways": gateways_str,   # standalone: "idA"; HA: "idA,idB"
        "interface": interface,     # standalone: "ge5"; HA: "ge5,ge5" (auto if needed)
        "name": safe_name,
        "cluster_id": int(cluster_id),
        "event_type": "addnetwork",
        "dhcp_service": norm_dhcp_service(vlan.get("dhcp_service",""), bool(vlan.get("dhcp_range"))),
        "share_over_vpn": bool(vlan.get("share_over_vpn", False)),
    }

def post_vlan(vlan_payload: Dict[str, Any]) -> Tuple[bool, str]:
    # Many tenants require the trailing slash on v2/Network
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
    global DEBUG
    ap = argparse.ArgumentParser(description="Bulk create sites then add VLANs from vlans_file (via gateway_id + cluster_id)")
    ap.add_argument("--csv", default="sites.csv", help="Path to sites.csv")
    ap.add_argument("--dry-run", action="store_true", help="Render site payloads only; do not POST")
    ap.add_argument("--debug", action="store_true", help="Print each HTTP call and status code")
    args = ap.parse_args()
    DEBUG = bool(args.debug)

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

        # Validate HA row consistency early
        try:
            validate_row_is_ha_consistent(r)
        except SystemExit as e:
            print(str(e)); fail += 1; continue

        # Resolve template_id if missing (from template_name)
        if not template_id:
            ok_tid, resolved_tid, err = ensure_template_id_for_row(r)
            if not ok_tid or not resolved_tid:
                print(f"SKIP: {site_name}: {err}")
                fail += 1; continue
            template_id = resolved_tid
            if DEBUG:
                print(f"Resolved template_name='{r.get('template_name')}' -> template_id={template_id}")

        # --- DHCP relay inference & validation (per-site) ---
        dhcp_ip = (r.get("dhcp_server_ip") or "").strip()
        svc = (r.get("dhcp_service_mode") or "").strip().lower()
        if svc not in ("", "relay", "server", "inherit"):
            svc = ""
        if not svc and dhcp_ip:
            svc = "relay"
        if svc == "relay" and not dhcp_ip:
            print(f"ERR : {site_name}: dhcp_service=relay requires dhcp_server_ip in sites.csv")
            fail += 1; continue

        # Jinja context must include template_id and relay mode if present
        ctx = dict(r)
        ctx["template_id"] = template_id
        ctx["dhcp_service_mode"] = svc  # Jinja can use this if you choose

        # Render site payload
        try:
            rendered = render_template(ctx)
            payload  = json.loads(rendered)
        except Exception as e:
            print(f"ERR : {site_name}: template render failed: {e}")
            fail += 1; continue

        # Ensure DHCP keys are present if we inferred/declared relay or server
        if svc == "relay":
            payload["dhcp_service"] = "relay"
            payload["dhcp_server_ip"] = dhcp_ip
        elif svc == "server":
            payload["dhcp_service"] = "server"
            payload.pop("dhcp_server_ip", None)
        # inherit/blank → rely on template defaults (send nothing)

        if args.dry_run:
            print(f"DRY: {site_name}: site-payload bytes={len(rendered)} (after inject: {len(json.dumps(payload))})")
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

        # 2) Resolve gateway ids + cluster (handles standalone vs HA)
        gateways_str, cluster_id = resolve_gateway_ids_and_cluster(site_name, retries=40, delay=3.0)
        if not gateways_str or not cluster_id:
            print(f"ERR : {site_name}: gateway/cluster not ready (gateways='{gateways_str}', cluster={cluster_id})")
            fail += 1; continue
        if DEBUG:
            print(f"Gateways: {gateways_str}  Cluster: {cluster_id}")

        # 3) Load VLANs & POST each (v2)
        try:
            vlans = load_vlans(vlans_file) if vlans_file else []
        except Exception as e:
            print(f"ERR : {site_name}: failed to load VLANs from {vlans_file}: {e}")
            fail += 1; continue

        per_net_dns = (r.get("per_site_dns") or "").strip()
        vlan_ok = 0; vlan_fail = 0
        for v in vlans:
            v2_payload = vlan_to_v2_payload(v, gateways_str, cluster_id, per_network_dns=per_net_dns)
            okv, m = post_vlan(v2_payload)
            if okv:
                vlan_ok += 1
            else:
                vlan_fail += 1
                print(f"    VLAN ERR: {m}")

        print(f"OK  : {site_name}: VLANs POSTed OK={vlan_ok} ERR={vlan_fail}")

        # 4) Enable + share_over_vpn + (re)apply dhcp_service — requires VLAN ids
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
                "per_network_dns": (per_net_dns or ""),
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
            if (v.get("dhcp_service") or "") == "":
                continue  # CSV blank → already defaulted at POST time
            vid = find_id_for(v)
            if not vid:
                print(f"    WARN dhcp_service: could not match VLAN id for {v.get('name')}/{v.get('tag')}")
                continue
            url = f"{API_V2}/Network/update/{vid}?refresh_token=enabled"
            payload = {
                "name": v.get("display_name") or v.get("name") or "",
                "subnet": str(v.get("subnet") or ""),
                "per_network_dns": (per_net_dns or ""),
                "dhcp_service": desired,
            }
            r = put_json(url, payload, headers=v2_hdrs)
            if r.status_code not in (200,204):
                print(f"    WARN dhcp_service PUT {vid}: {r.status_code} {r.text[:180]}")

        ok += 1

    print(f"\nDone. OK={ok}  ERR={fail}")

if __name__ == "__main__":
    main()