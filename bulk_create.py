#!/usr/bin/env python3
"""
bulk_create.py — Deploys sites + VLANs to ZTB from sites.csv and VLAN CSVs

Author: Mike Dechow (@m1k3d)
Repo: github.com/m1k3d/ztb-site-automation
License: MIT

Version: 1.4.0
  - VRRP after VLANs
  - VRRP link discovered via GET /api/v2/Gateway/interfaces (type == "ha"), with optional CSV override
  - Tracked interfaces strictly LAN+WAN (never mgmt, never the HA link), and must exist on all HA peers (intersection)
  - VRRP is POST-only (no PUT fallback)
  - Keeps WAN+LAN inference from sites.csv + VLAN CSV
  - Cleaner debug and dry-run previews

Usage:
  python3 bulk_create.py --dry-run
  python3 bulk_create.py
  python3 bulk_create.py --debug
  python3 bulk_create.py --csv other.csv

Environment (.env):
  ZTB_API_BASE=https://<tenant>-api.goairgap.com
  BEARER=<raw-token>
"""

import os, sys, csv, json, time, ipaddress, argparse, pathlib, subprocess, re
from typing import Any, Dict, List, Tuple, Optional, Iterable, Set
import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape
import zpa_provisioning
import ztb_login

# ------------------------
# tiny .env loader (OVERWRITES existing env vars)
# ------------------------
def load_env_file(path: str = ".env"):
    p = pathlib.Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # Remove inline comments
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            os.environ[k] = v

load_env_file(".env")

DEBUG = False

# Short, sensible polling defaults
POLL_RETRIES = 12
POLL_DELAY_S = 2.0

# Paths needed early (for ztb_login.py)
ROOT = pathlib.Path(__file__).resolve().parent
TEMPLATE_PATH = ROOT / "site_template.json.j2"
LOGIN_SCRIPT = ROOT / "ztb_login.py"

# -------- auth helpers --------
def _normalize_base_root(raw: str) -> str:
    base = (raw or "").strip().rstrip("/")
    if base.endswith("/api/v3") or base.endswith("/api/v2"):
        base = base.rsplit("/api/", 1)[0]
    return base

def _ensure_bearer_present_or_login():
    bearer = (os.environ.get("BEARER") or "").strip()
    if bearer:
        return
    if not LOGIN_SCRIPT.exists():
        print("ERROR: BEARER not set and ztb_login.py not found. Please run login manually.", file=sys.stderr)
        sys.exit(1)
    print("🔐 BEARER missing — invoking ztb_login.py to obtain a fresh token…")
    try:
        subprocess.run([sys.executable, str(LOGIN_SCRIPT)], check=True)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: ztb_login.py failed with exit code {e.returncode}", file=sys.stderr)
        sys.exit(1)
    load_env_file(".env")  # overwrite back into process

def _refresh_bearer_and_update_session(session: requests.Session) -> bool:
    if not LOGIN_SCRIPT.exists():
        print("ERROR: Cannot refresh token automatically (ztb_login.py not found).", file=sys.stderr)
        return False
    print("🔄 401 Unauthorized — refreshing token via ztb_login.py and retrying once…")
    try:
        subprocess.run([sys.executable, str(LOGIN_SCRIPT)], check=True)
    except subprocess.CalledProcessError as e:
        print(f"ERROR: ztb_login.py failed with exit code {e.returncode}", file=sys.stderr)
        return False
    load_env_file(".env")
    new_bearer = (os.environ.get("BEARER") or "").strip()
    if not new_bearer:
        print("ERROR: ztb_login.py ran but BEARER is still empty.", file=sys.stderr)
        return False
    session.headers["Authorization"] = f"Bearer {new_bearer}"
    return True

