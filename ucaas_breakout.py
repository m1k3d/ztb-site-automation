"""Optional site-level local breakout, using the native ZTB groups and PBR APIs.

Contracts checked against the tenant OpenAPI and native portal (2026-09-26).
WAN next-hop overrides are optional in the portal: empty values use the WAN's
configured routing. Never copy a reference site's static next hop to a branch.
"""
from copy import deepcopy
from dataclasses import dataclass
import ipaddress
import time
from urllib.parse import quote

from template_cloning import read_rows
from ucaas_catalog import DEFAULT_SERVICES, SERVICES, canonical_ports, domains, fingerprint, load_catalog


class BreakoutError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


MESSAGES = {
    "ucaas_path_selection": "Choose Best, None or Balanced for UCaaS path selection.",
    "ucaas_catalog": "Refresh UCaaS destinations, then preview again. The catalog is missing, invalid or more than 30 days old.",
    "ucaas_interfaces": "UCaaS local breakout needs two distinct WAN interfaces on a standalone gateway. Check the primary WAN and select a secondary WAN if the template has more than two.",
    "ucaas_inventory": "Cannot verify the SaaS catalog, destination/Port objects or site-network source object. Check tenant read permissions and preview again.",
    "ucaas_changed": "A reviewed object or WAN assignment changed. Preview again before deployment. Inspect any site already created.",
    "ucaas_object_unconfirmed": "Destination or Port object creation or verification is incomplete. Inspect the object names in the report before recovery; creation will not be retried automatically.",
    "ucaas_policy_unconfirmed": "Forwarding policy creation or verification is incomplete. Inspect this site's UCaaS rules in the report before recovery; creation will not be retried automatically.",
    "ucaas_order_unconfirmed": "Could not confirm UCaaS rule priority above existing site and template rules. Inspect forwarding order on this site; do not repeat site creation.",
}


PATH_SELECTIONS = {
    "best": {"label": "Best", "best_link": True, "ecmp_enabled": False},
    "none": {"label": "None", "best_link": False, "ecmp_enabled": False},
    "balanced": {"label": "Balanced", "best_link": False, "ecmp_enabled": True},
}


def selected_path(row):
    value = row.get("ucaas_path_selection") or "best"
    if value not in PATH_SELECTIONS:
        raise BreakoutError("ucaas_path_selection")
    return value


def selected_services(row):
    value = row.get("ucaas_services", DEFAULT_SERVICES)
    selected = sorted(set(part.strip() for part in value.split(",") if part.strip()))
    if not selected or any(key not in SERVICES for key in selected):
        raise ValueError("Select at least one supported UCaaS service")
    return selected


def split_breakout_ports(values):
    """Separate published web ports from media ports without dropping ranges."""
    web, media = [], []
    for value in canonical_ports(values):
        protocol, ranges = value.split(":")
        common = (80, 443) if protocol == "tcp" else (443,)
        for interval in ranges.split(","):
            ends = [int(n) for n in interval.split("-")]
            first, last = ends[0], ends[-1]
            for port in common:
                if first <= port <= last:
                    if first < port:
                        media.append(f"{protocol}:{first}-{port - 1}")
                    web.append(f"{protocol}:{port}")
                    first = port + 1
            if first <= last:
                media.append(f"{protocol}:{first}-{last}")
    return (canonical_ports(web) if web else [], canonical_ports(media) if media else [])


def group_id(row):
    identifier = row.get("group_id")
    if type(identifier) is not int or identifier <= 0:
        raise ValueError("Invalid object ID")
    return identifier


def usable(group):
    return (group.get("hidden") in (None, False, "false", 0) and group.get("owner") == "user"
            and group.get("member_groups") in (None, "", []) and group.get("has_groups") in (None, "", "false", False, 0))


def effective_values(group, kind, apps):
    """Only explicit flat objects are eligible. Never trust a provider-like label."""
    if not usable(group):
        return None
    attributes = group.get("member_attributes")
    if not isinstance(attributes, dict):
        return None
    try:
        if kind == "network" and group.get("type") == "network":
            if any(v for k, v in attributes.items() if k not in ("ip_prefix", "ip_prefix_local")):
                return None
            values = set(attributes.get("ip_prefix") or []) | set(attributes.get("ip_prefix_local") or [])
            return sorted({str(ipaddress.IPv4Network(v, strict=True)) for v in values})
        if kind == "domains" and group.get("type") == "domains":
            if any(v for k, v in attributes.items() if k != "fqdn"):
                return None
            return domains(attributes.get("fqdn") or [])
        if kind == "l4port" and group.get("type") == "l4port":
            if any(v for k, v in attributes.items() if k != "port_group"):
                return None
            return canonical_ports(attributes.get("port_group") or [])
        if kind == "domains" and group.get("type") == "saas_apps":
            if any(v for k, v in attributes.items() if k != "saas_apps"):
                return None
            result = []
            for name in attributes.get("saas_apps") or []:
                matches = [app for app in apps if app.get("app_name", "").casefold() == name.casefold()]
                if len(matches) != 1 or not isinstance(matches[0].get("domains"), list):
                    return None
                result.extend(matches[0]["domains"])
            return domains(result)
    except (ValueError, TypeError, AttributeError):
        return None
    return None


