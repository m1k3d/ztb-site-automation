"""Opt-in, create-only staging of disabled branch LAN application segments.

Uses the legacy ZPA mgmtconfig API, like zpa_provisioning.py. Never grants
policy access, reuses existing resources, or retries resource creation.
"""

from dataclasses import dataclass
import ipaddress
import re

import requests


PORT_RANGES = ((1, 52), (54, 65535))
COLLECTIONS = ("segmentGroup", "serverGroup", "application")


@dataclass(frozen=True)
class SegmentPlan:
    site_name: str
    segment_group_name: str
    server_group_name: str
    application_name: str
    subnets: tuple

    def report(self):
        return {
            "status": "planned",
            "application_name": self.application_name,
            "segment_group_name": self.segment_group_name,
            "server_group_name": self.server_group_name,
            "subnets": list(self.subnets),
            "enabled": False,
            "icmp_access": "NONE",
            "tcp_ports": "1-52,54-65535",
            "udp_ports": "1-52,54-65535",
            "health_reporting": "ON_ACCESS",
            "resources": {},
        }


def build_segment_plan(site_name, vlans):
    selected = [v for v in vlans if v.get("zpa_include") is True]
    if not selected:
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", site_name.lower()).strip("-")
    # Conservative local naming bound. Never truncate distinct names into one.
    if not slug or len(slug) > 100:
        raise ValueError("ZPA site name must normalize to 1–100 ASCII letters, digits, or hyphens")
    networks = set()
    for vlan in selected:
        network = ipaddress.IPv4Network(f"{vlan['default_gateway']}/{vlan['subnet']}", strict=False)
        if network.prefixlen == 0:
            raise ValueError("zpa_include cannot publish a default route")
        networks.add(network)
    subnets = tuple(str(n) for n in sorted(networks))
    base = f"ztb-{slug}"
    return SegmentPlan(site_name, f"{base}-segments", f"{base}-servers", f"{base}-lan", subnets)


def _url(context, resource):
    return f"{context.base_url}/mgmtconfig/v1/admin/customers/{context.customer_id}/{resource}"


def _request(context, method, resource, **kwargs):
    """Return JSON without including response bodies or credentials in errors."""
    try:
        response = requests.request(
            method, _url(context, resource),
            headers={"Authorization": f"Bearer {context.token}", "Content-Type": "application/json"},
            timeout=30, **kwargs,
        )
    except requests.RequestException:
        raise RuntimeError(f"ZPA {method} {resource}: request failed; inspect resources before retrying") from None
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"ZPA {method} {resource}: HTTP {response.status_code}; check permissions, capacity, and existing resources")
    if method == "PUT":
        return None
    try:
        return response.json()
    except ValueError:
        raise RuntimeError(f"ZPA {method} {resource}: invalid JSON; inspect resources before retrying") from None


def _identifier(value):
    return isinstance(value, (int, str)) and not isinstance(value, bool) and str(value).strip() not in ("", "None", "0") and bool(re.fullmatch(r"[A-Za-z0-9_-]+", str(value)))


def list_resources(context, resource):
    """Require complete, consistent pagination before concluding absence."""
    rows, seen = [], set()
    expected = None
    for page in range(1, 1001):
        data = _request(context, "GET", resource, params={"page": page, "pageSize": 100})
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise ValueError(f"ZPA {resource}: unrecognized inventory; cannot establish absence")
        try:
            total, pages = int(data["totalCount"]), int(data["totalPages"])
        except (KeyError, ValueError, TypeError):
            raise ValueError(f"ZPA {resource}: missing pagination totals") from None
        if total < 0 or pages < 0 or (total > 0 and pages == 0):
            raise ValueError(f"ZPA {resource}: invalid pagination totals")
        if expected is not None and expected != (total, pages):
            raise ValueError(f"ZPA {resource}: inventory changed during pagination; re-plan")
        expected = (total, pages)
        for row in data["list"]:
            if not isinstance(row, dict) or not _identifier(row.get("id")) or not isinstance(row.get("name"), str) or not row["name"].strip():
                raise ValueError(f"ZPA {resource}: malformed inventory row")
            identifier = str(row["id"])
            if identifier in seen:
                raise ValueError(f"ZPA {resource}: repeated inventory page or ID")
            seen.add(identifier)
            rows.append(row)
        if page >= pages:
            if len(rows) != total:
                raise ValueError(f"ZPA {resource}: incomplete inventory")
            return rows
        if not data["list"]:
            raise ValueError(f"ZPA {resource}: empty intermediate page")
    raise ValueError(f"ZPA {resource}: inventory exceeds pagination safety bound")


def load_inventory(context):
    return {resource: list_resources(context, resource) for resource in COLLECTIONS}


def _ip_network(value):
    try:
        return ipaddress.ip_network(value, strict=False)
    except ValueError:
        return None  # DNS names cannot be resolved safely into a static overlap proof.


def check_conflicts(plan, inventory):
    names = dict(zip(COLLECTIONS, (plan.segment_group_name, plan.server_group_name, plan.application_name)))
    for resource, name in names.items():
        for row in inventory[resource]:
            if row["name"].strip().casefold() == name.casefold():
                raise ValueError(f"ZPA {resource} '{name}' already exists; inspect it before recovery")
    planned = [ipaddress.ip_network(n) for n in plan.subnets]
    for application in inventory["application"]:
        domains = application.get("domainNames")
        if not isinstance(domains, list) or not all(isinstance(d, str) for d in domains):
            raise ValueError("ZPA application inventory is missing destinations; cannot check overlaps")
        for domain in domains:
            existing = _ip_network(domain)
            if domain.strip() == "*" or (existing and any(existing.version == n.version and existing.overlaps(n) for n in planned)):
                raise ValueError(f"ZPA application '{application['name']}' overlaps selected LAN subnets; review existing access before staging")


