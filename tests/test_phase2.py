"""Offline preflight, configuration, and reusable-engine contracts."""

import contextlib
import csv
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from api_client import ZTBClient
from automation_config import Settings, normalize_base
from bulk_create import main
from deployment_engine import DeploymentEngine
from input_validation import validate_csv, validate_rows
from site_payload import build_site_payload
import zpa_provisioning


def site(name="Branch", **values):
    return {"site_name": name, "gateway_name": name + "-gw", "wan_interface_name": "ge3",
            "location_type": "none", "template_id": "fixture-template", "post": "1", **values}


def vlan(**values):
    return {"name": "Users", "tag": "10", "subnet": "24", "default_gateway": "10.0.0.1",
            "interface": "ge5", "dhcp_start": "10.0.0.10", "dhcp_end": "10.0.0.20", **values}


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.http = self.stack.enter_context(patch.object(requests.Session, "request", side_effect=AssertionError("Unexpected HTTP")))

    def tearDown(self):
        self.http.assert_not_called()

    def write_csv(self, name, rows):
        path = self.directory / name
        fields = list(dict.fromkeys(k for row in rows for k in row))
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path


class ValidationTests(OfflineTests):
    def test_normalizes_valid_csv_and_resolves_paths_relative_to_site_csv(self):
        self.write_csv("networks.csv", [vlan()])
        path = self.write_csv("sites.csv", [site(vlans_file="networks.csv", dhcp_server_ip="10.1.0.1")])
        result = validate_csv(path)
        self.assertTrue(result.valid, result.issues)
        self.assertEqual(result.sites[0].row["dhcp_service_mode"], "relay")
        self.assertEqual(result.sites[0].vlans[0]["dhcp_range"], "10.0.0.10-10.0.0.20")
        self.assertEqual(result.sites[0].row["vlans_file"], str(self.directory / "networks.csv"))

    def test_selected_rows_only_and_post_typos_not_silently_skipped(self):
        self.assertTrue(validate_rows([{"post": "0"}, site()]).valid)
        for row in ({"post": "true"}, {"site_name": "missing post"}):
            self.assertEqual(validate_rows([row]).issues[0].field, "post")

    def test_site_errors_are_aggregated_with_source_row_and_field(self):
        result = validate_rows([site(), site("Bad", wan0_ip="not-ip", dhcp_service_mode="relay", appc_provision="typo")], source="input.csv")
        fields = {e.field for e in result.issues}
        self.assertTrue({"wan0_ip", "wan0_mask", "wan0_gw", "dhcp_server_ip", "appc_provision"} <= fields)
        self.assertTrue(all(e.source == "input.csv" and e.row == 3 for e in result.issues))

    def test_duplicate_site_and_gateway_names(self):
        result = validate_rows([site(), site("branch", gateway_name="BRANCH-GW")])
        self.assertEqual({e.field for e in result.issues}, {"site_name", "gateway_name"})

    def test_ha_dhcp_and_static_wans(self):
        row = site(gateway_name_b="Branch-b", wan1_interface_name="ge3")
        self.assertTrue(validate_rows([row]).valid)
        row.update(wan1_ip="192.0.2.2", wan1_mask="24", wan1_gw="192.0.2.1")
        result = validate_rows([row])
        self.assertTrue(result.valid, result.issues)
        self.assertEqual(result.sites[0].row["wan1_mask"], "255.255.255.0")
        for change in ({"gateway_name_b": ""}, {"wan1_interface_name": ""}, {"wan1_gw": "198.51.100.1"}):
            self.assertFalse(validate_rows([{**row, **change}]).valid)

    def test_invalid_site_values(self):
        for values in ({"wan_dns": "not-ip"}, {"private_dns": "10.0.0.1/99"}, {"dhcp_server_ip": "bad"},
                       {"location_type": "bad"}, {"location_type": "new"}, {"location_type": "existing"},
                       {"location_type": "new", "country": "NL", "location_template_id": "0"},
                       {"vrrp_vrid": "256"}, {"vrrp_link_interface": "bad interface"}):
            with self.subTest(values=values):
                self.assertFalse(validate_rows([site(**values)]).valid)

    def test_invalid_vlan_values(self):
        for values in ({"tag": "4095"}, {"tag": "NaN"}, {"subnet": "33"}, {"default_gateway": "10.0.0.0"},
                       {"dhcp_start": "10.0.0.22"}, {"dhcp_end": "10.0.1.25"}, {"dhcp_start": "10.0.0.1"},
                       {"dhcp_end": ""}, {"enabled": "maybe"}, {"share_over_vpn": "typo"},
                       {"dhcp_service": "typo"}, {"interface": "bad interface"}):
            with self.subTest(values=values):
                result = validate_rows([site(vlans=[vlan(**values)])])
                self.assertFalse(result.valid)
                self.assertTrue(all("VLANs" in e.source for e in result.issues))

    def test_duplicate_vlan_tags_on_same_interface(self):
        self.assertFalse(validate_rows([site(vlans=[vlan(), vlan(name="Other")])]).valid)
        self.assertTrue(validate_rows([site(vlans=[vlan(), vlan(name="Other", interface="ge6")])]).valid)

    def test_truncated_vlan_name_collision_is_rejected(self):
        result = validate_rows([site(vlans=[vlan(name="abcdefghijklmnop-A"), vlan(name="abcdefghijklmnop-B", tag="20")])])
        self.assertIn("16-character", str(result.issues[0]))

    def test_json_input_legacy_wrappers_and_bool_values(self):
        for data in ([vlan(enabled=False)], {"rows": [vlan()]}, {"result": {"rows": [vlan()]}}, {"vlans": [vlan()]}):
            path = self.directory / "networks.json"
            path.write_text(json.dumps(data))
            result = validate_rows([site(vlans_file=str(path))])
            self.assertTrue(result.valid, result.issues)
        path.write_text('{"unexpected": []}')
        self.assertFalse(validate_rows([site(vlans_file=str(path))]).valid)

    def test_malformed_csv_and_missing_files_are_errors(self):
        path = self.directory / "sites.csv"
        for text in ("", "post,post\n1,1\n", "post,site_name\n1\n", 'post,site_name\n1,"unclosed', "post\n1,extra\n"):
            path.write_text(text)
            self.assertFalse(validate_csv(path).valid)
        self.assertFalse(validate_csv(self.directory / "missing.csv").valid)

    def test_cli_validation_and_empty_selection_need_no_credentials(self):
        for rows in ([site(vlans=[vlan()])], [{"post": "0"}]):
            # CSV path intentionally uses no inline JSON field.
            path = self.write_csv("sites.csv", [{k: v for k, v in r.items() if k != "vlans"} for r in rows])
            with patch("bulk_create.Settings.load", side_effect=AssertionError("Credentials accessed")), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--csv", str(path), "--validate-only"]), 0)

    def test_payload_preserves_special_characters_and_json_types(self):
        row = site('A "quoted" & \\ branch')
        payload = build_site_payload(row, {"location_type": "none"})
        self.assertEqual(json.loads(json.dumps(payload))["name"], row["site_name"])
        self.assertIs(payload["gateways"][0]["is_wan_dhcp"], True)