# -------- env / session --------
def get_sessions_and_bases() -> Tuple[requests.Session, str, str, str, str]:
    base_env = os.environ.get("ZTB_API_BASE") or os.environ.get("ZIA_API_BASE") or ""
    base_root = _normalize_base_root(base_env)
    if not base_root:
        print("ERROR: Missing ZTB_API_BASE (or ZIA_API_BASE) in .env", file=sys.stderr)
        sys.exit(1)

    _ensure_bearer_present_or_login()
    bearer = (os.environ.get("BEARER") or "").strip()
    if not bearer:
        print("ERROR: BEARER still missing after ztb_login.py.", file=sys.stderr)
        sys.exit(1)

    base_v3 = f"{base_root}/api/v3"
    base_v2 = f"{base_root}/api/v2"

    origin_host = base_root.replace("-api.", ".")
    referer = origin_host + "/"

    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "bulk_create.py",
    })
    return s, base_v3, base_v2, origin_host, referer

session, API_V3, API_V2, ORIGIN, REFERER = get_sessions_and_bases()

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

# Central request wrapper with 401 refresh
def _request_with_auto_refresh(method: str, url: str, *, params=None, headers=None, timeout=60, json=None, data=None) -> requests.Response:
    r = session.request(method, url, params=params, headers=headers, timeout=timeout, json=json, data=data)
    if r.status_code == 401:
        if _refresh_bearer_and_update_session(session):
            r = session.request(method, url, params=params, headers=headers, timeout=timeout, json=json, data=data)
    return r

def get_json(url: str, params: Optional[Dict[str, str]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
    r = _request_with_auto_refresh("GET", url, params=params, headers=headers, timeout=60)
    _d("GET", getattr(r, "url", url), r.status_code)
    if r.status_code != 200:
        raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        raise ValueError(f"Non-JSON response from {url}: {r.text[:300]}")

def post_raw(url: str, data: str, headers: Optional[Dict[str, str]] = None, timeout: int = 90) -> requests.Response:
    r = _request_with_auto_refresh("POST", url, headers=headers, timeout=timeout, data=data)
    _d("POST", url, r.status_code)
    return r

def post_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
    return post_raw(url, json.dumps(payload), headers=headers, timeout=90)

def put_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, str]] = None) -> requests.Response:
    r = _request_with_auto_refresh("PUT", url, params=params, headers=headers, timeout=90, data=json.dumps(payload))
    _d("PUT", url, r.status_code)
    return r

def patch_json(url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
    r = _request_with_auto_refresh("PATCH", url, headers=headers, timeout=90, data=json.dumps(payload))
    _d("PATCH", url, r.status_code)
    return r

# ---------- v3 helpers ----------
def _v3_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
    }

def get_json_v3_gateway(params: Dict[str, str]) -> Any:
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
        return None

TEMPLATES = TemplateResolver()

def ensure_template_id_for_row(row: Dict[str, str]) -> Tuple[bool, Optional[str], Optional[str]]:
    tid = (row.get("template_id") or "").strip()
    if tid:
        return True, tid, None
    tname = (row.get("template_name") or "").strip()
    if not tname:
        return False, None, "missing template_id and template_name"
    resolved = TEMPLATES.resolve(tname)
    if resolved:
        row["template_id"] = resolved
        return True, resolved, None
    all_items = get_json_v3_templates()
    names_hint = ", ".join(sorted({it.get("name","") for it in all_items if it.get("name")}))
    return False, None, f"could not resolve template_id from template_name='{tname}'. Available names: {names_hint}"

# -------- value normalization --------
def _clean_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("1","true","yes","y")

def _split_dhcp(start: str, end: str) -> Optional[str]:
    s = (start or "").strip(); e = (end or "").strip()
    if not s and not e: return None
    if s and e: return f"{s}-{e}"
    return None

def norm_dhcp_service(val: str, has_range: bool) -> str:
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

def is_ha_gateways_str(gateways_str: str) -> bool:
    return "," in (gateways_str or "")

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

