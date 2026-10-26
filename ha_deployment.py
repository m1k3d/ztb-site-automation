"""Preflight HA interface assignments before any tenant resource is created."""
import json

MESSAGES = {
    'ha_inventory': 'Cannot verify both HA template gateways. Check template interface read access and preview again.',
    'ha_gateway_order': 'This draft uses a different reference gateway order. Pull the HA reference again and create a new branch so Gateway A/B match template Gateway-1/2.',
    'ha_wan_a': 'Gateway A primary WAN must be a WAN interface on template Gateway-1.',
    'ha_wan_b': 'Gateway B primary WAN must be a WAN interface on template Gateway-2.',
    'ha_link': 'Each template gateway needs one HA link. Any selected HA link override must match that interface on both gateways.',
    'ha_transit': 'Enhanced HA requires one HA-data interface on each template gateway.',
    'ha_network': 'Check each VLAN interface against its template gateway. Shared LAN interfaces are ordered A, B; management networks must target A or B explicitly.',
    'ha_tracking': 'HA needs a selected WAN or LAN interface that can be tracked on both gateways. Review template interface roles and VRRP tracking choices.',
}


class HaError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


def check_interfaces(row, vlans, settings, ports):
    slots = ('Gateway-1', 'Gateway-2')
    try:
        if json.loads(row.get('ha_template_slots_json', '["Gateway-1","Gateway-2"]')) != list(slots):
            raise HaError('ha_gateway_order')
    except (ValueError, TypeError) as exc:
        if isinstance(exc, HaError):raise
        raise HaError('ha_gateway_order') from None
    if not isinstance(ports, list) or not ports or any(not isinstance(p, dict) for p in ports):
        raise HaError('ha_inventory')
    usable = [p for p in ports if not p.get('bonding_parent')]
    if {p.get('gateway_id') for p in usable} != set(slots):
        raise HaError('ha_inventory')

    def matches(slot, name, roles):
        return len([p for p in usable if p.get('gateway_id') == slot and p.get('name') == name
                    and p.get('interface_type') in roles]) == 1

    for slot, field, code in zip(slots, ('wan_interface_name', 'wan1_interface_name'), ('ha_wan_a', 'ha_wan_b')):
        if not matches(slot, row.get(field), {'wan'}):raise HaError(code)
    for slot in slots:
        links = [p.get('name') for p in usable if p.get('gateway_id') == slot and p.get('interface_type') == 'ha']
        if len(links) != 1 or not links[0] or (row.get('vrrp_link_interface') and row['vrrp_link_interface'] != links[0]):
            raise HaError('ha_link')
        if settings['deployment_type'] == 'wan_edge_mode_ha' and len([
                p for p in usable if p.get('gateway_id') == slot and p.get('interface_type') == 'ha-data']) != 1:
            raise HaError('ha_transit')
    requested = {row['wan_interface_name'], row['wan1_interface_name']}
    for net in vlans:
        names = [p.strip() for p in net.get('interface', '').split(',')]
        target = net.get('gateway_target') or 'all'
        management = net.get('zone', '').lower() == 'management zone' or 'lo0' in names
        if management and target not in ('a', 'b'):raise HaError('ha_network')
        owners = slots if target == 'all' else (slots[0 if target == 'a' else 1],)
        if len(names) == 1 and len(owners) == 2:names *= 2
        if len(names) != len(owners):raise HaError('ha_network')
        for slot, name in zip(owners, names):
            # lo0 may appear only after activation; execution reports an unverified binding.
            if management and name == 'lo0':continue
            if not matches(slot, name, {'management', 'lan'} if management else {'lan'}):raise HaError('ha_network')
            if not management:requested.add(name)
    requested.update(p.strip() for p in row.get('vrrp_track_extra', '').split(',') if p.strip())
    if not any(all(matches(slot, name, {'lan', 'wan'}) for slot in slots) for name in requested):
        raise HaError('ha_tracking')
    # Bind approval to the checked interface roles; re-planning detects template edits.
    return sorted([{k: str(p.get(k) or '') for k in ('gateway_id', 'name', 'interface_type', 'bonding_parent')}
                   for p in ports], key=lambda p: tuple(p.values()))