@dataclass(frozen=True)
class BreakoutPlan:
    primary: str
    secondary: str
    source_id: int
    services: list
    objects: list
    catalog_date: str
    rules: list
    path_selection: str = "best"

    def summary(self):
        return dict(primary=self.primary, secondary=self.secondary,
                    path_selection=PATH_SELECTIONS[self.path_selection]["label"], ipv4_only=True,
                    source_id=self.source_id, catalog_date=self.catalog_date,
                    services=deepcopy(self.services), objects=deepcopy(self.objects), rules=deepcopy(self.rules),
                    rule_layout="web_media", policy_name="UCaaS Local Breakout", placement="Above existing site and template forwarding rules")


class LocalBreakout:
    def __init__(self, engine):
        self.engine = engine
        self._inventory = self._apps = self._catalog = None
        self._wan_ports = {}

    def get(self, version, path, params=None):
        base = self.engine.API_V2 if version == 2 else self.engine.API_V3
        return self.engine.get_json(base + path, params=params, headers=self.engine._v3_headers())

    def post(self, version, path, body, error):
        try:
            base = self.engine.API_V2 if version == 2 else self.engine.API_V3
            response = self.engine.post_json(base + path, body, headers=self.engine._v3_headers())
            if response.status_code not in (200, 201, 202, 204):
                raise ValueError("Write not confirmed")
        except Exception:
            raise BreakoutError(error) from None

    def inventory(self):
        result, seen, total = [], set(), None
        for page in range(1000):
            data = self.get(2, "/groups", {"group_type": "network,domains,saas_apps,l4port", "page": page, "size": 100,
                                           "sort": "group_id", "sortdir": "asc"})
            rows, count = data.get("result"), data.get("count")
            if not isinstance(rows, list) or type(count) is not int or count < 0 or (page and total != count):
                raise ValueError("Incomplete group inventory")
            total = count
            for row in rows:
                identifier = group_id(row)
                if identifier in seen:
                    raise ValueError("Repeated group inventory")
                seen.add(identifier)
                result.append(row)
            if len(result) == count:
                return result
            if not rows or len(result) > count:
                break
        raise ValueError("Incomplete group inventory")

    def apps(self):
        value = self.get(2, "/groups/saas-apps")
        if not isinstance(value, dict) or not isinstance(value.get("result"), list):
            raise ValueError("Invalid SaaS catalog")
        return value["result"]

    def wan_ports(self, template_id):
        rows = read_rows(lambda path, params: self.get(3, path, params),
                         "/templates/" + quote(template_id, safe="") + "/interfaces")
        if {row.get("gateway_id") for row in rows} != {"Gateway-1"}:
            raise BreakoutError("ucaas_interfaces")
        ports = [row["name"] for row in rows if row.get("interface_type") == "wan" and not row.get("bonding_parent")]
        if len(set(ports)) != len(ports):
            raise BreakoutError("ucaas_interfaces")
        return sorted(ports)

    @staticmethod
    def source(inventory):
        matches = [g for g in inventory if g.get("name") == "System-All-Networks-Group"
                   and g.get("type") == "network" and g.get("owner") == "system"]
        if len(matches) != 1:
            raise BreakoutError("ucaas_inventory")
        return group_id(matches[0])

    def plan(self, row, template_id):
        if row.get("ucaas_local_breakout") != "1":
            return None
        path_selection = selected_path(row)
        if row.get("gateway_name_b"):
            raise BreakoutError("ucaas_interfaces")
        try:
            if self._catalog is None:
                self._catalog = load_catalog(fresh=True)
        except Exception:
            raise BreakoutError("ucaas_catalog") from None
        try:
            if template_id not in self._wan_ports:
                self._wan_ports[template_id] = self.wan_ports(template_id)
            ports = self._wan_ports[template_id]
            primary = row.get("wan_interface_name", "")
            alternatives = [v for v in ports if v != primary]
            secondary = row.get("ucaas_secondary_wan") or (alternatives[0] if len(alternatives) == 1 else "")
            if primary not in ports or secondary not in alternatives:
                raise BreakoutError("ucaas_interfaces")
            if self._inventory is None:
                self._inventory, self._apps = self.inventory(), self.apps()
            source = self.source(self._inventory)
            services = [deepcopy(service) for service in self._catalog["services"] if service["id"] in selected_services(row)]
            objects, seen = [], set()
            def add_object(kind, values, prefix, service):
                key = fingerprint({"type": kind, "values": values})
                if key not in seen:
                    seen.add(key)
                    matches = [g for g in self._inventory if effective_values(g, kind, self._apps) == values]
                    matches.sort(key=lambda g: (g.get("type") != "saas_apps", group_id(g)))
                    name = f"{prefix}-{key[:8]}"
                    match = matches[0] if matches else None
                    if not match and any(g.get("name") == name for g in self._inventory):
                        raise BreakoutError("ucaas_changed")
                    objects.append(dict(key=key, service=service, type=kind, values=list(values),
                        name=match["name"] if match else name, id=group_id(match) if match else None,
                        action="reuse" if match else "create", object_type=match["type"] if match else kind))
                return key

            web_destinations, media_destinations = set(), set()
            web_ports, media_ports = [], []
            for service in services:
                provider = {"teams": "Teams", "zoom": "Zoom", "webex": "Webex", "meet": "Meet"}[service["id"]]
                for endpoint in service["endpoints"]:
                    web, media = split_breakout_ports(endpoint["ports"])
                    web_ports.extend(web)
                    media_ports.extend(media)
                    for kind, field, label in (("network", "ipv4", "IPv4"), ("domains", "domains", "Domains")):
                        values = endpoint[field]
                        if not values:
                            continue
                        key = add_object(kind, values,
                            f"UCaaS-{provider}-{endpoint['label'].replace(' ', '-')}-{label}", service["id"])
                        # Web breakout intentionally covers all selected UCaaS
                        # objects. Media ports apply only to media destinations.
                        web_destinations.add(key)
                        if media:
                            media_destinations.add(key)
            rules = []
            for category, destinations, values in (("Web", web_destinations, web_ports), ("Media", media_destinations, media_ports)):
                if not destinations or not values:
                    raise BreakoutError("ucaas_catalog")
                ports = canonical_ports(values)
                port_key = add_object("l4port", ports, f"UCaaS-{category}-Ports", "ucaas")
                rules.append(dict(key=category.lower(), service="ucaas", name=f"UCaaS {category} Breakout",
                                  destination_keys=sorted(destinations), ports=ports, port_key=port_key))
            return BreakoutPlan(primary, secondary, source, services, objects, self._catalog["retrieved_at"], rules, path_selection)
        except BreakoutError:
            raise
        except Exception:
            raise BreakoutError("ucaas_inventory") from None

    def policies(self, gateway):
        data = self.get(3, "/pbr/policies", {"gateway_id": gateway})
        if not isinstance(data, dict) or "policies" not in data or not isinstance(data["policies"], (list, type(None))):
            raise ValueError("Invalid policy inventory")
        rows = data["policies"] or []
        ids = [r.get("pbr_policy_id") for r in rows]
        if any(type(v) is not int or v <= 0 for v in ids) or len(ids) != len(set(ids)):
            raise ValueError("Invalid policy IDs")
        if any(r.get("gateway_id") != gateway for r in rows):
            raise ValueError("Wrong policy gateway")
        return rows

    @staticmethod
    def policy_matches(policy, payload):
        for key, value in payload.items():
            if key == "policy_description":
                continue
            actual = policy.get(key)
            # Native read-back omits these optional flags when false. Missing
            # fields must never conceal an expected or returned true value.
            if key in {"src_group_negate", "dst_group_negate", "dst_port_group_negate"} and key not in policy:
                actual = False
            if isinstance(value, list):
                if sorted(actual or []) != value:
                    return False
            elif isinstance(value, str):
                if (actual or "") != value:
                    return False
            elif actual != value:
                return False
        return True

    @staticmethod
    def policy_payload(plan, gateway, site_name, rule, identifiers):
        path = PATH_SELECTIONS[plan.path_selection]
        return dict(name="ucaas-lbo-" + fingerprint({"site": site_name, "gateway": gateway, "rule": rule["key"]})[:16],
            display_name=rule["name"], gateway_id=gateway, template_id="",
            policy_description="Combined selected UCaaS destinations and published " + rule["key"] + " ports; direct WAN egress.",
            src_group_id_set=[plan.source_id],
            dst_group_id_set=sorted({identifiers[key] for key in rule["destination_keys"]}),
            dst_app_segment_id_set=[], dst_port_group_id=identifiers[rule["port_key"]],
            src_group_negate=False, dst_group_negate=False, dst_port_group_negate=False,
            primary_int=plan.primary, secondary_int=plan.secondary, primary_ip="", secondary_ip="",
            nh_interfaces_type="lan_wan", best_link=path["best_link"], ecmp_enabled=path["ecmp_enabled"], wan_edge_ha=False,
            zia_gateway_id=0, match_reverse=False, output_chain=False)

    def ensure_objects(self, plan, report):
        inventory, apps = self.inventory(), self.apps()
        if self.source(inventory) != plan.source_id:
            raise BreakoutError("ucaas_changed")
        # Check every reviewed reuse before creating anything.
        for obj in plan.objects:
            if obj["id"] is not None:
                matches = [g for g in inventory if group_id(g) == obj["id"]]
                if len(matches) != 1 or effective_values(matches[0], obj["type"], apps) != obj["values"]:
                    raise BreakoutError("ucaas_changed")
        identifiers = {}
        for obj in plan.objects:
            record = deepcopy(obj)
            report["objects"].append(record)
            matches = [g for g in inventory if effective_values(g, obj["type"], apps) == obj["values"]
                       and (obj["id"] is None or group_id(g) == obj["id"])]
            matches.sort(key=group_id)
            if matches:
                record.update(id=group_id(matches[0]), name=matches[0]["name"], object_type=matches[0]["type"], status="reused")
            else:
                if any(g.get("name") == obj["name"] for g in inventory):
                    raise BreakoutError("ucaas_changed")
                record["status"] = "unconfirmed"
                attributes = {{"network": "ip_prefix_local", "domains": "fqdn", "l4port": "port_group"}[obj["type"]]: obj["values"]}
                self.post(2, "/groups", dict(name=obj["name"], display_name=obj["name"], type=obj["type"],
                    owner="user", autonomous=False, hidden=False, member_attributes=attributes, member_groups=""),
                    "ucaas_object_unconfirmed")
                for attempt in range(5):
                    inventory = self.inventory()
                    matches = [g for g in inventory if g.get("name") == obj["name"]]
                    if matches:
                        break
                    if attempt < 4:
                        time.sleep(1)
                if len(matches) != 1 or effective_values(matches[0], obj["type"], apps) != obj["values"]:
                    raise BreakoutError("ucaas_object_unconfirmed")
                record.update(id=group_id(matches[0]), status="created")
            identifiers[obj["key"]] = record["id"]
        if not identifiers:
            raise BreakoutError("ucaas_changed")
        return identifiers

    def apply(self, plan, gateway_ids, site_name, report):
        report.update(primary=plan.primary, secondary=plan.secondary,
                      path_selection=PATH_SELECTIONS[plan.path_selection]["label"], ipv4_only=True,
                      rule_layout="web_media",
                      services=[s["name"] for s in plan.services], catalog_date=plan.catalog_date,
                      versions={s["id"]: s["version"] for s in plan.services}, rules=deepcopy(plan.rules),
                      objects=[], policies=[], status="incomplete")
        gateway = gateway_ids.strip()
        if not gateway or "," in gateway:
            raise BreakoutError("ucaas_interfaces")
        report["gateway_id"] = gateway
        phase = "ucaas_changed"
        try:
            interfaces = self.get(3, "/pbr/interfaces", {"gateway_id": gateway})
            if interfaces.get("gateway_id") != gateway:
                raise BreakoutError("ucaas_changed")
            wan = [r["interface_name"] for r in interfaces["interfaces"] if r.get("interface_type") == "wan"]
            if plan.primary == plan.secondary or any(wan.count(v) != 1 for v in (plan.primary, plan.secondary)):
                raise BreakoutError("ucaas_changed")
            before = self.policies(gateway)
            if not plan.rules or any(p.get("name", "").startswith("ucaas-lbo-") or
                                     p.get("display_name", "").startswith("UCaaS ") for p in before):
                raise BreakoutError("ucaas_changed")
            phase = "ucaas_object_unconfirmed"
            identifiers = self.ensure_objects(plan, report)
            # All destinations and Ports must exist before the first rule.
            inventory, apps = self.inventory(), self.apps()
            for obj in plan.objects:
                match = [g for g in inventory if group_id(g) == identifiers[obj["key"]]]
                if len(match) != 1 or effective_values(match[0], obj["type"], apps) != obj["values"]:
                    raise BreakoutError("ucaas_changed")
            payloads, created = {}, []
            for rule in plan.rules:
                payload = self.policy_payload(plan, gateway, site_name, rule, identifiers)
                name = payload["name"]
                phase = "ucaas_policy_unconfirmed"
                record = dict(name=name, display_name=rule["name"], key=rule["key"], status="unconfirmed",
                              destination_ids=payload["dst_group_id_set"], port_id=payload["dst_port_group_id"], ports=rule["ports"])
                report["policies"].append(record)
                self.post(3, "/pbr/policies", payload, phase)
                for attempt in range(5):
                    after = self.policies(gateway)
                    matches = [p for p in after if p.get("name") == name]
                    if matches:
                        break
                    if attempt < 4:
                        time.sleep(1)
                if len(matches) != 1:
                    raise BreakoutError(phase)
                policy = matches[0]
                record["id"] = policy["pbr_policy_id"]
                if not self.policy_matches(policy, payload):
                    raise BreakoutError(phase)
                record["status"] = "created"
                created.append(policy)
                payloads[policy["pbr_policy_id"]] = payload
            # Preserve every existing rule and its relative order. Templates are
            # inherited and are deliberately excluded from the site reorder API.
            phase = "ucaas_order_unconfirmed"
            existing_after = [p for p in after if p["pbr_policy_id"] not in payloads]
            # Native POST inserts at the top and renumbers local rules. Verify
            # their definitions and relative order before the explicit reorder.
            def definitions(rows):
                return [{k: v for k, v in p.items() if k != "sequence_number"}
                        for p in sorted(rows, key=lambda p: p["pbr_policy_id"])]
            if (definitions(before) != definitions(existing_after)
                    or [p["pbr_policy_id"] for p in sorted(before, key=lambda p: p["sequence_number"]) if not p.get("template_id")] !=
                       [p["pbr_policy_id"] for p in sorted(existing_after, key=lambda p: p["sequence_number"]) if not p.get("template_id")]):
                raise BreakoutError(phase)
            local = sorted([p for p in before if not p.get("template_id")], key=lambda p: p["sequence_number"])
            ordered = created + local
            self.post(3, "/pbr/policies-reorder", {"gateway_id": gateway,
                "pbr_order": [{"pbr_policy_id": p["pbr_policy_id"], "sequence_number": i + 1} for i, p in enumerate(ordered)]}, phase)
            for attempt in range(5):
                current = self.policies(gateway)
                local_ids = [p["pbr_policy_id"] for p in sorted([p for p in current if not p.get("template_id")], key=lambda p: p["sequence_number"])]
                rules = self.get(3, "/pbr/rules", {"gateway_id": gateway}).get("pbr_rules")
                own = [i for i, r in enumerate(rules or []) if r.get("pbr_policy_id") in payloads]
                others = [i for i, r in enumerate(rules or []) if r.get("pbr_policy_id") not in payloads]
                unchanged_templates = fingerprint(sorted([p for p in current if p.get("template_id")], key=lambda p: p["pbr_policy_id"])) == fingerprint(sorted([p for p in before if p.get("template_id")], key=lambda p: p["pbr_policy_id"]))
                covered = {r.get("pbr_policy_id") for r in rules or []}
                own_policies = [p for p in current if p["pbr_policy_id"] in payloads]
                existing = [{k: v for k, v in p.items() if k != "sequence_number"} for p in current if p["pbr_policy_id"] not in payloads]
                original = [{k: v for k, v in p.items() if k != "sequence_number"} for p in before]
                definitions_unchanged = fingerprint(sorted(existing, key=lambda p: p["pbr_policy_id"])) == fingerprint(sorted(original, key=lambda p: p["pbr_policy_id"]))
                if (local_ids == [p["pbr_policy_id"] for p in ordered] and unchanged_templates
                        and own and (not others or max(own) < min(others))
                        and {p["pbr_policy_id"] for p in before + created}.issubset(covered)
                        and len(own_policies) == len(payloads)
                        and all(self.policy_matches(p, payloads[p["pbr_policy_id"]]) for p in own_policies)
                        and definitions_unchanged):
                    for record in report["policies"]:
                        record["status"] = "verified"
                    report.update(status="configured", priority="Above site and template rules")
                    return True
                if attempt < 4:
                    time.sleep(1)
            raise BreakoutError(phase)
        except BreakoutError:
            raise
        except Exception:
            raise BreakoutError(phase) from None
