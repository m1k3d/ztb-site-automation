import unittest
from unittest.mock import Mock, patch

from pull_site import site_to_csv_row
from automation_config import Settings
from site_reference import ReferenceSites, ReferenceError


class InterfaceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.reader = ReferenceSites('unused')
        self.reader.client = Mock()
        self.reader._get = Mock()
        self.template = {'id': 't1', 'name': 'Branch', 'platform_type': 'zt600', 'secret': 'private'}
        self.port = {'id': 'i1', 'name': 'ge5', 'interface_type': 'wan', 'gateway_id': 'Gateway-1', 'secret': 'private'}

    def responses(self, ports=None):
        ports = [self.port] if ports is None else ports
        self.reader._get.side_effect = [{'result': [self.template], 'count': 1}, {'result': ports, 'count': len(ports)}]

    def test_reads_template_roles_and_caches_without_exposing_raw_data(self):
        self.responses()
        first = self.reader.interfaces('branch')
        self.assertEqual(first['template']['platform'], 'zt600')
        self.assertEqual(first['gateways'][0]['interfaces'], [{'name': 'ge5', 'type': 'wan', 'bond_member': False}])
        self.assertNotIn('private', str(first))
        self.assertEqual(self.reader._get.call_args_list[1].args[0], '/templates/t1/interfaces')
        first['gateways'].clear()
        self.assertEqual(len(self.reader.interfaces('Branch')['gateways']), 1)
        self.assertEqual(self.reader._get.call_count, 2)

    def test_separate_gateway_roles_and_bond_members(self):
        self.responses([self.port, {**self.port, 'id':'i2', 'gateway_id':'Gateway-2', 'interface_type':'lan', 'bonding_parent':'bond0'}])
        result = self.reader.interfaces(template_id='t1')
        self.assertEqual(result['gateways'][1]['interfaces'][0]['type'], 'lan')
        self.assertTrue(result['gateways'][1]['interfaces'][0]['bond_member'])

    def test_explicit_refresh_fetches_again(self):
        self.responses()
        self.reader.interfaces('Branch')
        self.responses([{**self.port, 'name':'xe7'}])
        self.assertEqual(self.reader.interfaces('Branch', refresh=True)['gateways'][0]['interfaces'][0]['name'], 'xe7')
        self.assertEqual(self.reader._get.call_count, 4)

    def test_reconnection_clears_all_template_caches(self):
        self.responses()
        self.reader.interfaces('Branch')
        self.reader._get.side_effect = None
        self.reader._get.return_value = {'rows': []}
        with patch('site_reference.Settings.load', return_value=Settings(ztb_api_base='https://example-api.goairgap.com',api_key='fixture')):
            self.reader.connect()
        self.assertIsNone(self.reader.templates_cache)
        self.assertEqual(self.reader.interfaces_cache,{})

    def test_all_pages_are_read(self):
        self.reader._get.side_effect = [{'result':[self.template], 'count':2},
            {'result':[{**self.template,'id':'t2','name':'Other'}], 'count':2},
            {'result':[self.port], 'count':2},
            {'result':[{**self.port,'id':'i2','name':'ge6'}], 'count':2}]
        self.assertEqual(len(self.reader.interfaces('Branch')['gateways'][0]['interfaces']), 2)
        self.assertEqual([call.args[1]['page'] for call in self.reader._get.call_args_list], [0,1,0,1])

    def test_partial_repeated_and_malformed_inventory_is_not_cached(self):
        bad_responses = [
            [{'result':[self.port], 'count':2}, {'result':[], 'count':2}],
            [{'result':[self.port], 'count':2}, {'result':[self.port], 'count':2}],
            [{'result':[{**self.port,'gateway_id':'unknown'}], 'count':1}],
            [{'result':[{**self.port,'name':'<script>'}], 'count':1}],
            [{'result':[], 'count':0}],
        ]
        for response in bad_responses:
            with self.subTest(response=response):
                self.reader.templates_cache = None
                self.reader._get.side_effect = [{'result':[self.template], 'count':1}, *response]
                with self.assertRaises(ReferenceError): self.reader.interfaces('Branch')
                self.assertEqual(self.reader.interfaces_cache, {})

    def test_missing_or_ambiguous_template_and_invalid_inputs_do_not_read_arbitrary_paths(self):
        self.responses()
        with self.assertRaisesRegex(ReferenceError, 'not found'): self.reader.interfaces(template_id='../other')
        self.assertEqual(self.reader._get.call_count, 1)
        for args in ({'template_name':[]}, {'refresh':'yes'}, {'template_id':123}):
            with self.assertRaises(ReferenceError): self.reader.interfaces(**args)
        self.reader.templates_cache = [self.template,{**self.template,'id':'t2'}]
        with self.assertRaisesRegex(ReferenceError, 'uniquely'): self.reader.interfaces('Branch')
        self.reader.client = None
        with self.assertRaisesRegex(ReferenceError, 'Connect'): self.reader.interfaces('Branch')


