"""Activation WAN from the site-creation marker when a gateway has several WANs; offline."""
import unittest

from additional_wans import from_reference
from pull_site import site_to_csv_row


def wan(interface, gateway='gw-a', cluster=490, name=None, description='', dhcp=True):
    return dict(zone='WAN Zone', interface=interface, gateway_id=gateway, cluster_id=cluster, tag='1',
                name=name or f'wan1-{interface}', display_name=name or f'wan1-{interface}', description=description,
                dhcp_client=dhcp, default_gateway='0.0.0.0', subnet='32', wan_nexthop_ip='')


def site(gateways):
    return {'gateways': gateways, 'template_name': 'zt600-edwardjones'}


class ActivationWanTests(unittest.TestCase):
    def test_two_dhcp_wans_use_the_site_creation_marker(self):
        # Branch-EdwardJones: no WAN interface or gateway IP reported before activation.
        gateways = [{'gateway_id': 'gw-a', 'gateway_name': 'branch-edwardjones-gw-st', 'template_gateway_id': 'Gateway-1'}]
        networks = [wan('ge6', name='wan1-bran-dfGoYK'),
                    wan('ge5', name='WAN-490-branch-edwardjones-gw-st', description='Gatway WAN Interface')]
        fields = site_to_csv_row(site(gateways), 'Branch-EdwardJones', '', wan_networks=networks)
        self.assertEqual(fields['wan_interface_name'], 'ge5')
        extras = from_reference(networks, gateways, fields)
        self.assertEqual([(w['interface'], w['mode']) for w in extras], [('ge6', 'dhcp')])

    def test_name_marker_alone_is_enough(self):
        gateways = [{'gateway_id': 'gw-a', 'gateway_name': 'Site-GW'}]
        networks = [wan('ge5', name='wan-2', cluster=12), wan('ge6', name='WAN-12-Site-GW', cluster=12)]
        self.assertEqual(site_to_csv_row(site(gateways), 'Site', '', wan_networks=networks)['wan_interface_name'], 'ge6')

    def test_each_ha_gateway_uses_its_own_marker(self):
        gateways = [{'gateway_id': 'gw-a', 'gateway_name': 'Site-GW-01', 'template_gateway_id': 'Gateway-1'},
                    {'gateway_id': 'gw-b', 'gateway_name': 'Site-GW-02', 'template_gateway_id': 'Gateway-2'}]
        networks = [wan('ge8', 'gw-a'), wan('ge7', 'gw-a', name='WAN-452-Site-GW-01', description='Gatway WAN Interface'),
                    wan('ge8', 'gw-b'), wan('ge7', 'gw-b', name='WAN-452-Site-GW-02', description='Gatway WAN Interface')]
        fields = site_to_csv_row(site(gateways), 'Site', '', wan_networks=networks)
        self.assertEqual((fields['wan_interface_name'], fields['wan1_interface_name']), ('ge7', 'ge7'))

    def test_without_a_marker_the_choice_stays_unresolved(self):
        gateways = [{'gateway_id': 'gw-a', 'gateway_name': 'Site-GW'}]
        networks = [wan('ge5'), wan('ge6')]
        self.assertEqual(site_to_csv_row(site(gateways), 'Site', '', wan_networks=networks)['wan_interface_name'], '')


if __name__ == '__main__':
    unittest.main()