def _parse_cluster_id_from_create_resp(text: str) -> Optional[int]:
    if not text:
        return None
    try:
        j = json.loads(text)
        for k in ("cluster_id", "clusterId"):
            if k in j and isinstance(j[k], (int, str)):
                try:
                    return int(j[k])
                except:
                    pass
        for key in ("result", "data"):
            if key in j and isinstance(j[key], dict):
                for k in ("cluster_id", "clusterId"):
                    if k in j[key]:
                        try:
                            return int(j[key][k])
                        except:
                            pass
    except Exception:
        pass
    m = re.search(r'"cluster[_ ]?id"\s*:\s*(\d+)', text, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except:
            return None
    return None

def resolve_gateway_ids_and_cluster(site_name: str, *, prefer_cluster_id: Optional[int] = None, retries: int = POLL_RETRIES, delay: float = POLL_DELAY_S) -> Tuple[Optional[str], Optional[int]]:
    wanted_cluster = prefer_cluster_id
    for _ in range(max(1, retries)):
        row = find_site_row_by_name(site_name)
        gw_ids_str = None
        cl_id = wanted_cluster

        if row:
            gws = row.get("gateways") or []
            if isinstance(gws, list) and gws:
                ids = [g.get("gateway_id") for g in gws if g.get("gateway_id")]
                if ids:
                    gw_ids_str = ",".join(ids)
            ci = row.get("cluster_info") or {}
            found_cluster = ci.get("cluster_id")
            if not cl_id and found_cluster:
                cl_id = int(found_cluster)

        if wanted_cluster and gw_ids_str:
            return gw_ids_str, int(wanted_cluster)
        if gw_ids_str and cl_id:
            return gw_ids_str, int(cl_id)
        time.sleep(delay)

    return None, wanted_cluster if wanted_cluster else None

# -------- Interfaces discovery (v2) --------
def get_gateway_interfaces_v2(site_id: str) -> List[Dict[str, Any]]:
    """
    GET /api/v2/Gateway/interfaces?siteID=<site_id>
    Returns a list:
    [
      {"gateway_id":"...","gateway_name":"...", "interfaces":[{"name":"ge4","interface_type":"ha"}, ...]},
      ...
    ]
    """
    url = f"{API_V2}/Gateway/interfaces"
    params = {"siteID": site_id, "refresh_token": "enabled"}
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN, "Referer": REFERER,
    }
    data = get_json(url, params=params, headers=headers)
    return data if isinstance(data, list) else []

def discover_iface_inventory(site_id: str, gateways_str: str):
    """
    From interfaces GET, derive:
      - ha_link_map: {gateway_uuid: ha_iface_name}
      - mgmt_names: set of management iface names (e.g., {'ge1'})
      - common_trackables: names present on ALL peers whose type is LAN or WAN
    """
    items = get_gateway_interfaces_v2(site_id)
    gw_ids = {g.strip() for g in str(gateways_str or "").split(",") if g.strip()}

    ha_link_map: Dict[str, str] = {}
    mgmt_names: Set[str] = set()
    per_gw_trackables: List[Set[str]] = []

    for gw in items:
        gwid = gw.get("gateway_id")
        if not gwid or gwid not in gw_ids:
            continue
        names_trackable: Set[str] = set()
        for itf in gw.get("interfaces", []):
            name = (itf.get("name") or "").strip().lower()
            if not name:
                continue
            itype = (itf.get("interface_type") or "").strip().lower()
            if itype == "ha":
                ha_link_map[gwid] = name
            elif itype == "management":
                mgmt_names.add(name)
            elif itype in ("lan", "wan"):
                names_trackable.add(name)
        per_gw_trackables.append(names_trackable)

    common_trackables = set.intersection(*per_gw_trackables) if per_gw_trackables else set()

    if DEBUG:
        print("Interfaces discovery:")
        print("  HA link map:", ha_link_map)
        print("  mgmt names :", mgmt_names)
        print("  common trackables:", sorted(common_trackables))

    return ha_link_map, mgmt_names, common_trackables

# -------- VRRP helpers --------
def _clean_iface(x: str) -> str:
    return (x or "").strip().lower()

