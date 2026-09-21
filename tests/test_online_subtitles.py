import ast
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('online_subtitles', ROOT / 'app/helper/online_subtitles.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
OnlineSubtitles = module.OnlineSubtitles


class OnlineSubtitleTest(unittest.TestCase):
    def test_cid_small_and_sampled_files(self):
        for size in (17, 0xf000, 200000):
            content = bytes(index % 251 for index in range(size))
            with tempfile.NamedTemporaryFile() as media:
                media.write(content)
                media.flush()
                sample = content if size < 0xf000 else content[:0x5000] + content[size // 3:size // 3 + 0x5000] + content[-0x5000:]
                self.assertEqual(OnlineSubtitles.cid(media.name), hashlib.sha1(sample).hexdigest().upper())

    def test_thunder_deduplicates_and_prioritizes_hash_match(self):
        service = OnlineSubtitles()
        entries = [dict(name='A', url='https://example.com/a', ext='srt', languages=['简体'], cid='OTHER'),
                   dict(name='B', url='https://example.com/b', ext='.ASS', languages=['繁体'], cid='match')]
        with patch.object(service, 'cid', return_value='MATCH'), patch.object(service, '_json', return_value={'code': 0, 'data': entries + entries}):
            results, warnings = service.search('电影', '/media.mkv', 'thunder')
        self.assertEqual([item['name'] for item in results], ['B', 'A'])
        self.assertTrue(results[0]['hash_match'])
        self.assertFalse(warnings)

    def test_provider_failure_does_not_hide_other_results(self):
        service = OnlineSubtitles('secret')
        with patch.object(service, 'cid', return_value=''), patch.object(service, '_json', side_effect=[ValueError(), {'status': 0, 'sub': {'subs': [{'id': 42, 'native_name': '电影字幕'}]}}]):
            results, warnings = service.search('电影', '/media.mkv')
        self.assertEqual(results[0]['remote_id'], '42')
        self.assertEqual(len(warnings), 1)

    def test_missing_token_is_reported(self):
        items, warnings = OnlineSubtitles().search('影片', '', 'assrt')
        self.assertEqual(items, [])
        self.assertIn('Token', warnings[0])

    def test_archive_requires_selection_and_never_extracts_paths(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('../../a.srt', b'first subtitle')
            archive.writestr('other.ass', b'second subtitle')
            archive.writestr('readme.txt', b'ignored')
        service = OnlineSubtitles()
        item = dict(provider='thunder', url='https://example.com', name='archive', format='srt')
        with patch.object(service, '_download', return_value=stream.getvalue()):
            members, content = service.files(item)
            self.assertIsNone(content)
            self.assertEqual(members, ['../../a.srt', 'other.ass'])
            name, content = service.files(item, '../../a.srt')
            self.assertEqual(name, 'a.srt')
            self.assertEqual(content, b'first subtitle')
            with self.assertRaises(ValueError):
                service.files(item, 'not-present.srt')

    def test_archive_expansion_limit(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('a.srt', b'x' * 101)
        service = OnlineSubtitles()
        service.MAX_BYTES = 100
        with patch.object(service, '_download', return_value=stream.getvalue()), self.assertRaises(ValueError):
            service.files(dict(provider='thunder', url='https://example.com', name='a', format='srt'))

    def test_download_rejects_private_destination_and_redirect(self):
        with patch.object(module.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('127.0.0.1', 80))]):
            with self.assertRaises(ValueError):
                OnlineSubtitles._check_url('http://localhost/sub.srt')
        response = Mock(is_redirect=True, headers={'Location': 'http://localhost/private'})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        service = OnlineSubtitles()
        with patch.object(service, '_check_url', side_effect=[None, ValueError('private')]), patch.object(module.requests, 'get', return_value=response) as get:
            with self.assertRaises(ValueError):
                service._download('https://example.com/subtitle')
            self.assertEqual(get.call_count, 1)

    def test_proxy_fake_ip_is_only_allowed_for_known_providers(self):
        with patch.object(module.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('198.18.0.38', 80))]):
            OnlineSubtitles._check_url('https://subtitle.v.geilijiasu.com/file.srt')
            with self.assertRaises(ValueError):
                OnlineSubtitles._check_url('https://untrusted.example/file.srt')
        with patch.object(module.socket, 'getaddrinfo', return_value=[(None, None, None, None, ('192.168.1.1', 80))]):
            with self.assertRaises(ValueError):
                OnlineSubtitles._check_url('https://subtitle.v.geilijiasu.com/file.srt')

    def test_request_failure_does_not_expose_token(self):
        with patch.object(module.requests, 'get', side_effect=module.requests.RequestException('url?token=SECRET')):
            with self.assertRaises(ValueError) as context:
                OnlineSubtitles('SECRET')._json('https://api.assrt.net', {})
            self.assertNotIn('SECRET', str(context.exception))


