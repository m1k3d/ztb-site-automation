"""Read tenant references for the local editor; never create or export resources."""

from dataclasses import replace
from copy import deepcopy
import re
import threading
from urllib.parse import quote, urlsplit

import requests

from api_client import ZTBClient
from automation_config import Settings
from zpa_provisioning import prepare_zpa
from additional_wans import from_reference
import json
from pull_site import (is_ha_internal_vlan, is_wan_vlan, parse_private_dns_members,
                       site_to_csv_row, vlans_to_csv_rows)


class ReferenceError(ValueError):
    """A message safe to return to the browser, without API response bodies."""


class ReferenceSites:
    def __init__(self, env_file):
        self.env_file = env_file
        self.client = None
        self.rows = {}
        self.zpa_config = None
        self.zpa_context = None
        self.revision = 0
        self.zpa_override = False
        self.templates_cache = None
        self.interfaces_cache = {}
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            if self.client is not None:
                self.client.close()
            self.client = None
            self.rows = {}
            self.zpa_config = None
            self.zpa_context = None

    def connect_zpa(self, connection=None):
        """Check authentication/customer/certificate without creating resources or writing credentials."""
        with self.lock:
            self.revision += 1
            self.zpa_override = True
            self.zpa_config = None
            self.zpa_context = None
            try:
                if connection is None:
                    config = replace(Settings.load(self.env_file), env_path=None)
                else:
                    fields = ("base_url", "client_id", "client_secret", "customer_id", "enrollment_cert_name")
                    if not isinstance(connection, dict) or any(not isinstance(connection.get(key, ""), str) for key in fields):
                        raise ReferenceError("Enter valid ZPA connection details.")
                    values = {key: connection.get(key, "").strip() for key in fields}
                    values["enrollment_cert_name"] = values["enrollment_cert_name"] or "Connector"
                    config = Settings(zpa_enabled="true", **{f"zpa_{key}": value for key, value in values.items()})
                errors = config.errors(require_ztb=False, require_zpa=True)
                if errors:
                    labels = {"ZPA_BASE_URL": "Cloud API URL", "ZPA_CLIENT_ID": "Client ID",
                              "ZPA_CLIENT_SECRET": "Client secret", "ZPA_ENABLED": "ZPA_ENABLED in local settings"}
                    invalid = list(dict.fromkeys(labels.get(error.split(":", 1)[0], "ZPA settings") for error in errors))
                    raise ReferenceError("Check ZPA settings: " + ", ".join(invalid) + ".")
                context = prepare_zpa(config, write_env=False, quiet=True)
                self.zpa_config, self.zpa_context = config, context
                return {"cloud": urlsplit(context.base_url).hostname, "customer_id": context.customer_id,
                        "certificate": config.zpa_enrollment_cert_name}
            except ReferenceError:
                raise
            except ValueError as exc:
                if str(exc).startswith("ZPA_CUSTOMER_ID"):
                    raise ReferenceError("ZPA customer ID is missing or does not match the authenticated customer. Check the customer ID.") from None
                if str(exc).startswith("ZPA_ENROLLMENT_CERT_NAME"):
                    raise ReferenceError("Unable to find the ZPA enrollment certificate. Check its exact name and certificate read permissions.") from None
                raise ReferenceError("Unable to verify ZPA. Check the cloud URL, credentials, and API permissions.") from None
            except Exception:
                raise ReferenceError("Unable to connect to ZPA. Check the cloud URL, credentials, and network access.") from None

    def _get(self, path, params, label, *, version=3):
        try:
            base = self.client.api_v3 if version == 3 else self.client.api_v2
            response = self.client.request("GET", base + path, params=params, timeout=30,
                headers={"Origin": self.client.origin, "Referer": self.client.referer})
            if response.status_code != 200:
                raise ReferenceError(f"Unable to read {label} (HTTP {response.status_code}). Check the connection and API permissions.")
            return response.json()
        except ReferenceError:
            raise
        except (requests.RequestException, RuntimeError, ValueError, OSError):
            raise ReferenceError(f"Unable to read {label}. Check your tenant URL, API key, and network connection, then reconnect.") from None

    @staticmethod
    def _rows(data, label, *, allow_list=False):
        rows = data if allow_list and isinstance(data, list) else None
        if isinstance(data, dict):
            rows = data.get("rows")
            if rows is None and isinstance(data.get("result"), dict):
                rows = data["result"].get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ReferenceError(f"Unexpected {label} response. Nothing was imported.")
        return rows

    def connect(self, connection=None):
        with self.lock:
            self.revision += 1
            self.templates_cache = None
            self.interfaces_cache.clear()
            if self.client is not None:
                self.client.close()
            self.client = None
            self.rows = {}
            try:
                if connection is None:
                    config = Settings.load(self.env_file)
                else:
                    if not isinstance(connection, dict) or any(not isinstance(connection.get(k), str) for k in ("tenant_url", "api_key")):
                        raise ReferenceError("Enter your tenant URL and API key.")
                    config = Settings(ztb_api_base=connection["tenant_url"].strip(), api_key=connection["api_key"].strip())
                if config.errors():
                    raise ReferenceError("A valid HTTPS tenant API URL and API key are required. Check your connection settings.")
                # The editor keeps refreshed tokens in memory; it never rewrites .env.
                self.client = ZTBClient(replace(config, env_path=None), emit=lambda *_: None)
                rows = self._rows(self._get("/Gateway/", {"gateway_type": "isolation", "sortdir": "asc",
                    "sort": "location", "search": "", "page": 0, "limit": "100",
                    "refresh_token": "enabled"}, "sites"), "site inventory")
                sites = []
                for row in rows:
                    ci = row.get("cluster_info") or {}
                    site_id = ci.get("site_id") or row.get("site_id") or row.get("id")
                    name = row.get("location_display_name") or row.get("site_name") or row.get("name")
                    if not site_id or not isinstance(name, str) or not name.strip():
                        raise ReferenceError("A site has no usable name or ID. Nothing was imported.")
                    site_id = str(site_id)
                    if site_id in self.rows:
                        continue
                    self.rows[site_id] = (name, row)
                    location = row.get("location") if isinstance(row.get("location"), dict) else row
                    sites.append({"id": site_id, "name": name, "city": str(location.get("city") or ""),
                        "country": str(location.get("country") or ""),
                        "template": str(row.get("template_name") or ci.get("template_name") or "")})
                return {"tenant": urlsplit(self.client.base_root).hostname,
                        "sites": sorted(sites, key=lambda site: site["name"].lower()), "limited": len(rows) >= 100}
            except Exception as exc:
                if self.client is not None:
                    self.client.close()
                self.client = None
                self.rows = {}
                if isinstance(exc, ReferenceError):
                    raise
                raise ReferenceError("Unable to connect. Check the local credential file and network access.") from None

    def deployment_settings(self):
        """Return a private credential snapshot; never serialize this to the browser."""
        with self.lock:
            if self.client is None:
                raise ReferenceError("Connect to your ZTB tenant before previewing deployment.")
            config = replace(self.client.config, bearer=self.client.token or self.client.config.bearer, env_path=None)
            if self.zpa_override:
                zpa = self.zpa_config or Settings()
                config = replace(config, **{field: getattr(zpa, field) for field in
                    ("zpa_base_url", "zpa_client_id", "zpa_client_secret", "zpa_customer_id", "zpa_enabled", "zpa_enrollment_cert_name")})
            return self.revision, config

    def connection_status(self):
        with self.lock:
            return {"tenant": urlsplit(self.client.base_root).hostname if self.client else None,
                    "zpa_verified": self.zpa_context is not None, "revision": self.revision}

    def zones(self):
        """Read assignable zone objects; expose only display names and types."""
        with self.lock:
            if self.client is None:
                raise ReferenceError("Connect to your ZTB tenant to load zones.")
            types = {"lan_zone", "mgt_zone"}
            zones, seen, received = {}, set(), 0
            for page in range(100):
                data = self._get("/groups", {"group_type": "lan_zone,mgt_zone", "sort": "display_name",
                    "sortdir": "asc", "page": page, "size": 100, "refresh_token": "enabled"}, "zones", version=2)
                rows = data.get("result") if isinstance(data, dict) else None
                count = data.get("count") if isinstance(data, dict) else None
                if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                        or (count is not None and (type(count) is not int or count < 0))):
                    raise ReferenceError("Unexpected zone response. Existing VLAN zones were kept.")
                for row in rows:
                    name, kind = row.get("display_name"), row.get("type")
                    if not isinstance(name, str) or not name.strip() or kind not in types:
                        raise ReferenceError("Unexpected zone object. Existing VLAN zones were kept.")
                    identity = (name, kind)
                    if identity in seen:
                        raise ReferenceError("Zone lookup returned repeated results. Existing VLAN zones were kept.")
                    seen.add(identity)
                    if row.get("hidden") not in (True, "true", "1", 1):
                        zones[name] = {"name": name, "type": kind}
                received += len(rows)
                if (count is not None and received >= count) or (count is None and len(rows) < 100):
                    return {"zones": sorted(zones.values(), key=lambda zone: zone["name"].casefold())}
                if not rows:
                    break
            raise ReferenceError("Could not load the complete zone list. Existing VLAN zones were kept.")

    def _template_rows(self, path, label):
        """Read every page; never treat a truncated or repeated result as complete."""
        result, seen = [], set()
        for page in range(100):
            data = self._get(path, {"page": page, "size": 100, "sort": "name", "sortdir": "asc"}, label)
            rows = data if isinstance(data, list) else data.get("result") if isinstance(data, dict) else None
            count = data.get("count") if isinstance(data, dict) else None
            if isinstance(rows, dict):
                count = rows.get("count", count)
                rows = rows.get("rows")
            if (not isinstance(rows, list) or
                    (count is not None and (type(count) is not int or count < 0))):
                raise ReferenceError(f"Unexpected {label} response. Existing interfaces were kept.")
            for row in rows:
                if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                    raise ReferenceError(f"Unexpected {label} object. Existing interfaces were kept.")
                if row["id"] in seen:
                    raise ReferenceError(f"Repeated {label} results. Existing interfaces were kept.")
                seen.add(row["id"])
                result.append(row)
            if count is not None and len(result) > count:
                raise ReferenceError(f"Inconsistent {label} count. Refresh interfaces to try again.")
            if (count is not None and len(result) == count) or (count is None and len(rows) < 100):
                return result
            if not rows:
                break
        raise ReferenceError(f"Could not load the complete {label} list. Existing interfaces were kept.")

    def interfaces(self, template_name="", template_id="", refresh=False):
        """Cache role-aware template ports for this ZTB connection; expose no raw objects."""
        if (not isinstance(template_name, str) or not isinstance(template_id, str)
                or type(refresh) is not bool or max(len(template_name), len(template_id)) > 256):
            raise ReferenceError("Choose a valid site template to load interfaces.")
        template_name, template_id = template_name.strip(), template_id.strip()
        with self.lock:
            if self.client is None:
                raise ReferenceError("Connect to your ZTB tenant to load interfaces.")
            if not template_id and not template_name:
                raise ReferenceError("Choose a site template to load interfaces.")
            if refresh:
                self.templates_cache = None
                self.interfaces_cache.clear()
            if self.templates_cache is None:
                rows = self._template_rows("/templates", "templates")
                if any(not isinstance(row.get("name"), str) or not row["name"].strip() for row in rows):
                    raise ReferenceError("Unexpected template name. Existing interfaces were kept.")
                self.templates_cache = rows
            matches = [t for t in self.templates_cache if
                       (t["id"] == template_id if template_id else t["name"].casefold() == template_name.casefold())]
            if len(matches) != 1:
                raise ReferenceError("Site template was not found uniquely. Check the name or ID, then refresh interfaces.")
            template = matches[0]
            tid = template["id"]
            if tid not in self.interfaces_cache:
                rows = self._template_rows(f"/templates/{quote(tid, safe='')}/interfaces", "template interfaces")
                gateways = {}
                for row in rows:
                    name, kind, gateway = (row.get(k) for k in ("name", "interface_type", "gateway_id"))
                    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]*", name)
                            or not isinstance(kind, str) or gateway not in ("Gateway-1", "Gateway-2")):
                        raise ReferenceError("Unexpected template interface. Existing interfaces were kept.")
                    ports = gateways.setdefault(gateway, [])
                    if any(port["name"] == name for port in ports):
                        raise ReferenceError("Duplicate template interface. Existing interfaces were kept.")
                    ports.append({"name": name, "type": kind, "bond_member": bool(row.get("bonding_parent"))})
                if not gateways:
                    raise ReferenceError("The template has no interface inventory. Enter interfaces manually or choose another template.")
                self.interfaces_cache[tid] = {
                    "template": {"id": tid, "name": template["name"],
                                 "platform": str(template.get("platform_type") or ""),
                                 "deployment_type": str(template.get("deployment_type") or "")},
                    "gateways": [{"id": key, "interfaces": ports} for key, ports in sorted(gateways.items())],
                }
            return deepcopy(self.interfaces_cache[tid])

    def pull(self, site_id):
        with self.lock:
            if self.client is None or not isinstance(site_id, str) or site_id not in self.rows:
                raise ReferenceError("Connect and select a site from the current tenant list first.")
            name, row = self.rows[site_id]
            vlans = self._rows(self._get("/Network/", {"siteId": site_id, "refresh_token": "enabled"},
                "site VLANs", version=2), "VLAN", allow_list=True)
            dns_data = self._get("/group-membership", {"site_id": site_id,
                "group_name": "System-Private-DNS-Servers-Group", "refresh_token": "enabled"}, "private DNS", version=2)
            try:
                private_dns = parse_private_dns_members(dns_data, site_id)
                fields = site_to_csv_row(row, name, private_dns, wan_networks=vlans)
                gateways = row.get("gateways") or (row.get("cluster_info") or {}).get("gateways") or []
                extras = from_reference([v for v in vlans if is_wan_vlan(v)], gateways, fields)
                if extras:
                    fields.update(copy_additional_wans='1',additional_wans_json=json.dumps(extras))
                networks = vlans_to_csv_rows([v for v in vlans if not is_wan_vlan(v) and not is_ha_internal_vlan(v)], gateways=gateways)
                loopbacks = 0
                for vlan in networks:
                    if "lo0" in [port.strip().lower() for port in vlan["interface"].split(",")]:
                        vlan.update(subnet="32", dhcp_service="off", dhcp_start="", dhcp_end="")
                        loopbacks += 1
                gateways = row.get("gateways") or (row.get("cluster_info") or {}).get("gateways") or []
                slots = [g.get("template_gateway_id") or f"Gateway-{i+1}" for i, g in enumerate(gateways[:2])]
                warnings = []
                if not fields["wan_interface_name"] or (fields["gateway_name_b"] and not fields["wan1_interface_name"]):
                    warnings.append("WAN assignment could not be resolved uniquely; choose the WAN interface before deployment.")
                return {"site": {"fields": fields, "vlans": networks, "interface_gateways": slots,
                                 "reference": {"id": site_id, "name": name}},
                        "loopbacks": loopbacks, "warnings": warnings}
            except (ValueError, TypeError, AttributeError):
                raise ReferenceError("Unable to interpret the site's network settings. Nothing was imported.") from None
