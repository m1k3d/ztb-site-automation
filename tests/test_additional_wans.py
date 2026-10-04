import json
import unittest
from unittest.mock import Mock
from copy import deepcopy

from additional_wans import AdditionalWans, WanError, from_reference, selections
from pull_site import vlans_to_csv_rows


def wan(**kwargs):
    return dict(name='WAN2-A',gateway_target='a',interface='ge8',tag='1',mode='static',
                ip='172.31.180.2',mask='24',gateway='172.31.180.1',dns='1.1.1.1',**kwargs)


class AdditionalWanTests(unittest.TestCase):
    def row(self,wans):
        return dict(copy_additional_wans='1',additional_wans_json=json.dumps(wans),
                    gateway_name_b='B',wan_interface_name='ge7',wan1_interface_name='ge7')

    def test_disabled_option_makes_no_inventory_requests(self):
        e=Mock();service=AdditionalWans(e)
        self.assertEqual(service.plan({'additional_wans_json':'broken'},'template'),[])
        e.template_cloner.get.assert_not_called()

    def test_static_and_dhcp_validation(self):
        self.assertEqual(selections(self.row([wan()]))[0]['ip'],'172.31.180.2')
        w=wan();w.update(mode='dhcp',ip='stale',mask='stale',gateway='stale')
        self.assertEqual(selections(self.row([w]))[0]['ip'],'')

    def test_invalid_primary_port_duplicates_and_addresses_rejected(self):
        for edits in ({'interface':'ge7'},{'gateway_target':'c'},{'ip':'172.31.180.0'},
                      {'gateway':'172.31.181.1'},{'mask':'32'},{'dns':'bad'},{'tag':'4095'}):
            with self.subTest(edits=edits),self.assertRaises(WanError):
                selections(self.row([{**wan(),**edits}]))
        with self.assertRaises(WanError):selections(self.row([wan(),wan()]))

    def test_gateway_b_requires_ha(self):
        row=self.row([{**wan(),'gateway_target':'b'}]);row['gateway_name_b']=''
        with self.assertRaises(WanError):selections(row)

    def test_import_excludes_primary_wan_and_discards_dhcp_lease(self):
        source=[dict(name='Primary',interface='ge7',gateway_id='a',dhcp_client=True),
                dict(name='Secondary',interface='ge8',gateway_id='a',dhcp_client=True,tag='1',
                     default_gateway='172.16.3.15',subnet='24',wan_nexthop_ip='172.16.3.1')]
        values=from_reference(source,[dict(gateway_id='a')],{'wan_interface_name':'ge7'})
        self.assertEqual(len(values),1);self.assertEqual(values[0]['ip'],'')
        self.assertEqual(values[0]['mode'],'dhcp')

    def test_unknown_owner_is_rejected(self):
        with self.assertRaises(WanError):
            from_reference([dict(gateway_id='unknown')]*2,[dict(gateway_id='a')],{'wan_interface_name':'ge7'})

    def test_plan_checks_template_gateway_role(self):
        e=Mock();service=AdditionalWans(e)
        e.template_cloner.get.return_value={'count':1,'result':[dict(id='port',gateway_id='Gateway-1',name='ge8',interface_type='wan')]}
        self.assertEqual(len(service.plan(self.row([wan()]),'template')),1)
        e.template_cloner.get.return_value['result'][0]['interface_type']='lan'
        with self.assertRaises(WanError):service.plan(self.row([wan()]),'template')

    def api(self):
        e=Mock(API_V2='https://example.invalid/api/v2');live=[]
        e.vlan_gateway_targets.side_effect=lambda site,ids,items,row:{id(w):'new-'+w['gateway_target'] for w in items}
        e.list_site_vlans_v2.side_effect=lambda site:deepcopy(live)
        def create(p):
            live.append({**p,'id':'created','gateway_id':p['gateways'],'status':'notprovisioned'})
            return True,''
        def update(url,p,**kwargs):
            live[0].update(p);return Mock(status_code=200)
        e.post_vlan.side_effect=create;e.put_json.side_effect=update
        return e,live,AdditionalWans(e)

    def test_create_verify_and_reuse_without_duplicate(self):
        e,live,service=self.api();report={};wans=[wan()]
        self.assertTrue(service.apply(wans,'site','new-b,new-a',42,{},report))
        self.assertEqual(report['status'],'verified');self.assertNotIn('pending_write',report)
        self.assertEqual(e.post_vlan.call_args.args[0]['gateways'],'new-a')
        self.assertTrue(service.apply(wans,'site','new-b,new-a',42,{},{}))
        self.assertEqual(e.post_vlan.call_count,1)

    def test_conflict_is_not_overwritten(self):
        e,live,service=self.api()
        live.append(dict(gateway_id='new-a',interface='ge8',tag='1',zone='LAN Zone'))
        with self.assertRaises(WanError):service.apply([wan()],'site','new-a',42,{}, {})
        e.post_vlan.assert_not_called();e.put_json.assert_not_called()

    def test_timeout_is_not_retried_and_journal_retained(self):
        e,live,service=self.api();e.post_vlan.side_effect=TimeoutError();report={}
        with self.assertRaises(TimeoutError):service.apply([wan()],'site','new-a',42,{},report)
        self.assertEqual(e.post_vlan.call_count,1);self.assertEqual(report['pending_write']['method'],'POST')

    def test_wrong_readback_is_not_success(self):
        e,live,service=self.api();e.post_vlan.side_effect=lambda p:(True,'')
        report={}
        with self.assertRaises(WanError):service.apply([wan()],'site','new-a',42,{},report)
        self.assertEqual(report['status'],'incomplete');e.put_json.assert_not_called()

    def test_shared_ha_lan_imports_once_with_site_scope(self):
        source=dict(name='Users',tag='166',subnet='24',default_gateway='172.16.166.1',
                    interface='ge5,ge5',zone='LAN Zone',gateway_id='b,a')
        values=vlans_to_csv_rows([source],gateways=[dict(gateway_id='a'),dict(gateway_id='b')])
        self.assertEqual(len(values),1);self.assertEqual(values[0]['gateway_target'],'all')
        self.assertEqual(values[0]['interface'],'ge5')

    def test_shared_lan_keeps_different_ports_in_gateway_order(self):
        source=dict(name='Users',tag='166',interface='ge5,ge1',zone='LAN Zone',gateway_id='b,a')
        values=vlans_to_csv_rows([source],gateways=[dict(gateway_id='a'),dict(gateway_id='b')])
        self.assertEqual(values[0]['interface'],'ge1,ge5')
