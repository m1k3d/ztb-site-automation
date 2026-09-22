"""Reusable deployment operations. No credential loading or API calls on import."""

import json
import time
import ipaddress
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple, Optional, Iterable, Set

import requests
import zpa_provisioning
from api_client import ZTBClient
from automation_config import Settings
from input_validation import ValidationIssue, ValidationResult
from location_config import prepare_location_context
from site_payload import build_site_payload

POLL_RETRIES = 12
POLL_DELAY_S = 2.0


@dataclass
class PreparedSite:
    source: str
    row_number: int
    row: dict
    vlans: list
    payload: dict
    template_id: str


@dataclass
class DeploymentPlan:
    sites: list = field(default_factory=list)
    issues: list = field(default_factory=list)
    zpa_context: object = field(default=None, repr=False)
    owner: object = field(default=None, repr=False)


@dataclass
class SiteResult:
    name: str
    status: str
    stages: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


@dataclass
class BatchResult:
    sites: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    @property
    def exit_code(self):
        return int(bool(self.issues) or any(s.status not in ("success", "preview") for s in self.sites))

class TemplateResolver:
    def __init__(self, fetch, debug=False, emit=print):
        self._fetch, self._debug, self._emit = fetch, debug, emit
        self._by_lower_name: Dict[str, List[Dict[str, Any]]] = {}
        self._loaded = False
    def _load(self):
        if self._loaded:
            return
        items = self._fetch()
        for t in items:
            nm = str(t.get("name","")).strip()
            if nm:
                self._by_lower_name.setdefault(nm.lower(), []).append(t)
        self._loaded = True
        if self._debug:
            self._emit(f"Loaded {sum(len(v) for v in self._by_lower_name.values())} templates")
    def resolve(self, name: str) -> Optional[str]:
        self._load()
        hits = self._by_lower_name.get(name.strip().lower(), [])
        if len(hits) == 1:
            return hits[0].get("id")
        return None


class LocationResolver:
    def __init__(self, fetch, debug=False, emit=print):
        self._fetch, self._debug, self._emit = fetch, debug, emit
        self._by_lower_name: Dict[str, int] = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        locs = self._fetch()
        for l in locs:
            if not isinstance(l, dict):
                raise ValueError("Invalid entry in locations response")
            nm = str(l.get("name") or "").strip()
            lid = l.get("id")
            try:
                numeric_id = int(str(lid))
            except (ValueError, TypeError):
                raise ValueError("Location response contains an invalid ID") from None
            if not nm or numeric_id <= 0:
                raise ValueError("Location response requires a name and positive ID")
            if nm.lower() in self._by_lower_name:
                raise ValueError(f"Ambiguous existing location '{nm}'; use a unique location name")
            self._by_lower_name[nm.lower()] = numeric_id
        self._loaded = True
        if self._debug:
            self._emit(f"Loaded {len(self._by_lower_name)} ZIA locations")

    def resolve(self, name: str) -> Optional[int]:
        if not name: return None
        self._load()
        return self._by_lower_name.get(name.strip().lower())


class LocationTemplateResolver:
    def __init__(self, fetch, debug=False, emit=print):
        self._fetch, self._debug, self._emit = fetch, debug, emit
        self._by_lower_name: Dict[str, int] = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        templates = self._fetch()
        by_name: Dict[str, int] = {}
        for template in templates:
            if not isinstance(template, dict):
                raise ValueError("Invalid entry in location_templates response")
            name = str(template.get("name") or "").strip()
            template_id = template.get("id")
            try:
                numeric_id = int(str(template_id))
            except (TypeError, ValueError):
                raise ValueError("Location-template response contains an invalid ID") from None
            if not name or numeric_id <= 0:
                raise ValueError("Location-template response requires a name and positive ID")
            if name.lower() in by_name:
                raise ValueError(
                    f"Duplicate location template name '{name}'; specify location_template_id in CSV"
                )
            by_name[name.lower()] = numeric_id
        self._by_lower_name = by_name
        self._loaded = True
        if self._debug:
            self._emit(f"Loaded {len(self._by_lower_name)} ZIA Location Templates")

    def resolve(self, name: str) -> Optional[int]:
        if not name:
            return None
        self._load()
        return self._by_lower_name.get(name.strip().lower())