def check_batch_conflicts(plans, inventory):
    names, networks = set(), []
    for plan in plans:
        if plan.application_name in names:
            raise ValueError("Selected sites generate the same ZPA application name")
        names.add(plan.application_name)
        check_conflicts(plan, inventory)
        for value in plan.subnets:
            network = ipaddress.ip_network(value)
            for other_site, other in networks:
                if other_site != plan.site_name and network.overlaps(other):
                    raise ValueError(f"Selected ZPA subnet {value} overlaps another selected site: {other_site}")
            networks.append((plan.site_name, network))


def _linked_ids(data, key):
    values = data.get(key)
    if not isinstance(values, list) or any(not isinstance(v, dict) or not _identifier(v.get("id")) for v in values):
        return None
    return {str(v["id"]) for v in values}


def _ports(data, protocol):
    values = data.get(f"{protocol}PortRange")
    try:
        if isinstance(values, list):
            return sorted((int(v["from"]), int(v["to"])) for v in values)
        values = data.get(f"{protocol}PortRanges")
        if isinstance(values, list) and len(values) % 2 == 0:
            return sorted((int(values[i]), int(values[i + 1])) for i in range(0, len(values), 2))
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _create(context, resource, payload, report):
    data = _request(context, "POST", resource, json=payload)
    if not isinstance(data, dict) or not _identifier(data.get("id")):
        raise ValueError(f"ZPA {resource}: creation returned no valid ID; inspect by name before retrying")
    identifier = str(data["id"])
    report["resources"][resource] = {"id": identifier, "name": payload["name"], "verified": False}
    saved = _request(context, "GET", f"{resource}/{identifier}")
    if not isinstance(saved, dict) or str(saved.get("id")) != identifier or saved.get("name") != payload["name"]:
        raise ValueError(f"ZPA {resource}: read-back identity mismatch")
    # A newly created application must stay disabled even if the API ignores the POST setting.
    if resource == "application" and saved.get("enabled") is not False:
        _request(context, "PUT", f"{resource}/{identifier}", json=payload)
        saved = _request(context, "GET", f"{resource}/{identifier}")
        if not isinstance(saved, dict) or saved.get("enabled") is not False:
            raise ValueError("ZPA application disabled state could not be verified; inspect immediately")
    return identifier, saved


def stage_segments(context, plan, connector_group_id, report, *, emit=print):
    """Create three new objects and verify their complete access/linkage contract."""
    report["status"] = "incomplete"
    if not _identifier(connector_group_id):
        raise ValueError("ZPA staging requires the App Connector group ID created by this run")
    # Recheck immediately before writes; plans can become stale during ZTB deployment.
    check_conflicts(plan, load_inventory(context))
    description = f"Staged by ztb-site-automation for {plan.site_name}. Review policy and destinations before enabling."
    group_id, group = _create(context, "segmentGroup", {
        "name": plan.segment_group_name, "description": description, "enabled": True,
    }, report)
    if group.get("enabled") is not True:
        raise ValueError("ZPA segment group enabled state did not match")
    report["resources"]["segmentGroup"]["verified"] = True
    server_id, server = _create(context, "serverGroup", {
        "name": plan.server_group_name, "description": description, "enabled": True,
        "dynamicDiscovery": True, "appConnectorGroups": [{"id": str(connector_group_id)}],
    }, report)
    if server.get("enabled") is not True or server.get("dynamicDiscovery") is not True or _linked_ids(server, "appConnectorGroups") != {str(connector_group_id)}:
        raise ValueError("ZPA server group read-back does not match the new site's connector group")
    report["resources"]["serverGroup"]["verified"] = True
    ranges = [{"from": str(start), "to": str(end)} for start, end in PORT_RANGES]
    app_id, app = _create(context, "application", {
        "name": plan.application_name, "description": description, "enabled": False,
        "domainNames": list(plan.subnets), "segmentGroupId": group_id,
        "serverGroups": [{"id": server_id}], "tcpPortRange": ranges, "udpPortRange": ranges,
        "healthReporting": "ON_ACCESS", "icmpAccessType": "NONE",
        "bypassType": "NEVER", "ipAnchored": False, "doubleEncrypt": False,
        "isCnameEnabled": True,
    }, report)
    if (
        app.get("enabled") is not False
        or str(app.get("id")) != app_id or app.get("name") != plan.application_name
        or str(app.get("segmentGroupId")) != group_id
        or _linked_ids(app, "serverGroups") != {server_id}
        or not isinstance(app.get("domainNames"), list)
        or set(app["domainNames"]) != set(plan.subnets)
        or _ports(app, "tcp") != list(PORT_RANGES) or _ports(app, "udp") != list(PORT_RANGES)
        or app.get("icmpAccessType") != "NONE" or app.get("healthReporting") != "ON_ACCESS"
        or app.get("bypassType") != "NEVER" or app.get("ipAnchored") is not False
    ):
        raise ValueError("ZPA application read-back does not match the disabled LAN staging configuration")
    report["resources"]["application"]["verified"] = True
    report["status"] = "staged_disabled"
    emit(f"ZPA: staged {plan.application_name}: {len(plan.subnets)} subnet(s), disabled, ICMP off; review policy before enabling")
    return True
