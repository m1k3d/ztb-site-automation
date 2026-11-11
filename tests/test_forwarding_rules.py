"""Copied reference forwarding rules: capture, branch translation and verified deployment; offline."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

import requests

from automation_config import Settings
from deployment_engine import DeploymentEngine
from forwarding_rules import ForwardingError, capture, load, rule_name, translate
from input_validation import validate_rows
from pull_site import vlans_to_csv_rows
from run_report import save_report
from ui_deployment import plan_digest, public_issues, public_sites

GATEWAYS = [dict(gateway_id='ref-a', gateway_name='Ref-GW', template_gateway_id='Gateway-1')]
NETWORKS = [
    dict(display_name='Guest', zone='Guest-Zone', interface='ge2', tag='69', ip_range='172.16.69.0', subnet='24',
         default_gateway='172.16.69.1', gateway_id='ref-a', status='provisioned'),
    dict(display_name='Users', zone='LAN Zone', interface='ge2', tag='70', ip_range='172.16.70.0', subnet='24',
         default_gateway='172.16.70.1', gateway_id='ref-a', status='provisioned'),
    dict(display_name='wan-ge5', zone='WAN Zone', interface='ge5', tag='1', ip_range='172.16.3.0', subnet='24',
         default_gateway='172.16.3.157', wan_nexthop_ip='172.16.3.1', dhcp_client=True, gateway_id='ref-a'),
    dict(display_name='wan-ge6', zone='WAN Zone', interface='ge6', tag='1', ip_range='172.16.4.0', subnet='24',
         default_gateway='172.16.4.250', wan_nexthop_ip='172.16.4.1', dhcp_client=False, gateway_id='ref-a'),
]


def policy(pid, seq, name, display, kind='lan_wan', **fields):
    base = dict(pbr_policy_id=pid, sequence_number=seq, name=name, display_name=display, gateway_id='ref-a',
                nh_interfaces_type=kind, src_group_id_set=[2], dst_group_id_set=[34], dst_port_group_id=-1,
                primary_int='', secondary_int='', primary_ip='', secondary_ip='', best_link=False,
                ecmp_enabled=False, template_id='', wan_edge_ha=False, zia_gateway_id=0, match_reverse=False,
                output_chain=False)
    return {**base, **fields}


REFERENCE_POLICIES = [
    policy(1021, 1, 'opendns-AJuBUveD', 'opendns', src_group_id_set=[3], dst_group_id_set=[233],
           primary_int='ge6', secondary_int='ge5', primary_ip='172.16.4.1', secondary_ip='172.16.3.1',
           best_link=True, src_port_group_id=110),
    policy(1040, 2, 'ucaas-lbo-1df9ea5465483422', 'UCaaS Web Breakout', primary_int='ge5', secondary_int='ge6'),
    policy(784, 3, 'Guest-Blo-vPz3FZmr', 'Guest-Block', src_group_id_set=[152], dst_group_id_set=[34],
           primary_int='ge2.69', secondary_int='ge2.69', primary_ip='172.16.69.1', secondary_ip='172.16.69.1'),
    policy(759, 4, 'Default-ZPA-PBR', 'Default-ZPA-PBR', 'zpa'),
    policy(757, 5, 'Default-App-Segment-PBR', 'Default-App-Segment-PBR', 'zpa', src_group_id_set=[35], dst_port_group_id=110),
    policy(758, 6, 'DefaultZSRule', 'Default ZScaler Rule', src_group_id_set=[4], dst_group_id_set=[36],
           primary_int='ge5', secondary_int='ge6', primary_ip='172.16.3.1', secondary_ip='172.16.3.1'),
    policy(990, 7, 'o365-sTygwqrW', 'o365', 'zia_dtls', src_group_id_set=[3], dst_group_id_set=[42],
           primary_int='ge5', secondary_int='ge5', best_link=True, zia_gateway_id=208),
    policy(760, 8, 'DefaultZIARule', 'Default ZIA Rule', 'zia_dtls', dst_group_id_set=[3], dst_group_negate=True,
           zia_gateway_id=208),
    policy(1100, 9, 'opendns-Fe6Opp1Q', 'opendns', 'zia_dtls', template_id='template-id', dst_group_id_set=[296]),
]


def group(gid, name, kind='network', owner='system', values=None, hidden=False, **extra):
    attribute = {'network': 'ip_prefix_local', 'domains': 'fqdn', 'l4port': 'port_group', 'saas_apps': 'saas_apps'}[kind]
    return dict(group_id=gid, name=name, display_name=extra.pop('display_name', name), type=kind, owner=owner,
                hidden=hidden, autonomous=False, member_attributes={attribute: list(values or [])},
                member_groups='', parent_groups='', has_groups='', **extra)


def reference_groups():
    return [group(2, 'System-All-Networks-Group', display_name='ZTB Network'),
            group(3, 'System-Private-Subnets-Group', values=['10.0.0.0/8', '172.16.0.0/12'], display_name='Private Subnets'),
            group(34, 'System-All-ZPA-Apps-Group', 'domains', display_name='All ZPA App Segments'),
            group(42, 'o365', 'saas_apps', owner='user', values=['Office365']),
            group(110, 'System-Local-Ports-Forwardable-Over-ZPA', 'l4port', values=['tcp:53']),
            group(152, 'Guest Networks', owner='user', values=['172.16.69.0/24']),
            group(233, '-custom-internal-dst-pbr-opendns-AJuBUveD}', owner='user', values=['208.67.220.220/32'], hidden=True)]


def snapshot():
    lan = vlans_to_csv_rows([n for n in NETWORKS if n['zone'] != 'WAN Zone'], gateways=GATEWAYS)
    return capture('Utrecht-NL-BR', GATEWAYS, {'a': deepcopy(REFERENCE_POLICIES)}, lan, deepcopy(NETWORKS), reference_groups())


def vlan(name, tag, gateway, interface='ge2'):
    return dict(name=name, tag=tag, subnet='24', default_gateway=gateway, interface=interface, zone='LAN Zone',
                enabled='true', share_over_vpn='false', dhcp_service='off')


def branch(**overrides):
    extra = [dict(name='wan-ge6', gateway_target='a', interface='ge6', tag='1', mode='static', ip='10.9.4.250',
                  mask='24', gateway='10.9.4.1', dns='')]
    return {'site_name': 'Branch-2', 'gateway_name': 'Branch-2-GW', 'template_id': 'template-id',
            'wan_interface_name': 'ge5', 'location_type': 'none', 'post': '1',
            'copy_additional_wans': '1', 'additional_wans_json': json.dumps(extra),
            'forwarding_rules_json': json.dumps(snapshot()),
            'vlans': [vlan('Guest', '79', '172.17.69.1'), vlan('Users', '70', '172.17.70.1')], **overrides}


def translated(**overrides):
    validation = validate_rows([branch(**overrides)])
    if validation.issues:
        raise AssertionError(validation.issues)
    site = validation.sites[0]
    return translate(site.row, site.vlans)


class CaptureTests(unittest.TestCase):
    def test_captures_only_the_sites_own_rules_without_source_ports(self):
        data = snapshot()
        self.assertEqual([r['display_name'] for r in data['rules']], ['opendns', 'Guest-Block', 'o365'])
        self.assertEqual([r['before_default'] for r in data['rules']],
                         ['Default-ZPA-PBR', 'Default-ZPA-PBR', 'DefaultZIARule'])
        self.assertTrue(all('src_port_group_id' not in r for r in data['rules']))
        self.assertTrue(data['rules'][2]['zia_follows_default'])
        objects = {o['id']: o for o in data['objects']}
        self.assertEqual(sorted(objects), [3, 34, 42, 152, 233])
        self.assertTrue(objects[233]['private'])
        self.assertFalse(objects[152]['private'])
        self.assertEqual([w['interface'] for w in data['wans']], ['ge5', 'ge6'])
        self.assertEqual(load({'forwarding_rules_json': json.dumps(data)})['reference'], 'Utrecht-NL-BR')

    def test_no_custom_rules_means_nothing_to_copy(self):
        own = [p for p in REFERENCE_POLICIES if p['name'] in ('Default-ZPA-PBR', 'DefaultZIARule') or p['template_id']]
        self.assertIsNone(capture('Ref', GATEWAYS, {'a': own}, [], NETWORKS, reference_groups()))

    def test_missing_object_or_malformed_snapshot_is_rejected(self):
        with self.assertRaises(ValueError):
            capture('Ref', GATEWAYS, {'a': deepcopy(REFERENCE_POLICIES)}, [], NETWORKS, reference_groups()[:-1])
        data = snapshot()
        data['rules'][0]['dst_group_id_set'] = [999]
        with self.assertRaises(ForwardingError) as caught:
            load({'forwarding_rules_json': json.dumps(data)})
        self.assertEqual(caught.exception.code, 'forwarding_reference')

    def test_reference_pull_reads_rules_and_objects(self):
        from site_reference import ReferenceSites
        refs = ReferenceSites.__new__(ReferenceSites)
        calls = []

        def get(path, params, label, version=3):
            calls.append(path)
            if path == '/pbr/policies':
                return {'policies': deepcopy(REFERENCE_POLICIES)}
            if path == '/groups':
                rows = reference_groups()
                return {'count': len(rows), 'result': rows[params['page'] * 100:(params['page'] + 1) * 100]}
            raise AssertionError(path)
        refs._get = get
        lan = vlans_to_csv_rows([n for n in NETWORKS if n['zone'] != 'WAN Zone'], gateways=GATEWAYS)
        data = refs._forwarding('Utrecht-NL-BR', GATEWAYS, lan, NETWORKS)
        self.assertEqual(len(data['rules']), 3)
        calls.clear()
        refs._get = lambda path, params, label, version=3: calls.append(path) or {'policies': [REFERENCE_POLICIES[3]]}
        self.assertIsNone(refs._forwarding('Ref', GATEWAYS, lan, NETWORKS))
        self.assertEqual(calls, ['/pbr/policies'])


class TranslationTests(unittest.TestCase):
    def test_branch_networks_wans_and_objects(self):
        draft = translated()
        rules = {r['payload']['display_name']: r['payload'] for r in draft['rules']}
        name = rule_name('Branch-2', 'opendns', 'opendns-AJuBUveD')
        self.assertTrue(name.startswith('opendns-') and len(name) == 16)
        # Static extra WAN uses its own gateway; DHCP WAN leaves the next hop to routing.
        self.assertEqual((rules['opendns']['primary_ip'], rules['opendns']['secondary_ip']), ('10.9.4.1', ''))
        self.assertEqual(rules['opendns']['dst_group_id_set'], [f'new:-custom-internal-dst-pbr-{name}}}'])
        # VLAN tag changed on the branch: the sub-interface and gateway follow it.
        guest = rules['Guest-Block']
        self.assertEqual((guest['primary_int'], guest['primary_ip']), ('ge2.79', '172.17.69.1'))
        self.assertEqual(guest['src_group_id_set'], ['id:152'])
        objects = {o['key']: o for o in draft['objects']}
        # Shared objects are never edited; the branch network it lacks is only reported.
        self.assertEqual(objects['id:152']['action'], 'reuse')
        self.assertEqual(objects['id:152']['missing'], ['172.17.69.0/24'])
        self.assertEqual(objects['id:3']['action'], 'reuse')
        self.assertEqual(objects[f'new:-custom-internal-dst-pbr-{name}}}']['values'], ['208.67.220.220/32'])
        self.assertTrue(all('src_port_group_id' not in r['payload'] for r in draft['rules']))

    def test_same_addressing_reuses_shared_object_unchanged(self):
        draft = translated(vlans=[vlan('Guest', '69', '172.16.69.1'), vlan('Users', '70', '172.16.70.1')])
        guest = {o['key']: o for o in draft['objects']}['id:152']
        self.assertEqual((guest['action'], guest['missing']), ('reuse', []))

    def test_unmatched_network_or_unconfigured_wan_blocks_with_a_clear_message(self):
        validation = validate_rows([branch(vlans=[vlan('Visitors', '80', '172.17.80.1')])])
        self.assertEqual(validation.issues[0].field, 'forwarding_rules_json')
        self.assertIn('Guest-Block', validation.issues[0].message)
        self.assertIn('“Guest”', validation.issues[0].message)
        validation = validate_rows([branch(copy_additional_wans='0')])
        self.assertIn('WAN ge6', validation.issues[0].message)

    def test_next_hop_outside_the_rule_interface_is_not_guessed(self):
        data = snapshot()
        data['rules'][1]['primary_ip'] = '172.16.70.20'
        validation = validate_rows([branch(forwarding_rules_json=json.dumps(data))])
        self.assertIn('not on interface ge2.69', validation.issues[0].message)

    def test_static_wan_offsets_and_external_next_hops(self):
        data = snapshot()
        data['rules'][0]['primary_ip'] = '172.16.4.5'
        data['rules'][1]['primary_ip'] = '192.0.2.10'
        draft = translated(forwarding_rules_json=json.dumps(data))
        self.assertEqual(draft['rules'][0]['payload']['primary_ip'], '10.9.4.5')
        self.assertEqual(draft['rules'][1]['payload']['primary_ip'], '192.0.2.10')

    def test_zia_rule_with_another_tunnel_is_blocked(self):
        data = snapshot()
        data['rules'][2]['zia_follows_default'] = False
        validation = validate_rows([branch(forwarding_rules_json=json.dumps(data))])
        self.assertIn('different ZIA tunnel', validation.issues[0].message)

    def test_standalone_branch_from_ha_reference_uses_gateway_a_rules(self):
        data = snapshot()
        data['rules'] += [dict(deepcopy(r), gateway_target='b') for r in data['rules']]
        draft = translated(forwarding_rules_json=json.dumps(data))
        self.assertEqual({r['gateway_target'] for r in draft['rules']}, {'a'})
        self.assertEqual(draft['skipped'], 3)


class FakeTenant:
    """Branch site after creation: platform defaults, one template rule and its interfaces."""

    def __init__(self, engine, gateways=('gw-new',)):
        self.engine, self.writes = engine, []
        self.groups = reference_groups()
        self.groups[5]['member_attributes']['ip_prefix_local'] = ['172.16.69.0/24', '172.20.69.0/24']
        self.policies, next_id = [], 2000
        for gateway in gateways:
            for seq, (name, display, kind) in enumerate((('Default-ZPA-PBR', 'Default-ZPA-PBR', 'zpa'),
                    ('Default-App-Segment-PBR', 'Default-App-Segment-PBR', 'zpa'),
                    ('DefaultZSRule', 'Default ZScaler Rule', 'lan_wan'),
                    ('DefaultZIARule', 'Default ZIA Rule', 'zia_ipsec')), 1):
                self.policies.append(policy(next_id, seq, name, display, kind, gateway_id=gateway))
                next_id += 1
            self.policies.append(policy(next_id, 1, 'opendns-T', 'opendns', 'zia_ipsec', gateway_id=gateway, template_id='template-id'))
            next_id += 1
        # Before activation the gateway lists only its ports (live read, 2026-11-11).
        self.interfaces = ['ge2', 'ge3', 'ge4', 'ge5', 'ge6']
        owners = ','.join(gateways)
        self.networks = [dict(interface=','.join(['ge2'] * len(gateways)), tag=tag, gateway_id=owners, status='provisioned')
                         for tag in ('79', '70')]
        self.fail_post = None

    def get(self, url, params=None, **kwargs):
        path = url.removeprefix(self.engine.API_V2).removeprefix(self.engine.API_V3)
        if url.startswith(self.engine.API_V2) and path == '/groups':
            page, size = params['page'], params['size']
            return deepcopy(dict(count=len(self.groups), result=self.groups[page * size:(page + 1) * size]))
        if path == '/pbr/interfaces':
            return dict(gateway_id=params['gateway_id'], interfaces=[dict(interface_name=n) for n in self.interfaces])
        if path == '/pbr/policies':
            return deepcopy(dict(policies=[p for p in self.policies if p['gateway_id'] == params['gateway_id']]))
        if path == '/pbr/rules':
            rows = [p for p in self.policies if p['gateway_id'] == params['gateway_id']]
            local = sorted([p for p in rows if not p['template_id']], key=lambda p: p['sequence_number'])
            return dict(pbr_rules=[dict(pbr_policy_id=p['pbr_policy_id']) for p in local + [p for p in rows if p['template_id']]])
        raise AssertionError((url, params))

    def post(self, url, payload, **kwargs):
        self.writes.append(('POST', url.rsplit('/api/', 1)[1], deepcopy(payload)))
        if self.fail_post and self.fail_post(url, payload):
            return Mock(status_code=500)
        if url == self.engine.API_V2 + '/groups':
            self.groups.append({**deepcopy(payload), 'group_id': max(g['group_id'] for g in self.groups) + 1})
        elif url == self.engine.API_V3 + '/pbr/policies':
            local = [p for p in self.policies if p['gateway_id'] == payload['gateway_id'] and not p['template_id']]
            for p in local:
                p['sequence_number'] += 1
            stored = {k: v for k, v in deepcopy(payload).items() if not (k.endswith('negate') and v is False)}
            self.policies.append({**stored, 'pbr_policy_id': max(p['pbr_policy_id'] for p in self.policies) + 1,
                                  'sequence_number': 1})
        elif url == self.engine.API_V3 + '/pbr/policies-reorder':
            for order in payload['pbr_order']:
                next(p for p in self.policies if p['pbr_policy_id'] == order['pbr_policy_id'])['sequence_number'] = order['sequence_number']
        else:
            raise AssertionError(url)
        return Mock(status_code=201)

    def put(self, url, payload, **kwargs):
        self.writes.append(('PUT', url.rsplit('/api/', 1)[1], deepcopy(payload)))
        identifier = int(url.rsplit('/', 1)[1])
        target = next(g for g in self.groups if g['group_id'] == identifier)
        target.update(deepcopy(payload))
        return Mock(status_code=200)

    def local_order(self, gateway='gw-new'):
        return [p['name'] for p in sorted([p for p in self.policies if p['gateway_id'] == gateway and not p['template_id']],
                                          key=lambda p: p['sequence_number'])]


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        no_http = patch.object(requests.Session, 'request', side_effect=AssertionError('Unexpected HTTP'))
        self.http = no_http.start()
        self.addCleanup(no_http.stop)
        sleeper = patch('forwarding_rules.time.sleep')
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.engine = DeploymentEngine(Settings(ztb_api_base='https://example.invalid', bearer='offline'), emit=lambda *_: None)
        self.addCleanup(self.engine.client.close)
        self.tenant = FakeTenant(self.engine)
        self.ports = [dict(name=n, gateway_id='Gateway-1', interface_type=t) for n, t in
                      (('ge2', 'lan'), ('ge5', 'wan'), ('ge6', 'wan'))]
        engine = self.engine
        engine.get_json = Mock(side_effect=self.tenant.get)
        engine.post_json = Mock(side_effect=self.tenant.post)
        engine.put_json = Mock(side_effect=self.tenant.put)
        engine.get_template_settings = Mock(return_value={'deployment_type': 'standalone', 'dhcp_service': 'server'})
        engine.get_ha_template_interfaces = Mock(side_effect=lambda _: deepcopy(self.ports))
        engine.additional_wans.plan = Mock(side_effect=lambda row, _: __import__('additional_wans').selections(row))
        engine.additional_wans.apply = Mock(return_value=True)
        engine.list_site_inventory = Mock(return_value=[])
        engine.site_exists = Mock(return_value=False)
        engine.create_site = Mock(return_value=(True, 'OK', 1))
        engine.resolve_gateway_ids_and_cluster = Mock(return_value=('gw-new', 1))
        engine.resolve_site_id = Mock(return_value='site-new')
        engine.process_vlans_for_site = Mock(return_value=True)
        engine.list_site_vlans_v2 = Mock(side_effect=lambda _: deepcopy(self.tenant.networks))

    def tearDown(self):
        self.http.assert_not_called()

    def plan(self, **overrides):
        plan = self.engine.plan(validate_rows([branch(**overrides)]))
        self.assertFalse(plan.issues, plan.issues)
        return plan

    def test_preview_is_read_only_and_reports_final_values(self):
        plan = self.plan()
        result = self.engine.execute(plan, dry_run=True)
        data = public_sites(result)[0]['forwarding']
        self.assertEqual(data['status'], 'planned')
        self.assertEqual([r['display_name'] for r in data['rules']], ['opendns', 'Guest-Block', 'o365'])
        guest = data['rules'][1]
        self.assertEqual((guest['source'], guest['destination'], guest['egress']),
                         ('Guest Networks', 'All ZPA App Segments', 'ge2.79 via 172.17.69.1'))
        self.assertEqual(data['rules'][0]['egress'], 'ge6 via 10.9.4.1 → ge5')
        guest = {o['key']: o for o in data['objects']}['id:152']
        self.assertEqual((guest['action'], guest['missing']), ('reuse', ['172.17.69.0/24']))
        self.assertEqual(self.tenant.writes, [])
        # Approval is bound to the reviewed object contents.
        self.tenant.groups[5]['member_attributes']['ip_prefix_local'].append('172.17.69.0/24')
        self.assertNotEqual(plan_digest(plan), plan_digest(self.plan()))

    def test_deploys_after_vlans_in_reference_order_and_verifies(self):
        plan = self.plan()
        progress = []
        result = self.engine.execute(plan, progress=lambda *args: progress.append(args[1:]))
        site = result.sites[0]
        self.assertEqual(site.status, 'success', (site.errors, site.diagnostics))
        stages = [stage for stage, state in progress if state == 'running']
        self.assertLess(stages.index('VLANs'), stages.index('Forwarding rules'))
        self.assertEqual(site.forwarding['status'], 'configured')
        self.assertEqual([p['status'] for p in site.forwarding['policies']], ['verified'] * 3)
        names = {p['display_name']: p['name'] for p in site.forwarding['policies']}
        self.assertEqual(self.tenant.local_order(), [names['opendns'], names['Guest-Block'], 'Default-ZPA-PBR',
                         'Default-App-Segment-PBR', 'DefaultZSRule', names['o365'], 'DefaultZIARule'])
        # Shared objects are never edited, so other sites' rules are unaffected.
        self.engine.put_json.assert_not_called()
        self.assertEqual(self.tenant.groups[5]['member_attributes']['ip_prefix_local'], ['172.16.69.0/24', '172.20.69.0/24'])
        guest = {o['name']: o for o in site.forwarding['objects']}['Guest Networks']
        self.assertEqual((guest['status'], guest['missing']), ('reused', ['172.17.69.0/24']))
        # Rule-specific hidden object is created for the new rule only.
        created = [w[2] for w in self.tenant.writes if w[1] == 'v2/groups']
        self.assertEqual([g['name'] for g in created], [f"-custom-internal-dst-pbr-{names['opendns']}}}"])
        self.assertTrue(created[0]['hidden'])
        rules = {p['display_name']: p for p in self.tenant.policies if p['gateway_id'] == 'gw-new' and p['name'] in names.values()}
        new_object = next(g for g in self.tenant.groups if g['name'] == created[0]['name'])['group_id']
        self.assertEqual(rules['opendns']['dst_group_id_set'], [new_object])
        self.assertEqual(rules['Guest-Block']['src_group_id_set'], [152])
        self.assertTrue(all('src_port_group_id' not in p for p in rules.values()))
        # ZIA follows the new site's own tunnel, not the reference's DTLS gateway.
        self.assertEqual((rules['o365']['nh_interfaces_type'], rules['o365']['zia_gateway_id'],
                          rules['o365']['primary_int'], rules['o365']['best_link']), ('zia_ipsec', 0, '', False))

    def test_unexpected_existing_rule_stops_before_any_write(self):
        plan = self.plan()
        self.tenant.policies.append(policy(5000, 9, 'manual-rule', 'Manual', gateway_id='gw-new'))
        result = self.engine.execute(plan)
        self.assertEqual(result.sites[0].diagnostics['Forwarding rules'], 'forwarding_changed')
        self.assertEqual(self.tenant.writes, [])

    def test_vlan_failure_blocks_forwarding(self):
        plan = self.plan()
        self.engine.process_vlans_for_site.return_value = False
        result = self.engine.execute(plan)
        self.assertEqual(result.sites[0].diagnostics['Forwarding rules'], 'forwarding_prerequisite')
        self.assertEqual(self.tenant.writes, [])

    def test_uncertain_rule_write_is_reported_and_not_retried(self):
        plan = self.plan()
        self.tenant.fail_post = lambda url, payload: url.endswith('/pbr/policies') and payload['display_name'] == 'Guest-Block'
        result = self.engine.execute(plan)
        site = result.sites[0]
        self.assertEqual(site.status, 'partial')
        self.assertEqual(site.diagnostics['Forwarding rules'], 'forwarding_policy_unconfirmed')
        self.assertEqual(site.forwarding['pending_write']['method'], 'POST')
        attempts = [w for w in self.tenant.writes if w[1] == 'v3/pbr/policies' and w[2]['display_name'] == 'Guest-Block']
        self.assertEqual(len(attempts), 1)
        self.assertEqual([p['status'] for p in site.forwarding['policies']], ['created', 'unconfirmed'])
        self.engine.put_json.assert_not_called()

    def test_staged_vlan_sub_interface_requires_a_provisioned_network_on_that_gateway(self):
        plan = self.plan()
        self.tenant.networks[0]['status'] = 'pending'
        result = self.engine.execute(plan)
        self.assertEqual(result.sites[0].diagnostics['Forwarding rules'], 'forwarding_interfaces')
        self.assertEqual(self.tenant.writes, [])

    def test_preview_detects_object_and_interface_changes(self):
        self.tenant.groups = [g for g in self.tenant.groups if g['group_id'] != 152]
        plan = self.engine.plan(validate_rows([branch()]))
        self.assertEqual(plan.issues[0].field, 'forwarding_changed')
        self.tenant.groups = reference_groups()
        self.ports = [p for p in self.ports if p['name'] != 'ge2']
        plan = self.engine.plan(validate_rows([branch()]))
        self.assertEqual(plan.issues[0].field, 'forwarding_interfaces')
        self.assertTrue(public_issues(plan.issues)[0]['message'].startswith('A copied forwarding rule'))

    def test_network_already_in_shared_object_is_not_reported_missing(self):
        self.tenant.groups[5]['member_attributes']['ip_prefix_local'].append('172.17.69.0/24')
        result = self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].status, 'success')
        self.assertEqual({o['name']: o for o in result.sites[0].forwarding['objects']}['Guest Networks']['missing'], [])
        self.engine.put_json.assert_not_called()

    def test_ha_branch_creates_rules_on_each_peer_and_shared_objects_once(self):
        data = snapshot()
        data['rules'] += [dict(deepcopy(r), gateway_target='b') for r in data['rules']]
        data['wans'] += [dict(deepcopy(w), gateway_target='b') for w in data['wans']]
        self.tenant = FakeTenant(self.engine, gateways=('gw-a', 'gw-b'))
        self.engine.get_json.side_effect = self.tenant.get
        self.engine.post_json.side_effect = self.tenant.post
        self.engine.put_json.side_effect = self.tenant.put
        self.ports += [dict(p, gateway_id='Gateway-2') for p in self.ports] + [
            dict(name='ge4', gateway_id=slot, interface_type='ha') for slot in ('Gateway-1', 'Gateway-2')]
        self.engine.get_template_settings.return_value['deployment_type'] = 'standard_mode_ha'
        self.engine.resolve_gateway_ids_and_cluster.return_value = ('gw-b,gw-a', 1)
        self.engine.find_site_row_by_name = Mock(return_value={'id': 'site-new', 'gateways': [
            dict(gateway_id='gw-b', gateway_name='Branch-2-GW-B'), dict(gateway_id='gw-a', gateway_name='Branch-2-GW')]})
        self.engine.configure_vrrp = Mock(return_value=True)
        extra = [dict(name='wan-ge6', gateway_target=t, interface='ge6', tag='1', mode='static', ip=f'10.9.4.{n}',
                      mask='24', gateway='10.9.4.1', dns='') for t, n in (('a', 250), ('b', 251))]
        plan = self.plan(gateway_name_b='Branch-2-GW-B', wan1_interface_name='ge5', additional_wans_json=json.dumps(extra),
                         forwarding_rules_json=json.dumps(data), vrrp_link_interface='ge4')
        result = self.engine.execute(plan)
        site = result.sites[0]
        self.assertEqual(site.status, 'success', (site.errors, site.diagnostics))
        self.assertEqual([(p['gateway_target'], p['gateway_id']) for p in site.forwarding['policies']],
                         [('a', 'gw-a')] * 3 + [('b', 'gw-b')] * 3)
        self.assertEqual(len([w for w in self.tenant.writes if w[1] == 'v2/groups']), 1)
        self.engine.put_json.assert_not_called()
        self.assertEqual(self.tenant.local_order('gw-a')[:2], self.tenant.local_order('gw-b')[:2])

    def test_report_keeps_rule_ids_and_object_changes(self):
        import tempfile
        from pathlib import Path
        result = self.engine.execute(self.plan())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'run.json'
            save_report(path, result)
            report = json.loads(path.read_text())['sites'][0]['forwarding']
            text = path.with_suffix('.txt').read_text()
        self.assertEqual(report['status'], 'configured')
        self.assertTrue(all(p['id'] for p in report['policies']))
        self.assertIn("Shared object Guest Networks unchanged; it does not include this site's 172.17.69.0/24", text)


if __name__ == '__main__':
    unittest.main()
