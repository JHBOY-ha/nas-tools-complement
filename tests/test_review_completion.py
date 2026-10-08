"""Incomplete background work must never replace a successful snapshot."""
import json
import tempfile
import threading
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch


class CompletionRegressionTest(TestCase):
    def test_sync_real_async_results(self):
        import web.action as module
        from flask import Flask, g
        from flask_login import UserMixin
        from app.helper.action_tasks import ActionTasks
        from app.utils.workload import BoundedExecutor

        class Principal(UserMixin):
            id = '0'
            username = 'admin'

        user = Principal()
        app = Flask(__name__)
        app.secret_key = 'offline-test'
        # Exercise admission, copied request context and the real completion callback.
        for command, result, status, message in (
                ('start_mediasync', True, 'succeeded', '执行结束'),
                ('start_mediasync', False, 'failed', '同步失败'),
                ('start_mediasync', None, 'failed', '未执行'),
                ('sch', False, 'failed', '服务未执行'),
                ('sch', True, 'succeeded', '执行结束')):
            with self.subTest(command=command, result=result), tempfile.TemporaryDirectory() as root, ExitStack() as stack:
                # Service selection constructs unrelated singleton services;
                # mock those boundaries but retain real dispatch and completion.
                for name in ('TorrentRemover', 'Downloader', 'Sites', 'Rss', 'DoubanSync', 'Subscribe'):
                    stack.enter_context(patch.object(module, name))
                sync = stack.enter_context(patch.object(module, 'Sync'))
                sync.return_value.transfer_all_sync.return_value = result
                executor = BoundedExecutor(1, 8, 'media-sync-test')
                helper = SimpleNamespace(start_thread=lambda fn, args, **kw: executor.submit(fn, *args))
                registry = ActionTasks.__closure__[0].cell_contents(root=root, helper=helper)
                action = module.WebAction.__new__(module.WebAction)
                action._actions = {command: Mock()}
                try:
                    with patch.object(module, 'ActionTasks', return_value=registry), \
                            patch.object(module, 'User') as users, \
                            patch.object(module, 'MediaServer') as servers:
                        users.return_value.get_user.return_value = user
                        servers.return_value.sync_mediaserver.return_value = result
                        with app.test_request_context('/do'):
                            g.api_user = user
                            g._login_user = user
                            reply = action.action(command, {'item': 'sync'} if command == 'sch' else {})
                        self.assertTrue(reply['async'])
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            record = registry.get(reply['task_id'])
                            if record['status'] in ('failed', 'succeeded'):
                                break
                            time.sleep(0.01)
                        self.assertEqual(record['status'], status)
                        self.assertEqual(record['result']['code'], 0 if result is True else -1)
                        self.assertIn(message, record['result'].get('msg') or record['result'].get('retmsg'))
                        if command == 'sch':
                            sync.return_value.transfer_all_sync.assert_called_once()
                        else:
                            servers.return_value.sync_mediaserver.assert_called_once()
                finally:
                    executor.shutdown()

    def test_seeding_budget_rejects_partial_refresh(self):
        from app.sites.siteuserinfo.nexus_php import NexusPhpSiteUserInfo
        import app.sites.siteuserinfo._base as base
        import app.sites.sites as sites_module

        # Real NexusPHP pagination and parse, including a complete boundary case.
        for count, expired, expected, incomplete in ((101, False, 100, True),
                                                    (2, True, 1, True),
                                                    (100, False, 100, False)):
            with self.subTest(count=count, expired=expired), ExitStack() as stack:
                info = NexusPhpSiteUserInfo('test', 'https://test.invalid', '', '')
                info.userid = '7'
                info._user_traffic_page = None
                info._user_detail_page = None
                info._torrent_seeding_page = 'seed?page=1'
                calls = []

                def page(*args, **kwargs):
                    calls.append(args[0])
                    number = len(calls)
                    return ('<table><tr><td>Name</td></tr>'
                            '<tr><td>A</td><td>B</td><td>1 GB</td><td>3</td></tr></table>'
                            + (f'<a href="?page={number + 1}">下一页</a>' if number < count else ''))

                for method in ('_parse_favicon', '_parse_site_page', '_parse_user_base_info'):
                    stack.enter_context(patch.object(info, method))
                stack.enter_context(patch.object(info, '_parse_logged_in', return_value=True))
                stack.enter_context(patch.object(info, '_get_page_content', side_effect=page))
                # Replace the module clock only, without disturbing worker/runtime clocks.
                stack.enter_context(patch.object(base, 'time', SimpleNamespace(
                    monotonic=Mock(side_effect=[0, 0] + [301 if expired else 0] * 105))))
                sites_type = sites_module.Sites.__closure__[0].cell_contents
                sites = sites_type.__new__(sites_type)
                sites._sites_data = {}
                stack.enter_context(patch.object(sites, '_Sites__get_site_strict_url', return_value=info.site_url))
                factory = stack.enter_context(patch.object(sites_module, 'SiteUserInfoFactory'))
                factory.return_value.build.return_value = info
                returned = sites._Sites__refresh_site_data({'name': 'test'})
                self.assertEqual(len(calls), expected)
                self.assertEqual(info.seeding, expected)
                self.assertEqual(len(json.loads(info.seeding_info)), expected)
                if incomplete:
                    self.assertIn('采集不完整', info.err_msg)
                    # Only returned parsers enter the statistics and seed-detail DB writes.
                    self.assertIsNone(returned)
                    self.assertEqual(sites._sites_data['test'], {'err_msg': info.err_msg})
                else:
                    self.assertIsNone(info.err_msg)
                    self.assertIs(returned, info)

    def test_busy_directory_sync_fails_and_can_be_retried(self):
        import app.sync as sync_module
        import web.action as action_module
        from app.helper.action_tasks import ActionTasks
        from app.utils.workload import BoundedExecutor
        from flask import Flask, g
        from flask_login import UserMixin
        class Principal(UserMixin):
            id = '0'
            username = 'admin'
        user = Principal()
        app = Flask(__name__)
        app.secret_key = 'test-only'
        sync_type = sync_module.Sync.__closure__[0].cell_contents
        sync = sync_type.__new__(sync_type)
        sync.sync_dir_config = {
            '/test/a': {'id': 'a', 'target': '/target/a', 'onlylink': True},
            '/test/b': {'id': 'b', 'target': '/target/b', 'onlylink': True},
        }
        entered, release = threading.Event(), threading.Event()
        scanned = []
        def scan(path):
            scanned.append(path)
            if path == '/test/a':
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('test did not release worker')
            return []
        with tempfile.TemporaryDirectory() as root:
            executor = BoundedExecutor(2, 8)
            registry = ActionTasks.__closure__[0].cell_contents(root=root,
                helper=SimpleNamespace(start_thread=lambda fn, args, **kw: executor.submit(fn, *args)))
            action = action_module.WebAction.__new__(action_module.WebAction)
            action._actions = {'run_directory_sync': action._WebAction__run_directory_sync}
            try:
                with patch.object(action_module, 'Sync', return_value=sync), \
                     patch.object(action_module, 'ActionTasks', return_value=registry), \
                     patch.object(action_module, 'User') as users, \
                     patch.object(sync_module.PathUtils, 'get_dir_files', side_effect=scan):
                    users.return_value.get_user.return_value = user
                    def submit(sid):
                        with app.test_request_context('/do'):
                            g.api_user = user
                            g._login_user = user
                            return action.action('run_directory_sync', {'sid': sid})
                    first = submit('a')
                    self.assertTrue(entered.wait(2))
                    second = submit('b')
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        record = registry.get(second['task_id'])
                        if record['status'] in ('succeeded', 'failed'):
                            break
                        time.sleep(.01)
                    self.assertEqual(record['status'], 'failed')
                    self.assertNotEqual(record['result']['code'], 0)
                    self.assertIn('本次未执行', record['result']['msg'])
                    self.assertEqual(scanned, ['/test/a'])
                    release.set()
                    # Wait for the first real task's terminal callback before
                    # retrying B, which must now scan its own directory once.
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and registry.get(first['task_id'])['status'] != 'succeeded':
                        time.sleep(.01)
                    self.assertEqual(registry.get(first['task_id'])['status'], 'succeeded')
                    retry = submit('b')
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and registry.get(retry['task_id'])['status'] != 'succeeded':
                        time.sleep(.01)
                    self.assertEqual(registry.get(retry['task_id'])['status'], 'succeeded')
                    self.assertEqual(scanned, ['/test/a', '/test/b'])
            finally:
                release.set()
                executor.shutdown()
