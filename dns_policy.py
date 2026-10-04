"""Optional split DNS, independent of site creation: plan, inspect, apply, verify.

V1 accepts only the three automatic DNS policies (plus this module's own rule
when recovering). Existing-site selection and arbitrary DNS editing are V2.
"""
from copy import deepcopy
from dataclasses import dataclass
import ipaddress
import re

MESSAGES = {
    'dns_inventory': 'Cannot verify the DNS system objects or reusable domain objects. Check tenant read permissions.',
    'dns_changed': 'DNS settings or objects differ from the reviewed plan. Inspect the site and preview again; do not repeat site creation.',
    'dns_defaults': 'Expected automatic DNS rules are missing, modified or ambiguous. Inspect the site; DNS changes were stopped.',
    'dns_resolvers': 'Private or WAN DNS server settings could not be verified. Inspect the site DNS settings before recovering this step.',
    'dns_write_unconfirmed': 'A DNS write failed or its outcome is uncertain. Inspect the recorded object and policy IDs before retrying the DNS step; do not repeat site creation.',
    'dns_order_unconfirmed': 'DNS priority could not be verified. Keep Zscaler and ZPA first, private domains next, and all other domains last. Inspect the site before recovery.',
    'dns_prerequisite': 'DNS policy changes were blocked because the site or private DNS setup did not complete. Recover that prerequisite first.',
}
SYSTEM = {
    'source': ('System-Allow-All-Group', 'network'),
    'zte': ('System-ZTE-Domains-Group', 'domains'),
    'zpa_domains': ('System-All-ZPA-Apps-Group', 'domains'),
    'all_domains': ('System-All-Domains-Group', 'domains'),
    'wan': ('System-WAN-DNS-Servers-Group', 'dns_servers'),
    'private': ('System-Private-DNS-Servers-Group', 'dns_servers'),
    'zpa': ('System-ZPA-DNS-Servers-Group', 'dns_servers'),
}
ORDER = ['Zscaler domains → WAN DNS', 'ZPA application domains → ZPA DNS',
         'Private domains → Private DNS', 'All other domains → WAN DNS']
OBJECT_NAME_LIMIT = 255


class DnsError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


def private_domains(value):
    """Input describes DNS zones; include each apex and all its subdomains."""
    zones = set()
    for token in re.split(r'[\s,;]+', str(value).strip()):
        if not token:
            continue
        zone = token.lower().removesuffix('.').removeprefix('*.')
        labels = zone.split('.')
        if (len(zone) > 253 or len(labels) < 2 or not re.search(r'[a-z]', labels[-1])
                or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in labels)):
            raise ValueError('enter private DNS domains such as corp.example.com, without URLs, IP addresses or a catch-all wildcard')
        zones.add(zone)
    if not zones or len(zones) > 100:
        raise ValueError('enter between 1 and 100 private domains')
    return sorted({name for z in zones for name in (z, '*.' + z)})


def resolver_ips(value):
    tokens = re.split(r'[\s,;]+', str(value).strip())
    result = []
    for token in tokens:
        if not token:
            continue
        host = ipaddress.IPv4Interface(token)
        if host.network.prefixlen != 32 or host.ip.is_unspecified or host.ip.is_multicast or str(host.ip) == '255.255.255.255':
            raise ValueError('DNS servers must be individual IPv4 host addresses')
        if str(host.ip) not in result:
            result.append(str(host.ip))
    if not result:
        raise ValueError('explicit DNS server addresses are required for split DNS')
    return result


def readable_domain_name(domains):
    """Name DNS zones once, without counting their wildcard as another zone."""
    zones = sorted({name.removeprefix('*.') for name in domains})
    label = '-'.join(zones) if len(zones) <= 2 else f'{zones[0]}-plus-{len(zones) - 1}-more'
    name = 'DNS-Private-' + label
    if len(name) > OBJECT_NAME_LIMIT:
        name = name[:OBJECT_NAME_LIMIT - len('-truncated')].rstrip('.-') + '-truncated'
    return name


