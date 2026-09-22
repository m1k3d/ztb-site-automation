"""Offline validation and normalization shared by the CLI and future UI."""

import csv
from dataclasses import dataclass, field
import ipaddress
import json
from pathlib import Path
import re

from location_config import normalize_location_type


@dataclass(frozen=True)
class ValidationIssue:
    source: str
    row: int
    field: str
    message: str

    def __str__(self):
        return f"{self.source}: row {self.row}, {self.field}: {self.message}"


@dataclass
class ValidatedSite:
    source: str
    row_number: int
    row: dict
    vlans: list = field(default_factory=list)


@dataclass
class ValidationResult:
    sites: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    @property
    def valid(self):
        return not self.issues


def read_csv(path, issues):
    rows = []
    try:
        with Path(path).open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream, strict=True)
            headers = reader.fieldnames
            if not headers:
                raise ValueError("missing CSV header")
            headers = [h.strip() for h in headers]
            if not all(headers) or len(set(headers)) != len(headers):
                raise ValueError("CSV headers must be nonempty and unique")
            reader.fieldnames = headers
            for row in reader:
                number = reader.line_num
                if None in row:
                    issues.append(ValidationIssue(str(path), number, "CSV", "too many values; quote fields containing commas"))
                    continue
                if any(v is None for v in row.values()):
                    issues.append(ValidationIssue(str(path), number, "CSV", "too few values for the header"))
                    continue
                rows.append((number, row))
    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        issues.append(ValidationIssue(str(path), 1, "file", str(exc)))
    return rows


TRUE_VALUES = {"1", "true", "yes", "y"}
FALSE_VALUES = {"0", "false", "no", "n"}
INTERFACE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]*$")


def boolean(value, default=False):
    value = str(value).strip().lower()
    if not value:
        return default
    if value in TRUE_VALUES:
        return True
    if value in FALSE_VALUES:
        return False
    raise ValueError("expected true/false or 1/0")


def ipv4(value):
    try:
        return ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        raise ValueError("expected an IPv4 address") from None


class RowValidator:
    def __init__(self, source, number, issues):
        self.source, self.number, self.issues = str(source), number, issues

    def error(self, name, message):
        self.issues.append(ValidationIssue(self.source, self.number, name, message))

    def check(self, name, action):
        try:
            return action()
        except (ValueError, TypeError) as exc:
            self.error(name, str(exc))
            return None

    def required(self, row, name):
        if not row.get(name):
            self.error(name, "required")

    def interfaces(self, name, value, *, multiple=False):
        parts = value.split(",") if multiple else [value]
        if not all(INTERFACE.fullmatch(p.strip()) for p in parts):
            self.error(name, "expected an interface name" + (" or comma-separated interface names" if multiple else ""))