class PreflightTests(OfflineTests):
    def setUp(self):
        super().setUp()
        self.engine = DeploymentEngine(Settings(ztb_api_base="https://example.invalid", bearer="offline"), emit=lambda _: None)
        self.addCleanup(self.engine.client.close)
        self.stack.enter_context(patch.object(self.engine, "site_exists", return_value=False))
        self.create = self.stack.enter_context(patch.object(self.engine, "create_site", return_value=(True, "created", None)))
        self.lookup = self.stack.enter_context(patch.object(self.engine, "resolve_gateway_ids_and_cluster", return_value=("gw", 123)))

    def test_invalid_last_row_blocks_entire_batch_before_api_reads(self):
        result = self.engine.run(validate_rows([site(), site("Later", wan_dns="bad")]))
        self.assertEqual(result.exit_code, 1)
        self.create.assert_not_called()
        self.lookup.assert_not_called()

    def test_missing_last_vlan_file_blocks_entire_batch(self):
        result = self.engine.run(validate_rows([site(), site("Later", vlans_file=str(self.directory / "missing.csv"))]))
        self.assertEqual(result.exit_code, 1)
        self.create.assert_not_called()

    def test_later_template_failure_blocks_earlier_valid_site(self):
        with patch.object(self.engine, "get_json_v3_templates", return_value=[]):
            result = self.engine.run(validate_rows([site(), site("Later", template_id="", template_name="missing")]))
        self.assertEqual(result.exit_code, 1)
        self.create.assert_not_called()
        self.assertEqual(result.issues[0].row, 3)

    def test_later_location_failure_blocks_earlier_valid_site(self):
        with patch.object(self.engine, "get_json_v3_locations", return_value=[]):
            result = self.engine.run(validate_rows([site(), site("Later", location_type="existing", zia_location_name="missing")]))
        self.assertEqual(result.exit_code, 1)
        self.create.assert_not_called()

    def test_zpa_config_checked_before_ztb_lookups_or_writes(self):
        result = self.engine.run(validate_rows([site(), site("Later", appc_provision="1")]))
        self.assertEqual(result.exit_code, 1)
        self.create.assert_not_called()

    def test_zpa_preflight_failure_blocks_entire_batch(self):
        engine = DeploymentEngine(Settings(
            ztb_api_base="https://example.invalid", bearer="offline", zpa_base_url="https://config.example.invalid",
            zpa_client_id="dummy", zpa_client_secret="dummy",
        ), emit=lambda _: None)
        self.addCleanup(engine.client.close)
        with patch("zpa_provisioning.prepare_zpa", side_effect=ValueError("certificate unavailable")), patch.object(engine, "create_site") as create:
            result = engine.run(validate_rows([site(), site("Later", appc_provision="1")]))
        self.assertEqual(result.exit_code, 1)
        create.assert_not_called()

    def test_malformed_location_response_does_not_infer_new_location(self):
        with patch.object(self.engine, "get_json", return_value={"unexpected": []}):
            result = self.engine.run(validate_rows([site(location_type="auto", zia_location_name="Existing", country="Netherlands")]))
        self.assertEqual(result.exit_code, 1)
        self.assertIn("refusing to infer", str(result.issues[0]))
        self.create.assert_not_called()

    def test_ambiguous_or_invalid_location_records_block_creation(self):
        for records in ([{"name": "Existing", "id": 1}, {"name": "existing", "id": 2}], [{"name": "Existing", "id": None}]):
            with patch.object(self.engine, "get_json_v3_locations", return_value=records):
                result = self.engine.run(validate_rows([site(location_type="auto", zia_location_name="Existing", country="Netherlands")]))
            self.assertEqual(result.exit_code, 1)
        self.create.assert_not_called()

    def test_snapshot_execution_does_not_reread_vlan_file(self):
        path = self.write_csv("vlans.csv", [vlan()])
        plan = self.engine.plan(validate_rows([site(vlans_file=str(path))]))
        path.write_text("malformed replacement")
        with patch.object(self.engine, "resolve_site_id", return_value="site-id"), patch.object(self.engine, "process_vlans_for_site", return_value=True) as process:
            result = self.engine.execute(plan)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(process.call_args.args[3][0]["tag"], "10")

    def test_all_lookups_precede_first_create(self):
        order = []
        def templates():
            order.append("templates")
            return [{"name": "Fixture", "id": "fixture"}]
        def locations():
            order.append("locations")
            return [{"name": "Existing", "id": 42}]
        self.create.side_effect = lambda *a: (order.append("create") or True, "created", None)
        with patch.object(self.engine, "get_json_v3_templates", side_effect=templates), patch.object(self.engine, "get_json_v3_locations", side_effect=locations):
            result = self.engine.run(validate_rows([site(template_id="", template_name="Fixture"), site("Later", location_type="existing", zia_location_name="Existing")]))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(order, ["templates", "locations", "create", "create"])

    def test_plan_is_snapshot_and_rejects_other_engine(self):
        validation = validate_rows([site(vlans=[vlan()])])
        plan = self.engine.plan(validation)
        validation.sites[0].vlans[0]["tag"] = "99"
        validation.sites[0].row["site_name"] = "Changed"
        self.assertEqual(plan.sites[0].vlans[0]["tag"], "10")
        self.assertEqual(plan.sites[0].payload["name"], "Branch")
        other = DeploymentEngine(self.engine.config, emit=lambda _: None)
        self.addCleanup(other.client.close)
        with self.assertRaisesRegex(ValueError, "same engine"):
            other.execute(plan)

    def test_dry_run_no_provisioning_and_returns_structured_result(self):
        result = self.engine.run(validate_rows([site()]), dry_run=True)
        self.assertEqual(result.sites[0].status, "preview")
        self.create.assert_not_called()


