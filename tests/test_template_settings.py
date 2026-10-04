"""Template prerequisites fail before creation, including inherited DHCP relay."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch
import requests

from automation_config import Settings
from deployment_engine import DeploymentEngine
from input_validation import validate_rows
from ui_deployment import plan_digest, public_issues


class TemplateSettingsTests(unittest.TestCase):
    def setUp(self):
        block=patch.object(requests.Session,'request',side_effect=AssertionError('Unexpected HTTP'))
        self.http=block.start();self.addCleanup(block.stop)
        self.e=DeploymentEngine(Settings(ztb_api_base='https://example.invalid',bearer='offline'),emit=lambda *_:None)
        self.addCleanup(self.e.client.close)
        self.template=dict(id='template',name='Reference',platform_type='zt800',deployment_type='standalone',dhcp_service='server')
        self.e.get_json=Mock(side_effect=lambda *a,**k:deepcopy(self.template))
        self.e.post_json=Mock();self.e.create_site=Mock()

    def tearDown(self):
        self.http.assert_not_called()

    def row(self,**values):
        return dict(site_name='Branch',gateway_name='Branch-GW',wan_interface_name='ge7',
                    template_id='template',post='1',location_type='none',**values)

    def plan(self,**values):
        return self.e.plan(validate_rows([self.row(**values)]))

    def test_inherited_relay_requires_server_before_clone_even_with_vlan_dhcp_off(self):
        self.template['dhcp_service']='relay'
        plan=self.plan(template_mode='clone',new_template_name='Branch',vlans=[dict(name='Print',tag='20',
            interface='ge6',default_gateway='10.20.0.1',subnet='24',dhcp_service='off')])
        self.assertEqual(plan.issues[0].field,'template_dhcp_relay')
        self.e.execute(plan)
        self.e.post_json.assert_not_called();self.e.create_site.assert_not_called()
        self.assertIn('even when',public_issues(plan.issues)[0]['message'])

    def test_mismatched_explicit_mode_is_not_sent_as_an_ignored_override(self):
        self.template['dhcp_service']='relay'
        self.assertEqual(self.plan(dhcp_service_mode='server').issues[0].field,'template_dhcp_mode')

    def test_relay_with_explicit_address_uses_documented_creation_fields(self):
        self.template['dhcp_service']='relay'
        plan=self.plan(dhcp_service_mode='inherit',dhcp_server_ip='10.1.0.10')
        self.assertFalse(plan.issues)
        self.assertEqual(plan.sites[0].payload['dhcp_server_ip'],'10.1.0.10')
        self.assertNotIn('dhcp_service',plan.sites[0].payload)

    def test_both_ha_modes_require_two_gateways_and_standalone_requires_one(self):
        for mode in ('standard_mode_ha','wan_edge_mode_ha'):
            self.template['deployment_type']=mode
            self.assertEqual(self.plan().issues[0].field,'template_gateway_count')
            self.assertFalse(self.plan(gateway_name_b='Branch-GW-B',wan1_interface_name='ge7').issues)
        self.template['deployment_type']='standalone'
        self.assertEqual(self.plan(gateway_name_b='Branch-GW-B',wan1_interface_name='ge7').issues[0].field,'template_gateway_count')

    def test_template_lookup_cached_within_batch_but_reloaded_on_new_preview(self):
        second={**self.row(),'site_name':'Other','gateway_name':'Other-GW'}
        plan=self.e.plan(validate_rows([self.row(),second]))
        self.assertFalse(plan.issues)
        self.assertEqual(self.e.get_json.call_count,1)
        self.plan();self.assertEqual(self.e.get_json.call_count,2)

    def test_template_settings_change_invalidates_reviewed_digest(self):
        first=self.plan(dhcp_service_mode='inherit',dhcp_server_ip='10.1.0.10')
        self.template['dhcp_service']='relay'
        second=self.plan(dhcp_service_mode='inherit',dhcp_server_ip='10.1.0.10')
        self.assertFalse(first.issues or second.issues)
        self.assertNotEqual(plan_digest(first),plan_digest(second))

    def test_unknown_template_settings_or_failed_read_block_preflight(self):
        self.template['deployment_type']='unknown'
        self.assertEqual(self.plan().issues[0].field,'template_settings')
        self.e.get_json.side_effect=RuntimeError('private-token response')
        plan=self.plan()
        self.assertEqual(plan.issues[0].field,'template_settings')
        self.assertNotIn('private-token',str(public_issues(plan.issues)))
        self.e.execute(plan);self.e.create_site.assert_not_called()