def validate_vlan_rows(rows, source, issues):
    output, tags, names = [], {}, set()
    for number, raw in rows:
        check = RowValidator(source, number, issues)
        if not isinstance(raw, dict):
            check.error("VLAN", "expected an object")
            continue
        v = {str(k): str(value if value is not None else "").strip() for k, value in raw.items()}
        # Accept exported JSON and the existing CSV schema.
        v["name"] = v.get("display_name") or v.get("name", "")
        v["default_gateway"] = v.get("default_gateway") or v.get("start_ip", "")
        v["zone"] = v.get("zone") or "LAN Zone"
        for key in ("name", "tag", "subnet", "default_gateway", "interface"):
            check.required(v, key)
        name = v.get("name", "")
        short_name = name[:16].lower()
        if short_name in names:
            check.error("name", "duplicate VLAN name within this site after the API's 16-character truncation")
        names.add(short_name)
        tag = check.check("tag", lambda: int(v.get("tag", "")))
        if tag is not None:
            if not 1 <= tag <= 4094:
                check.error("tag", "must be between 1 and 4094")
            v["tag"] = str(tag)
        interface = v.get("interface", "")
        check.interfaces("interface", interface, multiple=True)
        for port in set(p.strip().lower() for p in interface.split(",")):
            key = (port, tag)
            if key in tags:
                check.error("tag", f"duplicate tag on {port} (first used at row {tags[key]})")
            tags[key] = number
        gateway = check.check("default_gateway", lambda: ipv4(v.get("default_gateway", "")))
        subnet = v.get("subnet", "")
        network = None
        if gateway is not None:
            network = check.check("subnet", lambda: ipaddress.IPv4Network(subnet if "/" in subnet else f"{gateway}/{subnet}", strict=False))
            if network is not None:
                if gateway not in network:
                    check.error("default_gateway", "must belong to the VLAN subnet")
                elif network.num_addresses > 2 and gateway in (network.network_address, network.broadcast_address):
                    check.error("default_gateway", "must be a usable host address")
                v["subnet"] = str(network.prefixlen)
                if "lo0" in {port.strip().lower() for port in interface.split(",")} and network.prefixlen != 32:
                    check.error("subnet", "loopback management interface lo0 requires /32")
        start, end = v.get("dhcp_start", ""), v.get("dhcp_end", "")
        if not start and not end:
            dr = raw.get("dhcp_range") or raw.get("range_list")
            if isinstance(dr, str) and "-" in dr:
                start, end = [s.strip() for s in dr.split("-", 1)]
            elif isinstance(dr, list) and len(dr) == 1 and isinstance(dr[0], list) and len(dr[0]) == 2:
                start, end = dr[0]
            elif dr:
                check.error("dhcp_range", "expected one start-end range")
        if bool(start) != bool(end):
            check.error("dhcp_start/dhcp_end", "both endpoints are required")
        if start and end:
            first = check.check("dhcp_start", lambda: ipv4(start))
            last = check.check("dhcp_end", lambda: ipv4(end))
            if first is not None and last is not None:
                if first > last:
                    check.error("dhcp_start/dhcp_end", "start must not be greater than end")
                if network is not None:
                    if first not in network or last not in network:
                        check.error("dhcp_start/dhcp_end", "range must be inside the VLAN subnet")
                    elif network.num_addresses > 2 and (first == network.network_address or last == network.broadcast_address):
                        check.error("dhcp_start/dhcp_end", "range must contain only usable host addresses")
                if gateway is not None and first <= gateway <= last:
                    check.error("dhcp_start/dhcp_end", "range must not include the default gateway")
        service = v.get("dhcp_service", "").lower().replace("-", "_")
        service = {"on": "inherit", "off": "no_dhcp", "": "inherit" if start and end else "no_dhcp"}.get(service, service)
        if service not in ("inherit", "no_dhcp", "non_airgapped"):
            check.error("dhcp_service", "expected inherit/on, no_dhcp/off, or non_airgapped")
        enabled_default = "status" not in raw or str(raw["status"]).lower() == "provisioned"
        enabled = check.check("enabled", lambda: boolean(v.get("enabled", ""), enabled_default))
        share = check.check("share_over_vpn", lambda: boolean(v.get("share_over_vpn", "")))
        output.append({
            "name": name, "display_name": name, "tag": v.get("tag", ""),
            "subnet": v.get("subnet", ""), "start_ip": v["default_gateway"],
            "default_gateway": v["default_gateway"], "interface": interface, "zone": v["zone"],
            "enabled": enabled, "share_over_vpn": share, "dhcp_service": service,
            **({"dhcp_range": f"{start}-{end}"} if start and end else {}),
        })
    return output


def load_vlan_rows(path, issues):
    if path.suffix.lower() == ".csv":
        return read_csv(path, issues)
    try:
        with path.open(encoding="utf-8-sig") as stream:
            data = json.load(stream)
        if isinstance(data, dict):
            if "rows" in data:
                data = data["rows"]
            elif isinstance(data.get("result"), dict) and "rows" in data["result"]:
                data = data["result"]["rows"]
            else:
                data = data.get("vlans")
        if not isinstance(data, list):
            raise ValueError("expected a VLAN list, rows, result.rows, or vlans")
        return list(enumerate(data, 1))
    except (OSError, ValueError, UnicodeError) as exc:
        issues.append(ValidationIssue(str(path), 1, "file", str(exc)))
        return []