def available_domain_name(domains, inventory, planned_names):
    base = readable_domain_name(domains)
    occupied = {g['name'].casefold() for g in inventory}
    contents = tuple(domains)
    name, number = base, 2
    while (name.casefold() in occupied or
           (name.casefold() in planned_names and planned_names[name.casefold()] != contents)):
        suffix = f'-{number}'
        name = base[:OBJECT_NAME_LIMIT - len(suffix)] + suffix
        number += 1
    planned_names[name.casefold()] = contents
    return name


def object_definition(group):
    return {k: deepcopy(group.get(k)) for k in ('group_id', 'name', 'type', 'owner', 'hidden',
            'autonomous', 'member_attributes', 'member_groups')}


def domain_values(group):
    # Dynamic/system/nested groups must never be mistaken for static user lists.
    if (group.get('type') != 'domains' or group.get('owner') != 'user' or group.get('hidden')
            or group.get('autonomous') or group.get('member_groups') or group.get('has_groups')
            or group.get('membership_info')):
        return None
    attrs = group.get('member_attributes') or {}
    if set(attrs) != {'fqdn'} or not isinstance(attrs['fqdn'], list):
        return None
    return sorted(set(str(v).lower().removesuffix('.') for v in attrs['fqdn']))


@dataclass
class DnsPlan:
    site_name: str
    domains: list
    private_dns: list
    wan_dns: list
    systems: dict
    domain_object: dict

    def summary(self):
        return dict(domains=list(self.domains), private_dns=list(self.private_dns), wan_dns=list(self.wan_dns),
                    policy_name=self.policy_name, domain_object=deepcopy(self.domain_object),
                    order=list(ORDER), default_action='Change the existing all-domains rule to WAN DNS',
                    wan_action='Configure and verify this site’s WAN DNS servers', systems=deepcopy(self.systems))

    @property
    def policy_name(self):
        return 'Private-Domains-to-Private-DNS-' + self.site_name


