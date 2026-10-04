import unittest
from input_validation import validate_vlan_rows
from deployment_engine import DeploymentEngine


class LoopbackValidationTests(unittest.TestCase):
    def test_rejects_reference_loopback_24_before_deployment(self):
        for interface in ('lo0', 'lo0,lo0'):
            issues = []
            validate_vlan_rows([(2, dict(name='MGMT', tag='1', subnet='24',
                default_gateway='172.16.65.1', interface=interface, dhcp_service='off'))], 'test.csv', issues)
            self.assertTrue(any(i.field == 'subnet' and '/32' in i.message for i in issues))

    def test_loopback_32_preserves_host_and_has_no_dhcp_range(self):
        issues = []
        vlans = validate_vlan_rows([(2, dict(name='MGMT', tag='1', subnet='32',
            default_gateway='172.16.65.1', interface='lo0', dhcp_service='off'))], 'test.csv', issues)
        self.assertEqual(issues, [])
        engine = object.__new__(DeploymentEngine)
        payload = engine.vlan_to_v2_payload(vlans[0], 'gateway-id', 123)
        self.assertEqual(payload['ip_range'], '172.16.65.1')
        self.assertEqual(payload['subnet'], '32')
        self.assertEqual(payload['dhcp_range'], '')
        self.assertEqual(payload['dhcp_service'], 'no_dhcp')

    def staging_engine(self):
        from unittest.mock import Mock
        engine = object.__new__(DeploymentEngine)
        engine.API_V2 = "https://example.invalid/api/v2"
        engine.ORIGIN = engine.REFERER = "https://example.invalid"
        engine.log = Mock()
        engine.get_gateway_interfaces_v2 = Mock(return_value=[{
            "gateway_id": "gw", "interfaces": [{"id": "physical", "name": "ge2"}]}])
        vlans = [dict(name="MGMT", tag="1", subnet="32", default_gateway="10.0.0.1",
                      interface="lo0", dhcp_service="off", enabled=True),
                 dict(name="LAN", tag="20", subnet="24", default_gateway="10.0.20.1",
                      interface="ge2", dhcp_service="off", enabled=True)]
        engine.post_vlan = Mock(return_value=(True, ""))
        engine.list_site_vlans_v2 = Mock(return_value=[dict(v, id=str(i)) for i, v in enumerate(vlans)])
        engine.put_json = Mock(return_value=Mock(status_code=200))
        return engine, vlans

    def test_missing_loopback_still_posts_and_enables_all_networks(self):
        engine, vlans = self.staging_engine()
        diagnostics = {}
        self.assertTrue(engine.process_vlans_for_site('site', 'gw', 1, vlans, {}, diagnostics=diagnostics))
        self.assertEqual([c.args[0]['interface'] for c in engine.post_vlan.call_args_list], ['lo0', 'ge2'])
        self.assertEqual(engine.put_json.call_count, 2)
        self.assertEqual(diagnostics, {'Loopback binding': 'loopback_binding_unverified'})

    def test_exposed_loopback_allows_success(self):
        engine, vlans = self.staging_engine()
        engine.get_gateway_interfaces_v2.return_value = [dict(gateway_id='gw', interfaces=[dict(name='lo0', id='loopback')])]
        diagnostics = {}
        self.assertTrue(engine.process_vlans_for_site('site', 'gw', 1, vlans, {}, diagnostics=diagnostics))
        self.assertEqual(diagnostics, {})
        self.assertEqual(engine.post_vlan.call_count, 2)

    def test_ha_missing_or_ambiguous_loopback_marks_incomplete_without_blocking_posts(self):
        for peers in ([dict(gateway_id='gw-a', interfaces=[dict(name='lo0', id='loopback')])],
                      [dict(gateway_id='gw-a', interfaces=[dict(name='lo0', id='a'), dict(name='lo0', id='b')])]):
            engine, vlans = self.staging_engine()
            engine.get_gateway_interfaces_v2.return_value = peers
            diagnostics = {}
            self.assertTrue(engine.process_vlans_for_site('site', 'gw-a,gw-b', 1, vlans, {}, diagnostics=diagnostics))
            self.assertEqual(engine.post_vlan.call_count, 2)
            self.assertEqual(diagnostics['Loopback binding'], 'loopback_binding_unverified')

    def test_rejected_or_uncertain_loopback_continues_lan_without_retry(self):
        for failure in ((False, 'rejected'), RuntimeError('private response')):
            engine, vlans = self.staging_engine()
            engine.post_vlan.side_effect = [failure, (True, '')]
            diagnostics = {}
            self.assertFalse(engine.process_vlans_for_site('site', 'gw', 1, vlans, {}, diagnostics=diagnostics))
            self.assertEqual(engine.post_vlan.call_count, 2)
            self.assertEqual(engine.post_vlan.call_args_list[1].args[0]['interface'], 'ge2')
            self.assertEqual(engine.put_json.call_count, 1)
            self.assertEqual(engine.put_json.call_args.args[1]['name'], 'LAN')
            self.assertEqual(diagnostics['VLANs'], 'loopback_submission_failed')

    def test_inventory_failure_does_not_block_lan_configuration(self):
        engine, vlans = self.staging_engine()
        engine.get_gateway_interfaces_v2.side_effect = RuntimeError('private response')
        diagnostics = {}
        self.assertTrue(engine.process_vlans_for_site('site', 'gw', 1, vlans, {}, diagnostics=diagnostics))
        self.assertEqual(engine.post_vlan.call_count, 2)
        self.assertEqual(engine.put_json.call_count, 2)
        self.assertEqual(diagnostics['Loopback binding'], 'loopback_binding_unverified')

    def test_dry_run_has_no_network_writes_or_inventory_lookup(self):
        engine, vlans = self.staging_engine()
        self.assertTrue(engine.process_vlans_for_site('site', 'gw', 1, vlans, {}, dry_run=True))
        engine.post_vlan.assert_not_called()
        engine.put_json.assert_not_called()
        engine.get_gateway_interfaces_v2.assert_not_called()
        engine.list_site_vlans_v2.assert_not_called()
