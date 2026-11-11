"""Copy a reference site's own forwarding (PBR) rules to new branch sites.

The rules are captured with the reference pull and recreated after the branch's
VLANs exist. Template rules arrive with the template, and the platform's default
rules exist on every new site; neither is copied or edited. Source ports are not
part of the native rule editor and are never read or written.

Rule, object and network shapes were read from the live tenant on 2026-11-11.
"""
from copy import deepcopy
import hashlib
import ipaddress
import json
import re
import string
import time

from input_validation import ValidationIssue
from ucaas_breakout import LocalBreakout

VERSION = 1
DEFAULT_RULES = ('Default-ZPA-PBR', 'Default-App-Segment-PBR', 'DefaultZSRule', 'DefaultZIARule')
UCAAS_PREFIX = 'ucaas-lbo-'
ZIA_TYPES = ('zia_dtls', 'zia_ipsec')
NEXT_HOP_TYPES = ('lan_wan', 'zpa') + ZIA_TYPES
# The native editor stores addresses typed directly into a rule as hidden objects
# named after that rule. Each copied rule gets its own, as in the portal.
PRIVATE_PREFIX = '-custom-internal-'
ATTRIBUTES = {'network': 'ip_prefix_local', 'domains': 'fqdn', 'l4port': 'port_group'}
NEGATES = ('src_group_negate', 'dst_group_negate', 'dst_port_group_negate')
FLAGS = NEGATES + ('best_link', 'ecmp_enabled', 'wan_edge_ha', 'match_reverse', 'output_chain')
TEXT = ('primary_int', 'secondary_int', 'primary_ip', 'secondary_ip')
INTERFACE = re.compile(r'[A-Za-z][A-Za-z0-9_.:-]*')
ALPHABET = string.ascii_letters + string.digits
RETRIES = 5

MESSAGES = {
    'forwarding_reference': 'The copied forwarding rules are incomplete or invalid. Pull the reference site again.',
    'forwarding_inventory': 'Cannot read the objects or rules used by the copied forwarding rules. Check tenant read permissions and preview again.',
    'forwarding_interfaces': 'A copied forwarding rule uses an interface that is not on the selected template gateway. Check the template, then preview again.',
    'forwarding_changed': 'An object or rule used by the copied forwarding rules changed. Preview again before deployment. Inspect any site already created.',
    'forwarding_prerequisite': 'Copied forwarding rules were not created because VLANs, additional WANs or UCaaS breakout did not complete. Inspect those stages before recovery.',
    'forwarding_object_unconfirmed': 'Creating a copied rule’s own object is incomplete. Inspect the objects named in the report before recovery; writes are not retried automatically.',
    'forwarding_policy_unconfirmed': 'Creating or verifying a copied forwarding rule is incomplete. Inspect this site’s forwarding rules in the report before recovery; creation is not retried automatically.',
    'forwarding_order_unconfirmed': 'Could not confirm the copied rules’ order around the default rules. Inspect forwarding order on this site; do not repeat site creation.',
}


class ForwardingIssue(ValidationIssue):
    """Fixed text plus local draft values only; safe to show without redaction."""


class ForwardingError(ValueError):
    """A fixed-code failure, or a mapping problem described from local draft data."""

    def __init__(self, code, detail=None):
        self.code = code
        super().__init__(detail or MESSAGES[code])


def _network(value):
    return ipaddress.IPv4Network(str(value), strict=False)


def _draft_network(vlan):
    subnet, gateway = str(vlan.get('subnet', '')), str(vlan.get('default_gateway', ''))
    return _network(subnet if '/' in subnet else f'{gateway}/{subnet}')


def _port(interface, target):
    ports = [p.strip() for p in str(interface or '').split(',') if p.strip()]
    return ports[min(1 if target == 'b' else 0, len(ports) - 1)] if ports else ''


def _applies(network, target):
    return (network.get('gateway_target') or 'all') in ('all', target)


def _ids(value):
    if value is None:
        return []
    if not isinstance(value, list) or any(type(v) is not int or v <= 0 for v in value):
        raise ValueError('Invalid object IDs')
    return sorted(value)


def path_label(rule):
    if rule.get('ecmp_enabled'):
        return 'Balanced'
    return 'Best' if rule.get('best_link') else 'None'


# ---------------------------------------------------------------- capture

