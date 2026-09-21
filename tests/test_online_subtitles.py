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
        entries = [dict(name='电影.srt', url='https://example.com/a', ext='srt', languages=['简体'], cid='OTHER'),
                   dict(name='B', url='https://example.com/b', ext='.ASS', languages=['繁体'], cid='match')]
        with patch.object(service, 'cid', return_value='MATCH'), patch.object(service, '_json', return_value={'code': 0, 'data': entries + entries}):
            results, warnings = service.search('电影', '/media.mkv', 'thunder')
        self.assertEqual([item['name'] for item in results], ['B', '电影.srt'])
        self.assertTrue(results[0]['hash_match'])
        self.assertFalse(warnings)

    def test_provider_failure_does_not_hide_other_results(self):
        service = OnlineSubtitles('secret')
        with patch.object(service, 'cid', return_value=''), patch.object(service, '_json', side_effect=[ValueError(), {'status': 0, 'sub': {'subs': [{'id': 42, 'native_name': '电影.srt'}]}}]):
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

    def test_movie_search_removes_other_films_sequels_and_wrong_year(self):
        service = OnlineSubtitles()
        names = ['Alien.1979.srt', 'Resident.Alien.1979.srt', 'Alien.Covenant.1979.srt',
                 'Alien.2.1979.srt', 'Alien.2000.srt', 'Other.1979.srt', 'Alien.S01E01.srt']
        entries = [dict(name=name, url='https://example.com/' + str(i), ext='srt') for i, name in enumerate(names)]
        with patch.object(service, 'cid', return_value=''), patch.object(service, '_json', return_value={'code': 0, 'data': entries}):
            items, warnings = service.search('Alien', '/Alien.mkv', 'thunder', {'year': '1979'})
        self.assertEqual([item['name'] for item in items], ['Alien.1979.srt'])
        self.assertIn('6', warnings[0])

    def test_episode_query_and_strict_season_episode_filter(self):
        service = OnlineSubtitles()
        names = ['Show.S01E02.srt', 'Other.S01E02.srt', 'Show.S01E03.srt',
                 'Show.S02E02.srt', 'Show.E02.srt', 'Show.S01E01-E03.srt',
                 'Show.1x02.srt', 'Show.S01E02E03.srt', 'Show.S01E020.srt']
        entries = [dict(name=name, url='https://example.com/' + str(i), ext='srt') for i, name in enumerate(names)]
        with patch.object(service, 'cid', return_value=''), patch.object(service, '_json', return_value={'code': 0, 'data': entries}) as request:
            items, _ = service.search('Show', '/Show.S01E02.mkv', 'thunder', {'media_type': 'episode', 'season': 1, 'episode': 2})
        self.assertEqual(request.call_args.args[1]['name'], 'Show S01E02')
        self.assertEqual([item['name'] for item in items], ['Show.S01E02.srt', 'Show.1x02.srt'])
        self.assertEqual(items[0]['target']['episode'], 2)

    def test_original_title_and_chinese_episode_names(self):
        target = OnlineSubtitles.search_target('中文剧名', '/Show.mkv', {'original_title': 'The Show', 'media_type': 'episode', 'season': 1, 'episode': 2})
        for name in ('The.Show.S01E02.ass', '中文剧名 第一季 第二集.srt', '中文剧名第一季第二集.srt'):
            self.assertEqual(OnlineSubtitles.match_result({'name': name}, target), '剧名与季集匹配')
        self.assertEqual(OnlineSubtitles.match_result({'name': '中文剧名 第二季 第二集.srt'}, target), '')

    def test_specific_episode_archive_hides_and_rejects_other_members(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for name in ['Show.S01E01.srt', 'Show.S01E02.ass', 'S02/Show.E02.srt', 'Other.S01E02.srt', 'E02.srt', 'unknown.srt']:
                archive.writestr(name, b'subtitle')
        item = dict(provider='thunder', url='https://example.com', name='Show.S01.Complete.zip', format='srt', target={'titles': ['Show'], 'season': 1, 'episode': 2})
        service = OnlineSubtitles()
        with patch.object(service, '_download', return_value=stream.getvalue()):
            members, _ = service.files(item)
            self.assertEqual(members, ['Show.S01E02.ass', 'E02.srt'])
            with self.assertRaises(ValueError):
                service.files(item, 'Show.S01E01.srt')
            self.assertEqual(service.files(item, 'E02.srt')[1], b'subtitle')

    def test_assrt_season_pack_and_detail_episode_validation(self):
        service = OnlineSubtitles('token')
        search = {'status': 0, 'sub': {'subs': [{'id': 1, 'native_name': 'Show.S01.Complete', 'subtype': 'ZIP'},
                                               {'id': 2, 'native_name': 'Show.S02.Complete', 'subtype': 'ZIP'}]}}
        with patch.object(service, '_json', return_value=search):
            items, _ = service.search('Show', '/Show.S01E02.mkv', 'assrt')
        self.assertEqual(len(items), 1)
        self.assertIn('字幕包', items[0]['match_label'])
        details = {'status': 0, 'sub': {'subs': [{'filename': 'Show.S01E03.srt', 'url': 'https://example.com/sub'}]}}
        with patch.object(service, '_json', return_value=details), patch.object(service, '_download', return_value=b'subtitle'):
            with self.assertRaises(ValueError):
                service.files(items[0])

    def test_inconsistent_or_missing_episode_metadata_is_rejected(self):
        with self.assertRaises(ValueError):
            OnlineSubtitles.search_target('Show', '/Show.S01E02.mkv', {'season': 1, 'episode': 3})
        with self.assertRaises(ValueError):
            OnlineSubtitles.search_target('Show', '/Show.mkv', {'media_type': 'episode'})
        target = OnlineSubtitles.search_target('Show', '/Show.S00E02.mkv', {'season': 0, 'episode': 2})
        self.assertEqual(target['season'], 0)

    def test_screenshot_query_rejects_unrelated_same_episode_results(self):
        target = OnlineSubtitles.search_target('Re：从零开始的异世界生活 Re:ゼロから始める異世界生活 2016 S04E17', '/media/Re - S04E17 - 第17集.mkv',
                                               {'title': 'Re：从零开始的异世界生活', 'original_title': 'Re:ゼロから始める異世界生活', 'season': 4, 'episode': 17, 'media_type': 'episode'})
        for name in ['生活大爆炸.The.Big.Bang.Theory.S04E17.Chi_Eng.ass', '2016-04-17 Yutori.E01.ass']:
            self.assertEqual(OnlineSubtitles.match_result({'name': name}, target), '')
        self.assertEqual(OnlineSubtitles.match_result({'name': 'Re：从零开始的异世界生活.S04E17.ass'}, target), '剧名与季集匹配')

    def test_numeric_movie_title_is_preserved(self):
        self.assertEqual(OnlineSubtitles._query_title('1984'), '1984')
        self.assertEqual(OnlineSubtitles._query_title('1917'), '1917')

    def test_editable_keywords_are_sent_intact_to_both_providers(self):
        service = OnlineSubtitles('token')
        keyword = '星际穿越 Interstellar 2014 1080p BluRay'
        with patch.object(service, 'cid', return_value=''), patch.object(service, '_json', side_effect=[{'code': 0, 'data': []}, {'status': 0, 'sub': {'subs': []}}]) as request:
            service.search(keyword, '/movie.mkv', 'all', {'title': '星际穿越', 'original_title': 'Interstellar', 'year': '2014', 'query_edited': True})
        self.assertEqual(request.call_args_list[0].args[1]['name'], keyword)
        self.assertEqual(request.call_args_list[1].args[1]['q'], keyword)

    def test_manual_year_edit_and_removal_override_metadata(self):
        context = {'title': 'Dune', 'year': '2021', 'query_edited': True}
        self.assertEqual(OnlineSubtitles.search_target('Dune 1984', '/Dune.mkv', context)['year'], '1984')
        self.assertEqual(OnlineSubtitles.search_target('Dune 1080p', '/Dune.mkv', context)['year'], '')
        self.assertEqual(OnlineSubtitles.search_target('Blade Runner 2049', '/movie.mkv', {'title': 'Blade Runner 2049', 'year': '2017', 'query_edited': True})['year'], '')
        self.assertEqual(OnlineSubtitles.search_target('2001 A Space Odyssey 1968', '/movie.mkv', {'title': '2001 A Space Odyssey', 'year': '1968'})['year'], '1968')

    def test_episode_keywords_preserve_release_terms_and_do_not_duplicate_episode(self):
        service = OnlineSubtitles()
        context = {'title': 'Show', 'season': 1, 'episode': 2, 'media_type': 'episode'}
        with patch.object(service, 'cid', return_value=''), patch.object(service, '_json', return_value={'code': 0, 'data': []}) as request:
            service.search('Show 2024 S01E02 1080p', '/Show.S01E02.mkv', 'thunder', context)
            self.assertEqual(request.call_args.args[1]['name'], 'Show 2024 S01E02 1080p')
            service.search('Show 2024 1080p', '/Show.S01E02.mkv', 'thunder', context)
            self.assertEqual(request.call_args.args[1]['name'], 'Show 2024 1080p S01E02')
            with self.assertRaises(ValueError):
                service.search('Show S01E03', '/Show.S01E02.mkv', 'thunder', context)


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

    def test_episode_context_reaches_search_and_is_bound_to_ticket(self):
        media = dict(title='Show', media_type='episode', season=2, episode=3)
        item = dict(name='Show.S02E03.srt', provider='thunder', format='srt', language='中文',
                    hash_match=False, target={'season': 2, 'episode': 3}, match_label='剧名与季集匹配')
        service = Mock()
        service.search.return_value = ([item], [])
        self.ns['_online_subtitle_service'] = lambda: service
        self.ns['_online_subtitle_media_path'] = lambda value: value
        with self.app.test_request_context(json={'keyword': 'Show', 'media_path': '/library/Show.S02E03.mkv', 'provider': 'thunder', 'media': media}):
            result = self.ns['library_online_subtitle_search']()
        service.search.assert_called_once_with('Show', '/library/Show.S02E03.mkv', 'thunder', media)
        payload = self.ns['_online_subtitle_signer']().loads(result['items'][0]['ticket'])
        self.assertEqual(payload['path'], '/library/Show.S02E03.mkv')
        self.assertEqual(payload['item']['target'], {'season': 2, 'episode': 3})

    def test_download_uses_bounded_upload_queue_and_path_authorization(self):
        service = Mock()
        service.files.return_value = ('subtitle.srt', b'1\n00:00:01,000 --> 00:00:02,000\nHello\n')
        manager = Mock()
        manager.submit_upload.return_value = ({'task_id': 'task-1'}, False)
        self.ns.update(_online_subtitle_service=lambda: service,
                       _online_subtitle_media_path=lambda path: path,
                       _subtitle_tasks=lambda: manager,
                       _get_all_media_library_root_paths=lambda: ['/library'],
                       _path_authorization_snapshot=lambda path, roots: {'real_path': path},
                       _subtitle_task_owner=lambda: 'user',
                       _subtitle_task_response=lambda task, reused, message: {'code': 0, 'task_id': task['task_id']},
                       Config=lambda: types.SimpleNamespace(get_config=lambda key: {'media_server': 'jellyfin'}))
        ticket = self.ns['_online_subtitle_signer']().dumps({'user': 'user', 'path': '/library/movie.mkv', 'item': {}})
        with self.app.test_request_context(json={'ticket': ticket}):
            result = self.ns['library_online_subtitle_download']()
        self.assertEqual(result, {'code': 0, 'task_id': 'task-1'})
        kwargs = manager.submit_upload.call_args.kwargs
        self.assertEqual(kwargs['payload']['canonical_media_file'], '/library/movie.mkv')
        self.assertEqual(kwargs['payload']['path_authorization']['target']['real_path'], '/library/movie.mkv')
        self.assertEqual(kwargs['server'], 'jellyfin')
        self.assertEqual(kwargs['files'][0].filename, 'subtitle.srt')
        self.assertTrue(kwargs['request_id'].startswith('online-'))
        manager.acquire_upload_admission.assert_called_once()
        manager.release_upload_admission.assert_called_once()

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
