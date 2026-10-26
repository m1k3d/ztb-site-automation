"""Two-peer UCaaS deployment, local WAN selection and partial failures, offline."""
from copy import deepcopy
import unittest
from unittest.mock import Mock
import requests

from tests import test_ucaas as fixture
from input_validation import validate_rows
from ui_deployment import plan_digest


class HaBreakoutTests(unittest.TestCase):
    get = fixture.BreakoutTests.get
    post = fixture.BreakoutTests.post
    tearDown = fixture.BreakoutTests.tearDown

    def setUp(self):
        fixture.BreakoutTests.setUp(self)
        self.engine.get_template_settings.return_value['deployment_type'] = 'standard_mode_ha'
        self.ports += [dict(id='b-'+name, name=name, gateway_id='Gateway-2', interface_type='wan')
                       for name in ('ge5', 'ge8')]
        self.ports += [dict(id=slot+'-ha', name='ge4', gateway_id=slot, interface_type='ha')
                       for slot in ('Gateway-1', 'Gateway-2')]
        self.engine.get_ha_template_interfaces = Mock(side_effect=lambda _: deepcopy(self.ports))
        self.engine.resolve_gateway_ids_and_cluster.return_value = ('gw-b,gw-a', 1)
        self.engine.find_site_row_by_name = Mock(return_value={'id':'site-new', 'gateways':[
            dict(gateway_id='gw-b', gateway_name='Branch-B'), dict(gateway_id='gw-a', gateway_name='Branch-A')]})
        self.engine.configure_vrrp = Mock(return_value=True)
        self.policy_rows = [dict(pbr_policy_id=index, name='existing-'+str(index), display_name='Existing',
            gateway_id=gateway, sequence_number=1, template_id=template)
            for index,gateway,template in ((10,'gw-a',''),(11,'gw-a','template-id'),(20,'gw-b',''),(21,'gw-b','template-id'))]

    def plan(self, **overrides):
        return self.engine.plan(validate_rows([fixture.row(gateway_name='Branch-A', gateway_name_b='Branch-B',
            wan1_interface_name='ge5', **overrides)]))

    def test_each_mode_creates_four_rules_on_correct_peers_and_shared_objects_once(self):
        baseline = deepcopy(self.policy_rows)
        for mode in ('standard_mode_ha','wan_edge_mode_ha'):
            self.engine.get_template_settings.return_value['deployment_type']=mode
            if mode=='wan_edge_mode_ha':
                self.ports += [dict(id=slot+'-data',name='ge3',gateway_id=slot,interface_type='ha-data')
                               for slot in ('Gateway-1','Gateway-2')]
            for selection,flags in (('best',(True,False)),('none',(False,False)),('balanced',(False,True))):
                with self.subTest(mode=mode, selection=selection):
                    self.policy_rows=deepcopy(baseline)
                    self.events.clear();self.reorders.clear()
                    plan=self.plan(ucaas_path_selection=selection)
                    self.assertFalse(plan.issues,plan.issues)
                    result=self.engine.execute(plan)
                    self.assertEqual(result.exit_code,0,result.sites[0])
                    rules=[p for p in self.policy_rows if p['name'].startswith('ucaas-lbo-')]
                    self.assertEqual(len(rules),4)
                    for rule in rules:
                        self.assertEqual((rule['primary_int'],rule['secondary_int']),
                            ('ge5','ge6') if rule['gateway_id']=='gw-a' else ('ge5','ge8'))
                        self.assertEqual((rule['best_link'],rule['ecmp_enabled']),flags)
                        self.assertFalse(rule['wan_edge_ha'])
                    expected=[{**p,'sequence_number':1 if p['template_id'] else 3} for p in baseline]
                    self.assertEqual([p for p in self.policy_rows if not p['name'].startswith('ucaas-lbo-')],expected)
                    self.assertEqual(len(self.reorders),2)
                    for reorder in self.reorders:
                        ids={p['pbr_policy_id'] for p in self.policy_rows if p['gateway_id']==reorder['gateway_id'] and not p['template_id']}
                        self.assertEqual({p['pbr_policy_id'] for p in reorder['pbr_order']},ids)
                    report=result.sites[0].ucaas
                    self.assertEqual([p['status'] for p in report['gateways']],['configured','configured'])
                    self.assertEqual([p['gateway_target'] for p in report['policies']],['a','a','b','b'])
                    creates=[e for e in self.events if isinstance(e,tuple) and e[0].endswith('/groups')]
                    self.assertLessEqual(len(creates),len(plan.sites[0].ucaas_plan.objects))

    def test_second_peer_invalid_blocks_before_objects_or_rules(self):
        plan=self.plan()
        self.ports=[p for p in self.ports if not (p['gateway_id']=='Gateway-2' and p['name']=='ge8')]
        result=self.engine.execute(plan)
        self.assertEqual(result.sites[0].status,'partial')
        self.engine.post_json.assert_not_called()

    def test_second_peer_timeout_preserves_first_peer_result_and_does_not_retry(self):
        def post(url,payload,**kwargs):
            if url.endswith('/pbr/policies') and payload['gateway_id']=='gw-b':
                raise requests.Timeout('private response')
            return self.post(url,payload,**kwargs)
        self.engine.post_json.side_effect=post
        result=self.engine.execute(self.plan())
        report=result.sites[0].ucaas
        self.assertEqual(result.sites[0].status,'partial')
        self.assertEqual([g['status'] for g in report['gateways']],['configured','incomplete'])
        calls=[c for c in self.engine.post_json.call_args_list if c.args[0].endswith('/pbr/policies') and c.args[1]['gateway_id']=='gw-b']
        self.assertEqual(len(calls),1)

    def test_first_peer_failure_leaves_second_pending(self):
        original=self.post
        self.engine.post_json.side_effect=lambda url,payload,**kw: (_ for _ in ()).throw(requests.Timeout()) if url.endswith('/pbr/policies') else original(url,payload,**kw)
        report=self.engine.execute(self.plan()).sites[0].ucaas
        self.assertEqual([g['status'] for g in report['gateways']],['incomplete','pending'])

    def test_wrong_gateway_identity_prevents_policy_writes(self):
        self.engine.find_site_row_by_name.return_value['gateways'][1]['gateway_name']='Another site'
        result=self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].status,'partial')
        self.engine.post_json.assert_not_called()

    def test_peer_wan_selection_is_in_approved_digest(self):
        self.ports.append(dict(id='b-ge9',name='ge9',gateway_id='Gateway-2',interface_type='wan'))
        ambiguous=self.plan()
        self.assertEqual(ambiguous.issues[0].field,'ucaas_interfaces')
        first=self.plan(ucaas_secondary_wan_b='ge8')
        second=self.plan(ucaas_secondary_wan_b='ge9')
        self.assertFalse(first.issues or second.issues)
        self.assertNotEqual(plan_digest(first),plan_digest(second))