def capture(reference, gateways, policies, lan_networks, networks, groups):
    """Snapshot the reference's own rules, or None when it has none.

    `policies` maps gateway targets ('a', 'b') to /pbr/policies rows. LAN networks
    use the draft VLAN format so they compare directly with a branch's VLANs.
    """
    inventory = {}
    for group in groups:
        identifier = group.get('group_id')
        if type(identifier) is not int or identifier <= 0 or identifier in inventory:
            raise ValueError('Invalid object inventory')
        inventory[identifier] = group
    owners = {str(g.get('gateway_id') or g.get('id') or ''): t for g, t in zip(gateways, ('a', 'b'))}
    wans = []
    for row in networks:
        if row.get('is_deleted') or str(row.get('zone', '')).lower() != 'wan zone':
            continue
        target = owners.get(str(row.get('gateway_id') or ''))
        if target is None:
            continue
        try:
            subnet = str(_network(f"{row.get('ip_range')}/{row.get('subnet')}"))
        except (ValueError, TypeError):
            subnet = ''
        wans.append(dict(gateway_target=target, interface=str(row.get('interface') or ''), network=subnet,
                         next_hop=str(row.get('wan_nexthop_ip') or ''), dhcp=row.get('dhcp_client') is True))
    rules, referenced = [], {}
    for target in ('a', 'b'):
        local = sorted([p for p in policies.get(target) or [] if not p.get('template_id')],
                       key=lambda p: p.get('sequence_number') or 0)
        defaults = [p for p in local if p.get('name') == 'DefaultZIARule']
        for index, policy in enumerate(local):
            name = policy.get('name')
            if not isinstance(name, str) or not name or name in DEFAULT_RULES or name.startswith(UCAAS_PREFIX):
                continue
            after = [p.get('name') for p in local[index + 1:] if p.get('name') in DEFAULT_RULES]
            kind = policy.get('nh_interfaces_type')
            rule = dict(gateway_target=target, name=name, display_name=str(policy.get('display_name') or name),
                        description=str(policy.get('policy_description') or ''), before_default=after[0] if after else '',
                        nh_interfaces_type=kind,
                        src_group_id_set=_ids(policy.get('src_group_id_set')),
                        dst_group_id_set=_ids(policy.get('dst_group_id_set')),
                        dst_app_segment_id_set=sorted(str(v) for v in policy.get('dst_app_segment_id_set') or []),
                        dst_port_group_id=policy.get('dst_port_group_id', -1),
                        zia_gateway_id=policy.get('zia_gateway_id') or 0,
                        zia_follows_default=bool(kind in ZIA_TYPES and len(defaults) == 1
                            and defaults[0].get('nh_interfaces_type') == kind
                            and (defaults[0].get('zia_gateway_id') or 0) == (policy.get('zia_gateway_id') or 0)),
                        **{key: str(policy.get(key) or '') for key in TEXT},
                        **{key: policy.get(key) is True for key in FLAGS})
            if type(rule['dst_port_group_id']) is not int or type(rule['zia_gateway_id']) is not int:
                raise ValueError('Invalid forwarding rule')
            rules.append(rule)
            for identifier in rule['src_group_id_set'] + rule['dst_group_id_set'] + (
                    [rule['dst_port_group_id']] if rule['dst_port_group_id'] > 0 else []):
                referenced.setdefault(identifier, set()).add(name)
    if not rules:
        return None
    objects = []
    for identifier, users in sorted(referenced.items()):
        group = inventory.get(identifier)
        if group is None:
            raise ValueError('A forwarding rule uses an object that could not be read')
        kind, attributes = group.get('type'), group.get('member_attributes')
        values = []
        if kind in ATTRIBUTES and isinstance(attributes, dict):
            values = attributes.get(ATTRIBUTES[kind])
            if values is None and kind == 'network':
                values = attributes.get('ip_prefix')
            values = [str(v) for v in values or []]
        owner = str(group.get('owner') or '')
        private = (group.get('hidden') is True and owner == 'user' and str(group.get('name', '')).startswith(PRIVATE_PREFIX)
                   and len(users) == 1 and next(iter(users)) in group['name'] and kind in ATTRIBUTES
                   and not group.get('member_groups'))
        objects.append(dict(id=identifier, name=str(group.get('name') or ''),
                            display_name=str(group.get('display_name') or group.get('name') or ''),
                            type=str(kind or ''), owner=owner, values=values,
                            rule=next(iter(users)) if private else '', private=private))
    kept = ('name', 'tag', 'interface', 'subnet', 'default_gateway', 'gateway_target')
    lan = [{k: str(n[k]) for k in kept if k in n} for n in lan_networks]
    return dict(version=VERSION, reference=str(reference), networks=lan, wans=wans,
                objects=objects, rules=rules)


