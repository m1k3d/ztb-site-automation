"""Build JSON-compatible site payloads without text/HTML interpolation."""


def build_site_payload(row, location):
    name = row["site_name"]
    payload = {"name": name, "site_name": name, "private_dns": row.get("wan_dns", ""), "gateways": []}
    mode = row.get("dhcp_service_mode", "")
    if mode:
        payload["dhcp_service"] = mode
        if mode == "relay":
            payload["dhcp_server_ip"] = row["dhcp_server_ip"]
    if location["location_type"] == "existing":
        payload["location"] = {"is_existing_location": True, "location_id": location["existing_location_id"]}
    elif location["location_type"] == "new":
        country = row["country"].strip().upper().replace(" ", "_").replace("-", "_")
        # ZIA uses THE_NETHERLANDS for the country commonly entered as Netherlands.
        country = {"NETHERLANDS": "THE_NETHERLANDS"}.get(country, country)
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
            gateway.update(wan_ip_address=ip, wan_subnet_mask=row[f"wan{index}_mask"], default_gw_ip=row[f"wan{index}_gw"])
        payload["gateways"].append(gateway)
    return payload
