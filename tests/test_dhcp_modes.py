"""Inactive scopes must not affect validation or reach deployment payloads."""
import copy
import unittest

from deployment_engine import DeploymentEngine
from input_validation import validate_vlan_rows


class DhcpModeTests(unittest.TestCase):
    def vlan(self, **values):
        return dict(name="Users", tag="20", subnet="24", default_gateway="10.20.0.1",
                    interface="ge2", **values)

    def test_off_ignores_all_scope_representations_without_mutating_input(self):
        for mode in ("off", "no_dhcp", " OFF "):
            for scope in (dict(dhcp_start="invalid", dhcp_end=""),
                          dict(dhcp_start="10.30.0.1", dhcp_end="10.30.0.255"),
                          dict(dhcp_range="bad-range"), dict(range_list=[["bad", "values"]])):
                with self.subTest(mode=mode, scope=scope):
                    row = self.vlan(dhcp_service=mode, **scope)
                    original = copy.deepcopy(row)
                    issues = []
                    result = validate_vlan_rows([(1, row)], "test", issues)
                    self.assertEqual(issues, [])
                    self.assertEqual(result[0]["dhcp_service"], "no_dhcp")
                    self.assertNotIn("dhcp_range", result[0])
                    self.assertEqual(row, original)

    def test_active_scopes_still_validate_and_blank_service_still_infers_dhcp(self):
        for mode in ("on", "inherit", "non_airgapped", ""):
            issues = []
            validate_vlan_rows([(1, self.vlan(dhcp_service=mode, dhcp_start="bad", dhcp_end="10.20.0.50"))], "test", issues)
            self.assertTrue(any(issue.field == "dhcp_start" for issue in issues))
        issues = []
        result = validate_vlan_rows([(1, self.vlan(dhcp_start="10.20.0.10", dhcp_end="10.20.0.20"))], "test", issues)
        self.assertEqual(issues, [])
        self.assertEqual(result[0]["dhcp_service"], "inherit")
        self.assertEqual(result[0]["dhcp_range"], "10.20.0.10-10.20.0.20")

    def test_off_still_validates_gateway(self):
        row = self.vlan(dhcp_service="off", dhcp_start="ignored")
        row["default_gateway"] = "invalid"
        issues = []
        validate_vlan_rows([(1, row)], "test", issues)
        self.assertEqual({issue.field for issue in issues}, {"default_gateway"})

    def test_payload_discards_off_scope_even_without_prior_validation(self):
        engine = object.__new__(DeploymentEngine)
        for mode, expected in (("off", ""), ("no_dhcp", ""), ("inherit", "10.20.0.10-10.20.0.20")):
            row = self.vlan(dhcp_service=mode, dhcp_range="10.20.0.10-10.20.0.20")
            payload = engine.vlan_to_v2_payload(row, "gateway", 1)
            self.assertEqual(payload["dhcp_range"], expected)