def _unique_preserve(seq: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()
    for s in seq:
        s = _clean_iface(s)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out

def _collect_wan_ifaces_from_row(row: Dict[str, str]) -> List[str]:
    candidates = [
        "wan_interface_name", "wan0_interface_name", "wan0_interface",
        "wan_interface", "wan1_interface_name", "wan1_interface"
    ]
    vals = []
    for k in candidates:
        v = _clean_iface(row.get(k, ""))
        if v:
            vals.append(v)
    return _unique_preserve(vals)

def _collect_lan_ifaces_from_vlans(vlans: List[Dict[str, Any]], exclude: Iterable[str]) -> List[str]:
    ex = { _clean_iface(x) for x in exclude }
    found: List[str] = []
    for v in vlans:
        iface = _clean_iface(v.get("interface", ""))
        if not iface:
            continue
        iface = iface.split(",")[0].strip()
        if "." in iface:
            iface = iface.split(".", 1)[0]
        if not iface or iface == "mgmt" or iface in ex:
            continue
        if iface not in found:
            found.append(iface)
    return found

def _vrrp_headers() -> Dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": ORIGIN,
        "Referer": REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/json",
    }

def _vrrp_url(cluster_id: int) -> str:
    return f"{API_V3}/vrrp/config/{cluster_id}?refresh_token=enabled"

