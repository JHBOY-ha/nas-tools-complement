"""Incomplete background work must never replace a successful snapshot."""
import json
import tempfile
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch


class CompletionRegressionTest(TestCase):
    def test_media_sync_real_async_results(self):
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
        for result, status, message in ((True, 'succeeded', '执行结束'),
                                        (False, 'failed', '同步失败'),
                                        (None, 'failed', '未执行')):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as root:
                executor = BoundedExecutor(1, 8, 'media-sync-test')
                helper = SimpleNamespace(start_thread=lambda fn, args, **kw: executor.submit(fn, *args))
                registry = ActionTasks.__closure__[0].cell_contents(root=root, helper=helper)
                action = module.WebAction.__new__(module.WebAction)
                action._actions = {'start_mediasync': Mock()}
                try:
                    with patch.object(module, 'ActionTasks', return_value=registry), \
                            patch.object(module, 'User') as users, \
                            patch.object(module, 'MediaServer') as servers:
                        users.return_value.get_user.return_value = user
                        servers.return_value.sync_mediaserver.return_value = result
                        with app.test_request_context('/do'):
                            g.api_user = user
                            g._login_user = user
                            reply = action.action('start_mediasync', {})
                        self.assertTrue(reply['async'])
                        deadline = time.monotonic() + 3
                        while time.monotonic() < deadline:
                            record = registry.get(reply['task_id'])
                            if record['status'] in ('failed', 'succeeded'):
                                break
                            time.sleep(0.01)
                        self.assertEqual(record['status'], status)
                        self.assertEqual(record['result']['code'], 0 if result is True else -1)
                        self.assertIn(message, record['result']['msg'])
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
