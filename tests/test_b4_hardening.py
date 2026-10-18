"""B4 contracts: actual process deadlines, leased upload resources and durable actions."""
import hashlib
import json
import os
import socket
from pathlib import Path
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch, Mock

from app.utils.isolated_io import IsolationPool, IsolatedIOTimeout, get_io_pool
from app.utils.isolated_fs import IsolatedFile, fs_os
from app.utils.workload import BoundedExecutor, TaskQueueFull


class IsolatedIOTest(TestCase):
    def test_small_close_and_competing_appends_preserve_bytes(self):
        # Exercise real child writes, including append after seek and IOBase close.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'append.txt'
            with IsolatedFile(path, 'w') as stream:
                stream.write('base')
            with IsolatedFile(path, 'a') as first, IsolatedFile(path, 'a') as second:
                first.write('A'); first.flush()
                second.seek(0); second.write('B'); second.flush()
                self.assertEqual(second.tell(), 6)
            self.assertEqual(path.read_text(), 'baseAB')
            with IsolatedFile(path, 'r') as reader:
                with self.assertRaises(OSError):
                    reader.write('bad')

    def test_isolated_transport_preserves_tls_verification_options(self):
        from app.utils.isolated_network import bounded_request
        from app.utils.isolated_worker import execute
        import requests
        for verify in (True, False, '/private-ca.pem'):
            with self.subTest(verify=verify), patch('requests.request') as request:
                response = request.return_value.__enter__.return_value
                response.status_code = 200
                response.headers = {}
                response.iter_content.return_value = [b'ok']
                # Keep the wire arguments intact while avoiding external network.
                with patch('app.utils.isolated_network.get_io_pool') as pool, \
                        patch.dict(os.environ, {'NASTOOL_OFFLINE_TESTS': '0'}):
                    pool.return_value.execute.side_effect = lambda op, timeout, **args: execute(op, args)
                    self.assertEqual(bounded_request('https://example.invalid', verify=verify).content, b'ok')
                self.assertIs(request.call_args.kwargs['verify'], verify)

    def test_file_stream_preserves_identity_content_and_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '字幕.json'
            with IsolatedFile(path, 'x+b') as output:
                output.write('内容'.encode())
                fs_os.fsync(output.fileno())
                output.seek(0)
                self.assertEqual(output.read(), '内容'.encode())
                identity = output.identity
            original = path.stat()
            self.assertEqual(identity, [original.st_dev, original.st_ino])
            with IsolatedFile(path, 'rb') as reader:
                path.unlink()
                path.write_bytes(b'replaced')
                with self.assertRaises(OSError):
                    reader.read(1)

    @staticmethod
    def pool():
        return IsolationPool(1, 1)

    def test_blocked_fifo_hash_times_out_and_reusable_capacity_recovers(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('FIFO integration requires POSIX')
        pool = self.pool()
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'blocked'
                os.mkfifo(path)
                started = time.monotonic()
                with self.assertRaises(IsolatedIOTimeout):
                    pool.execute('hash', path=str(path), timeout=0.15)
                self.assertLess(time.monotonic() - started, 1)
                self.assertGreater(pool.execute('disk_usage', path=directory)[2], 0)
                self.assertLessEqual(pool.snapshot()['processes'], 1)
        finally:
            pool.close()

    def test_http_slow_trickle_has_total_deadline(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', '10000')
                self.end_headers()
                try:
                    for _ in range(10000):
                        self.wfile.write(b'x'); self.wfile.flush(); time.sleep(0.02)
                except (BrokenPipeError, ConnectionResetError):
                    pass
            def log_message(self, *_args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        pool = self.pool()
        try:
            started = time.monotonic()
            with self.assertRaises(IsolatedIOTimeout):
                pool.execute('http', url='http://127.0.0.1:%s/' % server.server_port,
                             request_timeout=[1, 1], timeout=0.3)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            pool.close(); server.shutdown(); server.server_close(); thread.join(2)


class ReviewBoundaryTest(TestCase):
    def test_injected_store_still_releases_both_global_sessions(self):
        import app.db.session_scope as scope
        for error in (None, RuntimeError('cleanup failed')):
            with self.subTest(error=error), patch.object(scope, 'remove_main_session') as main, \
                    patch.object(scope, 'remove_media_session') as media:
                store = Mock()
                store.remove_session.side_effect = error
                scope.release_db_connections(store)
                main.assert_called_once_with()
                media.assert_called_once_with()
        with patch.object(scope, 'remove_main_session', side_effect=RuntimeError), \
                patch.object(scope, 'remove_media_session') as media:
            with self.assertRaises(RuntimeError):
                scope.release_db_connections()
            media.assert_called_once_with()

    def test_restored_upload_socket_ignores_started_late_timer(self):
        from tests.test_subtitle_task_security import WEB_GUARDS
        from werkzeug.test import EnvironBuilder
        builder = EnvironBuilder(path='/subtitle/upload', method='POST')
        self.addCleanup(builder.close)
        sock = Mock()
        sock.gettimeout.return_value = 17
        environ = builder.get_environ()
        environ['werkzeug.socket'] = sock
        request = WEB_GUARDS._NasToolsRequest(environ)
        with patch.object(WEB_GUARDS, 'Timer') as timer:
            restore = request._apply_body_read_timeout()
            callback = timer.call_args.args[1]
            restore()
            # Model a callback scheduled before cancel(), resumed after restore.
            callback()
        sock.shutdown.assert_not_called()
        sock.settimeout.assert_called_with(17)
        self.assertFalse(request._subtitle_body_expired.is_set())


class ActionLifecycleTest(TestCase):
    def setUp(self):
        from app.helper.action_tasks import ActionTasks
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        executor = BoundedExecutor(1, 8, 'action-tests')
        self.helper = SimpleNamespace(start_thread=lambda function, args, **_kwargs: executor.submit(function, *args))
        self.addCleanup(executor.shutdown)
        self.registry_type = ActionTasks.__closure__[0].cell_contents
        self.tasks = self.registry_type(root=self.root.name, helper=self.helper)

    def wait(self, task_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            record = self.tasks.get(task_id)
            if record['status'] in ('succeeded', 'failed', 'canceled', 'interrupted'):
                return record
            time.sleep(0.01)
        self.fail('Task did not finish')

    def test_result_is_persisted_without_parameters_or_credentials(self):
        record, reused = self.tasks.submit('rename', {'password': 'NEVER-SAVE', 'path': 'private-source'},
                                          '7', lambda: {'retcode': 0, 'retmsg': 'done'}, 'request-one')
        self.assertFalse(reused)
        final = self.wait(record['task_id'])
        # Wait for the separate disk snapshot, not just the in-memory transition.
        deadline = time.monotonic() + 2
        file = Path(self.root.name) / (record['task_id'] + '.json')
        while time.monotonic() < deadline:
            value = json.loads(file.read_text())
            if value['status'] == 'succeeded':
                break
            time.sleep(0.01)
        self.assertEqual(final['result'], {'retcode': 0, 'retmsg': 'done'})
        self.assertNotIn('NEVER-SAVE', file.read_text())
        self.assertNotIn('private-source', file.read_text())
        self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_simultaneous_retries_dispatch_once_and_request_aliases_recover(self):
        release, started = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls, records, errors = [], [], []

        def run():
            calls.append(1); started.set(); release.wait(3)
            return {'code': 0}
        def submit(number):
            try:
                records.append(self.tasks.submit('rename', {'path': 'same'}, '7', run, 'retry-%s' % number)[0])
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=submit, args=(number,)) for number in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(len({record['task_id'] for record in records}), 1)
        self.assertTrue(started.wait(2))
        self.assertEqual(calls, [1])
        for number in range(4):
            self.assertEqual(self.tasks.find('7', 'retry-%s' % number)['task_id'], records[0]['task_id'])
        with self.assertRaises(ValueError):
            self.tasks.submit('rename', {'path': 'different'}, '7', run, 'retry-0')

    def test_queued_cancel_does_not_dispatch_mutation(self):
        release, started = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.helper.start_thread(lambda: (started.set(), release.wait(3)), ())
        self.assertTrue(started.wait(1))
        calls = []
        record, _ = self.tasks.submit('auto_remove_torrents', {'tid': 1}, '7', lambda: calls.append(1), 'cancel-one')
        self.assertTrue(self.tasks.cancel(record['task_id']))
        self.assertEqual(self.tasks.get(record['task_id'])['status'], 'canceled')
        self.assertEqual(calls, [])
        release.set()

    def test_coalesced_retry_cannot_ack_before_failed_durable_admission(self):
        saving, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        effects, replies, errors = [], [], []
        def fail_save(_record):
            saving.set(); release.wait(3)
            raise OSError('unavailable storage')
        def submit():
            try:
                replies.append(self.tasks.submit('rename', {}, '7', lambda: effects.append(1), 'pending-save'))
            except Exception as error:
                errors.append(error)
        with patch.object(self.tasks, '_save', side_effect=fail_save):
            first = threading.Thread(target=submit)
            second = threading.Thread(target=submit)
            first.start(); self.assertTrue(saving.wait(1)); second.start()
            try:
                time.sleep(0.05)
                self.assertEqual(replies, [], 'No retry may acknowledge provisional admission')
            finally:
                release.set(); first.join(2); second.join(2)
        self.assertEqual(replies, [])
        self.assertEqual(len(errors), 2)
        self.assertEqual(effects, [])

    def test_restart_interrupts_running_and_queued_work_without_replay(self):
        release, started = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def run():
            started.set(); release.wait(3); return {'code': 0}
        record, _ = self.tasks.submit('rename', {}, '7', run, 'restart-one')
        self.assertTrue(started.wait(1))
        restarted = self.registry_type(root=self.root.name, helper=self.helper)
        self.assertEqual(restarted.get(record['task_id'])['status'], 'interrupted')
        duplicate, reused = restarted.submit('rename', {}, '7', lambda: self.fail('Mutation replayed'), 'restart-one')
        self.assertTrue(reused)
        self.assertEqual(duplicate['status'], 'interrupted')
        release.set()


class UploadLeaseTest(TestCase):
    def setUp(self):
        from app.helper.subtitle_tasks import SubtitleTaskManager
        from tests.test_subtitle_tasks import _MemoryDb
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.manager = SubtitleTaskManager(db=_MemoryDb(), staging_root=os.path.join(self.root.name, 'staging'))
        self.addCleanup(self.manager.shutdown)

    def test_slow_ingress_does_not_hold_shared_submit_lock_or_block_second_lease(self):
        first_ready, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        errors = []
        def first():
            try:
                token = self.manager.acquire_upload_admission(1024)
                first_ready.set(); release.wait(3)
                self.manager.release_upload_admission(token)
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(first_ready.wait(2))
            self.assertTrue(self.manager._submit_lock.acquire(blocking=False))
            self.manager._submit_lock.release()
            token = self.manager.acquire_upload_admission(1024)
            self.assertEqual(len(self.manager._admissions), 2)
            self.manager.account_upload_write(1024)
            from app.helper.subtitle_tasks import TaskUploadTooLarge
            with self.assertRaises(TaskUploadTooLarge):
                self.manager.account_upload_write(1)
            self.manager.release_upload_admission(token)
        finally:
            release.set(); thread.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(self.manager._admissions, {})

    def test_untracked_failed_submission_bytes_remain_charged(self):
        self.manager.start()
        orphan = Path(self.manager._staging_root) / 'orphan-submission'
        orphan.mkdir()
        (orphan / 'partial.srt').write_bytes(b'x' * 4096)
        self.assertEqual(self.manager._orphan_staging_bytes(), 4096)

    def test_socket_total_deadline_interrupts_a_trickling_multipart_body(self):
        from tests.test_subtitle_task_security import WEB_GUARDS
        from werkzeug.test import EnvironBuilder
        from werkzeug.exceptions import RequestTimeout
        left, right = socket.socketpair()
        stopped = threading.Event()
        body = (b'--limit\r\nContent-Disposition: form-data; name="file"; filename="a.srt"\r\n\r\n'
                + b'x' * 1000 + b'\r\n--limit--\r\n')
        def trickle():
            try:
                for value in body:
                    if stopped.is_set(): break
                    right.send(bytes([value])); time.sleep(0.02)
            except OSError:
                pass
        builder = EnvironBuilder(path='/subtitle/upload', method='POST',
                                 content_type='multipart/form-data; boundary=limit')
        environ = builder.get_environ()
        stream = left.makefile('rb')
        environ.update({'wsgi.input': stream, 'werkzeug.socket': left, 'CONTENT_LENGTH': str(len(body))})
        request = WEB_GUARDS._NasToolsRequest(environ)
        thread = threading.Thread(target=trickle)
        try:
            thread.start()
            started = time.monotonic()
            with patch.object(WEB_GUARDS, '_SUBTITLE_UPLOAD_BODY_TIMEOUT_SECONDS', 0.15):
                with self.assertRaises(RequestTimeout):
                    _ = request.files
            self.assertLess(time.monotonic() - started, 1)
        finally:
            stopped.set(); right.close(); left.close(); thread.join(1)
            request.close(); stream.close(); builder.close()


class ActionPermissionTest(TestCase):
    def test_status_requires_both_owner_and_original_permission(self):
        import web.action as action_module
        user = SimpleNamespace(get_id=lambda: '7', is_authenticated=True, pris='媒体整理')
        record = {'owner': '7', 'command': 'rename'}
        with patch.object(action_module.WebAction, '_action_principal', return_value=('7', user, False, False)):
            self.assertTrue(action_module.WebAction._visible_action_task(record))
            self.assertFalse(action_module.WebAction._visible_action_task(dict(record, owner='8')))
            user.pris = ''
            self.assertFalse(action_module.WebAction._visible_action_task(record))

    def test_accepted_http_action_rechecks_permission_before_execution(self):
        import web.action as action_module
        import web.main as web_module
        from flask import g, jsonify
        from flask_login import UserMixin
        from app.helper.action_tasks import ActionTasks
        class Principal(UserMixin):
            id = '7'; username = 'test-user'; pris = '媒体整理'
        user = Principal()
        root = tempfile.TemporaryDirectory()
        executor = BoundedExecutor(1, 8, 'permission-action-test')
        release, started = threading.Event(), threading.Event()
        def hold():
            started.set(); release.wait(3)
        executor.submit(hold); self.assertTrue(started.wait(1))
        helper = SimpleNamespace(start_thread=lambda function, args, **_kwargs: executor.submit(function, *args))
        registry = ActionTasks.__closure__[0].cell_contents(root=root.name, helper=helper)
        action = action_module.WebAction.__new__(action_module.WebAction)
        effects = []
        action._actions = {'run_directory_sync': lambda _data: effects.append(1)}
        try:
            with patch.object(action_module, 'ActionTasks', return_value=registry), \
                    patch.object(action_module, 'User') as users:
                users.return_value.get_user.return_value = user
                with web_module.App.test_request_context('/do', headers={'X-Request-ID': 'permission-test'}):
                    g._login_user = user; g.api_user = user
                    reply = action.action('run_directory_sync', {'sid': 1})
                    self.assertTrue(reply['async'])
                    self.assertEqual(web_module.add_header(jsonify(reply)).status_code, 202)
                    user.pris = ''
                release.set()
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and registry.get(reply['task_id'])['status'] not in ('failed', 'succeeded'):
                    time.sleep(0.01)
                self.assertEqual(registry.get(reply['task_id'])['status'], 'failed')
                self.assertEqual(effects, [], 'Revoked permission must prevent the queued mutation')
        finally:
            release.set(); executor.shutdown(); root.cleanup()


class ScheduledAdmissionTest(TestCase):
    def test_schedulers_share_a_finite_queue_and_close_only_owned_work(self):
        from datetime import datetime, timezone
        from apscheduler.schedulers.background import BackgroundScheduler
        import app.utils.scheduled_executor as module
        pool = BoundedExecutor(1, 2, 'scheduled-admission-test')
        release, started = threading.Event(), threading.Event()
        events = []
        def hold():
            started.set(); release.wait(3)
        with patch.object(module, '_pool', pool):
            first, second = module.SharedScheduledExecutor(), module.SharedScheduledExecutor()
            scheduler = BackgroundScheduler()
            first.start(scheduler, 'first'); second.start(scheduler, 'second')
            def job(identifier, function):
                return SimpleNamespace(id=identifier, func=function, args=(), kwargs={},
                                       max_instances=1, misfire_grace_time=60,
                                       _jobstore_alias='default', name=identifier)
            try:
                first.submit_job(job('one', hold), [datetime.now(timezone.utc)])
                self.assertTrue(started.wait(1))
                second.submit_job(job('two', lambda: events.append('two')), [datetime.now(timezone.utc)])
                first.shutdown(wait=False)
                self.assertFalse(pool._shutdown, 'A scheduler must not close shared admission capacity')
                second.submit_job(job('three', lambda: events.append('three')), [datetime.now(timezone.utc)])
                with self.assertRaises(TaskQueueFull):
                    second.submit_job(job('overflow', lambda: events.append('overflow')), [datetime.now(timezone.utc)])
                release.set(); second.shutdown(wait=True)
                self.assertEqual(events, ['two', 'three'])
            finally:
                release.set(); pool.shutdown()
