"""Focused branch-review regressions; real boundaries, disposable files, no network."""
import ast
import io
import os
import tempfile
import threading
import urllib
from html import escape
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import Mock, patch

from flask import Flask, g, make_response, request
from flask_login import LoginManager, UserMixin, current_user, login_required
from lxml import html
from werkzeug.datastructures import FileStorage

from tests.test_sync_reliability import env, load_class
from web.backend.action_permissions import PAGE_ACTIONS, action_allowed
from web.backend.uploads import save_upload, upload_directory, uploaded_path

ROOT = Path(__file__).resolve().parents[1]


def web_functions(app, namespace):
    """Keep the real routes/decorators without starting unrelated app services."""
    names = {'check_page_permission', 'upload', 'upload_torrent', '_save_web_upload', 'dirlist'}
    tree = ast.parse((ROOT / 'web/main.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    namespace.update(App=app, login_required=login_required, request=request,
                     current_user=current_user, make_response=make_response, escape=escape,
                     PAGE_ACTIONS=PAGE_ACTIONS, action_allowed=action_allowed,
                     save_upload=save_upload, upload_directory=upload_directory,
                     os=os, urllib=urllib, ExceptionUtils=Mock())
    exec(compile(ast.Module(body=nodes, type_ignores=[]), 'web/main.py', 'exec'), namespace)


class ReviewTransferTest(TestCase):
    def test_invalid_ignore_configuration_never_authorizes_download_deletion(self):
        from app.utils.types import RmtMode
        ns = env()
        ns['RmtMode'] = RmtMode
        transfer = load_class('app/filetransfer.py', 'FileTransfer',
                              ['transfer_media', 'check_ignore', 'link_sync_file'], ns)()
        transfer.progress = Mock()
        transfer._ignore_config_error = 'invalid regex'
        client = Mock()
        client.get_transfer_task.return_value = [{'path': '/download/file.mkv', 'id': 'torrent'}]
        downloader = load_class('app/downloader/downloader.py', 'Downloader', ['transfer'], ns)()
        downloader.default_client, downloader.filetransfer = client, transfer
        downloader._pt_monitor_only = False
        downloader._default_client_type = 'test'
        downloader._get_download_context = Mock(return_value=None)
        for mode in (RmtMode.MOVE, RmtMode.RCLONE, RmtMode.MINIO):
            with self.subTest(mode=mode):
                downloader._pt_rmt_mode = mode
                downloader.transfer()
        client.delete_torrents.assert_not_called()
        client.set_torrents_status.assert_not_called()
        self.assertEqual(transfer.link_sync_file('/source', '/source/file', '/target', RmtMode.LINK)[0], -1)
        # Valid ignore rules still produce a normal skip, not a configuration error.
        transfer._ignore_config_error = ''
        transfer._ignored_paths = None
        import re
        transfer._ignored_files = re.compile('sample')
        self.assertTrue(transfer.transfer_media('test', '/sample.mkv', files=['/sample.mkv'],
                                                rmt_mode=RmtMode.LINK)[0])

    def test_sync_empty_media_is_success_but_downloader_empty_media_is_failure(self):
        from app.utils.path_utils import PathUtils
        ns = env()
        from app.utils.types import RmtMode
        ns['RmtMode'] = RmtMode
        ns.update(PathUtils=PathUtils, StringUtils=NS(str_filesize=str), extract_special=lambda *_: (None, None))
        transfer = load_class('app/filetransfer.py', 'FileTransfer', ['transfer_media'], ns)()
        transfer.progress = Mock()
        transfer._min_filesize = 1024
        sync = load_class('app/sync.py', 'Sync', ['transfer_all_sync', '__transfer_all_sync'],
                          dict(ns, _transfer_all_lock=threading.Lock()))()
        sync.filetransfer = transfer
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / 'source'
            for folder, name in [('empty', None), ('metadata', 'movie.nfo'), ('sample', 'clip.mkv'),
                                 ('incomplete', 'movie.mkv.part')]:
                child = source / folder
                child.mkdir(parents=True)
                if name:
                    (child / name).write_bytes(b'x')
            sync.sync_dir_config = {str(source): {'target': root, 'syncmod': ns['RmtMode'].LINK}}
            self.assertTrue(sync.transfer_all_sync())
            self.assertFalse(transfer.transfer_media('downloader', str(source), rmt_mode=ns['RmtMode'].LINK)[0])
            # A readable parent with an inaccessible child is not an empty success.
            nested = source / 'metadata' / 'unreadable'
            nested.mkdir()
            real_scandir = os.scandir
            def scan(path):
                if str(path) == str(nested):
                    raise PermissionError('unreadable NAS subdirectory')
                return real_scandir(path)
            with patch('os.scandir', side_effect=scan):
                self.assertFalse(sync.transfer_all_sync())

    def test_move_uses_inode_and_retains_retry_evidence_until_subtitles_succeed(self):
        from app.media.meta.extra_transfer import publish_exclusive, has_pending_publication
        from app.utils.types import RmtMode
        with tempfile.TemporaryDirectory() as root:
            source, target = Path(root) / 'source.mkv', Path(root) / 'target.mkv'
            source.write_bytes(b'media bytes')
            inode = source.stat().st_ino
            transfer = Mock(side_effect=AssertionError('same-volume MOVE must not copy'))
            finish = Mock(side_effect=[-1, 0])
            self.assertEqual(publish_exclusive(str(source), str(target), RmtMode.MOVE,
                                              transfer, lambda: True, finish), -1)
            self.assertTrue(source.exists())
            self.assertTrue(has_pending_publication(str(source), str(target)))
            self.assertEqual(publish_exclusive(str(source), str(target), RmtMode.MOVE,
                                              transfer, lambda: True, finish), 0)
            self.assertEqual(target.stat().st_ino, inode)
            self.assertFalse(source.exists())
            self.assertEqual(list(Path(root).iterdir()), [target])
            transfer.assert_not_called()


class ReviewWebTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = Flask(__name__)
        self.app.secret_key = 'offline'
        login = LoginManager(self.app)
        class Principal(UserMixin):
            def __init__(self, value):
                self.id, self.username, self.pris = value, 'tester', '资源搜索'
        login.user_loader(lambda value: Principal(value))
        self.ns = {'Config': lambda: NS(get_temp_path=lambda: self.temp.name, get_config_path=lambda: self.temp.name)}
        web_functions(self.app, self.ns)
        self.client = self.app.test_client()
        self.login('7')

    def login(self, user):
        with self.client.session_transaction() as session:
            session['_user_id'] = user
            session['_fresh'] = True

    def upload(self, route, filename, content=b'torrent'):
        return self.client.post(route, data={'file': (io.BytesIO(content), filename)})

    def test_upload_permissions_confinement_collisions_and_action_consumers(self):
        self.assertEqual(self.upload('/upload', 'backup.zip').status_code, 403)
        for name in ('../escape.torrent', '/escape.torrent', r'..\escape.torrent',
                     'C:escape.torrent', 'backup.zip'):
            with self.subTest(name=name):
                self.assertEqual(self.upload('/upload/torrent', name).status_code, 400)
        first = self.upload('/upload/torrent', '电影.torrent').get_json()
        second = self.upload('/upload/torrent', '电影.torrent', b'other').get_json()
        self.assertEqual(first['code'], 0)
        self.assertNotEqual(first['filename'], second['filename'])
        directory = upload_directory(self.temp.name, 'torrent', '7')
        self.assertEqual(Path(uploaded_path(directory, first['filename'], '.torrent')).read_bytes(), b'torrent')
        outside = Path(self.temp.name) / 'outside.torrent'
        outside.write_bytes(b'keep')
        (Path(directory) / 'alias.torrent').symlink_to(outside)
        os.link(outside, Path(directory) / 'hardlink.torrent')
        self.assertEqual(self.upload('/upload/torrent', 'alias.torrent').status_code, 200)
        self.assertEqual(self.upload('/upload/torrent', 'hardlink.torrent').status_code, 200)
        self.assertEqual(outside.read_bytes(), b'keep')
        for name in ('../outside.torrent', str(outside), 'alias.torrent'):
            with self.assertRaises(ValueError):
                uploaded_path(directory, name, '.torrent')
        with self.assertRaises(ValueError):
            uploaded_path(upload_directory(self.temp.name, 'torrent', '8'), first['filename'], '.torrent')

        # Exercise the actual download action using the server-issued name.
        ns = dict(self.ns, g=g, current_user=current_user, upload_directory=upload_directory,
                  uploaded_path=uploaded_path, Media=Mock(), Downloader=Mock(), Message=Mock(),
                  SearchType=NS(WEB='web'))
        action = load_class('web/action.py', 'WebAction', ['__download_torrent', '__restory_backup'], ns)()
        ns['Downloader'].return_value.download.return_value = (True, '')
        with self.app.test_request_context('/'):
            g.api_user = NS(get_id=lambda: '7')
            g._login_user = NS(username='tester', get_id=lambda: '7')
            payload = {'files': [{'name': '电影.torrent', 'upload': {'filename': first['filename']}}]}
            self.assertEqual(action._WebAction__download_torrent(payload)['code'], 0)
            self.assertEqual(ns['Downloader'].return_value.download.call_args.kwargs['torrent_file'], first['filepath'])
            payload['files'][0]['upload']['filename'] = str(outside)
            self.assertEqual(action._WebAction__download_torrent(payload)['code'], -1)
            self.assertEqual(ns['Downloader'].return_value.download.call_count, 1)
        self.login('0')
        for api_key in (False, True):
            backup = self.upload('/upload', 'backup.zip').get_json()
            with self.app.test_request_context('/'), patch('app.db.backup.stage_restore', return_value={'code': 0}) as restore:
                g._login_user = NS(get_id=lambda: None if api_key else '0')
                g.api_key_authenticated = api_key
                self.assertEqual(action._WebAction__restory_backup({'file_name': backup['filename']})['code'], 0)
                self.assertEqual(restore.call_args.args[0], backup['filepath'])

    def test_dirlist_escapes_names_attributes_and_error_paths(self):
        names = ['<img src=x onerror=alert(1)>.mkv', 'movie.<svg onload=alert(1)>',
                 'movie." onmouseover="alert(1)']
        folder = Path(self.temp.name) / 'quotes"&'
        folder.mkdir()
        for name in names:
            (folder / name).write_bytes(b'x')
        reply = self.client.post('/dirlist', data={'dir': str(folder)})
        tree = html.fromstring(reply.data)
        self.assertFalse(tree.xpath('//img|//svg|//*[@onerror]|//*[@onmouseover]'))
        self.assertEqual({anchor.text: anchor.get('rel') for anchor in tree.xpath('//a')},
                         {name: str(folder / name) for name in names})
        with patch.object(self.ns['os'], 'listdir', side_effect=OSError('<img src=x onerror=alert(1)>')):
            reply = self.client.post('/dirlist', data={'dir': str(folder)})
        self.assertFalse(html.fromstring(reply.data).xpath('//img|//*[@onerror]'))

    def test_logout_revokes_raw_and_bearer_tokens(self):
        import web.security as security
        from app.utils import TokenCache
        ns = {'ClientResource': object, 'TokenCache': TokenCache,
              '_authorization_token': security._authorization_token}
        logout = load_class('web/apiv1.py', 'SystemLogout', ['post'], ns)
        for header in ('token', 'Bearer token', 'bEaReR   token'):
            with self.subTest(header=header), self.app.test_request_context('/', headers={'Authorization': header}), \
                    patch.object(security, 'identify', return_value=(True, 'tester')), patch.object(security, 'User') as users:
                TokenCache.set('token', 'token')
                users.return_value.get_user.return_value = object()
                self.assertIsNotNone(security._cached_token_user(security._authorization_token()))
                self.assertTrue(logout.post()['success'])
                self.assertIsNone(security._cached_token_user('token'))
