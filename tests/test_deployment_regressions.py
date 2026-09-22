"""Offline regressions based on the captured GET response and successful POST.

Only template names/IDs are retained in the response fixture. No credentials or
browser HAR are included. All HTTP is blocked unless explicitly mocked.
"""

import contextlib
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from automation_config import Settings
from deployment_engine import DeploymentEngine, LocationTemplateResolver
from input_validation import validate_rows
from location_config import prepare_location_context
from site_payload import build_site_payload

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    # Legacy scripts initialize auth on import. Use an empty working directory
    # and dummy credentials so tests never read the user's .env or log in.
    with tempfile.TemporaryDirectory() as directory:
        with contextlib.chdir(directory), patch.dict(
            os.environ,
            {"ZTB_API_BASE": "https://example.invalid", "BEARER": "offline-test"},
            clear=True,
        ), patch("dotenv.load_dotenv"), patch(
            "subprocess.run", side_effect=AssertionError("Unexpected login subprocess")
        ), patch.object(requests.Session, "request", side_effect=AssertionError("Unexpected HTTP")), patch.object(Path, "mkdir"):
            spec = importlib.util.spec_from_file_location("tested_" + name, ROOT / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module


class DeploymentRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template_response = json.loads(
            (ROOT / "tests/fixtures/location_templates.json").read_text()
        )

    def setUp(self):
        self.bulk = DeploymentEngine(Settings(ztb_api_base="https://example.invalid", bearer="offline-test"), emit=lambda message: print(message))
        self.addCleanup(self.bulk.client.close)
        exists = patch.object(self.bulk, "site_exists", return_value=False)
        exists.start()
        self.addCleanup(exists.stop)
        self.http_block = patch.object(requests.Session, "request", side_effect=AssertionError("Unexpected HTTP"))
        self.http_block.start()
        self.addCleanup(self.http_block.stop)

    def row(self, **overrides):
        return {
            "site_name": "newsite1234", "country": "United States",
            "zia_location_name": "newsite1234", "location_type": "new",
            "location_template_name": "Default Location Template",
            "template_id": "606851e4-4851-11f0-8ae3-06463ca92c5d",
            "gateway_name": "newsite1234-gw-st", "gateway_name_b": "",
            "wan_interface_name": "ge5", "wan0_ip": "", "wan_dns": "",
            "dhcp_service_mode": "", "post": "1", **overrides,
        }

    def test_lookup_and_render_match_captured_request(self):
        response = Mock(status_code=200, url="https://example.invalid/api/v3/settings/location_templates")
        response.json.return_value = self.template_response
        resolver = LocationTemplateResolver(self.bulk.get_json_v3_location_templates)
        row = self.row()
        with patch.object(self.bulk, "_request_with_auto_refresh", return_value=response) as request:
            ctx = prepare_location_context(row, Mock(), resolver.resolve)
            payload = build_site_payload(row, ctx)
            self.assertEqual(payload["location"], {
                "is_existing_location": False,
                "details": {"country": "UNITED_STATES", "name": "newsite1234"},
                "location_template_id": 3184987,
            })
            self.assertEqual(resolver.resolve(" zt-600-sa "), 3219369)
            self.assertEqual(request.call_count, 1)  # Cached across rows.
            self.assertEqual(request.call_args.args, ("GET", "https://example.invalid/api/v3/settings/location_templates"))
            self.assertEqual(request.call_args.kwargs["params"], {"refresh_token": "enabled"})

    def test_render_existing_and_none(self):
        for mode in ("existing", "none"):
            with self.subTest(mode=mode):
                row = self.row(location_type=mode)
                ctx = prepare_location_context(row, lambda _: 1234)
                payload = build_site_payload(row, ctx)
                if mode == "existing":
                    self.assertEqual(payload["location"], {"is_existing_location": True, "location_id": 1234})
                else:
                    self.assertNotIn("location", payload)

    def test_csv_template_name_ignores_environment_defaults(self):
        row = self.row()
        lookup = Mock(return_value=3184987)
        with patch.dict(os.environ, {"ZIA_LOCATION_TEMPLATE_ID": "999", "ZIA_LOCATION_TEMPLATE_NAME": "Wrong"}):
            ctx = prepare_location_context(row, Mock(), lookup)
        lookup.assert_called_once_with("Default Location Template")
        self.assertEqual(ctx["location_template_id"], 3184987)

    def test_missing_response_list_is_an_error(self):
        with patch.object(self.bulk, "get_json", return_value={"unexpected": []}):
            with self.assertRaisesRegex(ValueError, "location_templates list"):
                self.bulk.get_json_v3_location_templates()

    def test_duplicate_names_require_explicit_id(self):
        with patch.object(self.bulk, "get_json_v3_location_templates", return_value=[
            {"name": "Duplicate", "id": 1}, {"name": "duplicate", "id": 2},
        ]):
            with self.assertRaisesRegex(ValueError, "Duplicate location template"):
                LocationTemplateResolver(self.bulk.get_json_v3_location_templates).resolve("Duplicate")

    def test_batch_checks_later_rows_after_lookup_failure_without_posting(self):
        for failure in (RuntimeError("GET location_templates -> 404"), requests.Timeout("lookup timed out")):
            with self.subTest(failure=type(failure).__name__):
                rows = [self.row(), self.row(site_name="second", gateway_name="second-gw", location_template_id="3184987")]
                with patch.object(self.bulk, "get_json_v3_location_templates", side_effect=failure), patch.object(
                    self.bulk, "create_site"
                ) as create, contextlib.redirect_stdout(io.StringIO()) as output:
                    result = self.bulk.run(validate_rows(rows), dry_run=True)
                self.assertEqual(result.exit_code, 1)
                self.assertIn("newsite1234:", output.getvalue())
                self.assertIn("DRY: second:", output.getvalue())
                create.assert_not_called()


class CsvUpgradeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pull = load_script("pull_site")

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "sites.csv"
        self.path_patch = patch.object(self.pull, "CSV_PATH", self.path)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def write_csv(self, rows):
        with self.path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def read_csv(self):
        with self.path.open(newline="") as stream:
            return list(csv.DictReader(stream))

    def test_upgrade_middle_row_preserves_all_rows_and_custom_columns(self):
        self.write_csv([
            {"site_name": "First", "custom": "one"},
            {"site_name": "UpdateMe", "custom": "two"},
            {"site_name": "Last", "custom": "three"},
        ])
        self.pull.upsert_sites_csv_row({"site_name": "UpdateMe", "location_type": "auto", "location_template_name": "Default Location Template", "location_template_id": ""})
        rows = self.read_csv()
        self.assertEqual([row["site_name"] for row in rows], ["First", "UpdateMe", "Last"])
        self.assertEqual([row["custom"] for row in rows], ["one", "two", "three"])
        self.assertEqual(rows[1]["location_type"], "auto")

    def test_preserves_user_location_choices_on_reexport(self):
        self.write_csv([{"site_name": "Site", "location_type": "new", "location_template_name": "ZT-600-SA", "location_template_id": "3219369", "post": "1"}])
        self.pull.upsert_sites_csv_row({"site_name": "Site", "location_type": "auto", "location_template_name": "Default Location Template", "location_template_id": "", "post": "0"})
        row = self.read_csv()[0]
        self.assertEqual((row["location_type"], row["location_template_name"], row["location_template_id"]), ("new", "ZT-600-SA", "3219369"))
        self.assertEqual(row["post"], "0")

    def test_failed_replace_leaves_original_csv_intact(self):
        self.write_csv([{"site_name": "Original"}])
        original = self.path.read_bytes()
        with patch.object(self.pull.os, "replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                self.pull.upsert_sites_csv_row({"site_name": "New", "location_type": "auto"})
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    def test_malformed_row_leaves_original_csv_intact(self):
        self.path.write_text("site_name\nFirst,unexpected\n")
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "more values than headers"):
            self.pull.upsert_sites_csv_row({"site_name": "New"})
        self.assertEqual(self.path.read_bytes(), original)

    def test_creates_csv_when_missing(self):
        self.pull.upsert_sites_csv_row({"site_name": "New", "location_type": "auto"})
        self.assertEqual(self.read_csv()[0]["site_name"], "New")

    def export_fixture_site(self, dns_response=None, dns_error=None):
        args = Mock(site_name="Site", list_locations=False, list_templates=False,
                    include_wan=False, include_ha=False, json_only=False)
        with contextlib.ExitStack() as stack:
            http = stack.enter_context(patch.object(requests.Session, "request", side_effect=AssertionError("Unexpected HTTP")))
            stack.enter_context(patch.object(self.pull, "OUT_VLANS_DIR", self.path.parent / "vlans"))
            stack.enter_context(patch.object(self.pull, "list_gateways_rows", return_value=[
                {"id": "fixture-site-id", "site_name": "Site", "template_name": "Fixture Template"},
            ]))
            stack.enter_context(patch.object(self.pull, "get_vlans_v2_network", return_value=[]))
            stack.enter_context(patch.object(self.pull, "get_json", return_value={"result": []} if dns_response is None else dns_response, side_effect=dns_error))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.pull.export_or_list(args)
            http.assert_not_called()

    def test_new_export_omits_template_id(self):
        self.export_fixture_site()
        row = self.read_csv()[0]
        self.assertNotIn("template_id", row)
        self.assertEqual(row["template_name"], "Fixture Template")

    def test_reexport_does_not_restore_removed_template_id_column(self):
        self.write_csv([{"site_name": "Site", "template_name": "Fixture Template", "custom": "keep"}])
        self.export_fixture_site()
        row = self.read_csv()[0]
        self.assertNotIn("template_id", row)
        self.assertEqual(row["custom"], "keep")

    def test_reexport_preserves_legacy_template_id_override(self):
        self.write_csv([{"site_name": "Site", "template_name": "Fixture Template", "template_id": "explicit-uuid"}])
        self.export_fixture_site()
        self.assertEqual(self.read_csv()[0]["template_id"], "explicit-uuid")

    def test_new_export_omits_location_template_id(self):
        self.export_fixture_site()
        row = self.read_csv()[0]
        self.assertNotIn("location_template_id", row)
        self.assertEqual(row["location_template_name"], "Default Location Template")

    def test_reexport_does_not_restore_removed_location_template_id(self):
        self.write_csv([{"site_name": "Site", "location_template_name": "ZT-600-SA", "custom": "keep"}])
        self.export_fixture_site()
        row = self.read_csv()[0]
        self.assertNotIn("location_template_id", row)
        self.assertEqual(row["location_template_name"], "ZT-600-SA")
        self.assertEqual(row["custom"], "keep")

    def test_reexport_preserves_legacy_location_template_id(self):
        self.write_csv([{"site_name": "Site", "location_template_name": "ZT-600-SA", "location_template_id": "3219369"}])
        self.export_fixture_site()
        row = self.read_csv()[0]
        self.assertEqual(row["location_template_id"], "3219369")
        self.assertEqual(row["location_template_name"], "ZT-600-SA")

    def test_export_writes_confirmed_private_dns_response_to_csv(self):
        self.export_fixture_site(dns_response={"result": [{
            "site_id": "fixture-site-id", "membership_info": {"ip_prefix": ["192.0.2.53/32", "198.51.100.53/32"]},
        }]})
        self.assertEqual(self.read_csv()[0]["private_dns"], "192.0.2.53,198.51.100.53")

    def test_failed_dns_read_preserves_all_existing_export_files(self):
        self.write_csv([{"site_name": "Site", "private_dns": "192.0.2.53"}])
        directory = self.path.parent / "vlans"
        directory.mkdir()
        files = [self.path, directory / "Site.json", directory / "Site.csv"]
        for path in files[1:]:
            path.write_text("existing export\n")
        originals = {path: path.read_bytes() for path in files}
        for response, error in (({}, None), (None, requests.Timeout("fixture timeout")), (None, RuntimeError("fixture HTTP 403"))):
            with self.subTest(response=response, error=error), self.assertRaises((RuntimeError, ValueError)):
                self.export_fixture_site(dns_response=response, dns_error=error)
            self.assertEqual({path: path.read_bytes() for path in files}, originals)

    def test_confirmed_empty_dns_clears_old_csv_value(self):
        self.write_csv([{"site_name": "Site", "private_dns": "192.0.2.53"}])
        self.export_fixture_site(dns_response={"result": []})
        self.assertEqual(self.read_csv()[0]["private_dns"], "")


class PrivateDnsParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pull = load_script("pull_site")

    def test_confirmed_response_merges_members_and_deduplicates(self):
        data = {"result": [
            {"site_id": "fixture", "membership_info": {"ip_prefix": ["192.0.2.53/32"]}},
            {"site_id": "fixture", "membership_info": {"ip_prefix": ["192.0.2.53/32", "198.51.100.53/32"]}},
        ]}
        self.assertEqual(self.pull.parse_private_dns_members(data, "fixture"), "192.0.2.53,198.51.100.53")

    def test_legacy_shape_and_non_host_prefixes_are_preserved(self):
        data = {"member_attributes": {"ip_prefix": ["192.0.2.53/32", "198.51.100.0/24", "203.0.113.53"]}}
        self.assertEqual(self.pull.parse_private_dns_members(data, "fixture"), "192.0.2.53,198.51.100.0/24,203.0.113.53")

    def test_recognized_empty_responses(self):
        for data in ({"result": []}, {"result": [{"membership_info": {"ip_prefix": []}}]}, {"member_attributes": {"ip_prefix": []}}):
            self.assertEqual(self.pull.parse_private_dns_members(data, "fixture"), "")

    def test_unknown_or_malformed_shapes_are_errors(self):
        for data in ({}, [], {"result": {}}, {"result": [None]}, {"result": [{}]}, {"member_attributes": {}}, {"member_attributes": {"ip_prefix": "192.0.2.53"}}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.pull.parse_private_dns_members(data, "fixture")

    def test_invalid_prefixes_are_errors(self):
        for prefix in (None, 123, "", "not-an-ip", "192.0.2.1/99"):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                self.pull.parse_private_dns_members({"member_attributes": {"ip_prefix": [prefix]}}, "fixture")

    def test_different_site_membership_is_rejected(self):
        data = {"result": [{"site_id": "other-site", "membership_info": {"ip_prefix": ["192.0.2.53/32"]}}]}
        with self.assertRaisesRegex(ValueError, "different site"):
            self.pull.parse_private_dns_members(data, "fixture")


if __name__ == "__main__":
    unittest.main()