class WanReferenceTests(unittest.TestCase):
    def fields(self, row, networks=None):
        return site_to_csv_row(row, 'Reference', '', wan_networks=networks)

    def test_missing_wan_is_blank_instead_of_ge5(self):
        self.assertEqual(self.fields({})['wan_interface_name'], '')
        self.assertEqual(self.fields({'wan_interface_name':'xe7'})['wan_interface_name'], 'xe7')

    def test_incomplete_static_wan_cannot_silently_become_dhcp(self):
        with self.assertRaisesRegex(ValueError,'Static WAN'):
            self.fields({},[{'zone':'WAN Zone','interface':'ge6','dhcp_client':False}])

    def test_real_wan_assignment_matches_gateway_ids_even_when_networks_are_reversed(self):
        row={'gateways':[{'gateway_id':'a','gateway_name':'A','template_gateway_id':'Gateway-1'},
                         {'gateway_id':'b','gateway_name':'B','template_gateway_id':'Gateway-2'}]}
        networks=[{'zone':'WAN Zone','gateway_id':'b','interface':'ge8','dhcp_client':False,
                   'default_gateway':'192.0.2.2','subnet':'24','wan_nexthop_ip':'192.0.2.1'},
                  {'zone':'WAN Zone','gateway_id':'a','interface':'ge7','dhcp_client':True,
                   'default_gateway':'198.51.100.2','subnet':'24','wan_nexthop_ip':'198.51.100.1'}]
        result=self.fields(row, networks)
        self.assertEqual(result['wan_interface_name'], 'ge7')
        self.assertEqual(result['wan1_interface_name'], 'ge8')
        self.assertEqual(result['wan0_ip'], '')
        self.assertEqual((result['wan1_ip'],result['wan1_mask'],result['wan1_gw']), ('192.0.2.2','24','192.0.2.1'))

    def test_ambiguous_wan_never_selects_first_interface_or_another_gateway(self):
        row={'gateways':[{'gateway_id':'a','gateway_name':'A'}]}
        networks=[{'zone':'WAN Zone','gateway_id':'a','interface':'ge5'},
                  {'zone':'WAN Zone','gateway_id':'a','interface':'ge6'}]
        self.assertEqual(self.fields(row, networks)['wan_interface_name'], '')
        row['gateways'][0]['wan_interface']='ge6'
        self.assertEqual(self.fields(row, networks)['wan_interface_name'], 'ge6')
        del row['gateways'][0]['wan_interface']
        self.assertEqual(self.fields(row,[{'zone':'WAN Zone','gateway_id':'b','interface':'ge7'}])['wan_interface_name'], '')

    def test_multiwan_reference_defaults_to_gateway_primary_port_in_either_order(self):
        # Utrecht's API shape: no wan_interface field; the primary IP is on the gateway.
        row={'gateways':[{'gateway_id':'a','gateway_name':'Reference-GW','gateway_ip_address':'192.0.2.157'}]}
        primary={'zone':'WAN Zone','gateway_id':'a','interface':'ge5','dhcp_client':False,
                 'default_gateway':'192.0.2.157','subnet':'24','wan_nexthop_ip':'192.0.2.1'}
        secondary={**primary,'interface':'ge6','default_gateway':'198.51.100.159','wan_nexthop_ip':'198.51.100.1'}
        for networks in ([secondary,primary],[primary,secondary]):
            with self.subTest(networks=networks):
                result=self.fields(row,networks)
                self.assertEqual(result['wan_interface_name'],'ge5')
                self.assertEqual((result['wan0_ip'],result['wan0_mask'],result['wan0_gw']),('192.0.2.157','24','192.0.2.1'))

    def test_primary_wan_match_respects_gateway_ownership_and_dhcp(self):
        row={'gateways':[{'gateway_id':'a','gateway_name':'A','gateway_ip_address':'192.0.2.10'},
                         {'gateway_id':'b','gateway_name':'B','gateway_ip_address':'192.0.2.20'}]}
        wan={'zone':'WAN Zone','dhcp_client':True}
        networks=[{**wan,'gateway_id':'a','interface':'ge6','default_gateway':'198.51.100.10'},
                  {**wan,'gateway_id':'b','interface':'ge8','default_gateway':'192.0.2.20'},
                  {**wan,'gateway_id':'a','interface':'ge5','default_gateway':'192.0.2.10'},
                  {**wan,'gateway_id':'b','interface':'ge7','default_gateway':'192.0.2.10'}]
        result=self.fields(row,networks)
        self.assertEqual((result['wan_interface_name'],result['wan1_interface_name']),('ge5','ge8'))
        self.assertEqual((result['wan0_ip'],result['wan1_ip']),('',''))

    def test_unknown_or_nonunique_primary_address_does_not_guess(self):
        for primary_ip in ('','0.0.0.0','not-an-ip','192.0.2.1'):
            with self.subTest(primary_ip=primary_ip):
                row={'gateways':[{'gateway_id':'a','gateway_ip_address':primary_ip}]}
                networks=[{'zone':'WAN Zone','gateway_id':'a','interface':port,'default_gateway':primary_ip} for port in ('ge5','ge6')]
                self.assertEqual(self.fields(row,networks)['wan_interface_name'],'')

    def test_explicit_wan_choice_takes_precedence_over_primary_address(self):
        row={'gateways':[{'gateway_id':'a','gateway_ip_address':'192.0.2.1','wan_interface':'ge6'}]}
        networks=[{'zone':'WAN Zone','gateway_id':'a','interface':port,'default_gateway':ip,'dhcp_client':True}
                  for port,ip in [('ge5','192.0.2.1'),('ge6','198.51.100.1')]]
        self.assertEqual(self.fields(row,networks)['wan_interface_name'],'ge6')

    def test_single_unowned_wan_is_used_only_for_standalone(self):
        network={'zone':'WAN Zone','interface':'ge6','dhcp_client':True}
        self.assertEqual(self.fields({},[network])['wan_interface_name'], 'ge6')
        self.assertEqual(self.fields({'gateways':[{'gateway_name':'A'},{'gateway_name':'B'}]},[network])['wan_interface_name'], '')


if __name__ == '__main__': unittest.main()
