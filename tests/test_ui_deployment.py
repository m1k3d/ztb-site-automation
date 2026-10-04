from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from queue import SimpleQueue, Empty
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from automation_config import Settings
from deployment_engine import DeploymentEngine
from ui_deployment import DeploymentSession, run_worker


def rows():
    return [{"site_name": "Branch", "gateway_name": "Gateway", "template_id": "template-1",
             "wan_interface_name": "ge5", "location_type": "none", "post": "1", "vlans": []}]


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.references = Mock()
        self.config = Settings(ztb_api_base="https://example.invalid", api_key="secret", bearer="private-token")
        self.references.deployment_settings.return_value = (1, self.config)
        self.session = DeploymentSession(self.references, "/unused")
        self.start = patch.object(self.session, "_start").start()
        self.addCleanup(patch.stopall)

    def ready(self):
        job = self.session.preview(rows())
        self.session.job.update(state="ready", finished=time.time())
        self.session.resolved = "digest"
        return job["id"]

    def test_approval_is_consumed_once_and_status_never_exposes_credentials(self):
        identifier = self.ready()
        self.session.deploy(identifier, rows())
        self.session.deploy(identifier, rows())
        self.assertEqual(self.start.call_count, 2)  # One preview and one execution.
        for word in ("private-token", "secret", "api_key"):
            self.assertNotIn(word, json.dumps(self.session.status()))
        self.session.job["state"] = "interrupted"
        self.session.deploy(identifier, rows())
        self.assertEqual(self.start.call_count, 2)

    def test_edits_wrong_id_expiry_and_connection_changes_reject_approval(self):
        identifier = self.ready()
        changed = rows();changed[0]["site_name"] = "Changed"
        for job_id, batch in (("wrong", rows()), (identifier, changed)):
            with self.assertRaises(ValueError):
                self.session.deploy(job_id, batch)
        self.session.job["finished"] = time.time() - 301
        with self.assertRaisesRegex(ValueError, "expired"):
            self.session.deploy(identifier, rows())
        self.session.job["finished"] = time.time()
        self.references.deployment_settings.return_value = (2, self.config)
        with self.assertRaisesRegex(ValueError, "connection changed"):
            self.session.deploy(identifier, rows())
        self.assertEqual(self.start.call_count, 1)

    def test_connection_change_and_second_preview_block_while_running(self):
        self.session.preview(rows())
        action = Mock()
        with self.assertRaises(ValueError):self.session.connection_action(action)
        with self.assertRaises(ValueError):self.session.preview(rows())
        action.assert_not_called()

    def test_connection_change_invalidates_ready_preview_even_if_connect_fails(self):
        self.ready()
        with self.assertRaises(RuntimeError):
            self.session.connection_action(Mock(side_effect=RuntimeError("failed")))
        self.assertEqual(self.session.status()["state"], "expired")

    def test_invalid_input_and_ha_block_before_worker_start(self):
        for batch in ([], [{**rows()[0], "gateway_name": ""}],
                      [{**rows()[0], "gateway_name_b": "B", "wan1_interface_name": "ge5"}]):
            with self.assertRaises(ValueError):self.session.preview(batch)
        self.start.assert_not_called()

    def test_shutdown_prevents_late_requests_from_starting_a_new_worker(self):
        identifier = self.ready()
        self.start.reset_mock()
        self.session.close()
        self.session.close()  # Cleanup remains safe if called again.
        with self.assertRaisesRegex(ValueError, 'shutting down'):
            self.session.preview(rows())
        with self.assertRaisesRegex(ValueError, 'shutting down'):
            self.session.deploy(identifier, rows())
        self.start.assert_not_called()


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Settings(ztb_api_base="https://example.invalid", bearer="private-token")
        self.engine = DeploymentEngine(self.config, emit=lambda *_: None)
        self.engine.get_template_settings = Mock(return_value={"deployment_type":"standalone", "dhcp_service":"server"})
        self.engine.ensure_template_id_for_row = Mock(return_value=(True, "template-1", ""))
        self.engine.list_site_inventory = Mock(return_value=[])
        self.engine.site_exists = Mock(return_value=False)
        self.engine.create_site = Mock(return_value=(True, "", 1))
        self.engine.resolve_gateway_ids_and_cluster = Mock(return_value=("gw-1", 1))
        self.engine.resolve_site_id = Mock(return_value="site-1")
        self.engine.process_vlans_for_site = Mock(return_value=True)
        self.signals = patch("ui_deployment.ignore_terminal_signals").start()
        self.factory = patch("ui_deployment.DeploymentEngine", return_value=self.engine).start()
        self.capture = patch('ui_deployment.capture_results').start()
        self.http = patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected network")).start()
        self.addCleanup(patch.stopall)

    def run_job(self, mode="preview", expected=None, batch=None):
        queue = SimpleQueue()
        run_worker(batch or rows(), self.config, mode, expected, self.directory.name, queue)
        events = []
        while True:
            try:events.append(queue.get_nowait())
            except Empty:break
        self.http.assert_not_called()
        return events

    def test_preview_only_reads_and_deployment_rechecks_then_reports(self):
        preview = self.run_job()[-1]
        self.signals.assert_called_once_with()
        self.assertEqual(preview["state"], "ready")
        self.engine.create_site.assert_not_called()
        result = self.run_job("deployment", preview["plan_digest"])[-1]
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.engine.create_site.call_count, 1)
        self.assertEqual(self.engine.site_exists.call_count, 3)
        self.assertIsNone(self.factory.call_args.args[0].env_path)
        self.assertTrue((Path(self.directory.name) / result["report"]).exists())

    def test_unreadable_inventory_blocks_preview_with_safe_diagnostic(self):
        self.engine.list_site_inventory.side_effect = ValueError('private-token raw response')
        event = self.run_job()[-1]
        self.assertEqual(event['state'], 'blocked')
        site = event['sites'][0]
        self.assertEqual(site['status'], 'lookup_failed')
        self.assertIn('completely', site['diagnostics']['Existing site check'])
        self.assertIn('diagnostic', site['next_action'])
        self.assertNotIn('private-token', json.dumps(event))
        self.engine.create_site.assert_not_called()

    def test_changed_plan_and_existing_site_block_before_creation(self):
        self.assertEqual(self.run_job("deployment", "wrong")[-1]["state"], "blocked")
        self.engine.site_exists.return_value = True
        result = self.run_job()[-1]
        self.assertEqual(result["sites"][0]["status"], "already_exists")
        self.engine.create_site.assert_not_called()

    def test_partial_failure_reports_failed_stage_without_raw_secrets(self):
        batch = rows()
        batch[0]["vlans"] = [{"name": "LAN", "tag": "10", "subnet": "24", "default_gateway": "10.0.0.1", "interface": "ge2"}]
        preview = self.run_job(batch=batch)[-1]
        self.engine.process_vlans_for_site.side_effect = RuntimeError("private-token raw response")
        events = self.run_job("deployment", preview["plan_digest"], batch)
        result = events[-1]
        self.assertEqual(result["state"], "incomplete")
        self.assertFalse(result["sites"][0]["stages"]["VLANs"])
        self.assertNotIn("private-token", json.dumps(events))
        for path in Path(self.directory.name).iterdir():
            self.assertNotIn("private-token", path.read_text())

    def test_preflight_failure_returns_safe_action_and_creates_nothing(self):
        self.engine.ensure_template_id_for_row.side_effect = RuntimeError("private-token response")
        result = self.run_job()[-1]
        self.assertEqual(result["state"], "blocked")
        self.assertIn("template", result["issues"][0]["message"])
        self.assertNotIn("private-token", json.dumps(result))
        self.engine.create_site.assert_not_called()

    def test_deployment_persists_diagrams_and_additional_wans(self):
        from site_diagrams import planned
        def document(engine, plan, result, **kwargs):
            for prepared, outcome in zip(plan.sites, result.sites):
                outcome.diagram = planned(prepared.row, prepared.vlans, prepared.template_settings)
                outcome.additional_wans = {'status': 'verified', 'networks': []}
        self.capture.side_effect = document
        preview = self.run_job()[-1]
        final = self.run_job('deployment', preview['plan_digest'])[-1]
        self.assertEqual(final['sites'][0]['artifacts']['formats'], ['svg', 'vsdx', 'png'])
        report = json.loads((Path(self.directory.name)/final['report']).read_text())
        self.assertEqual(report['sites'][0]['additional_wans']['status'], 'verified')
        self.assertTrue(report['sites'][0]['diagrams'])

    def test_missing_loopback_reports_staging_warning_and_posts_all_vlans(self):
        batch = rows()
        batch[0]["vlans"] = [
            {"name": "MGMT", "tag": "1", "subnet": "32", "default_gateway": "10.0.0.1", "interface": "lo0", "dhcp_service": "off"},
            {"name": "LAN", "tag": "20", "subnet": "24", "default_gateway": "10.0.20.1", "interface": "ge2"},
        ]
        self.engine.process_vlans_for_site = DeploymentEngine.process_vlans_for_site.__get__(self.engine)
        self.engine.get_gateway_interfaces_v2 = Mock(return_value=[{
            "gateway_id": "gw-1", "interfaces": [{"id": "physical", "name": "ge2"}]}])
        self.engine.post_vlan = Mock(return_value=(True, ""))
        self.engine.list_site_vlans_v2 = Mock(return_value=[dict(v, id=str(i)) for i, v in enumerate(batch[0]["vlans"])])
        self.engine.put_json = Mock(return_value=Mock(status_code=200))
        preview = self.run_job(batch=batch)[-1]
        result = self.run_job("deployment", preview["plan_digest"], batch)[-1]
        self.assertEqual(result["state"], "incomplete")
        site = result["sites"][0]
        self.assertTrue(site["stages"]["Site"])
        self.assertTrue(site["stages"]["VLANs"])
        self.assertFalse(site["stages"]["Loopback binding"])
        self.assertIn("lo0", site["diagnostics"]["Loopback binding"])
        self.assertIn("accepted for staging", site["diagnostics"]["Loopback binding"])
        self.assertEqual(self.engine.post_vlan.call_count, 2)
        self.assertEqual(self.engine.put_json.call_count, 2)
        path = Path(self.directory.name) / result["report"]
        self.assertEqual(json.loads(path.read_text())["sites"][0]["diagnostics"], site["diagnostics"])
        self.assertIn("accepted for staging", path.with_suffix(".txt").read_text())
