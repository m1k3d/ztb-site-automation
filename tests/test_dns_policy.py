"""Split DNS ordering, preserved ZPA behavior, reuse, and uncertain-write recovery."""
from copy import deepcopy
import io
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import requests
from automation_config import Settings
from deployment_engine import DeploymentEngine
from dns_policy import DnsError, SYSTEM, private_domains, resolver_ips, readable_domain_name
from input_validation import validate_rows
from run_report import save_report
from ui_deployment import plan_digest, public_sites


def row(**overrides):
    return dict(site_name='NewBranch', gateway_name='NewBranch-GW', template_id='template-id',
                wan_interface_name='ge5', location_type='none', post='1', vlans=[], dns_split='1',
                dns_private_domains='mikedsecure.corp', wan_dns='1.1.1.1,8.8.8.8', private_dns='172.30.150.253,172.30.150.252', **overrides)


class DnsTests(unittest.TestCase):
    def setUp(self):
        block = patch.object(requests.Session, 'request', side_effect=AssertionError('Unexpected network'))
        self.http = block.start(); self.addCleanup(block.stop)
        self.engine = DeploymentEngine(Settings(ztb_api_base='https://example.invalid', bearer='offline'), emit=lambda *_: None)
        self.engine.get_template_settings = Mock(return_value={"deployment_type":"standalone", "dhcp_service":"server"})
        self.addCleanup(self.engine.client.close)
        self.module = self.engine.dns_policy
        self.ids = dict(source=4, zte=30, wan=31, private=32, zpa=33, zpa_domains=34, all_domains=108)
        self.groups = [dict(group_id=self.ids[key], name=name, display_name=name, type=kind, owner='system',
                            hidden=False, autonomous=False, member_groups='',
                            member_attributes={'fqdn': ['*']} if key == 'all_domains' else
                            {'fqdn': ['*.zscaler.net']} if key == 'zte' else
                            {'fqdn': []} if kind == 'domains' else {'ip_prefix': []})
                       for key, (name, kind) in SYSTEM.items()]
        self.memberships = {'private': ['172.30.150.253', '172.30.150.252'], 'wan': []}
        self.records = []
        for i, (prefix, dst, dns, cache) in enumerate([
                ('Default-ZScaler-Domains-DNS-Policy-', 'zte', 'wan', 'high'),
                ('Default-ZPA-DNS-Policy-', 'zpa_domains', 'zpa', 'low'),
                ('Default-DNS-Policy-', 'all_domains', 'private', 'no')]):
            self.records.append(self.record(10+i, dict(name=prefix+'NewBranch', policy_description=prefix+'NewBranch',
                src_group_id_set=[4], dst_group_id_set=[self.ids[dst]], site_id='new-site', type='dns',
                action='redirect', action_params={'dns_server_id': self.ids[dns], 'cache_level': cache}, sequence_number=i+1)))
        self.events, self.fail_after, self.wrong_compiled = [], None, False
        self.engine.get_json = Mock(side_effect=self.get)
        self.engine.post_json = Mock(side_effect=lambda url, payload, **kw: self.write('POST', url, payload, kw.get('params')))
        self.engine.put_json = Mock(side_effect=lambda url, payload, **kw: self.write('PUT', url, payload, kw.get('params')))
        self.engine.list_site_inventory = Mock(return_value=[])
        self.engine.site_exists = Mock(return_value=False)
        self.engine.create_site = Mock(side_effect=lambda *_: self.events.append('site') or (True, 'ok', 1))
        self.engine.resolve_gateway_ids_and_cluster = Mock(return_value=('new-gateway', 1))
        self.engine.resolve_site_id = Mock(return_value='new-site')
        self.engine.configure_private_dns = Mock(side_effect=lambda *_: self.events.append('private DNS') or True)

    def tearDown(self):
        self.http.assert_not_called()

    @staticmethod
    def record(identifier, payload):
        policy = {k: deepcopy(payload.get(k)) for k in ('name', 'policy_description', 'type', 'disable_logging', 'disable_log_throttling')}
        policy.update(policy_id=identifier, template_id=None, is_shadow_policy=False, rks=None)
        rule = dict(rule_id=identifier+100, policy_id=identifier, site_id=payload['site_id'],
                    src_group_id=payload['src_group_id_set'][0], dst_group_id=payload['dst_group_id_set'][0],
                    sequence_number=payload['sequence_number'], action=payload['action'], action_params=deepcopy(payload['action_params']))
        for k in ('src_port_group_id', 'dst_port_group_id', 'ts_group_id', 'src_zone_group_id', 'dst_zone_group_id'):
            rule[k] = payload.get(k, 0)
        for k in ('src_group_negate', 'dst_group_negate', 'dst_port_group_negate', 'log'):
            rule[k] = payload.get(k, False)
        for side in ('src', 'dst'):
            rule[side+'_zone_negate'] = payload.get(side+'_zone_group_negate', False)
        return dict(policy=policy, policy_rules=[rule], rks=payload.get('rks') or dict.fromkeys(('green', 'yellow', 'orange', 'red'), False))

    def ordered(self):
        return sorted(self.records, key=lambda p: p['policy_rules'][0]['sequence_number'])

    def get(self, url, params=None, **_):
        path = url.removeprefix(self.engine.API_V2)
        if path == '/groups':
            page, size = params['page'], params['size']
            return deepcopy(dict(result=self.groups[page*size:(page+1)*size], count=len(self.groups)))
        if path == '/group-policies':
            self.assertEqual(params, {'site_id':'new-site', 'gateway_type':'isolation', 'type':'dns'})
            return dict(result=deepcopy(self.ordered()), count=len(self.records))
        if path == '/group-policy-rules':
            self.assertEqual(params, {'site_id':'new-site', 'type':'dns'})
            self.events.append('compiled verification')
            rows = [dict(deepcopy(p['policy_rules'][0]), policy_type='dns') for p in self.ordered()]
            if self.wrong_compiled:
                rows[0]['sequence_number'], rows[-1]['sequence_number'] = rows[-1]['sequence_number'], rows[0]['sequence_number']
            return {'result': rows}
        if path == '/group-membership':
            key = next(k for k in ('private', 'wan') if SYSTEM[k][0] == params['group_name'])
            return dict(result=[dict(site_id='new-site', membership_info={'ip_prefix':[ip+'/32' for ip in self.memberships[key]]})])
        raise AssertionError((path, params))

    def write(self, method, url, payload, params):
        path = url.removeprefix(self.engine.API_V2)
        self.events.append((method, path, deepcopy(payload)))
        if path == '/groups':
            self.groups.append(dict(deepcopy(payload), group_id=200))
        elif path == '/group-membership':
            self.assertEqual(params['site_id'], 'new-site')
            self.assertEqual(params['group_name'], SYSTEM['wan'][0])
            self.memberships['wan'] = [str(v).removesuffix('/32') for v in payload['member_attributes']['ip_prefix']]
        elif path == '/group-policies':
            seq = payload['sequence_number']
            for p in self.records:
                if p['policy_rules'][0]['sequence_number'] >= seq:
                    p['policy_rules'][0]['sequence_number'] += 1
            self.records.append(self.record(13, payload))
        elif path == '/group-policies/resequence':
            for entry in payload['policy_sequence']:
                next(p for p in self.records if p['policy']['policy_id'] == entry['policy_id'])['policy_rules'][0]['sequence_number'] = entry['sequence_number']
        elif path == '/group-policies/12':
            self.assertIn('compiled verification', self.events)
            self.assertEqual([p['policy']['policy_id'] for p in self.ordered()], [10, 11, 13, 12])
            self.records[self.records.index(next(p for p in self.records if p['policy']['policy_id'] == 12))] = self.record(12, payload)
        else:
            raise AssertionError((method, path))
        if self.fail_after == path:
            raise requests.Timeout('secret raw response')
        return Mock(status_code=201 if method == 'POST' else 200)

    def plan(self, **overrides):
        fields = row(); fields.update(overrides)
        plan = self.engine.plan(validate_rows([fields]))
        self.assertFalse(plan.issues, plan.issues)
        return plan

    def apply(self, plan=None):
        report = {}
        self.module.apply((plan or self.plan()).sites[0].dns_plan, 'new-site', report)
        return report

    def test_disabled_performs_no_dns_lookups_or_policy_writes(self):
        plan = self.plan(dns_split='0', dns_private_domains='invalid')
        self.assertIsNone(plan.sites[0].dns_plan)
        self.engine.execute(plan)
        self.engine.get_json.assert_not_called()
        self.engine.post_json.assert_not_called()
        self.engine.put_json.assert_not_called()

    def test_normalizes_zones_and_covers_apex(self):
        self.assertEqual(private_domains('*.Mikedsecure.corp.\nmikedsecure.corp,other.example'),
                         ['*.mikedsecure.corp','*.other.example','mikedsecure.corp','other.example'])

    def test_rejects_missing_broad_malformed_and_ip_domains(self):
        for domain in ('', '*', '*.com', 'https://example.com', '10.0.0.1', 'foo.*.corp', '-foo.corp', 'foo..corp', 'a'*64+'.corp'):
            with self.subTest(domain=domain), self.assertRaises(ValueError): private_domains(domain)

    def test_resolvers_are_required_hosts(self):
        for value in ('', '0.0.0.0', '10.0.0.0/24', '2001:db8::1', 'dns.example', '224.0.0.1'):
            with self.subTest(value=value), self.assertRaises(ValueError): resolver_ips(value)
        self.assertEqual(resolver_ips('10.0.0.1/32,10.0.0.1'), ['10.0.0.1'])

    def test_validation_and_ha_gate(self):
        for overrides in ({'dns_split':'yes'}, {'dns_private_domains':''}, {'wan_dns':''}, {'private_dns':''}, {'gateway_name_b':'peer'}):
            fields=row();fields.update(overrides)
            with self.subTest(overrides=overrides): self.assertTrue(validate_rows([fields]).issues)

    def test_preview_no_writes_includes_order_and_object_action(self):
        result=self.engine.execute(self.plan(), dry_run=True)
        dns=public_sites(result)[0]['dns']
        self.assertEqual(dns['domain_object']['action'], 'create')
        self.assertEqual(dns['domain_object']['name'], 'DNS-Private-mikedsecure.corp')
        self.assertIn('ZPA', dns['order'][1])
        self.assertEqual(dns['order'][2], 'Private domains → Private DNS')
        self.engine.post_json.assert_not_called();self.engine.put_json.assert_not_called()

    def test_deployment_stage_after_site_and_private_dns(self):
        before=deepcopy(self.records[:2])
        result=self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].status,'success')
        self.assertTrue(result.sites[0].stages['DNS policy'])
        self.assertEqual(self.events[:2],['site','private DNS'])
        self.assertEqual(self.records[:2],before)
        self.assertEqual([p['policy']['policy_id'] for p in self.ordered()],[10,11,13,12])
        self.assertEqual(self.records[2]['policy_rules'][0]['action_params']['dns_server_id'],31)
        self.assertEqual(result.sites[0].dns['status'],'configured')

    def test_resolver_membership_verified_before_catchall_change(self):
        report=self.apply()
        self.assertEqual(self.memberships['wan'],['1.1.1.1','8.8.8.8'])
        self.assertEqual(report['wan_dns_before'],[])
        self.assertNotIn('pending_write',report)

    def test_private_dns_mismatch_blocks_all_writes(self):
        self.memberships['private']=['10.0.0.1']
        with self.assertRaisesRegex(DnsError,'Private or WAN'):self.apply()
        self.engine.post_json.assert_not_called();self.engine.put_json.assert_not_called()

    def test_private_dns_stage_failure_blocks_policy_stage(self):
        self.engine.configure_private_dns.return_value=False
        self.engine.configure_private_dns.side_effect=None
        result=self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].diagnostics['DNS policy'],'dns_prerequisite')
        self.engine.post_json.assert_not_called();self.engine.put_json.assert_not_called()

    def test_reuses_exact_static_domain_object(self):
        self.groups.append(dict(group_id=199,name='Existing private domains',type='domains',owner='user',
            hidden=False,autonomous=False,member_groups='',member_attributes={'fqdn':['mikedsecure.corp','*.mikedsecure.corp']}))
        report=self.apply()
        self.assertEqual(report['domain_object']['id'],199)
        self.assertFalse(any(isinstance(e,tuple) and e[1]=='/groups' for e in self.events))

    def test_readable_names_and_collisions_preserve_existing_objects(self):
        original = dict(group_id=199, name='DNS-Private-mikedsecure.corp', type='domains', owner='user',
                        member_attributes={'fqdn':['mikedsecure.corp']})
        self.groups.append(deepcopy(original))  # Missing the wildcard: not an exact match.
        self.groups.append(dict(group_id=198, name='DNS-PRIVATE-mikedsecure.corp-2', type='l4port'))
        report = self.apply()
        self.assertEqual(report['domain_object']['name'], 'DNS-Private-mikedsecure.corp-3')
        self.assertEqual(next(g for g in self.groups if g['group_id']==199), original)
        self.assertTrue(all(call.kwargs['params']['group_type']=='all' for call in self.engine.get_json.call_args_list
                            if call.args[0].endswith('/groups')))
        self.assertEqual(self.plan().sites[0].dns_plan.domain_object['id'], 200)

    def test_readable_name_conflict_after_preview_blocks_writes(self):
        plan = self.plan()
        self.groups.append(dict(group_id=199, name='DNS-PRIVATE-mikedsecure.corp', type='domains', owner='user',
                                member_attributes={'fqdn':['other.corp']}))
        with self.assertRaises(DnsError): self.apply(plan)
        self.engine.post_json.assert_not_called(); self.engine.put_json.assert_not_called()

    def test_multiple_lists_reserve_distinct_names_within_a_rollout(self):
        lists = ['a.corp,b.corp,c.corp', 'a.corp,d.corp,e.corp', 'c.corp,*.a.corp,b.corp']
        fields = [dict(row(), site_name=f'Branch-{i}', gateway_name=f'Gateway-{i}', dns_private_domains=value)
                  for i,value in enumerate(lists)]
        plan = self.engine.plan(validate_rows(fields))
        self.assertFalse(plan.issues, plan.issues)
        names = [site.dns_plan.domain_object['name'] for site in plan.sites]
        self.assertEqual(names, ['DNS-Private-a.corp-plus-2-more', 'DNS-Private-a.corp-plus-2-more-2',
                                 'DNS-Private-a.corp-plus-2-more'])
        # A fresh preview does not retain unused names from the previous rollout.
        self.assertEqual(self.plan(dns_private_domains=lists[1]).sites[0].dns_plan.domain_object['name'], names[0])

    def test_readable_names_normalize_input_and_bound_long_names(self):
        self.assertEqual(readable_domain_name(private_domains('*.Mikedsecure.corp.,branch.corp,mikedsecure.corp')),
                         'DNS-Private-branch.corp-mikedsecure.corp')
        zone = '.'.join(['a'*63]*3+['b'*61])
        name = readable_domain_name(private_domains(zone))
        self.assertLessEqual(len(name),255)
        self.assertTrue(name.endswith('-truncated'))

    def test_existing_hashed_name_is_reused_without_rename(self):
        name='DNS-Private-Domains-28f33c4a'
        self.groups.append(dict(group_id=199,name=name,type='domains',owner='user',
                                member_attributes={'fqdn':private_domains('mikedsecure.corp')}))
        report=self.apply()
        self.assertEqual(report['domain_object']['name'],name)
        self.assertEqual(report['domain_object']['id'],199)
        self.assertFalse(any(isinstance(e,tuple) and e[1].startswith('/groups') for e in self.events))

    def test_does_not_reuse_broader_nested_or_system_domain_groups(self):
        for extra in ({'owner':'system'},{'member_groups':'123'},{'hidden':True},{'autonomous':True},
                      {'member_attributes':{'fqdn':['*.mikedsecure.corp','mikedsecure.corp','*']}}):
            group=dict(group_id=199,name='Other',type='domains',owner='user',hidden=False,autonomous=False,
                       member_attributes={'fqdn':['*.mikedsecure.corp','mikedsecure.corp']})
            group.update(extra);self.groups.append(group)
            with self.subTest(extra=extra):self.assertEqual(self.plan().sites[0].dns_plan.domain_object['action'],'create')
            self.groups.pop()

    def test_idempotent_dns_recovery_does_not_create_site_or_duplicate_objects(self):
        plan=self.plan()
        self.apply(plan);self.events.clear()
        report=self.apply(plan)
        self.assertEqual(report['status'],'configured')
        self.assertFalse(any(isinstance(e,tuple) for e in self.events))
        self.engine.create_site.assert_not_called()

    def test_unknown_policy_or_changed_anchor_blocks_writes(self):
        for mutation in ('extra','target','order','site'):
            original=deepcopy(self.records)
            if mutation=='extra':
                extra=deepcopy(self.records[0]);extra['policy']['policy_id']=50;extra['policy']['name']='Custom';extra['policy_rules'][0].update(policy_id=50,sequence_number=4);self.records.append(extra)
            elif mutation=='target':self.records[1]['policy_rules'][0]['action_params']['dns_server_id']=32
            elif mutation=='order':self.records[1]['policy_rules'][0]['sequence_number']=4
            else:self.records[0]['policy_rules'][0]['site_id']='different-site'
            with self.subTest(mutation=mutation),self.assertRaises(DnsError):self.apply()
            self.records=original
        self.engine.post_json.assert_not_called();self.engine.put_json.assert_not_called()

    def test_compiled_priority_failure_does_not_change_catchall(self):
        self.wrong_compiled=True
        with self.assertRaises(DnsError):self.apply()
        self.assertFalse(any(isinstance(e,tuple) and e[1]=='/group-policies/12' for e in self.events))
        self.assertEqual(self.records[2]['policy_rules'][0]['action_params']['dns_server_id'],32)

    def test_compiled_match_difference_blocks_catchall_change(self):
        def altered(url, **kwargs):
            data=self.get(url, **kwargs)
            if url.endswith('/group-policy-rules'):
                data['result'][2]['dst_group_negate']=True
            return data
        self.engine.get_json.side_effect=altered
        with self.assertRaises(DnsError):self.apply()
        self.assertFalse(any(isinstance(e,tuple) and e[1]=='/group-policies/12' for e in self.events))

    def test_default_logging_and_cache_settings_are_preserved(self):
        default=self.records[2]
        default['policy'].update(disable_logging=True, disable_log_throttling=True)
        default['policy_rules'][0].update(log=True)
        default['policy_rules'][0]['action_params']['cache_level']='low'
        self.apply()
        after=self.records[2]
        self.assertTrue(after['policy']['disable_logging'])
        self.assertTrue(after['policy']['disable_log_throttling'])
        self.assertTrue(after['policy_rules'][0]['log'])
        self.assertEqual(after['policy_rules'][0]['action_params'],{'dns_server_id':31,'cache_level':'low'})

    def test_timeout_is_not_retried_and_dns_recovery_reuses_policy(self):
        plan=self.plan();self.fail_after='/group-policies'
        result=self.engine.execute(plan)
        self.assertEqual(result.sites[0].status,'partial')
        self.assertEqual(result.sites[0].diagnostics['DNS policy'],'dns_write_unconfirmed')
        self.assertEqual(sum(isinstance(e,tuple) and e[1]=='/group-policies' for e in self.events),1)
        self.assertIn('pending_write',result.sites[0].dns)
        self.fail_after=None
        report=self.apply(plan)
        self.assertEqual(report['status'],'configured')
        self.assertEqual(len(self.records),4)

    def test_changed_reviewed_object_blocks_writes(self):
        plan=self.plan()
        self.groups[0]['member_attributes']={'ip_prefix':['10.0.0.0/8']}
        with self.assertRaises(DnsError):self.apply(plan)
        self.engine.post_json.assert_not_called();self.engine.put_json.assert_not_called()

    def test_existing_site_never_enters_deployment_dns_stage(self):
        self.engine.site_exists.return_value=True
        result=self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].status,'already_exists')
        self.engine.post_json.assert_not_called();self.engine.put_json.assert_not_called()

    def test_plan_digest_pins_domains_and_resolvers(self):
        first=plan_digest(self.plan())
        self.assertNotEqual(first,plan_digest(self.plan(dns_private_domains='other.corp')))
        self.assertNotEqual(first,plan_digest(self.plan(wan_dns='9.9.9.9')))

    def test_report_contains_verified_dns_order_and_safe_failure(self):
        self.fail_after='/group-policies/12'
        result=self.engine.execute(self.plan())
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'report.json';save_report(path,result)
            text=path.read_text()
            self.assertNotIn('secret raw response',text)
            self.assertIn('dns_write_unconfirmed',str(result.sites[0].diagnostics))
            self.assertIn('pending_write',json.loads(text)['sites'][0]['dns'])

    def test_csv_round_trip_retains_opt_in_and_domains(self):
        from app import export_batch, parse_csv
        fields=row();fields.pop('vlans')
        payload=export_batch([{'fields':fields,'vlans':[]}])
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload['content']))) as archive:
            _,data=parse_csv(archive.read('sites.csv').decode(), 'sites.csv')
        self.assertEqual(data[0]['dns_split'],'1')
        self.assertEqual(data[0]['dns_private_domains'],'mikedsecure.corp')


if __name__ == '__main__':
    unittest.main()