def load(row):
    """Validate the stored snapshot without contacting the tenant."""
    raw = str(row.get('forwarding_rules_json') or '').strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get('version') != VERSION or not isinstance(data.get('reference'), str):
            raise ValueError()
        for key in ('networks', 'wans', 'objects', 'rules'):
            if not isinstance(data.get(key), list) or any(not isinstance(v, dict) for v in data[key]):
                raise ValueError()
        if not data['rules'] or len(data['rules']) > 500 or len(data['objects']) > 2000:
            raise ValueError()
        objects = {}
        for obj in data['objects']:
            if (type(obj.get('id')) is not int or obj['id'] <= 0 or obj['id'] in objects
                    or not isinstance(obj.get('name'), str) or not isinstance(obj.get('values'), list)
                    or not all(isinstance(v, str) for v in obj['values']) or not isinstance(obj.get('private'), bool)
                    or not all(isinstance(obj.get(k), str) for k in ('display_name', 'type', 'owner', 'rule'))
                    or (obj['private'] and (obj['type'] not in ATTRIBUTES or not obj['rule'] or obj['rule'] not in obj['name']))):
                raise ValueError()
            objects[obj['id']] = obj
        for network in data['networks']:
            if not all(isinstance(network.get(k), str) for k in ('name', 'tag', 'interface', 'subnet', 'default_gateway')):
                raise ValueError()
            _draft_network(network)
        for wan in data['wans']:
            if wan.get('gateway_target') not in ('a', 'b') or not isinstance(wan.get('interface'), str):
                raise ValueError()
            if wan.get('network'):
                _network(wan['network'])
            if wan.get('next_hop'):
                ipaddress.IPv4Address(wan['next_hop'])
        names = set()
        for rule in data['rules']:
            if (rule.get('gateway_target') not in ('a', 'b') or not isinstance(rule.get('name'), str) or not rule['name']
                    or (rule['gateway_target'], rule['name']) in names or not isinstance(rule.get('display_name'), str)
                    or rule.get('before_default') not in DEFAULT_RULES + ('',)
                    or not isinstance(rule.get('nh_interfaces_type'), str)
                    or type(rule.get('dst_port_group_id')) is not int or type(rule.get('zia_gateway_id')) is not int
                    or not all(isinstance(rule.get(k), bool) for k in FLAGS + ('zia_follows_default',))
                    or not all(isinstance(rule.get(k), str) for k in TEXT + ('description',))
                    or not isinstance(rule.get('dst_app_segment_id_set'), list)):
                raise ValueError()
            names.add((rule['gateway_target'], rule['name']))
            for key in ('primary_int', 'secondary_int'):
                if rule[key] and not INTERFACE.fullmatch(rule[key]):
                    raise ValueError()
            for key in ('primary_ip', 'secondary_ip'):
                if rule[key]:
                    ipaddress.IPv4Address(rule[key])
            used = _ids(rule.get('src_group_id_set')) + _ids(rule.get('dst_group_id_set')) + (
                [rule['dst_port_group_id']] if rule['dst_port_group_id'] > 0 else [])
            if any(identifier not in objects for identifier in used):
                raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ForwardingError('forwarding_reference') from None


# ------------------------------------------------------------ translation

def rule_name(site_name, display_name, reference_name):
    """Native-style name: the first nine display characters plus a stable suffix."""
    digest = hashlib.sha256(json.dumps([site_name.strip().casefold(), reference_name]).encode()).digest()
    return f"{display_name[:9]}-{''.join(ALPHABET[b % len(ALPHABET)] for b in digest[:8])}"