def post_vrrp(url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Tuple[bool, str, int]:
    body = json.dumps(payload)
    r = post_raw(url, body, headers=headers, timeout=60)
    return (r.status_code in (200, 204)), (r.text or "")[:300], r.status_code

def build_vrrp_payload(
    cluster_id: int,
    gateways_str: str,
    row: Dict[str, str],
    vlans: List[Dict[str, Any]],
    *,
    site_id: str,
    vrid: str = "16"
) -> Tuple[Optional[Dict[str,Any]], Optional[str], Optional[str]]:
    """
    Compose the VRRP payload using interface discovery:
      - vrrp_interface: HA link per gateway UUID (from GET), unless csv override present
      - track_interface: strictly LAN + WAN (csv-derived), excluding mgmt and HA link,
                         and must exist on ALL HA peers (intersection)
    Returns: (payload or None, link_iface_used or None, track_value or None)
    """
    if "," not in (gateways_str or ""):
        return None, None, None  # standalone

    # sanitize VRID
    try:
        n = int(str(vrid).strip()); n = max(1, min(255, n))
        vrid = str(n)
    except Exception:
        vrid = "16"

    # Discover actual interfaces on the device(s)
    ha_link_map, mgmt_names, common_trackables = discover_iface_inventory(site_id, gateways_str)

    # CSV override for HA link (optional)
    csv_link = (row.get("vrrp_link_interface") or "").strip().lower()
    if csv_link and "." in csv_link:
        csv_link = csv_link.split(".", 1)[0]

    # Ensure we have HA link per gateway (from discovery) when no override given
    keys = [g.strip() for g in str(gateways_str).split(",") if g.strip()]
    if not csv_link:
        missing = [k for k in keys if k not in ha_link_map or not ha_link_map[k]]
        if missing:
            raise SystemExit(
                f"❌ VRRP HA link unknown for some gateways (no 'ha' iface discovered). "
                f"Add vrrp_link_interface in sites.csv or verify template brings up HA ports."
            )

    # Build candidate track list from CSVs
    wan_ifaces = _collect_wan_ifaces_from_row(row)
    lan_ifaces = _collect_lan_ifaces_from_vlans(vlans, exclude=wan_ifaces)
    raw_track = _unique_preserve([*wan_ifaces, *lan_ifaces])

    # Exclusions: mgmt, HA link (from discovery or override), and ensure present on all peers
    ha_names = set(ha_link_map.values())
    link_name_to_exclude = csv_link or (next(iter(ha_names)) if ha_names else "")
    track_filtered = [
        i for i in raw_track
        if i and i not in mgmt_names and i != "mgmt" and i != link_name_to_exclude
    ]
    track_final = [i for i in track_filtered if i in common_trackables]

    # Optional extras from CSV (apply same filters)
    extras = (row.get("vrrp_track_extra") or "").strip().lower()
    if extras:
        extra_list = [x.strip() for x in extras.split(",") if x.strip()]
        for e in extra_list:
            if e in common_trackables and e not in mgmt_names and e != link_name_to_exclude and e not in track_final:
                track_final.append(e)

    if not track_final:
        raise SystemExit(
            f"❌ VRRP track list empty after validation. "
            f"Ensure WAN/LAN names in CSVs match real device interfaces and exist on both HA peers."
        )

    link_iface_used = csv_link or link_name_to_exclude
    track_value = ",".join(track_final)

    vrrp_interface_map = {k: (csv_link or ha_link_map[k]) for k in keys}
    track_map = {k: track_value for k in keys}

    payload = {
        "virtual_router_id": vrid,
        "advert_int": 10,
        "priority": 254,
        "vip": "0.0.0.0",
        "authentication_password": "",
        "track_interface": track_map,
        "vrrp_interface":  vrrp_interface_map,
    }

    if DEBUG:
        print("VRRP payload (keys redacted):", json.dumps(payload, indent=2))

    return payload, link_iface_used, track_value

# -------- site creation (v3) --------
def create_site(template_id: str, payload: Dict[str, Any]) -> Tuple[bool, str, Optional[int]]:
    url = f"{API_V3}/templates/{template_id}/deploy_site?refresh_token=enabled"
    r = post_json(url, payload, headers=_v3_headers())
    cid = None
    try:
        cid = _parse_cluster_id_from_create_resp(r.text or "")
    except Exception:
        cid = None
    if r.status_code in (200, 201, 202):
        return True, r.text, cid
    return False, f"{r.status_code} {r.text[:300]}", cid

# --- v2 VLAN helpers ---
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
    if "," in gateways_str:
        if interface and "," not in interface:
            return f"{interface},{interface}"
    return interface

def vlan_to_v2_payload(vlan: Dict[str, Any], gateways_str: str, cluster_id: int, per_network_dns: str = "") -> Dict[str, Any]:
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
        "gateways": gateways_str,
        "interface": interface,
        "name": safe_name,
        "cluster_id": int(cluster_id),
        "event_type": "addnetwork",
        "dhcp_service": norm_dhcp_service(vlan.get("dhcp_service",""), bool(vlan.get("dhcp_range"))),
        "share_over_vpn": bool(vlan.get("share_over_vpn", False)),
        "enabled": bool(vlan.get("enabled", True)),
    }

def post_vlan(vlan_payload: Dict[str, Any]) -> Tuple[bool, str]:
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
    nm = (v.get("display_name") or v.get("name") or "").strip().lower()
    tg = str(v.get("tag") or "").strip()
    iface = (v.get("interface") or "").strip().lower()
    gw = (v.get("default_gateway") or v.get("start_ip") or "").strip()
    return (nm, tg, iface, gw)

