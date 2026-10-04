"""Build JSON-compatible site payloads without text/HTML interpolation."""

from country_catalog import resolve_country
import ipaddress


TEMPLATE_MESSAGES = {
    'template_settings': 'Cannot verify the template deployment and DHCP settings. Check template read access and preview again.',
    'template_gateway_count': 'Gateway setup must match the template: one gateway for standalone, two for standard or enhanced HA.',
    'template_dhcp_mode': 'DHCP mode is inherited from the template during site creation. Select a matching template or update your dedicated template before deploying.',
    'template_dhcp_relay': 'This template uses DHCP relay. Enter the DHCP server IP address before deploying, even when the selected VLANs have DHCP disabled.',
}


class TemplateSettingsError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(TEMPLATE_MESSAGES[code])


def check_template_settings(row, settings):
    deployment, dhcp = settings.get('deployment_type'), settings.get('dhcp_service')
    if deployment not in ('standalone', 'standard_mode_ha', 'wan_edge_mode_ha') or dhcp not in ('server', 'relay'):
        raise TemplateSettingsError('template_settings')
    if bool(row.get('gateway_name_b')) != (deployment != 'standalone'):
        raise TemplateSettingsError('template_gateway_count')
    requested = row.get('dhcp_service_mode')
    if requested in ('server', 'relay') and requested != dhcp:
        raise TemplateSettingsError('template_dhcp_mode')
    if dhcp == 'relay' and not row.get('dhcp_server_ip'):
        raise TemplateSettingsError('template_dhcp_relay')


def build_site_payload(row, location):
    name = row["site_name"]
    payload = {"name": name, "site_name": name, "private_dns": row.get("wan_dns", ""), "gateways": []}
    # deploy_site inherits the template DHCP service; it does not accept a
    # service override. Preflight checks that an explicit choice agrees.
    if row.get("dhcp_server_ip"):
        payload["dhcp_server_ip"] = row["dhcp_server_ip"]
    if location["location_type"] == "existing":
        payload["location"] = {"is_existing_location": True, "location_id": location["existing_location_id"]}
    elif location["location_type"] == "new":
        country = resolve_country(row["country"])["value"]
        payload["location"] = {
            "is_existing_location": False,
            "details": {
                "country": country,
                "name": row.get("zia_location_name") or name,
            },
            "location_template_id": location["location_template_id"],
        }
    for index, name_field, interface_field in ((0, "gateway_name", "wan_interface_name"), (1, "gateway_name_b", "wan1_interface_name")):
        if index == 1 and not row.get(name_field):
            continue
        ip = row.get(f"wan{index}_ip", "")
        gateway = {"gateway_name": row[name_field], "is_wan_dhcp": not bool(ip), "wan_interface": row[interface_field]}
        if ip:
            # Native deploy_site uses a prefix string, even though validation
            # and CSV exports normalize masks to dotted decimal notation.
            prefix = str(ipaddress.IPv4Network(f"0.0.0.0/{row[f'wan{index}_mask']}").prefixlen)
            gateway.update(wan_ip_address=ip, wan_subnet_mask=prefix, default_gw_ip=row[f"wan{index}_gw"])
        payload["gateways"].append(gateway)
    return payload