class _Translator:
    def __init__(self, row, vlans, data):
        self.row, self.vlans, self.data = row, vlans, data
        self.networks = [dict(n, _net=_draft_network(n)) for n in data['networks']]
        self.wans = [dict(w, _net=_network(w['network']) if w.get('network') else None) for w in data['wans']]
        self.rule = ''

    def fail(self, detail):
        raise ForwardingError('forwarding_mapping', f'Copied forwarding rule “{self.rule}”: {detail}')

    def match(self, network, target='a'):
        name = network.get('name', '').strip().casefold()
        named = [v for v in self.vlans if str(v.get('name', '')).strip().casefold() == name]
        if len(named) == 1:
            return named[0]
        tagged = [v for v in self.vlans if str(v.get('tag')) == str(network.get('tag'))
                  and _port(v.get('interface'), target) == _port(network.get('interface'), target) and _applies(v, target)]
        if len(tagged) == 1:
            return tagged[0]
        self.fail(f'no network on this branch matches reference network “{network.get("name")}” '
                  f'(VLAN {network.get("tag")}). Keep its name or VLAN ID on this branch.')

    def attached(self, interface, target):
        parent, _, tag = interface.partition('.')
        return [n for n in self.networks if _applies(n, target) and _port(n.get('interface'), target) == parent
                and (not tag or str(n.get('tag')) == tag)]

    def interface(self, name, target):
        if '.' not in name:
            return name
        found = self.attached(name, target)
        if len(found) != 1:
            return name
        vlan = self.match(found[0], target)
        return f"{_port(vlan.get('interface'), target)}.{vlan.get('tag')}"

    def branch_wan(self, target, interface):
        index = 1 if target == 'b' else 0
        if self.row.get('wan1_interface_name' if index else 'wan_interface_name') == interface:
            ip = self.row.get(f'wan{index}_ip', '')
            return dict(static=bool(ip), gateway=self.row.get(f'wan{index}_gw', ''),
                        network=_network(f"{ip}/{self.row.get(f'wan{index}_mask', '')}") if ip else None)
        from additional_wans import selections
        for wan in selections(self.row):
            if wan['gateway_target'] == target and wan['interface'] == interface:
                static = wan['mode'] == 'static'
                return dict(static=static, gateway=wan['gateway'],
                            network=_network(f"{wan['ip']}/{wan['mask']}") if static else None)
        return None

    def offset(self, address, old, new, label):
        index = int(address) - int(old.network_address)
        result = new.network_address + index if index < new.num_addresses else None
        if result is None or (new.num_addresses > 2 and result in (new.network_address, new.broadcast_address)):
            self.fail(f'next hop {address} has no matching address in this branch’s {label} {new}.')
        return str(result)

    def next_hop(self, ip, reference_interface, interface, target):
        if not ip:
            return ''
        address = ipaddress.IPv4Address(ip)
        for wan in self.wans:
            if wan['gateway_target'] != target or wan['interface'] != reference_interface:
                continue
            branch = self.branch_wan(target, interface)
            if branch is None:
                self.fail(f'it uses WAN {interface}, which is not configured on this branch. '
                          'Keep Copy additional WANs on, or choose that WAN.')
            if ip == wan['next_hop']:
                # A DHCP WAN learns its gateway; empty values use the WAN's routing.
                return branch['gateway'] if branch['static'] else ''
            if wan['_net'] and address in wan['_net']:
                if not branch['static']:
                    self.fail(f'next hop {ip} is on WAN {interface}, which uses DHCP on this branch.')
                return self.offset(address, wan['_net'], branch['network'], f'WAN {interface} subnet')
        for network in self.attached(reference_interface, target):
            if address in network['_net']:
                vlan = self.match(network, target)
                if ip == network.get('default_gateway'):
                    return str(vlan.get('default_gateway'))
                return self.offset(address, network['_net'], _draft_network(vlan), f'network “{vlan.get("name")}”')
        if any(address in n['_net'] for n in self.networks) or any(w['_net'] and address in w['_net'] for w in self.wans):
            self.fail(f'next hop {ip} is a reference-site address that is not on interface {reference_interface}. '
                      'Correct the rule on the reference site, then pull it again.')
        return ip

    def prefix(self, value):
        """Translate a prefix inside a reference network; None when it is not site-specific."""
        try:
            prefix = _network(value)
        except ValueError:
            return None
        homes = sorted([n for n in self.networks if prefix.subnet_of(n['_net'])], key=lambda n: -n['_net'].prefixlen)
        if not homes:
            return None
        old, new = homes[0]['_net'], _draft_network(self.match(homes[0]))
        if prefix == old:
            return str(new)
        index = int(prefix.network_address) - int(old.network_address)
        if prefix.prefixlen < new.prefixlen or index + prefix.num_addresses > new.num_addresses:
            self.fail(f'address {value} has no matching range in this branch’s network {new}.')
        return str(ipaddress.IPv4Network((int(new.network_address) + index, prefix.prefixlen)))


