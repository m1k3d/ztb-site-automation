"""HA template slot validation and network ownership before tenant writes."""
import unittest
from unittest.mock import Mock

from automation_config import Settings
from deployment_engine import DeploymentEngine
from ha_deployment import check_interfaces, HaError
from pull_site import ordered_reference_gateways, vlans_to_csv_rows


class HaMappingTests(unittest.TestCase):
    def setUp(self):
        self.row=dict(site_name='Branch',gateway_name='A',gateway_name_b='B',wan_interface_name='ge7',wan1_interface_name='ge7')
        self.settings=dict(deployment_type='standard_mode_ha')
        self.ports=[dict(gateway_id=slot,name=name,interface_type=role)
            for slot in ('Gateway-1','Gateway-2') for name,role in (('ge7','wan'),('ge4','ha'),('ge3','ha-data'),('ge5','lan'),('ge6','lan'))]
        self.net=dict(name='Users',tag='10',subnet='24',default_gateway='10.20.0.1',interface='ge5,ge6',gateway_target='all',enabled=True)

    def test_standard_and_enhanced_accept_verified_networks(self):
        for mode in ('standard_mode_ha','wan_edge_mode_ha'):
            self.settings['deployment_type']=mode
            self.assertTrue(check_interfaces(self.row,[self.net],self.settings,self.ports))

    def test_invalid_mapping_rejected(self):
        cases=[('ha_wan_b',dict(wan1_interface_name='ge4'),[],None),
            ('ha_gateway_order',dict(ha_template_slots_json='["Gateway-2","Gateway-1"]'),[],None),
            ('ha_link',dict(vrrp_link_interface='ge5'),[],None),
            ('ha_network',{},[{**self.net,'interface':'ge4'}],None),
            ('ha_network',{},[{**self.net,'interface':'lo0','zone':'Management Zone'}],None),
            ('ha_wan_b',{},[],[p for p in self.ports if not(p['gateway_id']=='Gateway-2' and p['name']=='ge7')])]
        for code,fields,nets,ports in cases:
            with self.subTest(code=code),self.assertRaises(HaError) as error:
                check_interfaces({**self.row,**fields},nets,self.settings,ports if ports is not None else self.ports)
            self.assertEqual(error.exception.code,code)

    def test_enhanced_requires_transit_and_bond_members_cannot_be_wans(self):
        self.settings['deployment_type']='wan_edge_mode_ha'
        with self.assertRaises(HaError) as error:
            check_interfaces(self.row,[],self.settings,[p for p in self.ports if p['name']!='ge3'])
        self.assertEqual(error.exception.code,'ha_transit')
        for p in self.ports:
            if p['name']=='ge7':p['bonding_parent']='bond0'
        with self.assertRaises(HaError):check_interfaces(self.row,[],self.settings,self.ports)

    def test_explicit_management_loopbacks_can_be_staged(self):
        nets=[dict(interface='lo0',zone='Management Zone',gateway_target=t) for t in ('a','b')]
        self.assertTrue(check_interfaces(self.row,nets,self.settings,self.ports))

    def test_reference_reordering_keeps_owners_and_ports_together(self):
        gateways=[dict(gateway_id='b',template_gateway_id='Gateway-2'),dict(gateway_id='a',template_gateway_id='Gateway-1')]
        self.assertEqual(ordered_reference_gateways(gateways,strict=True)[0]['gateway_id'],'a')
        rows=vlans_to_csv_rows([{**self.net,'gateway_id':'b,a','interface':'ge6,ge5'}],gateways=gateways)
        self.assertEqual(rows[0]['interface'],'ge5,ge6')
        with self.assertRaises(ValueError):ordered_reference_gateways([dict(gateway_id='a'),dict(gateway_id='b')],strict=True)

    def test_shared_vlan_uses_named_order_and_rejects_wrong_readback_owner(self):
        engine=DeploymentEngine(Settings(ztb_api_base='https://example.invalid',bearer='offline'),emit=lambda *_:None)
        self.addCleanup(engine.client.close)
        engine.find_site_row_by_name=Mock(return_value=dict(id='site',gateways=[dict(gateway_id='b',gateway_name='B'),dict(gateway_id='a',gateway_name='A')]))
        engine.post_vlan=Mock(return_value=(True,'created'))
        engine.put_json=Mock(return_value=Mock(status_code=200))
        engine.list_site_vlans_v2=Mock(return_value=[{**self.net,'id':'network','gateway_id':'b,a','interface':'ge6,ge5'}])
        self.assertTrue(engine.process_vlans_for_site('site','b,a',1,[self.net],self.row))
        payload=engine.post_vlan.call_args.args[0]
        self.assertEqual(payload['gateways'],'a,b')
        self.assertEqual(payload['interface'],'ge5,ge6')
        for owners,ports in (('foreign,a','ge6,ge5'),('b,a','ge5,ge6')):
            engine.put_json.reset_mock()
            engine.list_site_vlans_v2.return_value=[{**self.net,'id':'network','gateway_id':owners,'interface':ports}]
            self.assertFalse(engine.process_vlans_for_site('site','b,a',1,[self.net],self.row))
            engine.put_json.assert_not_called()