def configure_private_dns(session: requests.Session, api_v2: str, site_id: str, private_dns_ips: str, dry_run: bool = False) -> bool:
    """
    Configures Private DNS for the site by adding IPs to the 'System-Private-DNS-Servers-Group'.
    PUT /api/v2/group-membership?site_id=...&group_name=...
    """
    if not private_dns_ips:
        return True

    # Parse IPs, ensuring /32 CIDR
    ips = []
    for ip in private_dns_ips.split(","):
        ip = ip.strip()
        if not ip: continue
        if "/" not in ip:
            ip = f"{ip}/32"
        ips.append(ip)
    
    if not ips:
        return True

    if dry_run:
        print(f"   [DRY-RUN] Would configure Private DNS for site {site_id}: {ips}")
        return True

    # Use the exact pattern from the screenshot:
    # PUT /api/v2/group-membership?site_id=...&group_name=System-Private-DNS-Servers-Group&refresh_token=enabled
    # Payload: { "member_attributes": { "ip_prefix": [...] } }
    
    url = f"{api_v2}/group-membership"
    params = {
        "site_id": site_id,
        "group_name": "System-Private-DNS-Servers-Group",
        "refresh_token": "enabled"
    }
    
    payload = {
        "member_attributes": {
            "ip_prefix": ips
        }
    }

    try:
        # We updated put_json to accept params
        r = put_json(url, payload, headers={"Accept": "application/json", "Content-Type": "application/json"}, params=params)
        if r.status_code in (200, 201, 204):
            print(f"   ✅ Configured Private DNS: {ips}")
            return True
        else:
            print(f"   ❌ Failed to configure Private DNS: {r.status_code} {r.text[:200]}")
            return False
    except Exception as e:
        print(f"   ❌ Error configuring Private DNS: {e}")
        return False

def process_vlans_for_site(session: requests.Session, api_v2: str, site_id: str, gw_ids: str, cluster_id: int, vlans_file: str, row: Dict[str, str], dry_run: bool = False):
    if not vlans_file or not os.path.exists(vlans_file):
        return

    try:
        vlans = load_vlans(vlans_file)
    except Exception as e:
        print(f"   ⚠️  Failed to load VLANs from {vlans_file}: {e}")
        return

    print(f"   Processing {len(vlans)} VLANs from {os.path.basename(vlans_file)}...")
    
    # Use wan_dns as default for per_network_dns if not specified
    per_net_dns = (row.get("wan_dns") or "").strip()

    vlan_ok = 0; vlan_fail = 0
    for v in vlans:
        v2_payload = vlan_to_v2_payload(v, gw_ids, cluster_id, per_network_dns=per_net_dns)
        
        if dry_run:
             print(f"   [DRY-RUN] Would POST VLAN {v.get('name')} tag={v.get('tag')}")
             vlan_ok += 1
             continue

        okv, m = post_vlan(v2_payload)
        if okv:
            vlan_ok += 1
        else:
            vlan_fail += 1
            print(f"    ❌ VLAN ERR: {m}")
    
    print(f"   ✅ VLANs processed: OK={vlan_ok} ERR={vlan_fail}")

    # Post-processing: Enable and Share Over VPN (Restored from original logic)
    if dry_run:
        return

    current = list_site_vlans_v2(str(site_id))
    id_map: Dict[Tuple[str,str,str,str], Dict[str,Any]] = {_vlan_key(v): v for v in current}

    def find_id_for(csv_vlan: Dict[str,Any]) -> Optional[str]:
        k = _vlan_key(csv_vlan)
        hit = id_map.get(k)
        if hit and hit.get("id"):
            return hit["id"]
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

    # a) Enable (PUT status="provisioned")
    for v in vlans:
        if not v.get("enabled", True):
            continue
        vid = find_id_for(v)
        if not vid:
            print(f"    ⚠️  WARN enable: could not match VLAN id for {v.get('name')}/{v.get('tag')}")
            continue
        
        # Only update if needed? The original code just did it.
        url = f"{api_v2}/Network/update/{vid}?refresh_token=enabled"
        payload = {
            "name": v.get("display_name") or v.get("name") or "",
            "subnet": str(v.get("subnet") or ""),
            "per_network_dns": (per_net_dns or ""),
            "status": "provisioned",
        }
        try:
            r_put = put_json(url, payload, headers=v2_hdrs)
            if r_put.status_code not in (200, 204):
                print(f"    ⚠️  WARN enable PUT {vid}: {r_put.status_code} {r_put.text[:180]}")
            else:
                # print(f"    ✅ Enabled VLAN {v.get('name')}") # Optional: reduce noise
                pass
        except Exception as e:
            print(f"    ⚠️  Error enabling VLAN {vid}: {e}")

    # b) share_over_vpn (PATCH)
    for v in vlans:
        if not v.get("share_over_vpn", False):
            continue
        vid = find_id_for(v)
        if not vid:
            print(f"    ⚠️  WARN share_over_vpn: could not match VLAN id for {v.get('name')}/{v.get('tag')}")
            continue
        
        url = f"{api_v2}/Network/share-over-vpn?refresh_token=enabled"
        payload = {"id": vid, "share_over_vpn": True}
        try:
            r_patch = patch_json(url, payload, headers=v2_hdrs)
            if r_patch.status_code not in (200, 204):
                print(f"    ⚠️  WARN share_over_vpn PATCH {vid}: {r_patch.status_code} {r_patch.text[:180]}")
            else:
                # print(f"    ✅ Shared VLAN {v.get('name')} over VPN")
                pass
        except Exception as e:
            print(f"    ⚠️  Error sharing VLAN {vid}: {e}")

