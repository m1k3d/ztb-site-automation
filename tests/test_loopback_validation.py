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

    def test_missing_loopback_stops_before_network_writes(self):
        from unittest.mock import Mock
        engine = object.__new__(DeploymentEngine)
        engine.get_gateway_interfaces_v2 = Mock(return_value=[{
            'gateway_id': 'gw', 'interfaces': [{'id': 'physical-id', 'name': 'ge1'}]}])
        engine.log = Mock()
        engine.post_vlan = Mock()
        engine.list_site_vlans_v2 = Mock()
        self.assertFalse(engine.process_vlans_for_site('site', 'gw', 1,
            [{'name': 'MGMT', 'interface': 'lo0'}], {}))
        engine.post_vlan.assert_not_called()
        engine.list_site_vlans_v2.assert_not_called()

    def test_ha_requires_loopback_on_each_gateway(self):
        from unittest.mock import Mock
        engine = object.__new__(DeploymentEngine)
        engine.get_gateway_interfaces_v2 = Mock(return_value=[{
            'gateway_id': 'gw-a', 'interfaces': [{'id': 'loopback-a', 'name': 'lo0'}]}])
        engine.log = Mock()
        engine.post_vlan = Mock()
        self.assertFalse(engine.process_vlans_for_site('site', 'gw-a,gw-b', 1,
            [{'name': 'MGMT', 'interface': 'lo0,lo0'}], {}))
        engine.post_vlan.assert_not_called()