class ConfigurationTests(OfflineTests):
    def test_environment_precedence_without_mutating_environment(self):
        path = self.directory / "test.env"
        path.write_text('ZTB_API_BASE="https://example.invalid"\nAPI_KEY="file-key"\nBEARER="AUTO_POPULATED"\n')
        before = dict(os.environ)
        config = Settings.load(path, environ={"API_KEY": "environment-key"})
        self.assertEqual(config.api_key, "environment-key")
        self.assertEqual(config.bearer, "")
        self.assertEqual(config.env_path, path)
        self.assertEqual(os.environ, before)
        self.assertNotIn("environment-key", repr(config))

    def test_base_urls_and_config_errors(self):
        self.assertEqual(normalize_base("https://example.invalid/api/v3/"), "https://example.invalid")
        self.assertEqual(normalize_base("api.example.invalid", zpa=True), "https://config.example.invalid")
        for url in ("", "http://example.invalid", "https://user:pass@example.invalid", "https://<tenant>.invalid", "https://example.invalid/other"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_base(url)
        self.assertTrue(Settings().errors())

    def test_single_401_refresh_uses_same_selected_settings(self):
        config = Settings(ztb_api_base="https://example.invalid", bearer="expired", api_key="dummy")
        session = Mock(headers={})
        session.request.side_effect = [Mock(status_code=401), Mock(status_code=401)]
        client = ZTBClient(config, session=session, emit=lambda _: None)
        with patch("api_client.ztb_login", return_value=("fresh", None)) as login:
            response = client.request("GET", client.api_v3 + "/templates")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(session.request.call_count, 2)
        login.assert_called_once_with(config=config, write_env=True, quiet=True)
        self.assertEqual(session.headers["Authorization"], "Bearer fresh")

    def test_imports_and_help_do_not_load_settings_or_create_directories(self):
        with patch.object(Settings, "load", side_effect=AssertionError("Unexpected credential read")), patch.object(Path, "mkdir", side_effect=AssertionError("Unexpected mkdir")):
            for name in ("bulk_create", "pull_site", "ztb_login", "zpa_login", "zpa_provisioning"):
                module = importlib.reload(importlib.import_module(name))
                if hasattr(module, "main"):
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit_info:
                        module.main(["--help"])
                    self.assertEqual(exit_info.exception.code, 0)

    def test_zpa_preflight_only_authenticates_and_reads_certificate(self):
        config = Settings(zpa_base_url="https://config.example.invalid", zpa_client_id="dummy", zpa_client_secret="dummy", zpa_customer_id="42")
        with patch("zpa_provisioning.zpa_login.zpa_login", return_value=("fixture-token", None)), patch("zpa_provisioning.get_enrollment_cert_id", return_value="cert"), patch("zpa_provisioning.create_app_connector_group") as group, patch("zpa_provisioning.create_provisioning_key") as key:
            context = zpa_provisioning.prepare_zpa(config)
        group.assert_not_called()
        key.assert_not_called()
        self.assertNotIn("fixture-token", repr(context))

    def test_login_uses_explicit_settings_and_only_writes_selected_file(self):
        import ztb_login
        import zpa_login
        config = Settings(
            ztb_api_base="https://example.invalid", api_key="dummy", env_path=self.directory / "selected.env",
            zpa_base_url="https://config.example.invalid", zpa_client_id="dummy-id", zpa_client_secret="dummy-secret",
        )
        for module, function, body, expected in (
            (ztb_login, ztb_login.ztb_login, {"result": {"delegate_token": "ztb-token", "expires_in": 3600}}, "ztb-token"),
            (zpa_login, zpa_login.zpa_login, {"access_token": "zpa-token", "expires_in": 3600}, "zpa-token"),
        ):
            response = Mock()
            response.json.return_value = body
            with patch.object(module.requests, "post", return_value=response), patch.object(module, "write_tokens") as write:
                token, _ = function(config=config, quiet=True)
            self.assertEqual(token, expected)
            self.assertEqual(write.call_args.args[0], config.env_path)

    def test_malformed_auth_response_never_persists_token(self):
        import ztb_login
        import zpa_login
        config = Settings(ztb_api_base="https://example.invalid", api_key="dummy", zpa_base_url="https://config.example.invalid", zpa_client_id="dummy", zpa_client_secret="dummy")
        for module, function in ((ztb_login, ztb_login.ztb_login), (zpa_login, zpa_login.zpa_login)):
            response = Mock()
            response.json.return_value = {"error": "not a token"}
            with patch.object(module.requests, "post", return_value=response), patch.object(module, "write_tokens") as write, self.assertRaises(RuntimeError):
                function(config=config, quiet=True)
            write.assert_not_called()

    def test_zpa_customer_mismatch_blocks_certificate_lookup(self):
        config = Settings(zpa_base_url="https://config.example.invalid", zpa_client_id="dummy", zpa_client_secret="dummy", zpa_customer_id="42")
        with patch("zpa_provisioning.zpa_login.zpa_login", return_value=("fixture-token", None)), patch("zpa_provisioning.get_customer_id", return_value="99"), patch("zpa_provisioning.get_enrollment_cert_id") as cert, self.assertRaisesRegex(ValueError, "does not match"):
            zpa_provisioning.prepare_zpa(config)
        cert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