def configure_vrrp(session: requests.Session, api_v2: str, gw_ids: str, cluster_id: int, vlans_file: str, row: Dict[str, str], site_id: str, dry_run: bool = False):
    # 1. Load VLANs (needed for track interface discovery)
    try:
        vlans = load_vlans(vlans_file) if vlans_file else []
    except:
        vlans = []
    
    # 2. Build payload
    try:
        payload, link_used, track_val = build_vrrp_payload(
            cluster_id, gw_ids, row, vlans, site_id=site_id, vrid=row.get("vrrp_vrid", "16")
        )
    except SystemExit as e:
        print(f"   ❌ VRRP Config Failed: {e}")
        return

    if not payload:
        # Not an error, just means standalone or no HA link found/needed
        return

    if dry_run:
        print(f"   [DRY-RUN] Would POST VRRP config for cluster {cluster_id}")
        print(f"             Link: {link_used}, Track: {track_val}")
        return

    url = _vrrp_url(cluster_id)
    headers = _vrrp_headers()
    
    ok, msg, code = post_vrrp(url, headers, payload)
    if ok:
         print(f"   ✅ VRRP Configured (Link={link_used}, Track={track_val})")
    else:
         print(f"   ❌ VRRP Failed: {code} {msg}")

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

    # Initialize session
    # session is already global and initialized at top level
    if not session:
        print("Failed to login to ZTB", file=sys.stderr); sys.exit(1)

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

        # Jinja context
        ctx = dict(r)
        ctx["template_id"] = template_id
        ctx["dhcp_service_mode"] = svc

        # Render site payload
        try:
            rendered = render_template(ctx)
            payload  = json.loads(rendered)
        except Exception as e:
            print(f"ERR : {site_name}: template render failed: {e}")
            fail += 1; continue

        # Ensure DHCP keys as needed
        if svc == "relay":
            payload["dhcp_service"] = "relay"
            payload["dhcp_server_ip"] = dhcp_ip
        elif svc == "server":
            payload["dhcp_service"] = "server"
            payload.pop("dhcp_server_ip", None)

        # If dry-run, preview and continue
        if args.dry_run:
            try:
                vlans = load_vlans(vlans_file) if vlans_file else []
            except Exception as e:
                vlans = []
                print(f"    VLAN load warn: {e}")
            wan_preview = _collect_wan_ifaces_from_row(r)
            lan_preview = _collect_lan_ifaces_from_vlans(vlans, exclude=wan_preview+["mgmt"])
            link_preview = (r.get("vrrp_link_interface") or "").strip().lower() if (r.get("gateway_name_b") or "").strip() else ""
            track_preview = ",".join(_unique_preserve([*wan_preview, *lan_preview]))
            print(f"DRY: {site_name}: site-payload bytes={len(rendered)} (after inject: {len(json.dumps(payload))})")
            print(f"     VLANs: total={len(vlans)}; WAN-ifaces={wan_preview}; LAN-ifaces={lan_preview}")
            if link_preview:
                print(f"     VRRP preview (keys redacted): VRID={str(r.get('vrrp_vrid','16'))}, link='{link_preview}', track='{track_preview}'")
            
            # 7) ZPA Provisioning (Dry Run)
            if str(r.get("appc_provision", "")).lower() in ("1", "true", "yes"):
                 base_root = API_V3.split("/api/v3")[0]
                 zpa_provisioning.provision_zpa_for_site(r, session, base_root, cluster_id=99999, dry_run=True)

            ok += 1; continue

        # 1) Create site (v3)
        # FIX: Use 'payload' (rendered JSON) instead of 'r' (raw CSV row) to avoid 400 "Gateway details required"
        ok_site, msg, cluster_hint = create_site(template_id, payload)
        if not ok_site:
            print(f"ERR : {site_name}: site create failed: {msg}")
            fail += 1; continue
        print(f"OK  : {site_name}: site create → {msg[:160]}")

        # 2) Resolve gateway ids + cluster (short poll)
        gateways_str, cluster_id = resolve_gateway_ids_and_cluster(
            site_name,
            prefer_cluster_id=cluster_hint,
            retries=POLL_RETRIES,
            delay=POLL_DELAY_S
        )
        if not gateways_str or not cluster_id:
            print(f"ERR : {site_name}: gateway/cluster not ready (gateways='{gateways_str}', cluster={cluster_id})")
            fail += 1; continue
        if DEBUG:
            print(f"Gateways: {gateways_str}  Cluster: {cluster_id}")

        # 3) Load VLANs
        # We just check existence here, actual loading is in process_vlans_for_site
        if vlans_file and not os.path.exists(vlans_file):
             print(f"   ⚠️  VLANs file not found: {vlans_file}")

        # 4) Configure Private DNS (New Step)
        private_dns = r.get("private_dns", "")
        if private_dns:
            # Retry lookup for site_id if not already found
            site_id = None
            # Increase retries to match polling duration
            for i in range(10):
                existing = find_site_row_by_name(site_name)
                if existing:
                    # Try multiple fields for site_id, similar to pull_site.py
                    ci = existing.get("cluster_info") or {}
                    site_id = ci.get("site_id") or existing.get("site_id") or existing.get("id")
                    if site_id:
                        break
                if i < 9: time.sleep(2)

            if site_id:
                configure_private_dns(session, API_V2, site_id, private_dns, dry_run=args.dry_run)
            else:
                print(f"   ⚠️  Could not find site ID for Private DNS config (checked 10x)")
                if existing:
                    print(f"       DEBUG: Found row keys: {list(existing.keys())}")

        # 5) VLANs
        if vlans_file and os.path.exists(vlans_file):
            # We need site_id for process_vlans_for_site?
            # process_vlans_for_site signature: (session, api_v2, site_id, gw_ids, cluster_id, vlans_file, row, dry_run)
            # We need site_id.
            if not locals().get("site_id"):
                 existing = find_site_row_by_name(site_name)
                 site_id = existing.get("id") if existing else None
            
            if site_id:
                process_vlans_for_site(session, API_V2, site_id, gateways_str, cluster_id, vlans_file, r, dry_run=args.dry_run)
            else:
                print(f"   ⚠️  Skipping VLANs: site_id not found")

        # 6) VRRP
        if len(gateways_str.split(",")) > 1:
            if not locals().get("site_id"):
                 existing = find_site_row_by_name(site_name)
                 site_id = existing.get("id") if existing else None
            configure_vrrp(session, API_V2, gateways_str, cluster_id, vlans_file, r, site_id, dry_run=args.dry_run)

        # 7) ZPA Provisioning
        if str(r.get("appc_provision", "")).lower() in ("1", "true", "yes"):
             base_root = API_V3.split("/api/v3")[0]
             zpa_provisioning.provision_zpa_for_site(r, session, base_root, cluster_id=cluster_id, dry_run=args.dry_run)

    print("\nDone.")

if __name__ == "__main__":
    main()