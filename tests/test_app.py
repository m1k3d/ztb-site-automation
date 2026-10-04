import base64
import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from app import MissingVlanFiles, export_batch, import_batch, normalize_batch, parse_csv, validate_batch
from input_validation import validate_csv, validate_rows
from site_payload import build_site_payload


def sample_batch():
    return [{"fields": {"site_name": "Utrecht-NL-BR", "gateway_name": "Utrecht-NL-ZT",
                        "template_name": "Example", "wan_interface_name": "ge5",
                        "location_type": "none", "post": "1", "appc_provision": "1"},
             "vlans": [{"name": "Printers", "tag": "20", "subnet": "24",
                        "default_gateway": "10.20.0.1", "interface": "ge2",
                        "dhcp_service": "off", "zpa_include": "1"}]}]


class EditorTests(unittest.TestCase):
    def test_selected_management_loopback_survives_ui_csv_export_and_import(self):
        batch = sample_batch()
        batch[0]["vlans"].append(dict(name="MGMT", tag="1", subnet="32", default_gateway="10.0.0.1",
                                      interface="lo0", zone="Management Zone", dhcp_service="off", zpa_include="1", enabled="true"))
        checked = validate_batch(batch)
        self.assertTrue(checked["valid"], checked["issues"])
        self.assertEqual(checked["sites"][0]["zpa"]["subnets"], ["10.0.0.1/32", "10.20.0.0/24"])
        exported = export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content"]))) as archive, tempfile.TemporaryDirectory() as directory:
            archive.extractall(directory)
            result = validate_csv(Path(directory) / "sites.csv")
            self.assertTrue(result.valid, result.issues)
            self.assertTrue(result.sites[0].vlans[1]["zpa_include"])
            uploads = [{"name": name, "content": archive.read(name).decode()}
                       for name in archive.namelist() if name.endswith(".csv")]
        self.assertEqual(validate_batch(import_batch(uploads))["sites"][0]["zpa"], checked["sites"][0]["zpa"])

    def test_vlan_dns_survives_editor_export_and_cli_validation(self):
        batch = sample_batch()
        batch[0]["vlans"][0].update(per_network_dns="10.20.0.1", dhcp_service="inherit",
                                     dhcp_start="10.20.0.2", dhcp_end="10.20.0.254")
        exported = export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content"]))) as archive, tempfile.TemporaryDirectory() as directory:
            archive.extractall(directory)
            result = validate_csv(Path(directory) / "sites.csv")
        self.assertTrue(result.valid, result.issues)
        self.assertEqual(result.sites[0].vlans[0]["per_network_dns"], "10.20.0.1")

    def test_export_blanks_disabled_scope_and_preserves_source(self):
        batch = sample_batch()
        batch[0]["vlans"][0].update(dhcp_start="stale", dhcp_end="", dhcp_range="stale-range")
        original = copy.deepcopy(batch)
        exported = export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content"]))) as archive:
            filename = next(name for name in archive.namelist() if name.startswith("vlans/"))
            _, rows = parse_csv(archive.read(filename).decode(), filename)
        self.assertEqual(rows[0]["dhcp_start"], "")
        self.assertEqual(rows[0]["dhcp_end"], "")
        self.assertNotIn("dhcp_range", rows[0])
        self.assertEqual(batch, original)

    def test_explicit_ha_requires_second_gateway(self):
        batch = sample_batch()
        batch[0]["ha_enabled"] = True
        result = validate_batch(batch)
        self.assertFalse(result["valid"])
        self.assertEqual({issue["field"] for issue in result["issues"]},
                         {"gateway_name_b", "wan1_interface_name"})
        with self.assertRaises(ValueError):
            export_batch(batch)

    def test_standalone_excludes_stale_second_gateway_without_mutating_draft(self):
        batch = sample_batch()
        batch[0]["ha_enabled"] = False
        batch[0]["fields"].update(gateway_name_b="Branch-B", wan1_interface_name="ge7",
                                 wan1_ip="198.51.100.10", vrrp_link_interface="ge4")
        batch[0]["wan_modes"] = {"0": "dhcp", "1": "static"}
        original = copy.deepcopy(batch)
        self.assertTrue(validate_batch(batch)["valid"])
        exported = export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content"]))) as archive:
            _, rows = parse_csv(archive.read("sites.csv").decode(), "sites.csv")
        self.assertTrue(all(rows[0][field] == "" for field in
                            ("gateway_name_b", "wan1_interface_name", "wan1_ip", "vrrp_link_interface")))
        gateways = build_site_payload(rows[0], {"location_type": "none"})["gateways"]
        self.assertEqual(len(gateways), 1)
        self.assertEqual(batch, original)

    def test_explicit_static_wan_cannot_fall_back_to_dhcp_when_blank(self):
        batch = sample_batch()
        batch[0]["wan_modes"] = {"0": "static"}
        result = validate_batch(batch)
        self.assertFalse(result["valid"])
        self.assertEqual({issue["field"] for issue in result["issues"]}, {"wan0_ip", "wan0_mask", "wan0_gw"})
        with self.assertRaises(ValueError):
            export_batch(batch)

    def test_dhcp_clears_stale_static_values_in_csv_and_payload(self):
        batch = sample_batch()
        batch[0]["fields"].update(wan0_ip="192.0.2.10", wan0_mask="24", wan0_gw="192.0.2.1")
        batch[0]["wan_modes"] = {"0": "dhcp"}
        exported = export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content"]))) as archive:
            _, rows = parse_csv(archive.read("sites.csv").decode(), "sites.csv")
        self.assertTrue(all(rows[0][field] == "" for field in ("wan0_ip", "wan0_mask", "wan0_gw")))
        row = validate_rows(normalize_batch(batch)).sites[0].row
        gateway = build_site_payload(row, {"location_type": "none"})["gateways"][0]
        self.assertTrue(gateway["is_wan_dhcp"])
        self.assertNotIn("wan_ip_address", gateway)

    def test_ha_wans_can_use_different_addressing_modes(self):
        batch = sample_batch()
        batch[0]["fields"].update(gateway_name_b="Branch-B", wan1_interface_name="ge7",
                                 wan1_ip="198.51.100.10", wan1_mask="24", wan1_gw="198.51.100.1")
        batch[0]["wan_modes"] = {"0": "dhcp", "1": "static"}
        self.assertTrue(validate_batch(batch)["valid"])
        row = validate_rows(normalize_batch(batch)).sites[0].row
        gateways = build_site_payload(row, {"location_type": "none"})["gateways"]
        self.assertTrue(gateways[0]["is_wan_dhcp"])
        self.assertFalse(gateways[1]["is_wan_dhcp"])
        self.assertEqual(gateways[1]["wan_subnet_mask"], "24")
        self.assertEqual(gateways[1]["wan_ip_address"], "198.51.100.10")

    def test_rejects_unknown_wan_mode(self):
        batch = sample_batch()
        batch[0]["wan_modes"] = {"0": "unknown"}
        with self.assertRaises(ValueError):
            validate_batch(batch)

    def test_static_wan_payload_converts_prefix_and_dotted_masks_to_native_prefix(self):
        for mask in ('24', '255.255.255.0'):
            batch = sample_batch()
            batch[0]['fields'].update(wan0_ip='192.0.2.10', wan0_mask=mask, wan0_gw='192.0.2.1')
            batch[0]['wan_modes'] = {'0': 'static'}
            checked = validate_rows(normalize_batch(batch))
            self.assertTrue(checked.valid, checked.issues)
            gateway = build_site_payload(checked.sites[0].row, {'location_type': 'none'})['gateways'][0]
            self.assertEqual(gateway['wan_subnet_mask'], '24')
            self.assertEqual(gateway['wan_ip_address'], '192.0.2.10')

    def test_export_round_trip_uses_cli_validation_and_preserves_extra_fields(self):
        batch = sample_batch()
        batch[0]["fields"]["vrrp_vrid"] = "42"
        batch[0]["vlans"][0]["display_name"] = "Printer network"
        original = copy.deepcopy(batch)
        exported = export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content"]))) as archive:
            with tempfile.TemporaryDirectory() as directory:
                archive.extractall(directory)
                result = validate_csv(Path(directory) / "sites.csv")
                self.assertTrue(result.valid, result.issues)
                self.assertTrue(result.sites[0].vlans[0]["zpa_include"])
            uploads = [{"name": name, "content": archive.read(name).decode()}
                       for name in archive.namelist() if name.endswith(".csv")]
        imported = import_batch(uploads)
        self.assertEqual(imported[0]["fields"]["vrrp_vrid"], "42")
        self.assertEqual(imported[0]["vlans"][0]["display_name"], "Printer network")
        self.assertEqual(validate_batch(imported), validate_batch(batch))
        self.assertEqual(batch, original)

    def test_validation_builds_combined_disabled_zpa_plan(self):
        result = validate_batch(sample_batch())
        self.assertTrue(result["valid"], result["issues"])
        segment = result["sites"][0]["zpa"]
        self.assertEqual(segment["application_name"], "ztb-utrecht-nl-br-lan")
        self.assertEqual(segment["subnets"], ["10.20.0.0/24"])
        self.assertFalse(segment["enabled"])

    def test_export_rejects_invalid_and_unselected_batches(self):
        batch = sample_batch()
        batch[0]["vlans"][0]["tag"] = "4095"
        with self.assertRaises(ValueError):
            export_batch(batch)
        batch = sample_batch()
        batch[0]["fields"]["post"] = "0"
        with self.assertRaises(ValueError):
            export_batch(batch)

    def test_zpa_requires_app_connector(self):
        batch = sample_batch()
        batch[0]["fields"]["appc_provision"] = "0"
        result = validate_batch(batch)
        self.assertFalse(result["valid"])
        self.assertTrue(any(issue["field"] == "appc_provision" for issue in result["issues"]))

    def test_import_reports_missing_files_without_reading_disk(self):
        files = [{"name": "sites.csv", "content": "site_name,vlans_file\nExample,/private/tenant.csv\nOther,vlans/other.csv\n"}]
        with patch.object(Path, "open", side_effect=AssertionError("No local reads")):
            with self.assertRaises(MissingVlanFiles) as raised:
                import_batch(files)
        self.assertEqual(raised.exception.names, ["other.csv", "tenant.csv"])

    def test_inline_validation_never_resolves_uploaded_paths(self):
        batch = sample_batch()
        batch[0]["fields"]["vlans_file"] = "/private/tenant.csv"
        with patch.object(Path, "open", side_effect=AssertionError("No local reads")):
            self.assertTrue(validate_batch(batch)["valid"])

    def test_import_rejects_ambiguous_basenames(self):
        with self.assertRaisesRegex(ValueError, "Two files"):
            import_batch([{"name": "first/sites.csv", "content": "site_name\nA\n"},
                          {"name": "second/sites.csv", "content": "site_name\nB\n"}])

    def test_parse_rejects_truncated_rows_and_duplicate_headers(self):
        for content in ("name,name\nA,B\n", "name,tag\nA\n", "name,tag\nA,1,2\n"):
            with self.subTest(content=content), self.assertRaises(ValueError):
                parse_csv(content, "vlans.csv")

    def test_zip_paths_are_safe_even_for_unselected_site_names(self):
        batch = sample_batch()
        unselected = copy.deepcopy(batch[0])
        unselected["fields"].update(site_name="../../outside", post="0")
        batch.append(unselected)
        result = export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(result["content"]))) as archive:
            self.assertTrue(all(not name.startswith("/") and ".." not in Path(name).parts
                                for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
