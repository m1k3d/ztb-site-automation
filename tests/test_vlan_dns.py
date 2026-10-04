import unittest
from unittest.mock import Mock

from deployment_engine import DeploymentEngine
from input_validation import validate_vlan_rows
from pull_site import vlans_to_csv_rows


class VlanDnsTests(unittest.TestCase):
    def vlan(self, **values):
        return dict(name="Users", tag="10", subnet="24", default_gateway="10.20.10.1",
                    interface="ge2", dhcp_service="inherit", dhcp_start="10.20.10.2",
                    dhcp_end="10.20.10.254", **values)

    def test_optional_dns_is_validated_and_preserved(self):
        issues = []
        rows = validate_vlan_rows([(1, self.vlan(per_network_dns="10.20.10.1, 1.1.1.1"))], "test", issues)
        self.assertEqual(issues, [])
        self.assertEqual(rows[0]["per_network_dns"], "10.20.10.1,1.1.1.1")
        for value in ("bad", "10.20.10.999", "1.1.1.1,", "10.20.10.0/24"):
            issues = []
            validate_vlan_rows([(1, self.vlan(per_network_dns=value))], "test", issues)
            self.assertTrue(any(issue.field == "per_network_dns" for issue in issues))

    def test_old_csv_and_blank_dns_keep_wan_dns_fallback(self):
        engine = object.__new__(DeploymentEngine)
        for fields in ({}, {"per_network_dns": ""}):
            issues = []
            vlan = validate_vlan_rows([(1, self.vlan(**fields))], "test", issues)[0]
            self.assertEqual(issues, [])
            payload = engine.vlan_to_v2_payload(vlan, "gateway", 1, "1.1.1.1")
            self.assertEqual(payload["per_network_dns"], "1.1.1.1")

    def test_each_vlan_dns_survives_creation_and_enable_update(self):
        engine = object.__new__(DeploymentEngine)
        engine.API_V2 = "https://example.invalid/api/v2"
        engine.ORIGIN = engine.REFERER = "https://example.invalid"
        engine.log = Mock()
        vlans = [self.vlan(per_network_dns="10.20.10.1"), self.vlan(per_network_dns="9.9.9.9")]
        vlans[1].update(name="Printers", tag="20", default_gateway="10.20.20.1")
        engine.post_vlan = Mock(return_value=(True, ""))
        engine.list_site_vlans_v2 = Mock(return_value=[dict(vlan, id=str(index)) for index, vlan in enumerate(vlans)])
        engine.put_json = Mock(return_value=Mock(status_code=200))
        self.assertTrue(engine.process_vlans_for_site("site", "gateway", 1, vlans, {"wan_dns": "1.1.1.1"}))
        self.assertEqual([call.args[0]["per_network_dns"] for call in engine.post_vlan.call_args_list], ["10.20.10.1", "9.9.9.9"])
        self.assertEqual([call.args[1]["per_network_dns"] for call in engine.put_json.call_args_list], ["10.20.10.1", "9.9.9.9"])

    def test_reference_export_preserves_per_vlan_dns(self):
        rows = vlans_to_csv_rows([self.vlan(per_network_dns="10.20.10.1"), self.vlan()])
        self.assertEqual(rows[0]["per_network_dns"], "10.20.10.1")
        self.assertEqual(rows[1]["per_network_dns"], "")