class DnsPolicy:
    def __init__(self, engine):
        self.engine = engine
        self.planned_names = {}

    def get(self, path, params=None):
        return self.engine.get_json(self.engine.API_V2 + path, params=params, headers=self.engine._v3_headers())

    def write(self, method, path, payload, report, params=None):
        report['pending_write'] = dict(method=method, path=path, payload=deepcopy(payload), params=params)
        try:
            response = (self.engine.post_json(self.engine.API_V2 + path, payload, headers=self.engine._v3_headers())
                        if method == 'POST' else self.engine.put_json(self.engine.API_V2 + path, payload,
                            params=params, headers=self.engine._v3_headers()))
            if response.status_code not in (200, 201, 204):
                raise DnsError('dns_write_unconfirmed')
        except Exception:
            raise DnsError('dns_write_unconfirmed') from None
        # Keep the write in the report until the caller verifies its read-back.

    def inventory(self):
        result, seen = [], set()
        for page in range(100):
            # Names can collide with any object type, even one we cannot reuse.
            data = self.get('/groups', {'group_type': 'all', 'page': page, 'size': 1000})
            count, rows = data['count'], data['result']
            for row in rows:
                identifier = row['group_id']
                if not isinstance(identifier, int) or identifier <= 0 or identifier in seen:
                    raise DnsError('dns_inventory')
                seen.add(identifier)
                result.append(row)
            if len(result) == count:
                return result
            if not rows or len(result) > count:
                break
        raise DnsError('dns_inventory')

    def plan(self, row):
        if row.get('dns_split') != '1':
            return None
        domains = private_domains(row.get('dns_private_domains', ''))
        private, wan = resolver_ips(row.get('private_dns', '')), resolver_ips(row.get('wan_dns', ''))
        try:
            inventory = self.inventory()
            systems = {}
            for key, (name, kind) in SYSTEM.items():
                matches = [g for g in inventory if g.get('name') == name and g.get('type') == kind and g.get('owner') == 'system']
                if len(matches) != 1:
                    raise DnsError('dns_inventory')
                systems[key] = object_definition(matches[0])
            if systems['all_domains']['member_attributes'] != {'fqdn': ['*']}:
                raise DnsError('dns_inventory')
            matches = sorted([g for g in inventory if domain_values(g) == domains], key=lambda g: g['group_id'])
            match = matches[0] if matches else None
            name = match['name'] if match else available_domain_name(domains, inventory, self.planned_names)
            obj = dict(name=name, id=match['group_id'] if match else None,
                       values=domains, action='reuse' if match else 'create')
            return DnsPlan(row['site_name'], domains, private, wan, systems, obj)
        except DnsError:
            raise
        except Exception:
            raise DnsError('dns_inventory') from None

    def check_objects(self, plan):
        inventory = self.inventory()
        for expected in plan.systems.values():
            matches = [g for g in inventory if g['group_id'] == expected['group_id']]
            if len(matches) != 1 or object_definition(matches[0]) != expected:
                raise DnsError('dns_changed')
        obj = plan.domain_object
        if obj['id'] is not None:
            matches = [g for g in inventory if g['group_id'] == obj['id']]
            if len(matches) != 1 or domain_values(matches[0]) != plan.domains:
                raise DnsError('dns_changed')
        return inventory

    def membership(self, site_id, key):
        data = self.get('/group-membership', {'site_id': site_id, 'group_name': SYSTEM[key][0], 'refresh_token': 'enabled'})
        rows = data['result']
        if len(rows) != 1 or rows[0].get('site_id') != site_id:
            raise DnsError('dns_resolvers')
        values = rows[0]['membership_info']['ip_prefix']
        return resolver_ips(','.join(values)) if values else []

    def policies(self, site_id):
        data = self.get('/group-policies', {'site_id': site_id, 'gateway_type': 'isolation', 'type': 'dns'})
        if not isinstance(data.get('result'), list) or data.get('count') != len(data['result']):
            raise DnsError('dns_defaults')
        result = []
        for item in data['result']:
            policy, rules = item['policy'], item['policy_rules']
            if policy.get('type') != 'dns' or policy.get('template_id') or policy.get('is_shadow_policy') or len(rules) != 1:
                raise DnsError('dns_defaults')
            r = rules[0]
            if r.get('site_id') != site_id or r.get('dst_app_segment_id') or r['policy_id'] != policy['policy_id']:
                raise DnsError('dns_defaults')
            payload = dict(name=policy['name'], policy_description=policy.get('policy_description') or '',
                           site_id=site_id, type='dns', sequence_number=r['sequence_number'],
                           src_group_id_set=[r['src_group_id']], dst_group_id_set=[r['dst_group_id']],
                           action=r['action'], action_params=deepcopy(r.get('action_params') or {}),
                           rks=deepcopy(item.get('rks') or dict.fromkeys(('green', 'yellow', 'orange', 'red'), False)),
                           is_shadow_policy=False)
            for field in ('disable_log_throttling', 'disable_logging'):
                payload[field] = bool(policy.get(field))
            for field in ('src_port_group_id', 'dst_port_group_id', 'ts_group_id', 'src_zone_group_id', 'dst_zone_group_id'):
                payload[field] = r.get(field) or 0
            for field in ('src_group_negate', 'dst_group_negate', 'dst_port_group_negate', 'log'):
                payload[field] = bool(r.get(field))
            for side in ('src', 'dst'):
                payload[side + '_zone_group_negate'] = bool(r.get(side + '_zone_group_negate', r.get(side + '_zone_negate')))
            result.append(dict(id=policy['policy_id'], payload=payload))
        if len({p['id'] for p in result}) != len(result) or len({p['payload']['sequence_number'] for p in result}) != len(result):
            raise DnsError('dns_defaults')
        return sorted(result, key=lambda p: p['payload']['sequence_number'])

    @staticmethod
    def definition(record):
        return {k: v for k, v in record['payload'].items() if k != 'sequence_number'}

    def inspect(self, plan, site_id):
        rows = self.policies(site_id)
        ids = {k: v['group_id'] for k, v in plan.systems.items()}
        anchors = [('Default-ZScaler-Domains-DNS-Policy-', 'zte', 'wan'),
                   ('Default-ZPA-DNS-Policy-', 'zpa_domains', 'zpa'),
                   ('Default-DNS-Policy-', 'all_domains', 'private')]
        defaults = []
        for prefix, destination, resolver in anchors:
            matches = [p for p in rows if p['payload']['name'] == prefix + plan.site_name]
            if len(matches) != 1:
                raise DnsError('dns_defaults')
            p = matches[0]['payload']
            permitted = {ids[resolver]}
            if destination == 'all_domains':
                permitted.add(ids['wan'])
            if (p['src_group_id_set'] != [ids['source']] or p['dst_group_id_set'] != [ids[destination]]
                    or p['action'] != 'redirect' or p['action_params'].get('dns_server_id') not in permitted
                    or set(p['action_params']) != {'dns_server_id', 'cache_level'}
                    or any(p[k] for k in ('src_group_negate', 'dst_group_negate', 'dst_port_group_negate',
                        'src_port_group_id', 'dst_port_group_id', 'ts_group_id', 'src_zone_group_id', 'dst_zone_group_id',
                        'src_zone_group_negate', 'dst_zone_group_negate'))):
                raise DnsError('dns_defaults')
            defaults.append(matches[0])
        if [p['id'] for p in rows if p in defaults] != [p['id'] for p in defaults]:
            raise DnsError('dns_order_unconfirmed')
        extras = [p for p in rows if p not in defaults]
        if len(extras) > 1 or (extras and extras[0]['payload']['name'] != plan.policy_name):
            raise DnsError('dns_defaults')
        return defaults, extras

    def verify(self, site_id, expected):
        actual = self.policies(site_id)
        if ([p['id'] for p in actual] != [p['id'] for p in expected]
                or any(self.definition(a) != self.definition(b) for a, b in zip(actual, expected))):
            raise DnsError('dns_order_unconfirmed')
        compiled = self.get('/group-policy-rules', {'site_id': site_id, 'type': 'dns'})['result']
        compiled = sorted(compiled, key=lambda r: r['sequence_number'])
        if ([r['policy_id'] for r in compiled] != [p['id'] for p in expected]
                or len({r['sequence_number'] for r in compiled}) != len(compiled)):
            raise DnsError('dns_order_unconfirmed')
        for r, p in zip(compiled, expected):
            d = p['payload']
            if (r.get('site_id') != site_id or r.get('policy_type') != 'dns'
                    or [r.get('src_group_id')] != d['src_group_id_set'] or [r.get('dst_group_id')] != d['dst_group_id_set']
                    or r.get('action') != d['action'] or r.get('action_params') != d['action_params']):
                raise DnsError('dns_order_unconfirmed')
            for field in ('src_group_negate', 'dst_group_negate', 'dst_port_group_negate', 'log'):
                if bool(r.get(field)) != d[field]:
                    raise DnsError('dns_order_unconfirmed')
            for field in ('src_port_group_id', 'dst_port_group_id', 'ts_group_id', 'src_zone_group_id', 'dst_zone_group_id'):
                if (r.get(field) or 0) != d[field]:
                    raise DnsError('dns_order_unconfirmed')
            for side in ('src', 'dst'):
                if bool(r.get(side + '_zone_group_negate', r.get(side + '_zone_negate'))) != d[side + '_zone_group_negate']:
                    raise DnsError('dns_order_unconfirmed')
        return actual

    def apply(self, plan, site_id, report):
        report.update(plan.summary(), site_id=site_id, status='incomplete', policies=[], phase='inspect')
        phase = 'dns_defaults'
        try:
            if not site_id:
                raise DnsError('dns_prerequisite')
            inventory = self.check_objects(plan)
            defaults, extras = self.inspect(plan, site_id)
            report['before'] = deepcopy(defaults + extras)
            # Private DNS is configured by the deployment prerequisite. Never
            # redirect new domains unless its exact site membership is verified.
            phase = 'dns_resolvers'
            if self.membership(site_id, 'private') != plan.private_dns:
                raise DnsError(phase)
            wan_before = self.membership(site_id, 'wan')
            report['wan_dns_before'] = wan_before
            phase = 'dns_changed'
            obj = deepcopy(plan.domain_object)
            matches = sorted([g for g in inventory if domain_values(g) == plan.domains
                              and (obj['id'] is None or g['group_id'] == obj['id'])], key=lambda g: g['group_id'])
            if not matches and any(g['name'].casefold() == obj['name'].casefold() for g in inventory):
                raise DnsError(phase)
            # A same-name policy is only reusable when its exact desired object
            # and definition can be verified; never rewrite unrelated rules.
            if extras and not matches:
                raise DnsError(phase)
            if not matches:
                report['phase'] = 'domain object'
                self.write('POST', '/groups', dict(name=obj['name'], display_name=obj['name'], type='domains', owner='user',
                    hidden=False, autonomous=False, member_groups='', member_attributes={'fqdn': plan.domains}), report)
                inventory = self.inventory()
                matches = [g for g in inventory if g['name'] == obj['name'] and domain_values(g) == plan.domains]
                if len(matches) != 1:
                    raise DnsError('dns_write_unconfirmed')
                obj['status'] = 'created'
            else:
                obj['status'] = 'reused'
            obj.update(id=matches[0]['group_id'], name=matches[0]['name'])
            report['domain_object'] = obj
            report.pop('pending_write', None)
            custom = deepcopy(defaults[2]['payload'])
            custom.update(name=plan.policy_name,
                          policy_description='Resolve selected private domains using this site’s private DNS servers, after ZPA.',
                          dst_group_id_set=[obj['id']], action_params={'dns_server_id': plan.systems['private']['group_id'], 'cache_level': 'no'})
            if extras and self.definition(extras[0]) != self.definition({'payload': custom}):
                raise DnsError('dns_changed')
            # Recheck after inventory/membership reads, before any policy writes.
            current_defaults, current_extras = self.inspect(plan, site_id)
            if current_defaults != defaults or current_extras != extras:
                raise DnsError('dns_changed')
            if wan_before != plan.wan_dns:
                report['phase'] = 'WAN DNS servers'
                self.write('PUT', '/group-membership', {'member_attributes': {'ip_prefix': [ip + '/32' for ip in plan.wan_dns]}}, report,
                           {'site_id': site_id, 'group_name': SYSTEM['wan'][0], 'refresh_token': 'enabled'})
                if self.membership(site_id, 'wan') != plan.wan_dns:
                    raise DnsError('dns_resolvers')
                report.pop('pending_write', None)
            report['phase'] = 'private-domain policy'
            if not extras:
                self.write('POST', '/group-policies', custom, report)
                after_defaults, extras = self.inspect(plan, site_id)
                if (len(extras) != 1 or self.definition(extras[0]) != self.definition({'payload': custom})
                        or any(self.definition(a) != self.definition(b) for a, b in zip(after_defaults, defaults))):
                    raise DnsError('dns_write_unconfirmed')
                report.pop('pending_write', None)
            report['policies'] = [dict(id=extras[0]['id'], name=plan.policy_name, status='created_or_reused')]
            desired = defaults[:2] + extras + defaults[2:]
            report['phase'] = 'rule order'
            if [p['id'] for p in self.policies(site_id)] != [p['id'] for p in desired]:
                self.write('POST', '/group-policies/resequence', {'policy_sequence': [
                    {'policy_id': p['id'], 'sequence_number': i + 1} for i, p in enumerate(desired)]}, report)
            # Verify the private-domain rule protects the requested names before
            # changing the catch-all resolver to WAN DNS.
            verified = self.verify(site_id, desired)
            report.pop('pending_write', None)
            report['phase'] = 'default resolver'
            updated = deepcopy(verified[-1])
            updated['payload']['action_params']['dns_server_id'] = plan.systems['wan']['group_id']
            if self.definition(updated) != self.definition(verified[-1]):
                self.write('PUT', '/group-policies/' + str(updated['id']), updated['payload'], report)
            desired = verified[:-1] + [updated]
            phase = 'dns_order_unconfirmed'
            final = self.verify(site_id, desired)
            report.pop('pending_write', None)
            self.check_objects(plan)
            final_objects = [g for g in self.inventory() if g['group_id'] == obj['id']]
            if len(final_objects) != 1 or domain_values(final_objects[0]) != plan.domains:
                raise DnsError('dns_changed')
            if (self.membership(site_id, 'private') != plan.private_dns or self.membership(site_id, 'wan') != plan.wan_dns):
                raise DnsError('dns_resolvers')
            report.update(status='configured', phase='verified', policies=[
                dict(id=p['id'], name=p['payload']['name'], position=i + 1, status='verified',
                     resolver_id=p['payload']['action_params']['dns_server_id']) for i, p in enumerate(final)])
            return True
        except DnsError:
            raise
        except Exception:
            raise DnsError(phase) from None
