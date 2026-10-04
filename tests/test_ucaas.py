"""UCaaS scope, object reuse, WAN targeting and failure-safe deployment; no tenant writes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from itertools import combinations
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from automation_config import Settings
from deployment_engine import DeploymentEngine
from input_validation import validate_rows
from ucaas_breakout import BreakoutError, LocalBreakout, effective_values, split_breakout_ports
from ucaas_catalog import load_catalog, parse_sources, refresh_catalog, validate_catalog, canonical_ports, SOURCES
from ui_deployment import plan_digest, public_issues, public_sites
from run_report import save_report


def row(**overrides):
    return {"site_name": "NewBranch", "gateway_name": "NewBranch-GW", "template_id": "template-id",
            "wan_interface_name": "ge5", "location_type": "none", "post": "1", "vlans": [],
            "ucaas_local_breakout": "1", "ucaas_services": "teams,zoom", **overrides}


class BreakoutTests(unittest.TestCase):
    def setUp(self):
        self.no_http = patch.object(requests.Session, "request", side_effect=AssertionError("Unexpected HTTP"))
        self.http = self.no_http.start()
        self.addCleanup(self.no_http.stop)
        self.catalog = load_catalog()
        self.catalog["retrieved_at"] = datetime.now(timezone.utc).isoformat()
        catalog = patch("ucaas_breakout.load_catalog", return_value=self.catalog)
        catalog.start(); self.addCleanup(catalog.stop)
        sleeper = patch("ucaas_breakout.time.sleep")
        sleeper.start(); self.addCleanup(sleeper.stop)
        self.engine = DeploymentEngine(Settings(ztb_api_base="https://example.invalid", bearer="offline"), emit=lambda *_: None)
        self.engine.get_template_settings = Mock(return_value={"deployment_type":"standalone", "dhcp_service":"server"})
        self.addCleanup(self.engine.client.close)
        self.groups = [{"group_id": 2, "name": "System-All-Networks-Group", "type": "network", "owner": "system"}]
        self.apps = [{"app_name": "Zoom", "domains": ["zoom.us", "zoom.com"]}]
        self.ports = [dict(id=name, name=name, gateway_id="Gateway-1", interface_type="wan") for name in ("ge5", "ge6")]
        self.policy_rows = [dict(pbr_policy_id=10, name="existing", display_name="Existing", gateway_id="gw-new", sequence_number=1, template_id=""),
                            dict(pbr_policy_id=11, name="inherited", display_name="Inherited", gateway_id="gw-new", sequence_number=1, template_id="template-id")]
        self.events, self.reorders = [], []
        self.rules_wrong_order = False
        self.engine.get_json = Mock(side_effect=self.get)
        self.engine.post_json = Mock(side_effect=self.post)
        self.engine.list_site_inventory = Mock(return_value=[])
        self.engine.site_exists = Mock(return_value=False)
        self.engine.create_site = Mock(side_effect=lambda *_: self.events.append("site") or (True, "OK", 1))
        self.engine.resolve_gateway_ids_and_cluster = Mock(return_value=("gw-new", 1))

    def tearDown(self):
        self.http.assert_not_called()

    def get(self, url, params=None, **kwargs):
        if url == self.engine.API_V2 + "/groups":
            page, size = params["page"], params["size"]
            return deepcopy(dict(count=len(self.groups), result=self.groups[page*size:(page+1)*size]))
        if url == self.engine.API_V2 + "/groups/saas-apps":
            return deepcopy(dict(result=self.apps))
        path = url.removeprefix(self.engine.API_V3)
        if path == "/templates/template-id/interfaces":
            return deepcopy(dict(count=len(self.ports), result=self.ports))
        if path == "/pbr/interfaces":
            return dict(gateway_id=params["gateway_id"], interfaces=[dict(interface_name=p["name"], interface_type=p["interface_type"]) for p in self.ports])
        if path == "/pbr/policies":
            return deepcopy(dict(policies=self.policy_rows))
        if path == "/pbr/rules":
            local = sorted([p for p in self.policy_rows if not p["template_id"]], key=lambda p: p["sequence_number"])
            inherited = [p for p in self.policy_rows if p["template_id"]]
            rules = inherited + local if self.rules_wrong_order else local + inherited
            return {"pbr_rules": deepcopy(rules)}
        raise AssertionError((url, params))

    def post(self, url, payload, **kwargs):
        self.events.append((url, deepcopy(payload)))
        if url == self.engine.API_V2 + "/groups":
            self.groups.append({**deepcopy(payload), "group_id": max(g["group_id"] for g in self.groups)+1})
        elif url == self.engine.API_V3 + "/pbr/policies":
            self.policy_rows.append({**deepcopy(payload), "pbr_policy_id": max(29, max(p["pbr_policy_id"] for p in self.policy_rows))+1, "sequence_number": len(self.policy_rows)+1})
        elif url == self.engine.API_V3 + "/pbr/policies-reorder":
            self.reorders.append(deepcopy(payload))
            for order in payload["pbr_order"]:
                next(p for p in self.policy_rows if p["pbr_policy_id"] == order["pbr_policy_id"])["sequence_number"] = order["sequence_number"]
        else:
            raise AssertionError(url)
        return Mock(status_code=201)

    def plan(self, **overrides):
        result = self.engine.plan(validate_rows([row(**overrides)]))
        self.assertFalse(result.issues, result.issues)
        return result

    def add_group(self, kind, values, name="Exact match", **extra):
        item = dict(group_id=100+len(self.groups), name=name, display_name=name, owner="user", hidden=False,
                    type=kind, member_attributes={{"network": "ip_prefix_local", "domains": "fqdn", "l4port": "port_group"}[kind]: values}, **extra)
        self.groups.append(item)
        return item

    def test_off_makes_no_ucaas_calls(self):
        plan = self.plan(ucaas_local_breakout="0")
        self.assertIsNone(plan.sites[0].ucaas_plan)
        self.engine.execute(plan)
        self.engine.get_json.assert_not_called()
        self.engine.post_json.assert_not_called()

    def test_preview_is_read_only_ipv4_and_selected_services_only(self):
        plan = self.plan(ucaas_services="zoom")
        result = self.engine.execute(plan, dry_run=True)
        data = public_sites(result)[0]["ucaas"]
        self.assertEqual([s["id"] for s in data["services"]], ["zoom"])
        self.assertEqual({o["type"] for o in data["objects"]}, {"network", "domains", "l4port"})
        self.assertTrue(all(":" not in v for o in data["objects"] if o["type"] == "network" for v in o["values"]))
        self.assertEqual((data["primary"], data["secondary"]), ("ge5", "ge6"))
        self.engine.post_json.assert_not_called()
        self.engine.create_site.assert_not_called()

    def test_wan_swap_and_explicit_third_wan(self):
        plan = self.plan(wan_interface_name="ge6")
        self.assertEqual((plan.sites[0].ucaas_plan.primary, plan.sites[0].ucaas_plan.secondary), ("ge6", "ge5"))
        self.ports.append(dict(id="ge7", name="ge7", gateway_id="Gateway-1", interface_type="wan"))
        bad = self.engine.plan(validate_rows([row()]))
        self.assertEqual(bad.issues[0].field, "ucaas_interfaces")
        plan = self.plan(ucaas_secondary_wan="ge7")
        self.assertEqual(plan.sites[0].ucaas_plan.secondary, "ge7")

    def test_path_modes_apply_to_both_rules_and_reuse_the_same_objects(self):
        existing = deepcopy(self.policy_rows)
        first_objects = None
        for mode, flags in (("best", (True, False)), ("none", (False, False)), ("balanced", (False, True))):
            with self.subTest(mode=mode):
                self.policy_rows = deepcopy(existing)
                # Omitted settings in older drafts must keep Best behavior.
                plan = self.plan(**({} if mode == "best" else {"ucaas_path_selection": mode}))
                breakout = plan.sites[0].ucaas_plan
                self.assertEqual(breakout.path_selection, mode)
                contents = [(o["key"], o["type"], o["values"]) for o in breakout.objects]
                if first_objects is None:
                    first_objects = contents
                self.assertEqual(contents, first_objects)
                self.assertEqual(breakout.summary()["path_selection"], mode.capitalize())
                before = len(self.groups)
                result = self.engine.execute(plan)
                self.assertEqual(result.exit_code, 0, result.sites[0])
                self.assertEqual(result.sites[0].ucaas["path_selection"], mode.capitalize())
                created = [p for p in self.policy_rows if p.get("name", "").startswith("ucaas-lbo-")]
                self.assertEqual(len(created), 2)
                self.assertTrue(all((p["best_link"], p["ecmp_enabled"]) == flags for p in created))
                if mode != "best":
                    self.assertEqual(len(self.groups), before)
                    self.assertTrue(all(o["status"] == "reused" for o in result.sites[0].ucaas["objects"]))

    def test_path_change_invalidates_review_and_blank_keeps_best(self):
        default = plan_digest(self.plan())
        self.assertEqual(default, plan_digest(self.plan(ucaas_path_selection="")))
        self.assertEqual(len({default, plan_digest(self.plan(ucaas_path_selection="none")),
                              plan_digest(self.plan(ucaas_path_selection="balanced"))}), 3)

    def test_invalid_path_is_rejected_before_reads_and_ignored_when_off(self):
        for mode in ("random", "Best", "false"):
            validated = validate_rows([row(ucaas_path_selection=mode)])
            self.assertTrue(any(i.field == "ucaas_path_selection" for i in validated.issues))
        self.engine.get_json.assert_not_called()
        with self.assertRaisesRegex(BreakoutError, "ucaas_path_selection"):
            self.engine.local_breakout.plan(row(ucaas_path_selection="random"), "template-id")
        self.engine.get_json.assert_not_called()
        self.assertIsNone(self.plan(ucaas_local_breakout="0", ucaas_path_selection="random").sites[0].ucaas_plan)

    def test_path_flag_mismatch_stops_before_second_rule(self):
        original = self.post
        def wrong_path(url, payload, **kwargs):
            response = original(url, payload, **kwargs)
            if url.endswith("/pbr/policies"):
                self.policy_rows[-1]["ecmp_enabled"] = False
            return response
        self.engine.post_json.side_effect = wrong_path
        result = self.engine.execute(self.plan(ucaas_path_selection="balanced"))
        self.assertEqual(result.sites[0].diagnostics["UCaaS local breakout"], "ucaas_policy_unconfirmed")
        self.assertEqual(sum(call.args[0].endswith("/pbr/policies") for call in self.engine.post_json.call_args_list), 1)

    def test_one_wan_or_lan_secondary_blocks_before_site(self):
        self.ports[1]["interface_type"] = "lan"
        plan = self.engine.plan(validate_rows([row()]))
        self.assertTrue(plan.issues)
        self.engine.execute(plan)
        self.engine.create_site.assert_not_called()

    def test_matching_existing_domains_and_saas_are_reused_not_labels(self):
        zoom = next(s for s in self.catalog["services"] if s["id"] == "zoom")
        self.add_group("network", ["0.0.0.0/0"], name="Zoom")
        exact = self.add_group("domains", zoom["domains"])
        plan = self.plan(ucaas_services="zoom")
        objs = plan.sites[0].ucaas_plan.objects
        self.assertEqual(next(o for o in objs if o["type"] == "domains")["id"], exact["group_id"])
        self.assertIsNone(objs[0]["id"])
        self.apps[0]["domains"] = zoom["domains"]
        self.groups.append(dict(group_id=300, name="Existing Zoom SaaS", owner="user", type="saas_apps", member_attributes={"saas_apps": ["Zoom"]}))
        plan = self.plan(ucaas_services="zoom")
        self.assertEqual(next(o for o in plan.sites[0].ucaas_plan.objects if o["type"] == "domains")["id"], 300)

    def test_malformed_or_nested_or_ipv6_existing_objects_are_not_reused(self):
        for group in [dict(type="saas_apps", member_attributes={"saas_apps": ["Webex"]}),
                      dict(type="domains", member_groups="123", member_attributes={"fqdn": ["*.webex.com"]}),
                      dict(type="network", member_attributes={"ip_prefix": ["52.112.0.0/14", "2603:1063::/38"]})]:
            self.assertIsNone(effective_values({"owner": "user", **group}, "network" if group["type"] == "network" else "domains",
                                               [{"app_name": "Webex", "domains": ["webex.net,cisco.com/webex"]}]))

    def test_successful_deployment_creates_objects_after_site_then_orders_only_local_policy(self):
        plan = self.plan()
        inherited = deepcopy(self.policy_rows[1])
        result = self.engine.execute(plan)
        self.assertEqual(result.exit_code, 0, result.sites[0])
        self.assertEqual(self.events[0], "site")
        self.assertEqual(len([e for e in self.events if isinstance(e, tuple) and e[0].endswith("/groups")]), len(plan.sites[0].ucaas_plan.objects))
        rule = next(p for p in self.policy_rows if p["pbr_policy_id"] == 30)
        self.assertEqual((rule["primary_int"], rule["secondary_int"], rule["best_link"]), ("ge5", "ge6", True))
        self.assertEqual((rule["primary_ip"], rule["secondary_ip"], rule["template_id"]), ("", "", ""))
        self.assertFalse(rule["ecmp_enabled"])
        self.assertEqual(self.reorders[0]["pbr_order"], [{"pbr_policy_id": identifier, "sequence_number": i+1} for i, identifier in enumerate(list(range(30, 30+len(plan.sites[0].ucaas_plan.rules))) + [10])])
        self.assertEqual(self.policy_rows[1], inherited)
        self.assertEqual(result.sites[0].ucaas["status"], "configured")

    def test_uncertain_object_post_is_never_retried_and_no_policy_posted(self):
        self.engine.post_json.side_effect = requests.Timeout("private API body must not appear")
        result = self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].status, "partial")
        self.engine.post_json.assert_called_once()
        self.assertEqual(result.sites[0].ucaas["objects"][0]["status"], "unconfirmed")
        self.assertNotIn("private API", json.dumps(public_sites(result)))

    def test_native_policy_omits_false_negations_and_inserts_at_top(self):
        original = self.post
        def native_post(url, payload, **kwargs):
            response = original(url, payload, **kwargs)
            if url.endswith('/pbr/policies'):
                new = self.policy_rows[-1]
                for key in ('src_group_negate', 'dst_group_negate', 'dst_port_group_negate'):
                    new.pop(key)
                for policy in self.policy_rows[:-1]:
                    if not policy['template_id']:
                        policy['sequence_number'] += 1
                new['sequence_number'] = 1
            return response
        self.engine.post_json.side_effect = native_post
        result = self.engine.execute(self.plan())
        self.assertEqual(result.exit_code, 0, result.sites[0])
        for flag in ('src_group_negate', 'dst_group_negate', 'dst_port_group_negate'):
            self.assertFalse(LocalBreakout.policy_matches({flag: True}, {flag: False}))
            self.assertFalse(LocalBreakout.policy_matches({}, {flag: True}))
        self.assertFalse(LocalBreakout.policy_matches({}, {'best_link': False}))

    def test_uncertain_policy_post_is_never_retried(self):
        original = self.post
        def post(url, payload, **kwargs):
            if url.endswith("/pbr/policies"):
                raise requests.Timeout("unknown outcome")
            return original(url, payload, **kwargs)
        self.engine.post_json.side_effect = post
        result = self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].diagnostics["UCaaS local breakout"], "ucaas_policy_unconfirmed")
        self.assertEqual(sum(call.args[0].endswith("/pbr/policies") for call in self.engine.post_json.call_args_list), 1)

    def test_inherited_rule_before_breakout_fails_readback(self):
        self.rules_wrong_order = True
        result = self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].status, "partial")
        self.assertEqual(result.sites[0].diagnostics["UCaaS local breakout"], "ucaas_order_unconfirmed")

    def test_changed_reused_object_or_actual_wan_blocks_rule(self):
        zoom = next(s for s in self.catalog["services"] if s["id"] == "zoom")
        group = self.add_group("domains", zoom["domains"])
        plan = self.plan(ucaas_services="zoom")
        group["member_attributes"]["fqdn"] = ["*.example.com"]
        result = self.engine.execute(plan)
        self.assertEqual(result.sites[0].diagnostics["UCaaS local breakout"], "ucaas_changed")
        self.engine.post_json.assert_not_called()
        self.ports[0]["interface_type"] = "lan"
        report = {}
        with self.assertRaises(BreakoutError):
            self.engine.local_breakout.apply(plan.sites[0].ucaas_plan, "gw-new", "NewBranch", report)
        self.engine.post_json.assert_not_called()

    def test_reports_keep_resource_ids_and_safe_recovery(self):
        result = self.engine.execute(self.plan())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            save_report(path, result)
            data = json.loads(path.read_text())["sites"][0]["ucaas"]
            self.assertEqual(data["policies"][0]["id"], 30)
            self.assertTrue(all(o["id"] for o in data["objects"]))

    def test_plan_digest_changes_with_destination_or_wan(self):
        first = plan_digest(self.plan())
        second = plan_digest(self.plan(wan_interface_name="ge6"))
        self.assertNotEqual(first, second)
        self.groups.append(dict(group_id=400, type="domains", owner="user", name="Exact", member_attributes={"fqdn": next(s for s in self.catalog["services"] if s["id"] == "zoom")["domains"]}))
        self.assertNotEqual(first, plan_digest(self.plan()))

    def test_bad_selection_and_ha_are_rejected_offline(self):
        for override in ({"ucaas_services": ""}, {"ucaas_services": "unknown"}, {"ucaas_secondary_wan": "ge5"},
                         {"gateway_name_b": "peer", "wan1_interface_name": "ge5"}, {"ucaas_local_breakout": "true"}):
            with self.subTest(override=override):
                self.assertTrue(validate_rows([row(**override)]).issues)

    def test_catalog_or_inventory_errors_are_safe_and_block_all_writes(self):
        with patch("ucaas_breakout.load_catalog", side_effect=ValueError("secret body")):
            plan = self.engine.plan(validate_rows([row()]))
        self.assertEqual(plan.issues[0].field, "ucaas_catalog")
        self.assertNotIn("secret", json.dumps(public_issues(plan.issues)))
        self.engine.execute(plan)
        self.engine.create_site.assert_not_called()

    def test_complete_group_pagination(self):
        self.groups += [dict(group_id=i, name=str(i), type="network", owner="system") for i in range(5, 250)]
        self.assertEqual(len(self.engine.local_breakout.inventory()), len(self.groups))
        self.assertEqual(len(self.engine.get_json.call_args_list), 3)

    def test_multiple_sites_share_exact_objects_without_duplicate_creation(self):
        plan = self.plan().sites[0].ucaas_plan
        first, second = {"objects": []}, {"objects": []}
        ids = self.engine.local_breakout.ensure_objects(plan, first)
        writes = self.engine.post_json.call_count
        self.assertEqual(ids, self.engine.local_breakout.ensure_objects(plan, second))
        self.assertEqual(self.engine.post_json.call_count, writes)
        self.assertTrue(all(o["status"] == "reused" for o in second["objects"]))

    def test_exact_ports_reused_but_broader_ports_not_reused(self):
        broad = self.add_group("l4port", ["udp:3478-3482"], name="Teams Media Ports")
        exact = self.add_group("l4port", ["UDP:3480-3481", "udp:3478,3479"], name="Existing Teams Media Ports")
        plan = self.plan(ucaas_services="teams").sites[0].ucaas_plan
        media = next(r for r in plan.rules if r["key"] == "media")
        ports = next(o for o in plan.objects if o["key"] == media["port_key"])
        self.assertEqual(ports["id"], exact["group_id"])
        self.assertNotEqual(ports["id"], broad["group_id"])
        self.assertEqual(ports["values"], ["udp:3478-3481"])

    def test_two_rules_use_verified_objects_and_create_all_objects_first(self):
        plan = self.plan(ucaas_services="teams,zoom,webex,meet")
        result = self.engine.execute(plan)
        self.assertEqual(result.exit_code, 0)
        policies = result.sites[0].ucaas["policies"]
        expected = plan.sites[0].ucaas_plan
        self.assertEqual(len(policies), 2)
        events = [url for url, _ in self.events[1:]]
        self.assertLess(max(i for i, url in enumerate(events) if url.endswith('/groups')),
                        min(i for i, url in enumerate(events) if url.endswith('/pbr/policies')))
        for rule in expected.rules:
            deployed = next(p for p in self.policy_rows if p.get("display_name") == rule["name"])
            self.assertGreater(deployed["dst_port_group_id"], 0)
            port = next(g for g in self.groups if g["group_id"] == deployed["dst_port_group_id"])
            self.assertEqual(port["type"], "l4port")
            self.assertEqual(effective_values(port, "l4port", []), rule["ports"])
            actual = [g for g in self.groups if g["group_id"] in deployed["dst_group_id_set"]]
            wanted = [o for o in expected.objects if o["key"] in rule["destination_keys"]]
            self.assertEqual(sorted((g["type"], effective_values(g, g["type"], [])) for g in actual),
                             sorted((o["type"], o["values"]) for o in wanted))
        self.assertTrue(all(p["status"] == "verified" for p in policies))

    def test_every_service_selection_keeps_all_original_destination_port_coverage(self):
        def pairs(values):
            result = set()
            for entry in values:
                protocol, ports = entry.split(':')
                for part in ports.split(','):
                    limits = list(map(int, part.split('-')))
                    result.update((protocol, port) for port in range(limits[0], limits[-1] + 1))
            return result
        for count in range(1, 5):
            for selection in combinations(('teams', 'zoom', 'webex', 'meet'), count):
                with self.subTest(selection=selection):
                    plan = self.plan(ucaas_services=','.join(selection)).sites[0].ucaas_plan
                    self.assertEqual([r['name'] for r in plan.rules], ['UCaaS Web Breakout', 'UCaaS Media Breakout'])
                    objects = {o['key']:o for o in plan.objects}
                    published = set()
                    for service in plan.services:
                        for endpoint in service['endpoints']:
                            published |= pairs(endpoint['ports'])
                            for kind, field in (('network', 'ipv4'), ('domains', 'domains')):
                                if not endpoint[field]:
                                    continue
                                matches = [o for o in plan.objects if o['type']==kind and o['values']==endpoint[field]]
                                self.assertEqual(len(matches), 1)
                                covered = set().union(*(pairs(r['ports']) for r in plan.rules if matches[0]['key'] in r['destination_keys']))
                                self.assertTrue(pairs(endpoint['ports']) <= covered)
                    self.assertEqual(set().union(*(pairs(r['ports']) for r in plan.rules)), published)
                    self.assertEqual(set(plan.rules[0]['destination_keys']), {o['key'] for o in plan.objects if o['type']!='l4port'})
                    self.assertTrue(all(objects[k]['type']=='network' for k in plan.rules[1]['destination_keys']))
                    self.assertEqual({o['service'] for o in plan.objects if o['type']!='l4port'}, set(selection))

    def test_default_combined_ports_and_optional_meet_are_exact(self):
        plan = self.plan(ucaas_services='teams,zoom,webex').sites[0].ucaas_plan
        self.assertEqual(plan.rules[0]['ports'], ['tcp:80,443', 'udp:443'])
        self.assertEqual(plan.rules[1]['ports'], ['tcp:5004,8801-8802', 'udp:3478-3481,5004,8801-8810,9000'])
        self.assertEqual(len(plan.objects), 12)
        expanded = self.plan(ucaas_services='teams,zoom,webex,meet').sites[0].ucaas_plan
        self.assertEqual(expanded.rules[1]['ports'], ['tcp:5004,8801-8802', 'udp:3478-3481,5004,8801-8810,9000,19302-19309'])
        self.assertEqual(len(expanded.rules), 2)
        meet = self.plan(ucaas_services='meet').sites[0].ucaas_plan
        self.assertEqual(meet.rules[0]['ports'], ['tcp:443', 'udp:443'])
        self.assertEqual(meet.rules[1]['ports'], ['udp:3478,19302-19309'])

    def test_web_port_boundaries_do_not_drop_adjacent_published_media_ports(self):
        web, media = split_breakout_ports(['tcp:79-81,442-444', 'udp:442-444'])
        self.assertEqual(web, ['tcp:80,443', 'udp:443'])
        self.assertEqual(media, ['tcp:79,81,442,444', 'udp:442,444'])

    def test_changed_reviewed_port_object_prevents_any_object_or_policy_write(self):
        group = self.add_group("l4port", ["udp:3478-3481"])
        plan = self.plan(ucaas_services="teams")
        group["member_attributes"]["port_group"] = ["tcp:3478-3481"]
        result = self.engine.execute(plan)
        self.assertEqual(result.sites[0].diagnostics["UCaaS local breakout"], "ucaas_changed")
        self.engine.post_json.assert_not_called()

    def test_mid_rule_failure_retains_confirmed_ids_and_does_not_retry_or_reorder(self):
        def post(url, payload, **kwargs):
            if url.endswith('/pbr/policies') and len(self.policy_rows) == 3:
                raise requests.Timeout("unknown outcome")
            return self.post(url, payload, **kwargs)
        self.engine.post_json.side_effect = post
        result = self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].status, "partial")
        policies = result.sites[0].ucaas["policies"]
        self.assertEqual(len(policies), 2)
        self.assertEqual((policies[0]["id"], policies[0]["status"]), (30, "created"))
        self.assertEqual(policies[1]["status"], "unconfirmed")
        self.assertGreater(policies[1]["port_id"], 0)
        self.assertFalse(self.reorders)

    def test_wrong_port_readback_cannot_be_reported_as_success(self):
        def post(url, payload, **kwargs):
            response = self.post(url, payload, **kwargs)
            if url.endswith('/pbr/policies'):
                self.policy_rows[-1]["dst_port_group_id"] = -1
            return response
        self.engine.post_json.side_effect = post
        result = self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].status, "partial")
        self.assertEqual(result.sites[0].diagnostics["UCaaS local breakout"], "ucaas_policy_unconfirmed")
        self.assertFalse(self.reorders)

    def test_port_change_alone_invalidates_review_digest(self):
        first = plan_digest(self.plan())
        self.catalog["services"][0]["endpoints"][0]["ports"] = ["udp:3478-3480"]
        self.assertNotEqual(first, plan_digest(self.plan()))

    def test_reorder_failure_keeps_ids_for_manual_recovery(self):
        original = self.post
        def post(url, payload, **kwargs):
            if url.endswith("policies-reorder"):
                raise requests.Timeout("private error")
            return original(url, payload, **kwargs)
        self.engine.post_json.side_effect = post
        result = self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].ucaas["policies"][0]["id"], 30)
        self.assertEqual(result.sites[0].status, "partial")
        self.assertEqual(result.sites[0].diagnostics["UCaaS local breakout"], "ucaas_order_unconfirmed")
        self.assertEqual(sum(call.args[0].endswith("policies-reorder") for call in self.engine.post_json.call_args_list), 1)

    def test_wrong_gateway_policy_inventory_is_rejected_before_object_writes(self):
        self.policy_rows[0]["gateway_id"] = "other-site"
        result = self.engine.execute(self.plan())
        self.assertEqual(result.sites[0].diagnostics["UCaaS local breakout"], "ucaas_changed")
        self.engine.post_json.assert_not_called()

    def test_ui_csv_roundtrip_keeps_optional_settings(self):
        import base64
        import io
        import zipfile
        from app import export_batch, import_batch, validate_batch
        fields = row(ucaas_services="zoom,meet", ucaas_secondary_wan="ge6", ucaas_path_selection="balanced")
        fields.pop("vlans")
        batch = [{"fields": fields, "vlans": []}]
        checked = validate_batch(batch)
        self.assertTrue(checked["valid"], checked["issues"])
        self.assertEqual(checked["sites"][0]["ucaas"]["path_selection"], "Balanced")
        exported = export_batch(batch)
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content"]))) as archive:
            files = [{"name": name, "content": archive.read(name).decode()} for name in archive.namelist() if name.endswith('.csv')]
        imported = import_batch(files)
        for key in ("ucaas_local_breakout", "ucaas_services", "ucaas_secondary_wan", "ucaas_path_selection"):
            self.assertEqual(imported[0]["fields"][key], fields[key])

    def test_optional_template_clone_precedes_site_objects_and_policy(self):
        with patch("deployment_engine.TemplateCloner") as factory:
            cloner = factory.return_value
            cloner.inventory.return_value = []
            clone = Mock(source_name="Source")
            clone.name = "NewBranch"
            clone.summary.return_value = {"name": "NewBranch", "source_name": "Source", "source_id": "template-id"}
            cloner.plan.return_value = clone
            cloner.create.side_effect = lambda *_: self.events.append("clone") or "cloned-id"
            result = self.engine.execute(self.plan(template_mode="clone"))
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.events[:2], ["clone", "site"])
        self.assertEqual(self.engine.create_site.call_args.args[0], "cloned-id")


def publications():
    """Small vendor-table fixtures retain destination/protocol/port pairings."""
    def table(rows):
        return '<table>' + ''.join('<tr>' + ''.join('<td>' + cell + '</td>' for cell in row) + '</tr>' for row in rows) + '</table>'
    zoom = ('<h3>Firewall rules for Zoom</h3>' + table([
        ['TCP', '80, 443', '*', '<p>*.zoom.us</p><p>*.zoom.com</p><p>gstatic.com</p>'],
        ['UDP', '443', '*', '<p>*.zoom.us</p><p>*.zoom.com</p>']]) +
        '<h3>Firewall rules for Zoom Meetings and Webinars</h3>' + table([
        ['TCP', '443, 8801, 8802', '*', '3.7.35.0/25'],
        ['UDP', '3478, 3479, 8801-8810', '*', '3.7.35.0/25']]) + '<h3>Zoom Phone</h3>')
    webex = table([
        ['443', 'TLS', 'Webex HTTPS signaling', 'Webex App'],
        ['5004 and 9000', 'SRTP over UDP', 'IP subnets for Webex media services', 'Webex App'],
        ['5004', 'SRTP over TCP', 'IP subnets for Webex media services', 'Webex App'],
        ['443', 'SRTP over TLS', 'IP subnets for Webex media services', 'Webex App'],
        ['50000-53000', 'SRTP over UDP', 'IP subnets for Webex media services', 'Video Mesh Node']])
    return {
        "microsoft": json.dumps([dict(id=11, required=True, serviceArea="Skype", ips=["52.112.0.0/14", "2603:1063::/38"], udpPorts="3478-3481"),
                                  dict(id=12, required=True, serviceArea="Skype", urls=["*.teams.microsoft.com"], tcpPorts="80,443", udpPorts="443"),
                                  dict(id=99, required=True, serviceArea="Exchange", ips=["40.96.0.0/13"], tcpPorts="443")]),
        "zoom": '<script type="application/ld+json">' + json.dumps({"articleBody": zoom}) + '</script>',
        "zoom_ipv4": "3.7.35.0/25\n", "zoom_ipv6": "2607:fb90::/32\n",
        "webex": webex + '<h3>IPv4 Subnets for Media Services</h3><p>23.89.0.0/16</p><h3>IPv6 Address Ranges for Media Services</h3><p>2402:2500::/34</p><p>* Azure data centers</p><h3>Cisco Webex Services URLs</h3><p>*.webex.com</p><p>*.webexapis.com</p><h3>Additional Webex related services - Cisco Owned domains</h3><p>*.unrelated.com</p><h3>History</h3><p>18.230.160.0/25 removed</p>',
        "google": '<h3>Step 1: Set up outbound ports for media traffic</h3><p>For audio and video, set up outbound UDP ports 3478 and 19302–19309.</p><p>For web traffic and user authentication, use outbound UDP and TCP port 443.</p><h3>Step 2: Allow access</h3><h3>Domains for static resources</h3><p>www.gstatic.com</p><p>meet.google.com</p><h3>Domains for user feedback &amp; event log uploads</h3><p>https://www.google.com/tools/feedback</p><h3>Step 3: Allow access to Google IP address ranges</h3><p>support Meet traffic over port 443</p><p>74.125.250.0/24</p><p>2001:4860:4864:5::0/64</p><p>SNI: workspace.turns.goog</p><h3>Step 4: Review bandwidth requirements</h3>',
    }


class CatalogTests(unittest.TestCase):
    def test_port_normalization_rejects_any_invalid_ranges_and_protocols(self):
        self.assertEqual(canonical_ports(["UDP:3479,3478", "tcp:443,80", "udp:3480-3481"]),
                         ["tcp:80,443", "udp:3478-3481"])
        for value in ([], ["any"], ["tcp:1-65535"], ["udp:0"], ["udp:65536"],
                      ["udp:3481-3478"], ["icmp:443"], ["tcp:443,unknown"], ["udp:1-30000,30001-65535"]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                canonical_ports(value)

    def test_vendor_ports_are_scoped_to_media_web_and_signaling(self):
        data = parse_sources(publications())
        endpoints = {s["id"]: {e["label"]: e for e in s["endpoints"]} for s in data["services"]}
        self.assertEqual(endpoints["teams"]["Media"]["ports"], ["udp:3478-3481"])
        self.assertEqual(endpoints["teams"]["Media"]["domains"], [])
        self.assertEqual(endpoints["zoom"]["Media"]["ports"], ["tcp:443,8801-8802", "udp:3478-3479,8801-8810"])
        self.assertEqual(endpoints["zoom"]["Web"]["ipv4"], [])
        self.assertEqual(endpoints["webex"]["Media"]["ports"], ["tcp:443,5004", "udp:5004,9000"])
        self.assertEqual(endpoints["webex"]["Signaling"]["ports"], ["tcp:443"])
        self.assertEqual(endpoints["meet"]["Media"]["ports"], ["udp:3478,19302-19309"])
        self.assertEqual(endpoints["meet"]["Media TLS"]["ports"], ["tcp:443"])

    def test_bad_port_publications_fail_without_widening_or_dropping_rules(self):
        for key, before, after in (("microsoft", "3478-3481", "1-65535"),
                                   ("zoom", "8801-8810", "8801-8810 or any"),
                                   ("webex", "SRTP over TCP", "New TCP format"),
                                   ("google", "UDP ports 3478", "UDP ports unknown")):
            source = publications()
            source[key] = source[key].replace(before, after)
            with self.subTest(key=key), self.assertRaises(ValueError):
                parse_sources(source)

    def test_scoped_parsing_excludes_ipv6_history_other_products_and_shared_zoom_dependencies(self):
        data = validate_catalog(parse_sources(publications()))
        by_id = {s["id"]: s for s in data["services"]}
        self.assertEqual(by_id["teams"]["ipv4"], ["52.112.0.0/14"])
        self.assertEqual(by_id["webex"]["ipv4"], ["23.89.0.0/16"])
        self.assertEqual(by_id["zoom"]["domains"], ["*.zoom.com", "*.zoom.us"])
        self.assertNotIn("www.google.com", by_id["meet"]["domains"])
        self.assertTrue(all(s["excluded_ipv6"] for s in data["services"]))

    def test_malformed_or_empty_feed_is_rejected(self):
        for key, value in (("zoom_ipv4", "<html>Error</html>"), ("zoom_ipv4", "0.0.0.0/0"),
                           ("webex", "new format"), ("microsoft", "[]"), ("zoom_ipv4", "10.0.0.0/8")):
            with self.subTest(key=key), self.assertRaises((ValueError, KeyError)):
                parse_sources({**publications(), key: value})

    def test_failed_refresh_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text("previous")
            with self.assertRaises(ValueError):
                refresh_catalog(path=path, fetch=lambda _: "invalid")
            self.assertEqual(path.read_text(), "previous")

    def test_refresh_and_staleness(self):
        fixture = publications()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            by_url = {url: fixture[key] for key, url in SOURCES.items()}
            refresh_catalog(path=path, fetch=by_url.__getitem__)
            self.assertEqual(len(load_catalog(path=path, fresh=True)["services"]), 4)
            data = json.loads(path.read_text())
            data["retrieved_at"] = (datetime.now(timezone.utc)-timedelta(days=31)).isoformat()
            path.write_text(json.dumps(data))
            self.assertEqual(len(load_catalog(path=path)["services"]), 4)
            with self.assertRaises(ValueError):
                load_catalog(path=path, fresh=True)


if __name__ == "__main__":
    unittest.main()
