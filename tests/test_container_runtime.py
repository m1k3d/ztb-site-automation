"""Container configuration, persistent catalogs, and local request boundaries."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app import Handler, LocalServer, main
from runtime_paths import ROOT, catalog_path, data_directory
from ucaas_catalog import CATALOG_PATH, SOURCES, load_catalog, refresh_catalog
from test_ucaas import publications


class ContainerRuntimeTests(unittest.TestCase):
    def test_native_paths_stay_compatible(self):
        with patch.dict(os.environ, {"ZTB_DATA_DIR": ""}):
            self.assertEqual(data_directory(), ROOT / "out")
            self.assertEqual(catalog_path(), CATALOG_PATH)

    def test_saved_catalog_is_shared_with_fresh_worker_and_preserved_on_failure(self):
        original = CATALOG_PATH.read_bytes()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ZTB_DATA_DIR": directory}):
            self.assertEqual(load_catalog(), load_catalog(path=CATALOG_PATH))
            self.assertFalse(catalog_path().exists())  # Bundled seed stays read-only.
            sources = publications()
            by_url = {url: sources[key] for key, url in SOURCES.items()}
            saved = refresh_catalog(fetch=by_url.__getitem__)
            self.assertEqual(catalog_path().parent, Path(directory).resolve() / "catalog")
            output = subprocess.check_output([
                sys.executable, "-c", "import json; from ucaas_catalog import load_catalog; print(json.dumps(load_catalog()))"
            ], cwd=ROOT, text=True)
            self.assertEqual(json.loads(output), saved)
            with self.assertRaises(ValueError):
                refresh_catalog(fetch=lambda _: "invalid")
            self.assertEqual(load_catalog(), saved)
        self.assertEqual(CATALOG_PATH.read_bytes(), original)

    def test_invalid_persistent_catalog_does_not_silently_use_bundled_values(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ZTB_DATA_DIR": directory}):
            catalog_path().parent.mkdir()
            catalog_path().write_text("not a catalog")
            with self.assertRaises(ValueError):
                load_catalog()
            with self.assertRaises(FileNotFoundError):
                load_catalog(path=Path(directory) / "missing.json")

    def test_server_projects_survive_recreation_and_reports_use_same_state_root(self):
        workspace = {"version": 1, "batch": [], "current": -1, "active_tab": "site"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ZTB_DATA_DIR": directory}), \
                patch("app.ThreadingHTTPServer.__init__", return_value=None):
            first = LocalServer(("0.0.0.0", 8765), env_file=str(Path(directory) / "absent.env"))
            first.projects.create("a" * 32, "Container draft", workspace)
            second = LocalServer(("0.0.0.0", 8765), env_file=str(Path(directory) / "absent.env"))
            self.assertEqual(second.projects.load("a" * 32)["workspace"], workspace)
            self.assertEqual(second.deployment.report_dir, Path(directory).resolve() / "runs")
            self.assertIsNone(second.references.client)

    def test_container_binding_preserves_host_origin_and_token_checks(self):
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(server_address=("0.0.0.0", 9876), token="expected-token")
        handler.path = "/healthz"
        for host, status in (("127.0.0.1:9876", 200), ("localhost:9876", 200),
                             ("example.com:9876", 403), ("localhost:8765", 403)):
            handler.headers = {"Host": host}
            handler.reply = Mock()
            handler.do_GET()
            self.assertEqual(handler.reply.call_args.args[0], status)
        handler.path = "/api/projects/list"
        handler.server.projects = Mock()
        for origin, token in (("https://example.com", "expected-token"),
                              ("http://localhost:9876", "wrong-token")):
            handler.headers = {"Host": "localhost:9876", "Origin": origin, "X-Local-Token": token}
            handler.reply = Mock()
            handler.do_POST()
            self.assertEqual(handler.reply.call_args.args[0], 403)
        handler.server.projects.list.assert_not_called()

    def test_cli_uses_published_port_and_passes_state_to_spawned_processes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"ZTB_PORT": "9876", "ZTB_DATA_DIR": ""}), \
                patch("app.LocalServer") as server_class, patch("sys.stdout", new_callable=io.StringIO):
            server = server_class.return_value.__enter__.return_value
            server.server_address = ("0.0.0.0", 9876)
            server.serve_forever.side_effect = KeyboardInterrupt
            main(["--bind", "0.0.0.0", "--data-dir", directory, "--no-browser"])
            self.assertEqual(server_class.call_args.args[0], ("0.0.0.0", 9876))
            self.assertEqual(data_directory(), Path(directory).resolve())

    def test_invalid_port_rejected_before_opening_server(self):
        with patch("app.LocalServer") as server, patch("sys.stderr", new_callable=io.StringIO):
            for port in ("0", "65536", "not-a-port"):
                with self.assertRaises(SystemExit):
                    main(["--port", port, "--no-browser"])
            server.assert_not_called()
