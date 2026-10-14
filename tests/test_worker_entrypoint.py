"""Worker startup stays independent of GUI/config/DB initialization."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_worker_module(name):
    # Import the standalone file without running app.utils package initialization.
    spec = importlib.util.spec_from_file_location(name, ROOT / 'app/utils' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkerEntrypointTest(unittest.TestCase):
    def test_early_entrypoint_serves_multiple_requests_without_app_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / 'test.db'
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE sample (value TEXT)')
            config = root / 'must-not-be-created' / 'config.yaml'
            requests = [
                {'operation': 'stat', 'arguments': {'path': str(database)}},
                {'operation': 'sqlite_validate', 'arguments': {'source': str(database)}},
            ]
            # -I excludes local PYTHONPATH/site overrides. The entrypoint needs
            # neither watchdog/Flask nor a usable application configuration.
            result = subprocess.run(
                [sys.executable, '-I', str(ROOT / 'run.py'), '--nastool-io-worker'],
                input=''.join(json.dumps(value) + '\n' for value in requests),
                capture_output=True, text=True, timeout=10,
                env=dict(os.environ, NASTOOL_CONFIG=str(config), NASTOOL_OFFLINE_TESTS='1'))
            self.assertEqual(result.returncode, 0, result.stderr)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(replies), 2)
            self.assertTrue(all(reply['ok'] for reply in replies), replies)
            self.assertEqual(replies[0]['value']['values'][6], database.stat().st_size)
            self.assertFalse(config.parent.exists())
            self.assertFalse((root / '.sqlite-runtime.lock').exists())

    def test_frozen_pool_uses_early_entrypoint_and_reuses_worker(self):
        module = load_worker_module('isolated_io')
        spawn = subprocess.Popen
        commands = []

        def launch(command, **kwargs):
            commands.append(command)
            self.assertEqual(command, [sys.executable, '--nastool-io-worker'])
            # Simulate the frozen bootloader's dispatch with the real entrypoint;
            # retain real pipes, framing, deadlines and worker reuse.
            return spawn([sys.executable, str(ROOT / 'run.py'), command[1]], **kwargs)

        pool = module.IsolationPool(1, 1)
        try:
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(module.subprocess, 'Popen', side_effect=launch):
                for _ in range(2):
                    value = pool.execute('stat', path=str(ROOT / 'run.py'))
                    self.assertGreater(value['values'][6], 0)
            self.assertEqual(len(commands), 1)
        finally:
            pool.close()

    def test_windowed_worker_restores_binary_inherited_pipes(self):
        worker = load_worker_module('isolated_worker')
        incoming, feeder = os.pipe()
        receiver, outgoing = os.pipe()
        streams = SimpleNamespace(stdin=None, stdout=None)
        kernel = SimpleNamespace(GetStdHandle=Mock(side_effect=[100, 200]))
        crt = SimpleNamespace(open_osfhandle=Mock(side_effect=[incoming, outgoing]))
        # Exercise real fd-backed streams while substituting only Windows APIs.
        windows_os = SimpleNamespace(name='nt', O_RDONLY=os.O_RDONLY, O_WRONLY=os.O_WRONLY,
                                     O_BINARY=0x8000, fdopen=os.fdopen)
        try:
            with patch.object(worker, 'os', windows_os), patch.object(worker, 'sys', streams), \
                    patch.object(ctypes, 'WinDLL', return_value=kernel, create=True), \
                    patch.dict(sys.modules, {'msvcrt': crt}):
                worker._restore_windows_pipes()
            os.write(feeder, b'{"operation":"stat"}\n')
            self.assertEqual(streams.stdin.buffer.readline(), b'{"operation":"stat"}\n')
            streams.stdout.buffer.write(b'{"ok":true}\n')
            streams.stdout.buffer.flush()
            self.assertEqual(os.read(receiver, 128), b'{"ok":true}\n')
            self.assertEqual([call.args[0] for call in kernel.GetStdHandle.call_args_list], [-10, -11])
        finally:
            for name, descriptor in (('stdin', incoming), ('stdout', outgoing)):
                stream = getattr(streams, name)
                stream.close() if stream is not None else os.close(descriptor)
            os.close(feeder)
            os.close(receiver)


if __name__ == '__main__':
    unittest.main()