def translate(row, vlans):
    """Branch-specific rules and object actions, or None. Makes no tenant calls."""
    data = load(row)
    if data is None:
        return None
    site = row.get('site_name', '')
    if not site:
        raise ForwardingError('forwarding_reference')
    t = _Translator(row, vlans, data)
    objects = {o['id']: o for o in data['objects']}
    targets = ('a', 'b') if row.get('gateway_name_b') else ('a',)
    planned, actions, rules = {}, {}, []
    for ref in data['rules']:
        if ref['gateway_target'] not in targets:
            continue
        t.rule, target = ref['display_name'], ref['gateway_target']
        kind = ref['nh_interfaces_type']
        if kind not in NEXT_HOP_TYPES:
            t.fail(f'next-hop type “{kind}” is not supported for copying.')
        if kind in ZIA_TYPES and not ref['zia_follows_default']:
            t.fail('it uses a different ZIA tunnel than the reference site’s Default ZIA Rule. Review it in ZTB.')
        name = rule_name(site, ref['display_name'], ref['name'])

        def use(identifier):
            obj = objects[identifier]
            if obj['private']:
                values = [t.prefix(v) or v for v in obj['values']] if obj['type'] == 'network' else list(obj['values'])
                new = obj['name'].replace(obj['rule'], name, 1)
                key = 'new:' + new
                action = dict(key=key, action='create', name=new, display_name=new, type=obj['type'], values=values)
            else:
                # Shared objects are reused exactly as they are; editing one would
                # change other sites. List branch networks it lacks, for review only.
                key = f'id:{identifier}'
                missing = []
                if obj['type'] == 'network' and obj['owner'] == 'user':
                    for value in obj['values']:
                        try:
                            mapped = t.prefix(value)
                        except ForwardingError:
                            continue
                        if mapped and mapped not in obj['values'] and mapped not in missing:
                            missing.append(mapped)
                action = dict(key=key, action='reuse', id=identifier, name=obj['name'],
                              display_name=obj['display_name'], type=obj['type'], missing=missing)
            if key in actions and actions[key] != action:
                t.fail('it shares an object with another rule in a way that cannot be copied.')
            actions[key] = action
            return key

        interfaces = {k: t.interface(ref[k], target) for k in ('primary_int', 'secondary_int')}
        payload = dict(name=name, display_name=ref['display_name'], template_id='',
                       src_group_id_set=sorted(use(i) for i in ref['src_group_id_set']),
                       dst_group_id_set=sorted(use(i) for i in ref['dst_group_id_set']),
                       dst_app_segment_id_set=list(ref['dst_app_segment_id_set']),
                       dst_port_group_id=use(ref['dst_port_group_id']) if ref['dst_port_group_id'] > 0 else -1,
                       nh_interfaces_type=kind, zia_gateway_id=ref['zia_gateway_id'],
                       primary_int=interfaces['primary_int'], secondary_int=interfaces['secondary_int'],
                       primary_ip=t.next_hop(ref['primary_ip'], ref['primary_int'], interfaces['primary_int'], target),
                       secondary_ip=t.next_hop(ref['secondary_ip'], ref['secondary_int'], interfaces['secondary_int'], target),
                       **{k: ref[k] for k in FLAGS})
        if ref['description']:
            payload['policy_description'] = ref['description']
        if (target, name) in planned:
            t.fail('two reference rules would receive the same name on this branch.')
        planned[(target, name)] = True
        rules.append(dict(gateway_target=target, reference_name=ref['name'], before_default=ref['before_default'],
                          zia_follows_default=ref['zia_follows_default'], payload=payload))
    if not rules:
        return None
    skipped = sum(r['gateway_target'] not in targets for r in data['rules'])
    return dict(reference=data['reference'], rules=rules, objects=list(actions.values()), skipped=skipped)


def review(row, vlans):
    """Short offline summary for Validate & review."""
    draft = translate(row, vlans)
    if draft is None:
        return None
    return dict(reference=draft['reference'], rules=len(draft['rules']),
                missing=[o['display_name'] for o in draft['objects'] if o.get('missing')])


# ---------------------------------------------------------- tenant stage

def own_values(group):
    attributes = group.get('member_attributes') if isinstance(group.get('member_attributes'), dict) else {}
    kind = group.get('type')
    if kind not in ATTRIBUTES:
        return []
    values = attributes.get(ATTRIBUTES[kind])
    if values is None and kind == 'network':
        values = attributes.get('ip_prefix')
    return [str(v) for v in values or []]


class ForwardingPlan:
    def __init__(self, draft):
        self.reference = draft['reference']
        self.rules = draft['rules']
        self.objects = draft['objects']
        self.skipped = draft['skipped']

    def summary(self):
        names = {o['key']: o.get('display_name') or o['name'] for o in self.objects}

        def describe(rule):
            p = rule['payload']
            if p['nh_interfaces_type'] == 'zpa':
                egress = 'ZPA'
            elif p['nh_interfaces_type'] in ZIA_TYPES:
                egress = 'ZIA tunnel used by this site’s Default ZIA Rule'
            else:
                hops = [f"{p[i]}{' via ' + p[ip] if p[ip] else ''}" for i, ip in
                        (('primary_int', 'primary_ip'), ('secondary_int', 'secondary_ip')) if p[i]]
                egress = ' → '.join(dict.fromkeys(hops)) or 'Configured routing'
            sources = [names.get(k, k) for k in p['src_group_id_set']]
            destinations = [names.get(k, k) for k in p['dst_group_id_set']]
            if p['dst_app_segment_id_set']:
                destinations.append(f"{len(p['dst_app_segment_id_set'])} ZPA application segment(s)")
            return dict(gateway_target=rule['gateway_target'], name=p['name'], display_name=p['display_name'],
                        reference_name=rule['reference_name'], before_default=rule['before_default'],
                        source=('Not ' if p['src_group_negate'] else '') + (', '.join(sources) or 'Any'),
                        destination=('Not ' if p['dst_group_negate'] else '') + (', '.join(destinations) or 'Any'),
                        ports=('Not ' if p['dst_port_group_negate'] else '') + (names.get(p['dst_port_group_id'], 'Any')
                              if p['dst_port_group_id'] != -1 else 'Any'),
                        egress=egress, path=path_label(p), payload=deepcopy(p))
        return dict(reference=self.reference, rules=[describe(r) for r in self.rules],
                    objects=deepcopy(self.objects), skipped=self.skipped,
                    placement='Same order as the reference site, around the platform default rules')