class DeploymentEngine:
    def __init__(self, config: Settings, *, client=None, debug=False, emit=print):
        self.config = config
        self.client = client if client is not None else ZTBClient(config, debug=debug, emit=emit)
        self.API_V3, self.API_V2 = self.client.api_v3, self.client.api_v2
        self.ORIGIN, self.REFERER = self.client.origin, self.client.referer
        self.DEBUG, self._emit = debug, emit
        self.TEMPLATES = TemplateResolver(self.get_json_v3_templates, debug, emit)
        self.ZIA_LOCATIONS = LocationResolver(self.get_json_v3_locations, debug, emit)
        self.ZIA_LOCATION_TEMPLATES = LocationTemplateResolver(self.get_json_v3_location_templates, debug, emit)

    def log(self, *values):
        self._emit(" ".join(str(value) for value in values))

    def plan(self, validation: ValidationResult) -> DeploymentPlan:
        """Resolve every selected row before any deployment writes. Auth may refresh tokens."""
        plan = DeploymentPlan(issues=list(validation.issues), owner=self)
        if plan.issues or not validation.sites:
            return plan
        wants_zpa = any(s.row.get("appc_provision") == "1" for s in validation.sites)
        for error in self.config.errors(require_zpa=wants_zpa):
            plan.issues.append(ValidationIssue("configuration", 0, "credentials", error))
        if plan.issues:
            return plan
        # Cache only within a single plan, not across subsequent runs.
        self.TEMPLATES = TemplateResolver(self.get_json_v3_templates, self.DEBUG, self._emit)
        self.ZIA_LOCATIONS = LocationResolver(self.get_json_v3_locations, self.DEBUG, self._emit)
        self.ZIA_LOCATION_TEMPLATES = LocationTemplateResolver(self.get_json_v3_location_templates, self.DEBUG, self._emit)
        for site in validation.sites:
            row = deepcopy(site.row)
            stage = "template_name/template_id"
            try:
                ok, template_id, error = self.ensure_template_id_for_row(row)
                if not ok or not template_id:
                    raise ValueError(error or "template could not be resolved")
                row["template_id"] = template_id
                stage = "location"
                location = prepare_location_context(row, self.ZIA_LOCATIONS.resolve, self.ZIA_LOCATION_TEMPLATES.resolve)
                stage = "site payload"
                payload = build_site_payload(row, location)
                plan.sites.append(PreparedSite(site.source, site.row_number, row, deepcopy(site.vlans), payload, template_id))
            except (Exception, SystemExit) as exc:
                plan.issues.append(ValidationIssue(site.source, site.row_number, stage, f"{row.get('site_name')}: {exc}"))
        if wants_zpa and not plan.issues:
            try:
                plan.zpa_context = zpa_provisioning.prepare_zpa(self.config)
            except (Exception, SystemExit) as exc:
                plan.issues.append(ValidationIssue("configuration", 0, "ZPA preflight", str(exc)))
        return plan

    def execute(self, plan: DeploymentPlan, *, dry_run=False) -> BatchResult:
        """Execute a plan from this engine. Invalid plans cannot create resources."""
        if plan.owner is not self:
            raise ValueError("Use the same engine to plan and execute; re-plan after changing configuration")
        result = BatchResult(issues=list(plan.issues))
        for issue in plan.issues:
            self.log(f"ERR : {issue}")
        if plan.issues and not dry_run:
            self.log("Preflight failed. No deployment resources were changed. Correct the errors and validate again.")
            return result
        for site in plan.sites:
            row, name = site.row, site.row["site_name"]
            try:
                exists = self.site_exists(name)
            except Exception:
                result.sites.append(SiteResult(name, "lookup_failed", errors=["Cannot verify whether site exists; no changes made to this site."]))
                self.log(f"ERR : {name}: existence lookup failed; no changes made to this site.")
                continue
            if exists:
                result.sites.append(SiteResult(name, "already_exists", errors=["Site already exists; no changes made to this site."]))
                self.log(f"STOP: {name}: already exists; no changes made to this site.")
                continue
            if dry_run:
                self.log(f"DRY: {name}: site-payload bytes={len(json.dumps(site.payload))}; VLANs={len(site.vlans)}; HA={bool(row.get('gateway_name_b'))}; ZPA={row.get('appc_provision') == '1'}")
                result.sites.append(SiteResult(name, "preview"))
                continue
            entry = SiteResult(name, "failed")
            result.sites.append(entry)
            try:
                ok, message, cluster_hint = self.create_site(site.template_id, site.payload)
            except Exception as exc:
                message = f"site create failed: {exc}. Creation outcome may be unknown; check the site before rerunning."
                entry.errors.append(message)
                self.log(f"ERR : {name}: {message}")
                continue
            entry.stages["Site"] = bool(ok)
            if not ok:
                entry.errors.append(f"site create failed: {message}")
                self.log(f"ERR : {name}: {entry.errors[-1]}")
                continue
            entry.status = "partial"
            self.log(f"OK  : {name}: site created")
            try:
                gateways, cluster_id = self.resolve_gateway_ids_and_cluster(name, prefer_cluster_id=cluster_hint, retries=POLL_RETRIES, delay=POLL_DELAY_S)
                if not gateways or not cluster_id:
                    raise ValueError("gateway/cluster not ready; inspect the created site before rerunning")
            except Exception as exc:
                entry.errors.append(f"site created but gateway/cluster lookup failed: {exc}")
                self.log(f"ERR : {name}: {entry.errors[-1]}")
                continue
            private_dns = row.get("private_dns", "")
            is_ha = len(gateways.split(",")) > 1
            site_id = None  # Never carry another row's target forward.
            if private_dns or site.vlans or is_ha:
                try:
                    site_id = self.resolve_site_id(name)
                except Exception as exc:
                    entry.errors.append(f"site ID lookup failed: {exc}")
                if not site_id:
                    entry.errors.append("site ID unavailable; dependent stages cannot run")
                    self.log(f"ERR : {name}: {entry.errors[-1]}")
            actions = []
            if private_dns:
                actions.append(("Private DNS", lambda: bool(site_id) and self.configure_private_dns(site_id, private_dns)))
            if site.vlans:
                actions.append(("VLANs", lambda: bool(site_id) and self.process_vlans_for_site(site_id, gateways, cluster_id, site.vlans, row)))
            if is_ha:
                actions.append(("VRRP", lambda: bool(site_id) and self.configure_vrrp(gateways, cluster_id, site.vlans, row, site_id)))
            if row.get("appc_provision") == "1":
                actions.append(("ZPA", lambda: zpa_provisioning.provision_zpa_for_site(row, self.client, self.client.base_root, cluster_id=cluster_id, config=self.config, context=plan.zpa_context)))
            for stage, action in actions:
                entry.stages[stage] = self.run_site_stage(name, stage, action)
            incomplete = [stage for stage, success in entry.stages.items() if not success]
            if incomplete:
                entry.errors.append("incomplete stages: " + ", ".join(incomplete))
                self.log(f"PARTIAL: {name}: {entry.errors[-1]}")
            else:
                entry.status = "success"
                self.log(f"OK  : {name}: all requested stages completed")
        good = sum(s.status in ("success", "preview") for s in result.sites)
        bad = len(result.sites) - good + len(result.issues)
        self.log(f"\nDone. {'Preview' if dry_run else 'Deployment'}: OK={good} ERR={bad}")
        if not dry_run and any(s.status in ("partial", "failed") for s in result.sites):
            self.log("Some sites may be partially configured. Check reported errors and existing resources before rerunning; automatic resume is not yet supported.")
        return result

    def run(self, validation: ValidationResult, *, dry_run=False) -> BatchResult:
        return self.execute(self.plan(validation), dry_run=dry_run)

    def _request_with_auto_refresh(self, method, url, **kwargs):
        return self.client.request(method, url, **kwargs)

    def _d(self, method, url, status):
        if self.DEBUG:
            self.log(f"{method} {url} -> {status}")

    def get_json(self, url: str, params: Optional[Dict[str, str]] = None, headers: Optional[Dict[str, str]] = None) -> Any:
        r = self._request_with_auto_refresh("GET", url, params=params, headers=headers, timeout=60)
        self._d("GET", getattr(r, "url", url), r.status_code)
        if r.status_code != 200:
            raise RuntimeError(f"GET {url} -> {r.status_code}: {r.text[:300]}")
        try:
            return r.json()
        except Exception:
            raise ValueError(f"Non-JSON response from {url}: {r.text[:300]}")


    def post_raw(self, url: str, data: str, headers: Optional[Dict[str, str]] = None, timeout: int = 90) -> requests.Response:
        r = self._request_with_auto_refresh("POST", url, headers=headers, timeout=timeout, data=data)
        self._d("POST", url, r.status_code)
        return r


    def post_json(self, url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
        return self.post_raw(url, json.dumps(payload), headers=headers, timeout=90)


    def put_json(self, url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, str]] = None) -> requests.Response:
        r = self._request_with_auto_refresh("PUT", url, params=params, headers=headers, timeout=90, data=json.dumps(payload))
        self._d("PUT", url, r.status_code)
        return r


    def patch_json(self, url: str, payload: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> requests.Response:
        r = self._request_with_auto_refresh("PATCH", url, headers=headers, timeout=90, data=json.dumps(payload))
        self._d("PATCH", url, r.status_code)
        return r


    def _v3_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.ORIGIN,
            "Referer": self.REFERER,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
        }


    def get_json_v3_gateway(self, params: Dict[str, str]) -> Any:
        headers = self._v3_headers()
        primary = f"{self.API_V3}/Gateway"
        try:
            return self.get_json(primary, params=params, headers=headers)
        except RuntimeError as e:
            if "404" in str(e) or "405" in str(e):
                return self.get_json(f"{self.API_V3}/Gateway/", params=params, headers=headers)
            raise


    def get_json_v3_detail(self, path: str) -> Any:
        return self.get_json(f"{self.API_V3}/{path.lstrip('/')}", headers=self._v3_headers())


    def get_json_v3_templates(self) -> List[Dict[str, Any]]:
        headers = self._v3_headers()
        base = f"{self.API_V3}/templates"
        try:
            data = self.get_json(base, headers=headers)
        except RuntimeError as e:
            if "404" in str(e) or "405" in str(e):
                data = self.get_json(base + "/", headers=headers)
            else:
                raise
        if isinstance(data, dict):
            if isinstance(data.get("result"), list):
                return data["result"]
            if isinstance(data.get("result"), dict) and isinstance(data["result"].get("rows"), list):
                return data["result"]["rows"]
        if isinstance(data, list):
            return data
        raise ValueError("Template response must contain a list, result list, or result.rows list")


    def ensure_template_id_for_row(self, row: Dict[str, str]) -> Tuple[bool, Optional[str], Optional[str]]:
        tid = (row.get("template_id") or "").strip()
        if tid:
            return True, tid, None
        tname = (row.get("template_name") or "").strip()
        if not tname:
            return False, None, "missing template_id and template_name"
        resolved = self.TEMPLATES.resolve(tname)
        if resolved:
            row["template_id"] = resolved
            return True, resolved, None
        all_items = self.get_json_v3_templates()
        names_hint = ", ".join(sorted({it.get("name","") for it in all_items if it.get("name")}))
        return False, None, f"could not resolve template_id from template_name='{tname}'. Available names: {names_hint}"


    def get_json_v3_locations(self) -> List[Dict[str, Any]]:
        # GET /api/v3/settings/locations?no_cache=true&refresh_token=enabled
        # Use the same endpoint we added to pull_site.py
        params = {
            "no_cache": "true",
            "refresh_token": "enabled",
        }
        url = f"{self.API_V3}/settings/locations"
        headers = self._v3_headers()
        try:
            data = self.get_json(url, params=params, headers=headers)
        except RuntimeError:
            # Retry with trailing slash just in case, though screenshot didn't show it needed.
            data = self.get_json(url + "/", params=params, headers=headers)

        if not isinstance(data, dict) or not isinstance(data.get("locations"), list):
            raise ValueError("Location response must contain a locations list; refusing to infer a new location")
        return data["locations"]


    def get_json_v3_location_templates(self) -> List[Dict[str, Any]]:
        params = {
            "refresh_token": "enabled",
        }
        url = f"{self.API_V3}/settings/location_templates"
        headers = self._v3_headers()
        data = self.get_json(url, params=params, headers=headers)

        # Response shape verified against the Add Site browser capture.
        if not isinstance(data, dict) or not isinstance(data.get("location_templates"), list):
            raise ValueError("Location-template response must contain a location_templates list")
        return data["location_templates"]


    def norm_dhcp_service(self, val: str, has_range: bool) -> str:
        v = (val or "").strip().lower().replace("-", "_")
        if v == "on": return "inherit"
        if v in ("inherit", "non_airgapped", "no_dhcp"): return v
        if v == "off": return "no_dhcp"
        return "inherit" if has_range else "no_dhcp"


    def get_json_v3_gateway_list(self, site_name: str) -> Dict[str, Any]:
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
        return self.get_json_v3_gateway(params)


    def find_site_row_by_name(self, site_name: str) -> Optional[Dict[str, Any]]:
        data = self.get_json_v3_gateway_list(site_name)
        rows = data.get("rows") or data.get("result",{}).get("rows",[]) or []
        wanted = site_name.strip().lower()
        for r in rows:
            nm = (r.get("location_display_name") or r.get("site_name") or r.get("location") or "").strip().lower()
            if nm == wanted:
                return r
        return None

    def site_exists(self, site_name: str) -> bool:
        """Fail closed when the inventory cannot establish absence.

        Use an unfiltered inventory: server search matching may differ from our
        case-insensitive exact comparison. Large inventories need pagination;
        until supported, refuse to infer absence from a potentially partial page.
        """
        data = self.get_json_v3_gateway_list("")
        body = data.get("result", data) if isinstance(data, dict) else None
        rows = body.get("rows") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Unrecognized site inventory")
        wanted = site_name.strip().casefold()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Invalid site inventory row")
            names = [row.get(key) for key in ("location_display_name", "site_name", "location")]
            if not any(isinstance(name, str) and name.strip() for name in names):
                raise ValueError("Site inventory row has no name")
            if any(isinstance(name, str) and name.strip().casefold() == wanted for name in names):
                return True
        if len(rows) >= 100:
            raise ValueError("Site inventory may be truncated")
        for container in (data, body):
            for key in ("total", "total_count", "totalCount", "count"):
                if key in container and int(container[key]) > len(rows):
                    raise ValueError("Site inventory is incomplete")
        return False


    def get_gateway_detail_v3(self, gateway_id: str) -> Dict[str, Any]:
        return self.get_json_v3_detail(f"Gateway/{gateway_id}")


    def _parse_cluster_id_from_create_resp(self, text: str) -> Optional[int]:
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


    def resolve_gateway_ids_and_cluster(self, site_name: str, *, prefer_cluster_id: Optional[int] = None, retries: int = POLL_RETRIES, delay: float = POLL_DELAY_S) -> Tuple[Optional[str], Optional[int]]:
        wanted_cluster = prefer_cluster_id
        for _ in range(max(1, retries)):
            row = self.find_site_row_by_name(site_name)
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


    def resolve_site_id(self, site_name: str, retries: int = 10, delay: float = POLL_DELAY_S) -> Optional[str]:
        """Resolve this site's ID for DNS, VLANs, and HA using the same response fields."""
        for attempt in range(max(1, retries)):
            row = self.find_site_row_by_name(site_name)
            if row:
                site_id = (row.get("cluster_info") or {}).get("site_id") or row.get("site_id") or row.get("id")
                if site_id:
                    return str(site_id)
            if attempt < retries - 1:
                time.sleep(delay)
        return None


    def get_gateway_interfaces_v2(self, site_id: str) -> List[Dict[str, Any]]:
        """
        GET /api/v2/Gateway/interfaces?siteID=<site_id>
        Returns a list:
        [
          {"gateway_id":"...","gateway_name":"...", "interfaces":[{"name":"ge4","interface_type":"ha"}, ...]},
          ...
        ]
        """
        url = f"{self.API_V2}/Gateway/interfaces"
        params = {"siteID": site_id, "refresh_token": "enabled"}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.ORIGIN, "Referer": self.REFERER,
        }
        data = self.get_json(url, params=params, headers=headers)
        return data if isinstance(data, list) else []


    def discover_iface_inventory(self, site_id: str, gateways_str: str):
        """
        From interfaces GET, derive:
          - ha_link_map: {gateway_uuid: ha_iface_name}
          - mgmt_names: set of management iface names (e.g., {'ge1'})
          - common_trackables: names present on ALL peers whose type is LAN or WAN
        """
        items = self.get_gateway_interfaces_v2(site_id)
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

        if self.DEBUG:
            self.log("Interfaces discovery:")
            self.log("  HA link map:", ha_link_map)
            self.log("  mgmt names :", mgmt_names)
            self.log("  common trackables:", sorted(common_trackables))

        return ha_link_map, mgmt_names, common_trackables


    def _clean_iface(self, x: str) -> str:
        return (x or "").strip().lower()


    def _unique_preserve(self, seq: Iterable[str]) -> List[str]:
        out: List[str] = []
        seen: Set[str] = set()
        for s in seq:
            s = self._clean_iface(s)
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out


    def _collect_wan_ifaces_from_row(self, row: Dict[str, str]) -> List[str]:
        candidates = [
            "wan_interface_name", "wan0_interface_name", "wan0_interface",
            "wan_interface", "wan1_interface_name", "wan1_interface"
        ]
        vals = []
        for k in candidates:
            v = self._clean_iface(row.get(k, ""))
            if v:
                vals.append(v)
        return self._unique_preserve(vals)


    def _collect_lan_ifaces_from_vlans(self, vlans: List[Dict[str, Any]], exclude: Iterable[str]) -> List[str]:
        ex = { self._clean_iface(x) for x in exclude }
        found: List[str] = []
        for v in vlans:
            iface = self._clean_iface(v.get("interface", ""))
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


    def _vrrp_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.ORIGIN,
            "Referer": self.REFERER,
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/json",
        }


    def _vrrp_url(self, cluster_id: int) -> str:
        return f"{self.API_V3}/vrrp/config/{cluster_id}?refresh_token=enabled"


    def post_vrrp(self, url: str, headers: Dict[str, str], payload: Dict[str, Any]) -> Tuple[bool, str, int]:
        body = json.dumps(payload)
        r = self.post_raw(url, body, headers=headers, timeout=60)
        return (r.status_code in (200, 204)), (r.text or "")[:300], r.status_code


    def build_vrrp_payload(self,
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
        ha_link_map, mgmt_names, common_trackables = self.discover_iface_inventory(site_id, gateways_str)

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
        wan_ifaces = self._collect_wan_ifaces_from_row(row)
        lan_ifaces = self._collect_lan_ifaces_from_vlans(vlans, exclude=wan_ifaces)
        raw_track = self._unique_preserve([*wan_ifaces, *lan_ifaces])

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

        if self.DEBUG:
            self.log("VRRP payload (keys redacted):", json.dumps(payload, indent=2))

        return payload, link_iface_used, track_value


    def create_site(self, template_id: str, payload: Dict[str, Any]) -> Tuple[bool, str, Optional[int]]:
        url = f"{self.API_V3}/templates/{template_id}/deploy_site?refresh_token=enabled"
        r = self.post_json(url, payload, headers=self._v3_headers())
        cid = None
        try:
            cid = self._parse_cluster_id_from_create_resp(r.text or "")
        except Exception:
            cid = None
        if r.status_code in (200, 201, 202):
            return True, r.text, cid
        return False, f"{r.status_code} {r.text[:300]}", cid


    def _network_base_from_start(self, start_ip: str, subnet_bits: str) -> Optional[str]:
        s = (start_ip or "").strip()
        b = (subnet_bits or "").strip()
        if not s or not b:
            return None
        try:
            net = ipaddress.ip_network(f"{s}/{b}", strict=False)
            return str(net.network_address)
        except Exception:
            return None


    def _short_name(self, name: str, maxlen: int = 16) -> str:
        n = (name or "").strip()
        return n if len(n) <= maxlen else n[:maxlen]


    def _maybe_dup_interface_for_ha(self, interface: str, gateways_str: str) -> str:
        if "," in gateways_str:
            if interface and "," not in interface:
                return f"{interface},{interface}"
        return interface


    def vlan_to_v2_payload(self, vlan: Dict[str, Any], gateways_str: str, cluster_id: int, per_network_dns: str = "") -> Dict[str, Any]:
        start_ip = vlan.get("start_ip") or vlan.get("default_gateway") or ""
        subnet   = str(vlan.get("subnet") or "").strip()
        ip_range = self._network_base_from_start(start_ip, subnet) or vlan.get("ip_range") or ""
        display   = vlan.get("display_name") or vlan.get("name") or ""
        safe_name = self._short_name(vlan.get("name") or display, 16)
        interface = self._maybe_dup_interface_for_ha(vlan.get("interface") or "", gateways_str)
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
            "dhcp_service": self.norm_dhcp_service(vlan.get("dhcp_service",""), bool(vlan.get("dhcp_range"))),
            "share_over_vpn": bool(vlan.get("share_over_vpn", False)),
            "enabled": bool(vlan.get("enabled", True)),
        }


    def post_vlan(self, vlan_payload: Dict[str, Any]) -> Tuple[bool, str]:
        url = f"{self.API_V2}/Network/?refresh_token=enabled"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.ORIGIN,
            "Referer": self.REFERER,
            "Content-Type": "application/json",
        }
        r = self.post_json(url, vlan_payload, headers=headers)
        if r.status_code in (200,201,202):
            return True, r.text
        return False, f"{r.status_code} {r.text[:300]}"


    def list_site_vlans_v2(self, site_id: str) -> List[Dict[str, Any]]:
        url = f"{self.API_V2}/Network/"
        params = {"siteId": site_id, "refresh_token": "enabled"}
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Origin": self.ORIGIN,
            "Referer": self.REFERER,
        }
        data = self.get_json(url, params=params, headers=headers)
        if isinstance(data, dict):
            rows = data.get("rows") or data.get("result", {}).get("rows")
            return rows or []
        if isinstance(data, list):
            return data
        return []


    def _vlan_key(self, v: Dict[str, Any]) -> Tuple[str, str, str, str]:
        nm = (v.get("display_name") or v.get("name") or "").strip().lower()
        tg = str(v.get("tag") or "").strip()
        iface = (v.get("interface") or "").strip().lower()
        gw = (v.get("default_gateway") or v.get("start_ip") or "").strip()
        return (nm, tg, iface, gw)


    def configure_private_dns(self, site_id: str, private_dns_ips: str, dry_run: bool = False) -> bool:
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
            self.log(f"   [DRY-RUN] Would configure Private DNS for site {site_id}: {ips}")
            return True

        # Use the exact pattern from the screenshot:
        # PUT /api/v2/group-membership?site_id=...&group_name=System-Private-DNS-Servers-Group&refresh_token=enabled
        # Payload: { "member_attributes": { "ip_prefix": [...] } }

        url = f"{self.API_V2}/group-membership"
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
            r = self.put_json(url, payload, headers={"Accept": "application/json", "Content-Type": "application/json"}, params=params)
            if r.status_code in (200, 201, 204):
                self.log(f"   ✅ Configured Private DNS: {ips}")
                return True
            else:
                self.log(f"   ❌ Failed to configure Private DNS: {r.status_code} {r.text[:200]}")
                return False
        except Exception as e:
            self.log(f"   ❌ Error configuring Private DNS: {e}")
            return False


    def process_vlans_for_site(self, site_id: str, gw_ids: str, cluster_id: int, vlans: list, row: Dict[str, str], dry_run: bool = False) -> bool:
        if not vlans:
            return True
        if any("lo0" in {p.strip().lower() for p in v.get("interface", "").split(",")} for v in vlans):
            # An accepted network POST is not proof of an interface binding.
            # The API can retain the literal "lo0" with no gateway association
            # when the target gateway does not yet have a loopback interface.
            inventory = self.get_gateway_interfaces_v2(site_id)
            for gateway_id in (g.strip() for g in gw_ids.split(",")):
                matches = [i for g in inventory if g.get("gateway_id") == gateway_id
                           for i in g.get("interfaces", [])
                           if str(i.get("name", "")).lower() == "lo0" and i.get("id")]
                if len(matches) != 1:
                    self.log(f"ERR : loopback lo0 is unavailable or ambiguous on gateway {gateway_id}; no VLAN writes performed. Verify gateway activation and interface configuration before retrying.")
                    return False
        self.log(f"   Processing {len(vlans)} validated VLANs...")

        # Use wan_dns as default for per_network_dns if not specified
        per_net_dns = (row.get("wan_dns") or "").strip()

        vlan_ok = 0; vlan_fail = 0
        for v in vlans:
            v2_payload = self.vlan_to_v2_payload(v, gw_ids, cluster_id, per_network_dns=per_net_dns)

            if dry_run:
                 self.log(f"   [DRY-RUN] Would POST VLAN {v.get('name')} tag={v.get('tag')}")
                 vlan_ok += 1
                 continue

            okv, m = self.post_vlan(v2_payload)
            if okv:
                vlan_ok += 1
            else:
                vlan_fail += 1
                self.log(f"    ❌ VLAN ERR: {m}")

        success = vlan_fail == 0
        marker = "✅" if success else "❌"
        self.log(f"   {marker} VLANs processed: OK={vlan_ok} ERR={vlan_fail}")

        # Post-processing: Enable and Share Over VPN (Restored from original logic)
        if dry_run:
            return success

        current = self.list_site_vlans_v2(str(site_id))
        id_map: Dict[Tuple[str,str,str,str], Dict[str,Any]] = {self._vlan_key(v): v for v in current}

        def find_id_for(csv_vlan: Dict[str,Any]) -> Optional[str]:
            k = self._vlan_key(csv_vlan)
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
            "Origin": self.ORIGIN,
            "Referer": self.REFERER,
            "Content-Type": "application/json",
        }

        # a) Enable (PUT status="provisioned")
        for v in vlans:
            if not v.get("enabled", True):
                continue
            vid = find_id_for(v)
            if not vid:
                self.log(f"    ⚠️  WARN enable: could not match VLAN id for {v.get('name')}/{v.get('tag')}")
                success = False
                continue

            # Only update if needed? The original code just did it.
            url = f"{self.API_V2}/Network/update/{vid}?refresh_token=enabled"
            payload = {
                "name": v.get("display_name") or v.get("name") or "",
                "subnet": str(v.get("subnet") or ""),
                "per_network_dns": (per_net_dns or ""),
                "status": "provisioned",
            }
            try:
                r_put = self.put_json(url, payload, headers=v2_hdrs)
                if r_put.status_code not in (200, 204):
                    self.log(f"    ⚠️  WARN enable PUT {vid}: {r_put.status_code} {r_put.text[:180]}")
                    success = False
                else:
                    # print(f"    ✅ Enabled VLAN {v.get('name')}") # Optional: reduce noise
                    pass
            except Exception as e:
                self.log(f"    ⚠️  Error enabling VLAN {vid}: {e}")
                success = False

        # b) share_over_vpn (PATCH)
        for v in vlans:
            if not v.get("share_over_vpn", False):
                continue
            vid = find_id_for(v)
            if not vid:
                self.log(f"    ⚠️  WARN share_over_vpn: could not match VLAN id for {v.get('name')}/{v.get('tag')}")
                success = False
                continue

            url = f"{self.API_V2}/Network/share-over-vpn?refresh_token=enabled"
            payload = {"id": vid, "share_over_vpn": True}
            try:
                r_patch = self.patch_json(url, payload, headers=v2_hdrs)
                if r_patch.status_code not in (200, 204):
                    self.log(f"    ⚠️  WARN share_over_vpn PATCH {vid}: {r_patch.status_code} {r_patch.text[:180]}")
                    success = False
                else:
                    # print(f"    ✅ Shared VLAN {v.get('name')} over VPN")
                    pass
            except Exception as e:
                self.log(f"    ⚠️  Error sharing VLAN {vid}: {e}")
                success = False

        return success


    def configure_vrrp(self, gw_ids: str, cluster_id: int, vlans: list, row: Dict[str, str], site_id: str, dry_run: bool = False) -> bool:

        # 2. Build payload
        try:
            payload, link_used, track_val = self.build_vrrp_payload(
                cluster_id, gw_ids, row, vlans, site_id=site_id, vrid=row.get("vrrp_vrid", "16")
            )
        except (Exception, SystemExit) as e:
            self.log(f"   ❌ VRRP Config Failed: {e}")
            return False

        if not payload:
            # No VRRP configuration is required for a standalone gateway.
            return True

        if dry_run:
            self.log(f"   [DRY-RUN] Would POST VRRP config for cluster {cluster_id}")
            self.log(f"             Link: {link_used}, Track: {track_val}")
            return True

        url = self._vrrp_url(cluster_id)
        headers = self._vrrp_headers()

        ok, msg, code = self.post_vrrp(url, headers, payload)
        if ok:
             self.log(f"   ✅ VRRP Configured (Link={link_used}, Track={track_val})")
        else:
             self.log(f"   ❌ VRRP Failed: {code} {msg}")
        return ok


    def run_site_stage(self, site_name: str, stage: str, action: Callable[[], bool]) -> bool:
        """Report a stage failure without abandoning the rest of the batch."""
        try:
            success = bool(action())
        except (Exception, SystemExit) as e:
            # Legacy auth/validation helpers use SystemExit for operational errors.
            # KeyboardInterrupt is deliberately not caught.
            self.log(f"ERR : {site_name}: {stage} failed: {e}")
            return False
        if not success:
            self.log(f"ERR : {site_name}: {stage} incomplete; see details above")
        return success