class OnlineSubtitleRouteTest(unittest.TestCase):
    def setUp(self):
        from flask import Flask, request
        app = Flask(__name__)
        app.secret_key = 'test-key'
        names = {'_online_subtitle_signer', '_online_subtitle_media_path',
                 'library_online_subtitle_search', 'library_online_subtitle_download'}
        tree = ast.parse((ROOT / 'web/main.py').read_text())
        nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        for node in nodes:
            node.decorator_list = []
        self.ns = dict(App=app, request=request, os=os, RMT_MEDIAEXT=['.mkv'],
                       current_user=types.SimpleNamespace(get_id=lambda: 'user'))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), '<routes>', 'exec'), self.ns)
        self.app = app

    def test_tampered_ticket_is_rejected_before_download(self):
        service = Mock()
        self.ns['_online_subtitle_service'] = service
        with self.app.test_request_context(json={'ticket': 'tampered'}):
            result = self.ns['library_online_subtitle_download']()
        self.assertEqual(result['code'], -1)
        service.assert_not_called()

    def test_another_users_ticket_is_rejected(self):
        ticket = self.ns['_online_subtitle_signer']().dumps({'user': 'other-user'})
        with self.app.test_request_context(json={'ticket': ticket}):
            self.assertEqual(self.ns['library_online_subtitle_download']()['code'], -1)

    def test_search_hides_download_url_and_binds_ticket_to_media(self):
        service = Mock()
        service.search.return_value = ([dict(name='sub', provider='thunder', format='srt', language='中文', hash_match=True, url='https://example.com/sub')], [])
        self.ns['_online_subtitle_service'] = lambda: service
        self.ns['_online_subtitle_media_path'] = lambda value: '/library/movie.mkv'
        with self.app.test_request_context(json={'keyword': 'Movie', 'media_path': '/library/movie.mkv'}):
            result = self.ns['library_online_subtitle_search']()
        self.assertEqual(result['code'], 0)
        self.assertNotIn('url', result['items'][0])
        payload = self.ns['_online_subtitle_signer']().loads(result['items'][0]['ticket'])
        self.assertEqual(payload['path'], '/library/movie.mkv')
        self.assertEqual(payload['user'], 'user')

    def test_download_reuses_validated_upload_and_refreshes_library(self):
        service = Mock()
        service.files.return_value = ('subtitle.srt', b'1\n00:00:01,000 --> 00:00:02,000\nHello\n')
        subtitle = Mock()
        subtitle.upload_subtitle.return_value = (True, 'saved', {'synced': True})
        library = Mock()
        server = Mock()
        server.refresh_root_library_by_type.return_value = True
        self.ns.update(_online_subtitle_service=lambda: service,
                       _online_subtitle_media_path=lambda path: path,
                       Subtitle=lambda: subtitle, MediaLibrary=library,
                       MediaServer=lambda: server, Config=lambda: types.SimpleNamespace(get_config=lambda key: {'media_server': 'jellyfin'}))
        ticket = self.ns['_online_subtitle_signer']().dumps({'user': 'user', 'path': '/library/movie.mkv', 'item': {}})
        with self.app.test_request_context(json={'ticket': ticket}):
            result = self.ns['library_online_subtitle_download']()
        self.assertEqual(result['code'], 0)
        args, kwargs = subtitle.upload_subtitle.call_args
        self.assertEqual(args[1], '/library/movie.mkv')
        self.assertEqual(kwargs['target_media_file'], '/library/movie.mkv')
        self.assertEqual(kwargs['server_type'], 'jellyfin')
        library.invalidate_subtitle_directory_cache.assert_called_once_with('/library/movie.mkv')
        server.refresh_root_library_by_type.assert_called_once_with('jellyfin')

    def test_media_path_rejects_symlink_outside_library(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            actual = Path(outside) / 'private.mkv'
            actual.touch()
            link = Path(root) / 'movie.mkv'
            link.symlink_to(actual)
            self.ns['_get_all_media_library_root_paths'] = lambda: [root]
            self.ns['PathUtils'] = types.SimpleNamespace(is_path_in_path=lambda base, path: os.path.commonpath([base, path]) == base)
            with self.assertRaises(ValueError):
                self.ns['_online_subtitle_media_path'](str(link))