class ForwardingRules:
    def __init__(self, engine):
        self.engine = engine

    def get(self, version, path, params=None):
        base = self.engine.API_V2 if version == 2 else self.engine.API_V3
        return self.engine.get_json(base + path, params=params, headers=self.engine._v3_headers())

    def inventory(self):
        result, seen, total = [], set(), None
        for page in range(1000):
            data = self.get(2, '/groups', {'page': page, 'size': 100, 'sort': 'group_id', 'sortdir': 'asc'})
            rows, count = data.get('result'), data.get('count')
            if not isinstance(rows, list) or type(count) is not int or count < 0 or (page and total != count):
                raise ValueError('Incomplete object inventory')
            total = count
            for row in rows:
                identifier = row.get('group_id')
                if type(identifier) is not int or identifier in seen:
                    raise ValueError('Invalid object inventory')
                seen.add(identifier)
                result.append(row)
            if len(result) == count:
                return result
            if not rows or len(result) > count:
                break
        raise ValueError('Incomplete object inventory')

    def plan(self, row, vlans, template_id):
        draft = translate(row, vlans)
        if draft is None:
            return None
        try:
            inventory = {g['group_id']: g for g in self.inventory()}
            ports = self.engine.get_ha_template_interfaces(template_id)
        except ForwardingError:
            raise
        except Exception:
            raise ForwardingError('forwarding_inventory') from None
        names = {str(g.get('name', '')).casefold() for g in inventory.values()}
        for obj in draft['objects']:
            if obj['action'] == 'create':
                if obj['name'].casefold() in names:
                    raise ForwardingError('forwarding_changed')
                continue
            live = inventory.get(obj['id'])
            if live is None or live.get('type') != obj['type']:
                raise ForwardingError('forwarding_changed')
            obj['name'], obj['display_name'] = live.get('name', obj['name']), live.get('display_name') or live.get('name', obj['name'])
            if obj.get('missing'):
                current = set(own_values(live))
                obj['missing'] = [value for value in obj['missing'] if value not in current]
        for rule in draft['rules']:
            slot = 'Gateway-2' if rule['gateway_target'] == 'b' else 'Gateway-1'
            available = {p.get('name') for p in ports if p.get('gateway_id') == slot}
            for key in ('primary_int', 'secondary_int'):
                value = rule['payload'][key]
                if value and value.split('.')[0] not in available:
                    raise ForwardingError('forwarding_interfaces')
        return ForwardingPlan(draft)

    # ---- deployment

    def post(self, version, path, body, code, report, label):
        report['pending_write'] = dict(method='POST', path=path, name=label)
        try:
            base = self.engine.API_V2 if version == 2 else self.engine.API_V3
            response = self.engine.post_json(base + path, body, headers=self.engine._v3_headers())
            ok = response.status_code in (200, 201, 202, 204)
        except Exception:
            ok = False
        if not ok:
            raise ForwardingError(code)

    def policies(self, gateway):
        return self.engine.local_breakout.policies(gateway)

    def apply(self, plan, gateways, site_id, row, report):
        report.update(plan.summary(), status='incomplete', objects=[], policies=[])
        targets = sorted({r['gateway_target'] for r in plan.rules})
        try:
            if row.get('gateway_name_b'):
                requests_ = [{'gateway_target': 'a'}, {'gateway_target': 'b'}]
                resolved = self.engine.vlan_gateway_targets(site_id, gateways, requests_, row)
                gateway_for = {r['gateway_target']: resolved[id(r)] for r in requests_}
            else:
                gateway = gateways.strip()
                if not gateway or ',' in gateway:
                    raise ForwardingError('forwarding_changed')
                gateway_for = {'a': gateway}
        except ForwardingError:
            raise
        except Exception:
            raise ForwardingError('forwarding_changed') from None
        phase = 'forwarding_changed'
        try:
            # Check every gateway before writing any object or rule.
            networks = self.engine.list_site_vlans_v2(site_id)
            checked = {target: self.check_gateway(plan, target, gateway_for[target], networks) for target in targets}
            phase = 'forwarding_object_unconfirmed'
            identifiers = self.ensure_objects(plan, report)
            for target in targets:
                phase = 'forwarding_policy_unconfirmed'
                self.apply_gateway(plan, target, gateway_for[target], checked[target], identifiers, report)
            report.pop('pending_write', None)
            report['status'] = 'configured'
            return True
        except ForwardingError:
            raise
        except Exception:
            raise ForwardingError(phase) from None

    @staticmethod
    def staged_interfaces(networks, gateway, available):
        """VLAN sub-interfaces of provisioned site networks on this gateway.

        Before activation the gateway lists only its ports; the appliance adds
        `port.tag` interfaces later (seen on activated sites, 2026-11-11).
        """
        result = set()
        for network in networks:
            if network.get('status') != 'provisioned' or network.get('is_deleted'):
                continue
            ports = [p.strip() for p in str(network.get('interface') or '').split(',')]
            owners = [o.strip() for o in str(network.get('gateway_id') or '').split(',')]
            if len(ports) != len(owners):
                ports = ports[:1] * len(owners)
            for port, owner in zip(ports, owners):
                if owner == gateway and port in available:
                    result.add(f"{port}.{network.get('tag')}")
        return result

    def check_gateway(self, plan, target, gateway, networks):
        rules = [r for r in plan.rules if r['gateway_target'] == target]
        interfaces = self.get(3, '/pbr/interfaces', {'gateway_id': gateway})
        if not isinstance(interfaces, dict) or interfaces.get('gateway_id') != gateway:
            raise ForwardingError('forwarding_changed')
        available = {r.get('interface_name') for r in interfaces.get('interfaces') or []}
        available |= self.staged_interfaces(networks, gateway, available)
        for rule in rules:
            for key in ('primary_int', 'secondary_int'):
                if rule['payload'][key] and rule['payload'][key] not in available:
                    raise ForwardingError('forwarding_interfaces')
        before = self.policies(gateway)
        local = [p for p in before if not p.get('template_id')]
        names = [p.get('name') for p in local]
        new = {r['payload']['name'] for r in rules}
        if (any(n not in DEFAULT_RULES and not str(n).startswith(UCAAS_PREFIX) for n in names)
                or any(names.count(n) > 1 for n in DEFAULT_RULES) or new.intersection(p.get('name') for p in before)):
            raise ForwardingError('forwarding_changed')
        zia = None
        if any(r['zia_follows_default'] for r in rules):
            defaults = [p for p in local if p.get('name') == 'DefaultZIARule']
            if len(defaults) != 1 or defaults[0].get('nh_interfaces_type') not in ZIA_TYPES:
                raise ForwardingError('forwarding_changed')
            zia = defaults[0]
        return dict(before=before, zia=zia)

    def ensure_objects(self, plan, report):
        inventory = self.inventory()
        by_id = {g['group_id']: g for g in inventory}
        for obj in plan.objects:
            if obj['action'] != 'create':
                live = by_id.get(obj['id'])
                if live is None or live.get('type') != obj['type']:
                    raise ForwardingError('forwarding_changed')
            elif any(str(g.get('name', '')).casefold() == obj['name'].casefold() for g in inventory):
                raise ForwardingError('forwarding_changed')
        identifiers = {}
        for obj in plan.objects:
            record = dict(name=obj['name'], type=obj['type'], action=obj['action'], status='unconfirmed')
            report['objects'].append(record)
            if obj['action'] == 'create':
                body = dict(name=obj['name'], display_name=obj['name'], type=obj['type'], owner='user', autonomous=False,
                            hidden=True, member_attributes={ATTRIBUTES[obj['type']]: obj['values']}, member_groups='')
                self.post(2, '/groups', body, 'forwarding_object_unconfirmed', report, obj['name'])
                found = self._wait(lambda: [g for g in self.inventory() if g.get('name') == obj['name']])
                if (len(found) != 1 or found[0].get('type') != obj['type'] or found[0].get('hidden') is not True
                        or sorted(own_values(found[0])) != sorted(obj['values'])):
                    raise ForwardingError('forwarding_object_unconfirmed')
                record.update(id=found[0]['group_id'], status='created')
            else:
                record.update(id=obj['id'], status='reused', missing=list(obj.get('missing') or []))
            report.pop('pending_write', None)
            identifiers[obj['key']] = record['id']
        return identifiers

    @staticmethod
    def _wait(read):
        for attempt in range(RETRIES):
            found = read()
            if found:
                return found
            if attempt < RETRIES - 1:
                time.sleep(1)
        return []

    @staticmethod
    def payload(rule, gateway, identifiers, zia):
        p = deepcopy(rule['payload'])
        p['gateway_id'] = gateway
        p['src_group_id_set'] = sorted(identifiers[k] for k in p['src_group_id_set'])
        p['dst_group_id_set'] = sorted(identifiers[k] for k in p['dst_group_id_set'])
        if p['dst_port_group_id'] != -1:
            p['dst_port_group_id'] = identifiers[p['dst_port_group_id']]
        if rule['zia_follows_default']:
            # Follow the new site's own tunnel; reference tunnel IDs are site-specific.
            same = zia.get('nh_interfaces_type') == p['nh_interfaces_type']
            p.update(nh_interfaces_type=zia['nh_interfaces_type'], zia_gateway_id=zia.get('zia_gateway_id') or 0)
            if not same:
                p.update({k: str(zia.get(k) or '') for k in TEXT}, best_link=zia.get('best_link') is True,
                         ecmp_enabled=zia.get('ecmp_enabled') is True)
        return p

    def apply_gateway(self, plan, target, gateway, checked, identifiers, report):
        before, rules = checked['before'], [r for r in plan.rules if r['gateway_target'] == target]
        payloads, created = {}, {}
        for rule in rules:
            payload = self.payload(rule, gateway, identifiers, checked['zia'])
            record = dict(gateway_target=target, gateway_id=gateway, name=payload['name'],
                          display_name=payload['display_name'], status='unconfirmed')
            report['policies'].append(record)
            self.post(3, '/pbr/policies', payload, 'forwarding_policy_unconfirmed', report, payload['name'])
            found = self._wait(lambda: [p for p in self.policies(gateway) if p.get('name') == payload['name']])
            if len(found) != 1 or not LocalBreakout.policy_matches(found[0], payload):
                raise ForwardingError('forwarding_policy_unconfirmed')
            record.update(id=found[0]['pbr_policy_id'], status='created')
            payloads[found[0]['pbr_policy_id']] = payload
            created[rule['payload']['name']] = found[0]
            report.pop('pending_write', None)
        # Native creation inserts at the top. Restore the reference order around
        # the defaults; UCaaS rules created by this run stay above everything.
        current = self.policies(gateway)
        local = sorted([p for p in current if not p.get('template_id')], key=lambda p: p['sequence_number'])
        order = [p for p in local if str(p.get('name')).startswith(UCAAS_PREFIX)]
        defaults = [p for p in local if p.get('name') in DEFAULT_RULES]
        anchored = {d.get('name') for d in defaults}
        for default in defaults:
            order += [created[r['payload']['name']] for r in rules if r['before_default'] == default['name']]
            order.append(default)
        order += [created[r['payload']['name']] for r in rules if r['before_default'] not in anchored]
        if sorted(p['pbr_policy_id'] for p in order) != sorted(p['pbr_policy_id'] for p in local):
            raise ForwardingError('forwarding_order_unconfirmed')
        self.post(3, '/pbr/policies-reorder', {'gateway_id': gateway, 'pbr_order': [
            {'pbr_policy_id': p['pbr_policy_id'], 'sequence_number': i + 1} for i, p in enumerate(order)]},
            'forwarding_order_unconfirmed', report, 'reorder')
        expected = [p['pbr_policy_id'] for p in order]
        for attempt in range(RETRIES):
            if self.verify(gateway, before, payloads, expected):
                for record in report['policies']:
                    if record['gateway_target'] == target:
                        record['status'] = 'verified'
                report.pop('pending_write', None)
                return True
            if attempt < RETRIES - 1:
                time.sleep(1)
        raise ForwardingError('forwarding_order_unconfirmed')

    def verify(self, gateway, before, payloads, expected):
        current = self.policies(gateway)
        local = [p['pbr_policy_id'] for p in sorted([p for p in current if not p.get('template_id')],
                                                    key=lambda p: p['sequence_number'])]
        if local != expected:
            return False
        compiled = self.get(3, '/pbr/rules', {'gateway_id': gateway}).get('pbr_rules') or []
        seen = []
        for rule in compiled:
            identifier = rule.get('pbr_policy_id')
            if identifier in expected and (not seen or seen[-1] != identifier):
                seen.append(identifier)
        if seen != [i for i in expected if i in seen] or not set(payloads).issubset(seen):
            return False

        def definition(policy):
            return json.dumps({k: v for k, v in policy.items() if k != 'sequence_number'}, sort_keys=True)
        by_id = {p['pbr_policy_id']: p for p in current}
        if any(p['pbr_policy_id'] not in by_id or definition(by_id[p['pbr_policy_id']]) != definition(p) for p in before):
            return False
        if len(current) != len(before) + len(payloads):
            return False
        return all(LocalBreakout.policy_matches(by_id[i], payload) for i, payload in payloads.items() if i in by_id)
