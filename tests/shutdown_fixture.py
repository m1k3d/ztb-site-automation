"""Offline app/worker fixture used by native and isolated Docker shutdown checks.

Run as a script with --data-dir. No tenant HTTP, credentials, or actual resources.
The controller releases the fake deployment by writing DATA/release.
"""
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

# Native test script and Docker's /app both import the real runtime modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from automation_config import Settings
from deployment_engine import BatchResult, SiteResult
from runtime_paths import data_directory
from ui_deployment import plan_digest, run_worker


def fake_plan():
    return SimpleNamespace(sites=[], issues=[], zpa_context=None)


def controlled_worker(rows, config, mode, expected_plan, report_dir, channel, project_id=''):
    folder = Path(report_dir).parent

    def execute(plan, *, dry_run=False, progress=None):
        if dry_run:
            return BatchResult()
        # run_worker already installed handlers and reserved its report.
        (folder/'worker-ready').write_text(str(os.getpid()))
        deadline = time.monotonic()+30
        while not (folder/'release').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('Fixture was not released')
            time.sleep(.01)
        # Exceed the queue pipe capacity, exercising the shutdown drain loop.
        for _ in range(16):
            progress('Test site', 'x'*131072, 'success')
        return BatchResult([SiteResult('Test site', 'success', {'Site':True})])

    engine = Mock()
    engine.plan.return_value = fake_plan()
    engine.execute.side_effect = execute
    with patch('ui_deployment.DeploymentEngine', return_value=engine), \
            patch('ui_deployment.capture_results'), patch('ui_deployment.DiagramStore'), \
            patch('requests.sessions.Session.request', side_effect=AssertionError('Unexpected network')):
        run_worker(rows, config, mode, expected_plan, report_dir, channel, project_id)


class FixtureServer(app.LocalServer):
    def __init__(self, address, env_file, project_dir):
        # Ephemeral socket: never open an existing workspace or fixed port.
        super().__init__(('127.0.0.1', 0), str(data_directory()/'absent.env'), project_dir)

    def serve_forever(self):
        self.deployment.rows = []
        self.deployment.config = Settings(ztb_api_base='https://example.invalid', bearer='offline')
        self.deployment.resolved = plan_digest(fake_plan())
        self.deployment.job = {'state':'deploying'}
        with patch('ui_deployment.run_worker', controlled_worker):
            self.deployment._start('deployment')
        super().serve_forever(poll_interval=.01)

    def server_close(self):
        (data_directory()/'server-closing').touch()
        super().server_close()
        (data_directory()/'server-finished').write_text(json.dumps(self.deployment.status()))


if __name__ == '__main__':
    with patch('app.LocalServer', FixtureServer):
        app.main()