def validate_rows(rows, *, base_dir=".", source="sites", numbered=False):
    """Validate CSV-like rows; UI callers may supply a `vlans` list directly."""
    result = ValidationResult()
    site_names, gateway_names = {}, {}
    for number, raw in (rows if numbered else enumerate(rows, 2)):
        check = RowValidator(source, number, result.issues)
        if not isinstance(raw, dict):
            check.error("site", "expected an object")
            continue
        if "post" not in raw:
            check.error("post", "column required; use 1 to select a site or 0 to skip")
            continue
        post = str(raw.get("post") or "").strip()
        if post not in ("", "0", "1"):
            check.error("post", "expected 1 to select a site or 0 to skip")
            continue
        if post != "1":
            continue
        row = {str(k): str(v if v is not None else "").strip() for k, v in raw.items() if k != "vlans"}
        for name in ("site_name", "gateway_name", "wan_interface_name"):
            check.required(row, name)
        if not (row.get("template_name") or row.get("template_id")):
            check.error("template_name/template_id", "one is required")
        name = row.get("site_name", "").lower()
        if name in site_names:
            check.error("site_name", f"duplicate selected site (first used at row {site_names[name]})")
        site_names[name] = number
        for field_name in ("gateway_name", "gateway_name_b"):
            gateway_name = row.get(field_name, "").lower()
            if gateway_name:
                if gateway_name in gateway_names:
                    check.error(field_name, f"duplicate gateway name (first used at row {gateway_names[gateway_name]})")
                gateway_names[gateway_name] = number
        for index, iface in ((0, "wan_interface_name"), (1, "wan1_interface_name")):
            fields = [f"wan{index}_{part}" for part in ("ip", "mask", "gw")]
            values = [row.get(key, "") for key in fields]
            if index == 1 and not row.get("gateway_name_b"):
                if any(values) or row.get(iface):
                    check.error("gateway_name_b", "required when WAN1 fields are set")
                continue
            check.required(row, iface)
            check.interfaces(iface, row.get(iface, ""))
            if any(values):
                for key in fields:
                    check.required(row, key)
                address = check.check(fields[0], lambda: ipv4(values[0]))
                gateway = check.check(fields[2], lambda: ipv4(values[2]))
                if address is not None:
                    network = check.check(fields[1], lambda: ipaddress.IPv4Network(f"{address}/{values[1]}", strict=False))
                    if network is not None:
                        row[fields[1]] = str(network.netmask)
                        if gateway is not None and gateway not in network:
                            check.error(fields[2], "must belong to the WAN subnet")
                        if network.num_addresses > 2:
                            for key, value in ((fields[0], address), (fields[2], gateway)):
                                if value in (network.network_address, network.broadcast_address):
                                    check.error(key, "must be a usable host address")
                        if gateway == address:
                            check.error(fields[2], "must differ from the WAN address")
        for key in ("wan_dns", "private_dns"):
            if row.get(key):
                for value in row[key].split(","):
                    value = value.strip()
                    check.check(key, lambda: ipaddress.IPv4Network(value, strict=False) if key == "private_dns" and "/" in value else ipv4(value))
        if row.get("dhcp_server_ip"):
            check.check("dhcp_server_ip", lambda: ipv4(row["dhcp_server_ip"]))
        service = row.get("dhcp_service_mode", "").lower()
        if service not in ("", "relay", "server", "inherit"):
            check.error("dhcp_service_mode", "expected relay, server, inherit, or blank")
        if not service and row.get("dhcp_server_ip"):
            service = "relay"
        if service == "relay" and not row.get("dhcp_server_ip"):
            check.error("dhcp_server_ip", "required for relay mode")
        row["dhcp_service_mode"] = service
        mode = check.check("location_type", lambda: normalize_location_type(row.get("location_type")))
        row["location_type"] = mode or "auto"
        if mode == "new" or (mode == "auto" and not row.get("zia_location_name")):
            check.required(row, "country")
        if mode == "existing":
            check.required(row, "zia_location_name")
        if row.get("location_template_id") and mode in ("new", "auto"):
            template_id = check.check("location_template_id", lambda: int(row["location_template_id"]))
            if template_id is not None and template_id <= 0:
                check.error("location_template_id", "must be positive")
        appc = check.check("appc_provision", lambda: boolean(row.get("appc_provision", "")))
        row["appc_provision"] = "1" if appc else "0"
        for key in ("vrrp_link_interface", "vrrp_track_extra"):
            if row.get(key):
                check.interfaces(key, row[key], multiple=key.endswith("extra"))
        if row.get("vrrp_vrid"):
            vrid = check.check("vrrp_vrid", lambda: int(row["vrrp_vrid"]))
            if vrid is not None and not 1 <= vrid <= 255:
                check.error("vrrp_vrid", "must be between 1 and 255")
        vlans = []
        if "vlans" in raw:
            if row.get("vlans_file"):
                check.error("vlans", "use either inline vlans or vlans_file, not both")
            if not isinstance(raw["vlans"], list):
                check.error("vlans", "expected a list of VLAN objects")
            else:
                vlans = validate_vlan_rows(enumerate(raw["vlans"], 1), f"{source} row {number} VLANs", result.issues)
        elif row.get("vlans_file"):
            path = Path(row["vlans_file"]).expanduser()
            if not path.is_absolute():
                path = Path(base_dir) / path
            path = path.resolve()
            row["vlans_file"] = str(path)
            vlans = validate_vlan_rows(load_vlan_rows(path, result.issues), path, result.issues)
        result.sites.append(ValidatedSite(str(source), number, row, vlans))
    return result


def validate_csv(path):
    path = Path(path).resolve()
    issues = []
    rows = read_csv(path, issues)
    result = validate_rows(rows, base_dir=path.parent, source=str(path), numbered=True)
    result.issues[:0] = issues
    return result
