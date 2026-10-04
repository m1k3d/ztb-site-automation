from concurrent.futures import ThreadPoolExecutor
import json
import io
import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

from app import Handler, normalize_batch
from project_store import ProjectConflict, ProjectStore


def draft():
    return {"version": 1, "batch": [{
        "fields": {"site_name": "Unfinished branch", "post": "0", "gateway_name": "Custom-GW", "location_type": "new", "custom_column": "keep me"},
        "vlans": [{"name": "Users", "dhcp_service": "inherit", "dhcp_start": "10.0.0.50", "dhcp_end": "10.0.0.90"}],
        "ha_enabled": False, "wan_modes": {"0": "static", "1": "dhcp"},
        "gateway_b_draft": {"fields": {"gateway_name_b": "Saved-peer"}, "mode": "static"},
        "reference": {"id": "source-id", "name": "Reference"}, "interface_gateways": ["Gateway-1"],
        "editor": {"location_use_site_name": False, "gateway_names": {"gateway_name": None, "gateway_name_b": "-GW-B"},
                   "vlans": [{"range_mode": "custom", "dns_mode": "custom", "zone_manual": True}]},
    }], "current": 0, "active_tab": "vlans"}


class ProjectStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ProjectStore(self.directory.name, normalize_batch)
        self.identifier = uuid4().hex

    def create(self, workspace=None):
        return self.store.create(self.identifier, "Netherlands rollout", workspace or draft())

    def test_incomplete_draft_and_editor_choices_survive_store_restart(self):
        original = draft()
        self.create(original)
        restarted = ProjectStore(self.directory.name, normalize_batch)
        self.assertEqual(restarted.load(self.identifier)["workspace"], original)
        self.assertEqual(restarted.list()[0]["site_count"], 1)
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_save_and_rename_do_not_change_other_project(self):
        self.create()
        other = uuid4().hex
        self.store.create(other, "Another rollout", draft())
        changed = draft(); changed["batch"][0]["fields"]["city"] = "Amsterdam"
        saved = self.store.save(self.identifier, 1, "Renamed rollout", changed)
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(self.store.load(self.identifier)["workspace"], changed)
        self.assertEqual(self.store.load(other)["workspace"], draft())

    def test_stale_tab_cannot_overwrite_newer_save(self):
        self.create()
        changed = draft(); changed["batch"][0]["fields"]["site_name"] = "Newer version"
        self.store.save(self.identifier, 1, "Updated", changed)
        with self.assertRaises(ProjectConflict):
            self.store.save(self.identifier, 1, "Old tab", draft())
        self.assertEqual(self.store.load(self.identifier)["workspace"], changed)

    def test_two_connections_cannot_both_save_the_same_revision(self):
        self.create()
        other = ProjectStore(self.directory.name, normalize_batch)
        def save(store, name):
            try:
                return store.save(self.identifier, 1, name, draft())["revision"]
            except ProjectConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda args: save(*args), [(self.store, "Tab A"), (other, "Tab B")]))
        self.assertCountEqual(results, [2, "conflict"])

    def test_lost_response_can_replay_create_and_save_without_duplicates(self):
        first = self.create()
        self.assertEqual(self.create(), first)
        saved = self.store.save(self.identifier, 1, "Renamed", draft())
        self.assertEqual(self.store.save(self.identifier, 1, "Renamed", draft()), saved)
        self.assertEqual(len(self.store.list()), 1)

    def test_failed_validation_preserves_saved_project(self):
        self.create()
        for workspace in ({}, {**draft(), "current": 50}, {**draft(), "active_tab": "unknown"}, {**draft(), "batch": [None]}):
            with self.assertRaises(ValueError):
                self.store.save(self.identifier, 1, "Broken", workspace)
        self.assertEqual(self.store.load(self.identifier)["workspace"], draft())
        self.assertEqual(self.store.load(self.identifier)["revision"], 1)

    def test_credentials_and_runtime_approval_are_not_saved(self):
        workspace = draft()
        workspace["connections"] = {"api_key": "secret-one"}
        workspace["deploymentJob"] = {"approval": "secret-two"}
        workspace["batch"][0]["fields"].update(API_KEY="secret-three", client_secret="secret-four", refresh_token="secret-five", ZTB_API_KEY="secret-seven", ztb_bearer="secret-eight")
        workspace["batch"][0]["credentials"] = {"password": "secret-six"}
        self.create(workspace)
        text = json.dumps(self.store.load(self.identifier))
        self.assertNotIn("secret-", text)
        self.assertIn("keep me", text)
        self.assertNotIn("secret-", self.store.path.read_bytes().decode(errors="ignore"))

    def test_invalid_identifiers_names_and_revisions_are_rejected(self):
        for identifier in ("../outside", "x' OR 1=1", None):
            with self.assertRaises(ValueError):self.store.load(identifier)
        for name in ("", " " * 3, "n" * 121, None):
            with self.assertRaises(ValueError):self.store.create(self.identifier, name, draft())
        self.create()
        for revision in (True, "1", 0):
            with self.assertRaises(ValueError):self.store.save(self.identifier, revision, "Rollout", draft())

    def request(self, endpoint, payload, token="local-test"):
        handler = object.__new__(Handler)
        handler.path = "/api/projects/" + endpoint
        body = json.dumps(payload).encode()
        handler.headers = {"Host": "127.0.0.1:8765", "Origin": "http://127.0.0.1:8765",
                           "X-Local-Token": token, "Content-Type": "application/json", "Content-Length": str(len(body))}
        handler.server = Mock(token="local-test", server_address=("127.0.0.1", 8765), projects=self.store)
        handler.rfile = io.BytesIO(body)
        handler.reply = Mock()
        handler.do_POST()
        handler.server.deployment.assert_not_called()
        self.assertEqual(handler.server.deployment.method_calls, [])
        self.assertEqual(handler.server.references.method_calls, [])
        return handler.reply.call_args.args

    def test_project_endpoints_round_trip_without_deployment_or_credentials(self):
        status, created = self.request("create", {"id": self.identifier, "name": "Project", "workspace": draft()})
        self.assertEqual(status, 200)
        status, loaded = self.request("load", {"id": created["id"]})
        self.assertEqual(loaded["workspace"], draft())
        status, saved = self.request("save", {"id": self.identifier, "revision": 1, "name": "Updated", "workspace": draft()})
        self.assertEqual(saved["revision"], 2)
        self.assertEqual(self.request("list", {})[1]["projects"][0]["name"], "Updated")
        self.assertEqual(self.request("save", {"id": self.identifier, "revision": 1, "name": "Stale", "workspace": draft()})[0], 409)
        self.assertEqual(self.request("load", {"id": self.identifier}, token="wrong")[0], 403)
