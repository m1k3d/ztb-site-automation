import unittest
import base64
import csv
import io
import zipfile
from unittest.mock import Mock

from deployment_engine import DeploymentEngine
from input_validation import validate_vlan_rows
from pull_site import vlans_to_csv_rows
from app import export_batch


def network(name='mgmt-a', target='a', ip='172.31.165.1'):
    return dict(name=name, tag='1', subnet='32', default_gateway=ip,
                interface='lo0', zone='Management Zone', dhcp_service='off',
                gateway_target=target, enabled=True, share_over_vpn=False)


class HaNetworkTargetsTests(unittest.TestCase):
    def test_reference_preserves_gateway_ownership_without_source_ids(self):
        gateways = [dict(gateway_id='source-a'), dict(gateway_id='source-b')]
        rows = vlans_to_csv_rows([dict(network(), gateway_id='source-b'),
                                 dict(network('mgmt-b'), gateway_id='source-a')], gateways=gateways)
        self.assertEqual([v['gateway_target'] for v in rows], ['b', 'a'])
        self.assertNotIn('source-', str(rows))

    def test_ambiguous_reference_owner_is_rejected(self):
        for owner in ('', 'unknown'):
            with self.assertRaisesRegex(ValueError, 'HA gateway'):
                vlans_to_csv_rows([dict(network(), gateway_id=owner)],
                                  gateways=[dict(gateway_id='a'), dict(gateway_id='b')])

    def validate(self, rows, ha=True):
        issues = []
        output = validate_vlan_rows(enumerate(rows, 1), 'test', issues, ha=ha)
        return output, issues

    def test_same_tag_and_port_on_separate_gateways_is_valid(self):
        rows, issues = self.validate([network(), network('mgmt-b', 'b', '172.31.165.2')])
        self.assertEqual(issues, [])
        self.assertEqual([v['gateway_target'] for v in rows], ['a', 'b'])

    def test_duplicate_tag_on_same_gateway_or_shared_target_is_rejected(self):
        for target in ('a', 'all'):
            _, issues = self.validate([network(), network('duplicate', target)])
            self.assertTrue(any(i.field == 'tag' for i in issues))

    def test_gateway_b_requires_ha(self):
        _, issues = self.validate([network(target='b')], ha=False)
        self.assertTrue(any(i.field == 'gateway_target' for i in issues))

    def test_invalid_scope_and_multiport_single_gateway_are_rejected(self):
        for row, field in ((network(target='source-uuid'), 'gateway_target'),
                           (dict(network(), interface='lo0,lo0'), 'interface')):
            _, issues = self.validate([row])
            self.assertTrue(any(i.field == field for i in issues))

    def engine(self, vlans):
        engine = object.__new__(DeploymentEngine)
        engine.API_V2 = 'https://example.invalid/api/v2'
        engine.ORIGIN = engine.REFERER = 'https://example.invalid'
        engine.log = Mock()
        engine.find_site_row_by_name = Mock(return_value=dict(cluster_info={'site_id':'new-site'}, gateways=[
            dict(gateway_id='new-b', gateway_name='Verify-GW-B'),
            dict(gateway_id='new-a', gateway_name='Verify-GW')]))
        engine.post_vlan = Mock(return_value=(True, ''))
        engine.list_site_vlans_v2 = Mock(return_value=[dict(v, id=str(i), gateway_id='new-'+v['gateway_target'])
                                                      for i, v in enumerate(vlans)])
        engine.get_gateway_interfaces_v2 = Mock(return_value=[dict(gateway_id='new-'+target,
            interfaces=[dict(name='lo0', id='lo-'+target)]) for target in ('a','b')])
        engine.put_json = Mock(return_value=Mock(status_code=200))
        return engine

    def apply(self, engine, vlans, diagnostics=None):
        return engine.process_vlans_for_site('new-site', 'new-b,new-a', 42, vlans,
            dict(site_name='Verify', gateway_name='Verify-GW', gateway_name_b='Verify-GW-B'), diagnostics=diagnostics)

    def test_deployment_uses_gateway_names_despite_reversed_api_order(self):
        vlans = [network(), network('mgmt-b', 'b', '172.31.165.2')]
        engine = self.engine(vlans)
        self.assertTrue(self.apply(engine, vlans))
        payloads = [c.args[0] for c in engine.post_vlan.call_args_list]
        self.assertEqual([v['gateways'] for v in payloads], ['new-a', 'new-b'])
        self.assertEqual([v['interface'] for v in payloads], ['lo0', 'lo0'])
        self.assertEqual(engine.put_json.call_count, 2)

    def test_unresolved_gateway_stops_before_any_vlan_writes(self):
        vlans = [network()]
        for actual in ([], [dict(gateway_id='new-b', gateway_name='different')],
                       [dict(gateway_id='new-a', gateway_name='Verify-GW')]*2):
            engine = self.engine(vlans)
            engine.find_site_row_by_name.return_value['gateways'] = actual
            with self.assertRaises(ValueError):
                self.apply(engine, vlans)
            engine.post_vlan.assert_not_called()

    def test_wrong_site_stops_before_vlan_writes(self):
        vlans = [network()]
        engine = self.engine(vlans)
        engine.find_site_row_by_name.return_value['cluster_info']['site_id'] = 'other'
        with self.assertRaises(ValueError):
            self.apply(engine, vlans)
        engine.post_vlan.assert_not_called()

    def test_enable_never_targets_matching_network_on_wrong_gateway(self):
        vlans = [network()]
        engine = self.engine(vlans)
        engine.list_site_vlans_v2.return_value[0]['gateway_id'] = 'new-b'
        self.assertFalse(self.apply(engine, vlans))
        engine.put_json.assert_not_called()

    def test_loopback_binding_checks_only_the_selected_gateway(self):
        vlans = [network()]
        engine = self.engine(vlans)
        engine.get_gateway_interfaces_v2.return_value = [dict(gateway_id='new-a', interfaces=[dict(name='lo0', id='lo-a')])]
        diagnostics = {}
        self.assertTrue(self.apply(engine, vlans, diagnostics))
        self.assertEqual(diagnostics, {})

    def test_csv_export_preserves_each_gateway_target(self):
        batch = [dict(fields=dict(site_name='HA',gateway_name='HA-A',gateway_name_b='HA-B',
                     wan_interface_name='ge7',wan1_interface_name='ge7',template_name='HA template',
                     location_type='none',post='1'), ha_enabled=True,
                     vlans=[network(), network('mgmt-b','b','172.31.165.2')])]
        exported = export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported['content']))) as archive:
            name = next(n for n in archive.namelist() if n.startswith('vlans/'))
            rows = list(csv.DictReader(io.StringIO(archive.read(name).decode())))
        self.assertEqual([v['gateway_target'] for v in rows], ['a','b'])

    def test_new_unbound_loopback_can_be_staged_but_binding_stays_incomplete(self):
        vlans=[network()];engine=self.engine(vlans)
        unbound=dict(vlans[0],id='new-network',interface='',gateway_id='')
        engine.list_site_vlans_v2.side_effect=[[],[unbound]]
        engine.get_gateway_interfaces_v2.return_value=[]
        diagnostics={}
        self.assertTrue(self.apply(engine,vlans,diagnostics))
        self.assertEqual(engine.put_json.call_count,1)
        self.assertEqual(diagnostics,{'Loopback binding':'loopback_binding_unverified'})

    def test_existing_unbound_loopback_is_not_guessed_as_new_post_result(self):
        vlans=[network()];engine=self.engine(vlans)
        unbound=dict(vlans[0],id='existing',interface='',gateway_id='')
        engine.list_site_vlans_v2.return_value=[unbound]
        self.assertFalse(self.apply(engine,vlans))
        engine.put_json.assert_not_called()


if __name__ == '__main__':
    unittest.main()
