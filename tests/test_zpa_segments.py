"""LAN staging contracts: scope, linkage, disabled state, and safe failure."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from automation_config import Settings
from deployment_engine import DeploymentEngine
from input_validation import validate_rows, validate_csv
from pull_site import vlans_to_csv_rows
from run_report import reserve_report, save_report
from zpa_provisioning import ZPAContext, provision_zpa_for_site
import zpa_segments as segments


def vlan(**kwargs):
    return dict(name="Printers", tag="20", subnet="24", default_gateway="10.20.0.1",
                interface="ge5", enabled="true", zpa_include="1", **kwargs)


def site(name="Utrecht-NL-BR", vlans=None, **kwargs):
    return dict(site_name=name, gateway_name=name + "-gw", template_id="template",
                wan_interface_name="ge3", location_type="none", post="1",
                appc_provision="1", vlans=vlans if vlans is not None else [vlan()], **kwargs)


CONTEXT = ZPAContext("https://example.invalid", "customer", "private-test-token", "cert")


class FakeZPA:
    def __init__(self):
        self.data = {key: {} for key in segments.COLLECTIONS}
        self.calls = []
        self.fail_resource = None
        self.alter = lambda resource, data: data

    def request(self, method, url, **kwargs):
        resource = url.split("/customers/customer/")[1]
        self.calls.append((method, resource, deepcopy(kwargs.get("json"))))
        collection, _, identifier = resource.partition("/")
        if method == "POST":
            if collection == self.fail_resource:
                return Mock(status_code=403, text="private-test-token")
            identifier = str(len(self.data[collection]) + 1)
            self.data[collection][identifier] = dict(deepcopy(kwargs["json"]), id=identifier)
            body = self.data[collection][identifier]
        elif method == "PUT":
            self.data[collection][identifier] = dict(deepcopy(kwargs["json"]), id=identifier)
            body = {}
        elif identifier:
            body = self.alter(collection, deepcopy(self.data[collection][identifier]))
        else:
            rows = list(self.data[collection].values())
            body = dict(list=deepcopy(rows), totalPages=1, totalCount=len(rows))
        return Mock(status_code=200, json=Mock(return_value=body))


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.http = self.stack.enter_context(patch.object(requests.Session, "request", side_effect=AssertionError("Unexpected HTTP")))
        self.api = FakeZPA()
        self.request = self.stack.enter_context(patch.object(segments.requests, "request", side_effect=self.api.request))
        self.validation = validate_rows([site()])
        self.assertTrue(self.validation.valid, self.validation.issues)
        self.plan = segments.build_segment_plan("Utrecht-NL-BR", self.validation.sites[0].vlans)
        self.report = self.plan.report()

    def tearDown(self):
        self.http.assert_not_called()

    def test_selection_defaults_off_and_is_strict(self):
        for value in (None, "", "0", "false"):
            row = vlan()
            row["zpa_include"] = value
            result = validate_rows([site(vlans=[row])])
            self.assertTrue(result.valid, result.issues)
            self.assertIsNone(segments.build_segment_plan("Branch", result.sites[0].vlans))
        row = vlan()
        del row["zpa_include"]
        self.assertFalse(validate_rows([site(vlans=[row])]).sites[0].vlans[0]["zpa_include"])
        row["zpa_include"] = "maybe"
        self.assertIn("zpa_include", [i.field for i in validate_rows([site(vlans=[row])]).issues])
        self.request.assert_not_called()

    def test_selected_vlan_requires_connector_provisioning(self):
        row = site()
        row["appc_provision"] = "0"
        self.assertFalse(validate_rows([row]).valid)
        self.assertTrue(validate_rows([site(vlans=[{**vlan(), "zone": "PRINTERS-ZONE"}])]).valid)

    def test_management_wan_ha_and_disabled_vlans_are_rejected(self):
        for change in ({"interface": "lo0", "subnet": "32"}, {"zone": "Management Zone"},
                       {"zone": "WAN Zone"}, {"interface": "ge3"}, {"enabled": "false"}):
            with self.subTest(change=change):
                self.assertFalse(validate_rows([site(vlans=[{**vlan(), **change}])]).valid)
        self.assertFalse(validate_rows([site(vrrp_link_interface="ge5")]).valid)

    def test_combines_only_selected_subnets_and_normalizes_name(self):
        rows = [vlan(), {**vlan(), "tag": "30", "name": "Servers", "default_gateway": "10.30.0.1", "subnet": "255.255.255.0"},
                {**vlan(), "tag": "40", "name": "Guests", "zpa_include": "0"}]
        validated = validate_rows([site("Utrecht NL BR", vlans=rows)])
        self.assertTrue(validated.valid, validated.issues)
        plan = segments.build_segment_plan("Utrecht NL BR", validated.sites[0].vlans)
        self.assertEqual(plan.application_name, "ztb-utrecht-nl-br-lan")
        self.assertEqual(plan.subnets, ("10.20.0.0/24", "10.30.0.0/24"))
        self.assertFalse(validate_rows([site("Utrecht NL BR"), site("Utrecht-NL-BR")]).valid)

    def test_export_defaults_to_no_staging_and_roundtrips_csv(self):
        row = vlans_to_csv_rows([{**vlan(), "status": "provisioned", "zpa_include": True}])[0]
        self.assertEqual(row["zpa_include"], "0")
        import csv
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            row["zpa_include"] = "1"
            with (root / "vlans.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=row)
                writer.writeheader()
                writer.writerow(row)
            source = site()
            source.pop("vlans")
            source["vlans_file"] = "vlans.csv"
            with (root / "sites.csv").open("w", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=source)
                writer.writeheader()
                writer.writerow(source)
            result = validate_csv(root / "sites.csv")
        self.assertTrue(result.valid, result.issues)
        self.assertIs(result.sites[0].vlans[0]["zpa_include"], True)

    def test_duplicate_and_overlap_checks_include_disabled_existing_apps(self):
        for resource, name in (("application", self.plan.application_name.upper()),
                               ("segmentGroup", self.plan.segment_group_name), ("serverGroup", self.plan.server_group_name)):
            inventory = {r: [] for r in segments.COLLECTIONS}
            inventory[resource] = [{"name": name, "id": "old", "domainNames": []}]
            with self.assertRaisesRegex(ValueError, "already exists"):
                segments.check_conflicts(self.plan, inventory)
        for destination in ("10.20.0.5", "10.0.0.0/8", "10.20.0.0/25", "*"):
            inventory = {r: [] for r in segments.COLLECTIONS}
            inventory["application"] = [{"id": "old", "name": "Old", "domainNames": [destination], "enabled": False}]
            with self.assertRaisesRegex(ValueError, "overlaps"):
                segments.check_conflicts(self.plan, inventory)

    def test_batch_overlap_rejected_before_creation(self):
        other = segments.build_segment_plan("Other", self.validation.sites[0].vlans)
        with self.assertRaisesRegex(ValueError, "another selected site"):
            segments.check_batch_conflicts([self.plan, other], {r: [] for r in segments.COLLECTIONS})

    def test_inventory_paginates_and_fails_closed(self):
        pages = [dict(totalCount="2", totalPages="2", list=[{"id": str(i), "name": f"App{i}"}]) for i in (1, 2)]
        self.request.side_effect = [Mock(status_code=200, json=Mock(return_value=p)) for p in pages]
        self.assertEqual(len(segments.list_resources(CONTEXT, "application")), 2)
        self.assertEqual([c.kwargs["params"]["page"] for c in self.request.call_args_list], [1, 2])
        for data in ({}, {"list": []}, dict(list=[], totalCount=2, totalPages=1),
                     dict(list=[{}], totalCount=1, totalPages=1)):
            self.request.side_effect = None
            self.request.return_value = Mock(status_code=200, json=Mock(return_value=data))
            with self.assertRaises(ValueError):
                segments.list_resources(CONTEXT, "application")
        self.request.side_effect = [Mock(status_code=200, json=Mock(return_value=pages[0]))] * 2
        with self.assertRaisesRegex(ValueError, "repeated"):
            segments.list_resources(CONTEXT, "application")

    def test_staging_creates_one_disabled_segment_linked_to_exact_new_connector(self):
        self.assertTrue(segments.stage_segments(CONTEXT, self.plan, "new-connector", self.report, emit=lambda _: None))
        posts = [c for c in self.api.calls if c[0] == "POST"]
        self.assertEqual([c[1] for c in posts], ["segmentGroup", "serverGroup", "application"])
        app = posts[-1][2]
        self.assertIs(app["enabled"], False)
        self.assertEqual(app["icmpAccessType"], "NONE")
        self.assertEqual(app["domainNames"], ["10.20.0.0/24"])
        self.assertEqual(app["tcpPortRange"], [{"from": "1", "to": "52"}, {"from": "54", "to": "65535"}])
        self.assertEqual(app["udpPortRange"], app["tcpPortRange"])
        self.assertEqual(posts[1][2]["appConnectorGroups"], [{"id": "new-connector"}])
        self.assertEqual(self.report["status"], "staged_disabled")
        self.assertTrue(all(r["verified"] for r in self.report["resources"].values()))
        self.assertFalse(any("policy" in c[1].lower() for c in self.api.calls))
        self.assertNotIn("private-test-token", json.dumps(self.report))

    def test_rerun_or_missing_connector_never_posts(self):
        with self.assertRaisesRegex(ValueError, "group ID"):
            segments.stage_segments(CONTEXT, self.plan, None, self.report)
        self.request.assert_not_called()
        segments.stage_segments(CONTEXT, self.plan, "connector", self.report, emit=lambda _: None)
        before = len([c for c in self.api.calls if c[0] == "POST"])
        with self.assertRaisesRegex(ValueError, "already exists"):
            segments.stage_segments(CONTEXT, self.plan, "connector", self.plan.report())
        self.assertEqual(len([c for c in self.api.calls if c[0] == "POST"]), before)

    def test_failure_stops_dependent_writes_and_retains_created_ids(self):
        self.api.fail_resource = "serverGroup"
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            segments.stage_segments(CONTEXT, self.plan, "connector", self.report)
        self.assertEqual(list(self.report["resources"]), ["segmentGroup"])
        self.assertEqual(self.report["status"], "incomplete")
        self.assertFalse(any(c[1] == "application" and c[0] == "POST" for c in self.api.calls))

    def test_timeout_is_not_retried_or_logged_with_secret(self):
        self.request.side_effect = requests.Timeout("private-test-token")
        with self.assertRaises(RuntimeError) as caught:
            segments.stage_segments(CONTEXT, self.plan, "connector", self.report)
        self.request.assert_called_once()
        self.assertNotIn("private-test-token", str(caught.exception))

    def test_readback_mismatch_fails_instead_of_reporting_success(self):
        for field, wrong in (("icmpAccessType", "PING"), ("domainNames", ["0.0.0.0/0"]),
                             ("serverGroups", [{"id": "other"}]), ("tcpPortRange", [{"from": "1", "to": "65535"}])):
            with self.subTest(field=field):
                self.api = FakeZPA()
                self.api.alter = lambda resource, data: {**data, field: wrong} if resource == "application" else data
                self.request.side_effect = self.api.request
                report = self.plan.report()
                with self.assertRaisesRegex(ValueError, "read-back"):
                    segments.stage_segments(CONTEXT, self.plan, "connector", report)
                self.assertEqual(report["status"], "incomplete")

    def test_unexpected_enabled_readback_triggers_disable_only_on_new_application(self):
        self.api.alter = lambda resource, data: {**data, "enabled": True} if resource == "application" else data
        with self.assertRaisesRegex(ValueError, "disabled state"):
            segments.stage_segments(CONTEXT, self.plan, "connector", self.report)
        puts = [c for c in self.api.calls if c[0] == "PUT"]
        self.assertEqual(len(puts), 1)
        self.assertEqual(puts[0][1], "application/1")
        self.assertIs(puts[0][2]["enabled"], False)

    def engine(self):
        engine = DeploymentEngine(Settings(ztb_api_base="https://example.invalid", bearer="test",
            zpa_base_url="https://example.invalid", zpa_client_id="test", zpa_client_secret="test"), emit=lambda _: None)
        self.addCleanup(engine.client.close)
        for method, value in (("site_exists", False), ("create_site", (True, "created", None)),
                              ("resolve_gateway_ids_and_cluster", ("gateway", 123)), ("resolve_site_id", "site"),
                              ("process_vlans_for_site", True)):
            self.stack.enter_context(patch.object(engine, method, return_value=value))
        self.stack.enter_context(patch("zpa_provisioning.prepare_zpa", return_value=CONTEXT))
        def provision(*args, resources=None, **kwargs):
            resources["appConnectorGroup"] = {"id": "new-connector", "name": "Utrecht-NL-BR"}
            return True
        self.stack.enter_context(patch("zpa_provisioning.provision_zpa_for_site", side_effect=provision))
        return engine

    def test_engine_preview_and_all_row_preflight_never_write(self):
        engine = self.engine()
        result = engine.run(self.validation, dry_run=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.sites[0].zpa_segments["status"], "preview")
        engine.create_site.assert_not_called()
        self.assertTrue(all(c[0] == "GET" for c in self.api.calls))
        self.api.data["application"]["old"] = {"id": "old", "name": "Existing", "domainNames": ["10.20.0.0/24"]}
        result = engine.run(self.validation)
        self.assertEqual(result.exit_code, 1)
        engine.create_site.assert_not_called()

    def test_engine_dependency_failure_blocks_staging(self):
        engine = self.engine()
        engine.process_vlans_for_site.return_value = False
        result = engine.run(self.validation)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(result.sites[0].stages["ZPA segments"])
        self.assertEqual(result.sites[0].zpa_segments["status"], "blocked")
        self.assertTrue(all(c[0] == "GET" for c in self.api.calls))

    def test_engine_report_records_disabled_state_and_ids_without_credentials(self):
        engine = self.engine()
        result = engine.run(self.validation)
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(result.sites[0].stages["ZPA segments"])
        payload = engine.vlan_to_v2_payload(self.validation.sites[0].vlans[0], "gw", 123)
        self.assertNotIn("zpa_include", json.dumps(payload))
        with tempfile.TemporaryDirectory() as directory:
            path = reserve_report(directory)
            save_report(path, result)
            data = json.loads(path.read_text())
            text = path.with_suffix(".txt").read_text()
        self.assertNotIn(CONTEXT.token, json.dumps(data) + text)
        self.assertEqual(data["sites"][0]["zpa_segments"]["status"], "staged_disabled")
        self.assertIn("before enabling", data["sites"][0]["next_action"])
        self.assertIn("staged_disabled", text)

    def test_provisioning_returns_created_group_only_without_key(self):
        resources = {}
        with patch("zpa_provisioning.create_app_connector_group", return_value="new-group"), \
             patch("zpa_provisioning.create_provisioning_key", return_value="private-key"), \
             patch("zpa_provisioning.update_ztb_site_zpa", return_value=True), patch("builtins.print"):
            self.assertTrue(provision_zpa_for_site({"site_name": "Branch"}, Mock(), "https://example.invalid", 123,
                                                context=CONTEXT, resources=resources))
        self.assertEqual(resources, {"appConnectorGroup": {"id": "new-group", "name": "Branch"}})
