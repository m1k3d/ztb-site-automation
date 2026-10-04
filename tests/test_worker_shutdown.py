"""Real spawned-worker and app signal checks; never signal the test runner/group."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from app import terminal_shutdown_signals


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(sys.platform == 'win32', 'POSIX signals are not Windows console events')
class WorkerShutdownTests(unittest.TestCase):
    def wait_file(self, folder, name, process):
        deadline = time.monotonic()+15
        path = folder/name
        while not path.exists():
            self.assertIsNone(process.poll(), f'Fixture exited before {name}')
            self.assertLess(time.monotonic(), deadline, f'Timed out waiting for {name}')
            time.sleep(.02)
        return path

    def test_worker_report_and_parent_exit_after_interrupt_hangup_and_repeated_signals(self):
        for initial in (signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=initial), tempfile.TemporaryDirectory() as directory:
                folder = Path(directory)
                with (folder/'output').open('w+') as output:
                    process = subprocess.Popen([sys.executable, str(ROOT/'tests/shutdown_fixture.py'),
                        '--no-browser', '--data-dir', directory], cwd=ROOT, start_new_session=True,
                        stdout=output, stderr=output)
                    worker_pid = None
                    try:
                        worker_pid = int(self.wait_file(folder, 'worker-ready', process).read_text())
                        # Target both known PIDs, just as a terminal signal reaches
                        # both, without ever sending a signal to our process group.
                        os.kill(worker_pid, initial)
                        process.send_signal(initial)
                        self.wait_file(folder, 'server-closing', process)
                        for repeated in (signal.SIGINT, signal.SIGHUP, signal.SIGINT):
                            process.send_signal(repeated)
                            os.kill(worker_pid, repeated)
                            time.sleep(.03)
                        self.assertIsNone(process.poll())
                        (folder/'release').touch()
                        code = process.wait(timeout=15)
                        output.seek(0)
                        self.assertEqual(code, 0, output.read())
                        report, = (folder/'runs').glob('*.json')
                        self.assertEqual(json.loads(report.read_text())['sites'][0]['status'], 'success')
                        self.assertEqual(json.loads((folder/'server-finished').read_text())['state'], 'completed')
                    finally:
                        if process.poll() is None:
                            if worker_pid is not None:
                                try:
                                    os.kill(worker_pid, signal.SIGTERM)
                                except ProcessLookupError:
                                    pass
                            process.terminate()
                            process.wait(timeout=10)

    def test_sigterm_is_not_ignored_by_worker(self):
        code = 'from ui_deployment import ignore_terminal_signals; import os,signal; ignore_terminal_signals(); os.kill(os.getpid(),signal.SIGTERM)'
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, timeout=10)
        self.assertEqual(result.returncode, -signal.SIGTERM)

    def test_server_restores_original_handlers(self):
        before = {number:signal.getsignal(number) for number in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM)}
        with terminal_shutdown_signals():
            self.assertEqual(signal.getsignal(signal.SIGTERM), before[signal.SIGTERM])
        self.assertEqual({number:signal.getsignal(number) for number in before}, before)
