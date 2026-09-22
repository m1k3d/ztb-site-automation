"""Deployment orchestration regressions using local CSVs and mocked API boundaries."""

import contextlib
import csv
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

import deployment_engine
from deployment_engine import DeploymentEngine
from automation_config import Settings
from input_validation import validate_csv


class DeploymentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.bulk = DeploymentEngine(Settings(
            ztb_api_base="https://example.invalid", bearer="offline-test",
            zpa_base_url="https://config.example.invalid", zpa_client_id="dummy", zpa_client_secret="dummy",
        ), emit=lambda message: print(message))
        self.addCleanup(self.bulk.client.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.csv_path = self.directory / "sites.csv"
        self.vlans_path = self.directory / "vlans.csv"
        self.vlan = {
            "name": "Users", "tag": "10", "subnet": "24",
            "default_gateway": "10.0.0.1", "interface": "ge5",
            "enabled": "true", "share_over_vpn": "true",
        }
        self.write_csv(self.vlans_path, [self.vlan])
        self.http = self.stack.enter_context(patch.object(
            requests.Session, "request", side_effect=AssertionError("Unexpected HTTP")
        ))
        self.login = self.stack.enter_context(patch(
            "subprocess.run", side_effect=AssertionError("Unexpected login subprocess")
        ))
        self.mock("time.sleep")
        self.mock("site_exists", return_value=False)
        self.create = self.mock("create_site", return_value=(True, "created", None))
        self.gateways = self.mock(
            "resolve_gateway_ids_and_cluster",
            side_effect=lambda name, **kw: (name + "-gw-a," + name + "-gw-b", 123),
        )
        self.lookup = self.mock("find_site_row_by_name", side_effect=lambda name: {"id": name + "-id"})
        self.post_vlan = self.mock("post_vlan", return_value=(True, "created"))
        self.list_vlans = self.mock(
            "list_site_vlans_v2",
            side_effect=lambda site_id: [{**self.vlan, "id": site_id + "-vlan"}],
        )
        self.put = self.mock("put_json", return_value=Mock(status_code=200, text=""))
        self.patch = self.mock("patch_json", return_value=Mock(status_code=200, text=""))
        self.interfaces = self.mock("get_gateway_interfaces_v2", side_effect=self.interface_inventory)
        self.vrrp = self.mock("post_vrrp", return_value=(True, "", 200))
        self.zpa = self.mock("zpa_provisioning.provision_zpa_for_site", return_value=True)
        self.zpa_preflight = self.mock("zpa_provisioning.prepare_zpa", return_value=Mock())

    def tearDown(self):
        # Catch unexpected HTTP even if application error handling caught the assertion.
        self.http.assert_not_called()
        self.login.assert_not_called()

    def mock(self, target, **kwargs):
        obj = deployment_engine if target.split(".")[0] in ("time", "zpa_provisioning") else self.bulk
        parts = target.split(".")
        for part in parts[:-1]:
            obj = getattr(obj, part)
        return self.stack.enter_context(patch.object(obj, parts[-1], **kwargs))

    @staticmethod
    def write_csv(path, rows):
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def row(self, name="Site-A", **overrides):
        return {
            "site_name": name, "template_id": "test-template", "location_type": "none",
            "gateway_name": name + "-gw-a", "gateway_name_b": name + "-gw-b",
            "wan_interface_name": "ge3", "wan0_ip": "192.0.2.2",
            "wan0_mask": "255.255.255.0", "wan0_gw": "192.0.2.1",
            "wan1_interface_name": "ge3", "wan1_ip": "198.51.100.2",
            "wan1_mask": "255.255.255.0", "wan1_gw": "198.51.100.1",
            "vlans_file": str(self.vlans_path), "private_dns": "", "wan_dns": "",
            "appc_provision": "0", "post": "1", **overrides,
        }

    def interface_inventory(self, site_id):
        name = site_id.removesuffix("-id")
        return [
            {"gateway_id": name + suffix, "interfaces": [
                {"name": "ge3", "interface_type": "wan"},
                {"name": "ge4", "interface_type": "ha"},
                {"name": "ge5", "interface_type": "lan"},
            ]}
            for suffix in ("-gw-a", "-gw-b")
        ]

    def run_batch(self, rows=None, dry_run=False):
        self.write_csv(self.csv_path, rows or [self.row()])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.result = self.bulk.run(validate_csv(self.csv_path), dry_run=dry_run)
        return self.result.exit_code, output.getvalue()

    def test_each_site_targets_its_own_vlans_and_ha_interfaces(self):
        for first_dns in ("", "10.1.0.1"):
            with self.subTest(first_private_dns=first_dns):
                self.put.reset_mock()
                self.patch.reset_mock()
                self.interfaces.reset_mock()
                status, output = self.run_batch([
                    self.row(private_dns=first_dns), self.row("Site-B"),
                ])
                self.assertEqual(status, 0, output)
                enable_urls = [c.args[0] for c in self.put.call_args_list if "/Network/update/" in c.args[0]]
                self.assertEqual(enable_urls, [
                    self.bulk.API_V2 + "/Network/update/Site-A-id-vlan?refresh_token=enabled",
                    self.bulk.API_V2 + "/Network/update/Site-B-id-vlan?refresh_token=enabled",
                ])
                self.assertEqual([c.args[1]["id"] for c in self.patch.call_args_list], ["Site-A-id-vlan", "Site-B-id-vlan"])
                self.assertEqual([c.args[0] for c in self.interfaces.call_args_list], ["Site-A-id", "Site-B-id"])

    def test_site_id_response_shapes_work_without_private_dns(self):
        for response in ({"cluster_info": {"site_id": "Site-A-id"}}, {"site_id": "Site-A-id"}, {"id": "Site-A-id"}):
            with self.subTest(response=response):
                self.lookup.side_effect = None
                self.lookup.return_value = response
                self.list_vlans.reset_mock()
                status, output = self.run_batch()
                self.assertEqual(status, 0, output)
                self.list_vlans.assert_called_once_with("Site-A-id")

    def test_unresolved_second_site_never_reuses_first_site_id(self):
        self.lookup.side_effect = lambda name: {"id": "Site-A-id"} if name == "Site-A" else None
        status, output = self.run_batch([self.row(), self.row("Site-B")])
        self.assertEqual(status, 1, output)
        self.list_vlans.assert_called_once_with("Site-A-id")
        self.interfaces.assert_called_once_with("Site-A-id")
        self.assertIn("Site-B", output)

    def test_successful_requested_stages_return_success(self):
        status, output = self.run_batch([self.row(private_dns="10.1.0.1", appc_provision="1")])
        self.assertEqual(status, 0, output)
        self.post_vlan.assert_called_once()
        self.vrrp.assert_called_once()
        self.zpa.assert_called_once()
        self.assertIn("Done. Deployment: OK=1 ERR=0", output)

    def test_vlan_creation_failure_returns_failure(self):
        self.post_vlan.return_value = (False, "fixture rejected VLAN")
        status, output = self.run_batch()
        self.assertEqual(status, 1, output)
        self.assertIn("fixture rejected VLAN", output)
        self.assertIn("PARTIAL: Site-A: incomplete stages: VLANs", output)
        self.assertIn("Done. Deployment: OK=0 ERR=1", output)

    def test_vlan_enable_and_sharing_failures_return_failure(self):
        for operation in (self.put, self.patch):
            for failure in (Mock(status_code=500, text="fixture failure"), requests.Timeout("fixture timeout")):
                with self.subTest(operation=operation, failure=failure):
                    operation.return_value = failure
                    operation.side_effect = failure if isinstance(failure, Exception) else None
                    try:
                        status, output = self.run_batch()
                        self.assertEqual(status, 1, output)
                    finally:
                        operation.side_effect = None
                        operation.return_value = Mock(status_code=200, text="")

    def test_missing_vlan_id_returns_failure(self):
        self.list_vlans.side_effect = None
        self.list_vlans.return_value = []
        status, output = self.run_batch()
        self.assertEqual(status, 1, output)
        self.put.assert_not_called()
        self.patch.assert_not_called()

    def test_private_dns_failure_returns_failure(self):
        self.put.side_effect = lambda url, *a, **kw: Mock(status_code=500 if "group-membership" in url else 200, text="fixture DNS failure")
        status, output = self.run_batch([self.row(private_dns="10.1.0.1")])
        self.assertEqual(status, 1, output)
        self.assertIn("Private DNS", output)

    def test_vrrp_validation_and_api_failures_return_failure(self):
        self.interfaces.side_effect = None
        self.interfaces.return_value = []
        status, output = self.run_batch()
        self.assertEqual(status, 1, output)
        self.vrrp.assert_not_called()
        self.interfaces.side_effect = self.interface_inventory
        self.vrrp.return_value = (False, "fixture VRRP failure", 500)
        status, output = self.run_batch()
        self.assertEqual(status, 1, output)
        self.assertIn("fixture VRRP failure", output)

    def test_zpa_failure_returns_failure_in_live_and_preview_modes(self):
        self.zpa.return_value = False
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                # Preview performs authentication/reference checks, never provisioning.
                self.zpa_preflight.side_effect = RuntimeError("fixture ZPA preflight failure") if dry_run else None
                status, output = self.run_batch([self.row(appc_provision="1")], dry_run=dry_run)
                self.assertEqual(status, 1, output)

    def test_zpa_login_exit_is_reported_and_next_site_continues(self):
        self.zpa.side_effect = SystemExit("fixture missing ZPA credentials")
        status, output = self.run_batch([self.row(appc_provision="1"), self.row("Site-B")])
        self.assertEqual(status, 1, output)
        self.assertEqual(self.create.call_count, 2)
        self.assertIn("fixture missing ZPA credentials", output)

    def test_missing_vlan_file_returns_failure_in_live_and_preview_modes(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run):
                status, output = self.run_batch([self.row(vlans_file=str(self.directory / "missing.csv"))], dry_run=dry_run)
                self.assertEqual(status, 1, output)

    def test_requested_stage_timeout_is_reported_and_next_site_continues(self):
        self.list_vlans.side_effect = [requests.Timeout("fixture read timeout"), [{**self.vlan, "id": "Site-B-id-vlan"}]]
        status, output = self.run_batch([self.row(), self.row("Site-B")])
        self.assertEqual(status, 1, output)
        self.assertEqual(self.create.call_count, 2)
        self.assertIn("fixture read timeout", output)
        self.assertIn("Done. Deployment: OK=1 ERR=1", output)

    def test_create_and_gateway_timeouts_are_reported_and_next_site_continues(self):
        for target, success in ((self.create, (True, "created", None)), (self.gateways, ("Site-B-gw-a,Site-B-gw-b", 123))):
            with self.subTest(stage=target):
                self.create.reset_mock()
                self.create.side_effect = None
                self.gateways.side_effect = lambda name, **kw: (name + "-gw-a," + name + "-gw-b", 123)
                target.side_effect = [requests.Timeout("fixture stage timeout"), success]
                status, output = self.run_batch([self.row(), self.row("Site-B")])
                self.assertEqual(status, 1, output)
                self.assertEqual(self.create.call_count, 2)
                self.assertIn("fixture stage timeout", output)

    def test_no_optional_stages_is_success(self):
        self.gateways.side_effect = None
        self.gateways.return_value = ("standalone-gw", 123)
        status, output = self.run_batch([self.row(
            vlans_file="", gateway_name_b="", wan1_ip="", wan1_mask="",
            wan1_gw="", wan1_interface_name="",
        )])
        self.assertEqual(status, 0, output)
        self.lookup.assert_not_called()
        self.post_vlan.assert_not_called()
        self.vrrp.assert_not_called()
        self.zpa.assert_not_called()

    def test_preview_does_not_call_deployment_writes(self):
        status, output = self.run_batch(dry_run=True)
        self.assertEqual(status, 0, output)
        for operation in (self.create, self.post_vlan, self.put, self.patch, self.vrrp):
            operation.assert_not_called()


if __name__ == "__main__":
    unittest.main()
