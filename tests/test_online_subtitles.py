import ast
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('online_subtitles', ROOT / 'app/helper/online_subtitles.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
OnlineSubtitles = module.OnlineSubtitles


class OnlineSubtitleTest(unittest.TestCase):
    @staticmethod
    def response(code=200, payload=None, content=b'subtitle'):
        # Model requests' context manager and HTTPError response for fallback tests.
        response = Mock(status_code=code, is_redirect=False)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.json.return_value = payload
        response.iter_content.return_value = [content]
        if code >= 400:
            response.raise_for_status.side_effect = module.requests.HTTPError(response=response)
        return response

    def test_assrt_api_mirror_preserves_endpoint_and_parameters(self):
        params = {'token': 'SECRET', 'id': '123456'}
        for failure in (self.response(520), module.requests.ConnectTimeout()):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(module, 'bounded_request', side_effect=[failure, self.response(payload={'status': 0})]) as get:
                    self.assertEqual(OnlineSubtitles()._json('https://api.assrt.net/v1/sub/detail', params), {'status': 0})
                self.assertEqual(get.call_args.args[0], 'https://api.makedie.me/v1/sub/detail')
                self.assertEqual(get.call_args.kwargs['params'], params)
                self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_api_fallback_is_bounded_and_does_not_retry_client_errors(self):
        for url, responses, expected_calls in (
                ('https://api.assrt.net/v1/sub/search', [self.response(401)], 1),
                ('https://example.com/api', [self.response(520)], 1),
                ('https://api.assrt.net/v1/sub/search', [self.response(520), self.response(520)], 2),
                ('https://api.assrt.net/v1/sub/search', [self.response(payload=[])], 1)):
            with self.subTest(url=url, expected_calls=expected_calls):
                with patch.object(module, 'bounded_request', side_effect=responses) as get:
                    with self.assertRaises(ValueError):
                        OnlineSubtitles()._json(url, {'token': 'SECRET'})
                self.assertEqual(get.call_count, expected_calls)
        with patch.object(module, 'bounded_request', return_value=self.response(payload={'status': 30900})) as get:
            self.assertEqual(OnlineSubtitles()._json('https://api.assrt.net/v1/sub/search', {})['status'], 30900)
            self.assertEqual(get.call_count, 1)

    def test_assrt_http_520_upgrades_preserving_signed_url(self):
        service = OnlineSubtitles()
        url = 'http://file1.assrt.net:80/download/123/a%20b.srt?_=123&-=a%2Fb&api=1'
        with patch.object(service, '_check_url') as check, patch.object(module, 'bounded_request', side_effect=[self.response(520), self.response()]) as get:
            self.assertEqual(service._download(url), b'subtitle')
        upgraded = 'https://file1.assrt.net/download/123/a%20b.srt?_=123&-=a%2Fb&api=1'
        self.assertEqual([call.args[0] for call in get.call_args_list], [url, upgraded])
        self.assertEqual([call.args[0] for call in check.call_args_list], [url, upgraded])

    def test_download_fallback_does_not_change_hosts_or_downgrade_https(self):
        service = OnlineSubtitles()
        for url, code, count in (
                ('http://file1.assrt.net/sub', 520, 2),
                ('https://file1.assrt.net/sub', 520, 1),
                ('http://file1.assrt.net/sub', 403, 1),
                ('http://file1.assrt.net.evil.example/sub', 520, 1),
                ('http://example.com/sub', 520, 1)):
            with self.subTest(url=url, code=code):
                with patch.object(service, '_check_url'), patch.object(service, '_report_download_failure'), patch.object(module, 'bounded_request', side_effect=[self.response(code) for _ in range(count)]) as get:
                    with self.assertRaisesRegex(ValueError, 'HTTP ' + str(code)):
                        service._download(url)
                self.assertEqual(get.call_count, count)

    def test_thunder_failure_reasons_reach_realtime_log_without_secrets(self):
        invalid_json = self.response()
        invalid_json.json.side_effect = ValueError('SECRET response body')
        cases = [
            (self.response(403), 'HTTP 403'),
            (self.response(520), 'HTTP 520'),
            (module.requests.ConnectTimeout('SECRET'), '请求超时'),
            (module.requests.exceptions.SSLError('SECRET'), 'TLS/证书验证失败'),
            (module.requests.exceptions.ProxyError('http://user:SECRET@proxy'), '代理连接失败'),
            (module.requests.ConnectionError('SECRET'), '网络连接失败'),
            (invalid_json, '无法解析 JSON'),
            (self.response(payload=[]), 'JSON 顶层不是对象'),
            (self.response(payload={'code': 1001, 'message': 'SECRET'}), 'code=1001'),
            (self.response(payload={'code': 'SECRET'}), 'code=缺失或格式异常'),
            (self.response(payload={'code': 0, 'data': {}}), 'data 不是列表'),
            (self.response(payload={'code': 0, 'data': ['SECRET']}), '字幕条目不是对象'),
        ]
        service = OnlineSubtitles('SECRET')
        # Exercise the real queue append, replacing only the configured logger.
        for response, expected in cases:
            with self.subTest(expected=expected):
                with patch.object(service, 'cid', return_value=''), patch.object(module, 'bounded_request', side_effect=[response]), patch.object(module.log.Logger, 'get_instance', return_value=Mock()), patch.object(module.log, 'LOG_QUEUE', []):
                    results, warnings = service.search('影片', '/private/SECRET.mkv', 'thunder')
                    self.assertFalse(results)
                    self.assertEqual(warnings, ['迅雷检索失败，请稍后重试'])
                    self.assertEqual(len(module.log.LOG_QUEUE), 1)
                    record = module.log.LOG_QUEUE[0]
                    self.assertEqual(record['level'], 'ERROR')
                    self.assertIn(expected, record['text'])
                    self.assertIn('api-shoulei-ssl.xunlei.com', record['text'])
                    self.assertNotIn('SECRET', record['text'])

    def test_thunder_empty_results_do_not_log_failure(self):
        service = OnlineSubtitles()
        with patch.object(service, 'cid', return_value=''), patch.object(module, 'bounded_request', return_value=self.response(payload={'code': 0, 'data': []})), patch.object(module.log, 'error') as error:
            self.assertEqual(service.search('影片', '/media.mkv', 'thunder'), ([], []))
            error.assert_not_called()

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
        # A logging backend failure must not interrupt Assrt fallback.
        with patch.object(module.log, 'error', side_effect=RuntimeError('logger unavailable')), patch.object(service, 'cid', return_value=''), patch.object(service, '_json', side_effect=[ValueError(), {'status': 0, 'sub': {'subs': [{'id': 42, 'native_name': '电影.srt'}]}}]):
            results, warnings = service.search('电影', '/media.mkv')
        self.assertEqual(results[0]['remote_id'], '42')
        self.assertEqual(len(warnings), 1)

    def test_missing_token_is_reported(self):
        items, warnings = OnlineSubtitles().search('影片', '', 'assrt')
        self.assertEqual(items, [])
        self.assertIn('Token', warnings[0])

    def test_opensubtitles_matches_api_movie_metadata_without_auto_confidence_threshold(self):
        client = Mock()
        client.is_configured.return_value = (True, '')
        client.search_subtitles.return_value = ([
            dict(file_id=42, release='GROUP', language='zh-cn', high_confidence=False,
                 feature={'feature_type': 'movie', 'title': 'Movie', 'year': 2024}),
            dict(file_id=42, release='duplicate', language='zh-cn', feature={}),
            dict(file_id=43, release='Other.2024', language='zh-cn',
                 feature={'feature_type': 'movie', 'title': 'Other', 'year': 2024}),
            dict(file_id=44, release='Movie.2000', language='zh-cn',
                 feature={'feature_type': 'movie', 'title': 'Movie', 'year': 2000}),
        ], '')
        service = OnlineSubtitles(opensubtitles=client)
        items, warnings = service.search('Movie 2024', '/Movie.mkv', 'opensubtitles',
                                         {'title': 'Movie', 'year': 2024, 'imdbid': 'tt123'})
        self.assertEqual([item['remote_id'] for item in items], ['42'])
        self.assertEqual(items[0]['match_label'], '片名与年份匹配')
        self.assertEqual(items[0]['language'], '简体中文')
        self.assertIn('2', warnings[0])
        request = client.search_subtitles.call_args
        self.assertEqual(request.kwargs['query'], 'Movie 2024')
        self.assertEqual(request.args[0]['imdbid'], 'tt123')
        client.download.assert_not_called()

    def test_opensubtitles_episode_query_is_bound_to_path_and_filters_wrong_episode_hash(self):
        client = Mock()
        client.is_configured.return_value = (True, '')
        client.search_subtitles.return_value = ([
            dict(file_id=1, release='release', language='ze', feature={
                'feature_type': 'episode', 'parent_title': 'Show', 'season_number': 2, 'episode_number': 3}),
            dict(file_id=2, release='Show.S02E04', language='ze', moviehash_match=True, feature={
                'feature_type': 'episode', 'parent_title': 'Show', 'season_number': 2, 'episode_number': 4}),
            dict(file_id=3, release='Other.S02E03', language='ze', feature={
                'feature_type': 'episode', 'parent_title': 'Other', 'season_number': 2, 'episode_number': 3}),
        ], '')
        service = OnlineSubtitles(opensubtitles=client)
        items, _ = service.search('Show 1080p', '/Show.S02E03.mkv', 'opensubtitles',
                                  {'title': 'Show', 'media_type': 'episode', 'season': 2, 'episode': 3,
                                   'imdbid': 'tt123', 'query_edited': True})
        self.assertEqual([item['remote_id'] for item in items], ['1'])
        self.assertEqual(items[0]['target']['episode'], 3)
        self.assertEqual(items[0]['match_label'], '剧名与季集匹配')
        request = client.search_subtitles.call_args
        self.assertEqual(request.kwargs['query'], 'Show 1080p')
        self.assertIsNone(request.args[0]['imdbid'])
        self.assertEqual(request.args[0]['season'], 2)
        self.assertEqual(request.args[0]['episode'], 3)

    def test_opensubtitles_preserves_the_configured_language_order(self):
        client = Mock()
        client.is_configured.return_value = (True, '')
        client.search_subtitles.return_value = ([
            dict(file_id=1, release='Movie', language='ze', identity_match=True),
            dict(file_id=2, release='Movie', language='zh-cn', identity_match=True),
        ], '')
        items, _ = OnlineSubtitles(opensubtitles=client).search('Movie', '/Movie.mkv', 'opensubtitles')
        self.assertEqual([item['remote_id'] for item in items], ['1', '2'])

    def test_opensubtitles_failure_keeps_other_source_results(self):
        client = Mock()
        client.is_configured.return_value = (True, '')
        client.search_subtitles.return_value = ([], 'OpenSubtitles请求失败：HTTP 403')
        service = OnlineSubtitles('token', opensubtitles=client)
        with patch.object(service, 'cid', return_value=''), patch.object(service, '_json', side_effect=[
            {'code': 0, 'data': [{'name': 'Movie.2024.srt', 'url': 'https://example.com/sub', 'ext': 'srt'}]},
            {'status': 0, 'sub': {'subs': []}},
        ]):
            items, warnings = service.search('Movie 2024', '/Movie.mkv')
        self.assertEqual(items[0]['provider'], 'thunder')
        self.assertIn('HTTP 403', warnings[0])

    def test_opensubtitles_configuration_and_download_errors_do_not_consume_another_link(self):
        items, warnings = OnlineSubtitles().search('Movie', '/Movie.mkv', 'opensubtitles')
        self.assertFalse(items)
        self.assertIn('OpenSubtitles', warnings[0])
        client = Mock()
        client.is_configured.return_value = (False, '未配置OpenSubtitles用户名或密码')
        service = OnlineSubtitles(opensubtitles=client)
        item = dict(provider='opensubtitles', remote_id='42', name='Movie', language_code='zh-cn')
        with self.assertRaisesRegex(ValueError, '用户名或密码'):
            service.files(item)
        client.download.assert_not_called()
        client.is_configured.return_value = (True, '')
        client.download.return_value = (None, 'OpenSubtitles请求失败：HTTP 406，下载次数已用尽')
        with self.assertRaisesRegex(ValueError, 'HTTP 406'):
            service.files(item)
        client.download.assert_called_once_with('42')

    def test_opensubtitles_download_uses_signed_file_id_and_language_with_existing_content_guard(self):
        client = Mock()
        client.is_configured.return_value = (True, '')
        client.download.return_value = ({'link': 'https://www.opensubtitles.com/download/signed'}, '')
        service = OnlineSubtitles(opensubtitles=client)
        content = b'1\n00:00:01,000 --> 00:00:02,000\nHello\n'
        item = dict(provider='opensubtitles', remote_id='42', name='Show.S02E03.ass', language_code='ze',
                    target={'season': 2, 'episode': 3}, hash_match=False)
        proxies = {'https': 'http://proxy.example:7890'}
        with patch.object(service, '_download', return_value=content) as download, \
                patch.object(module, 'Config', return_value=types.SimpleNamespace(get_proxies=lambda: proxies)):
            name, result = service.files(item)
        self.assertEqual(name, 'Show.S02E03.zh-cn.srt')
        self.assertEqual(result, content)
        client.download.assert_called_once_with('42')
        download.assert_called_once_with('https://www.opensubtitles.com/download/signed', proxies=proxies)

    def test_opensubtitles_proxy_fake_ip_is_allowed_only_for_its_real_domain(self):
        with patch.object(module, 'resolve_addresses', return_value=[(None, None, None, None, ('198.18.0.38', 443))]):
            OnlineSubtitles._check_url('https://www.opensubtitles.com/download/sub.srt')
            with self.assertRaises(ValueError):
                OnlineSubtitles._check_url('https://opensubtitles.com.evil.example/sub.srt')

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

    def test_rar_pack_is_reported_as_unsupported(self):
        from tests.test_subtitle_season_pack import rar_pack
        service = OnlineSubtitles()
        item = dict(provider='thunder', url='https://example.com', name='archive', format='srt')
        with patch.object(service, '_download', return_value=rar_pack(b'first subtitle', name=b'Show.S01E01.srt')):
            with self.assertRaises(ValueError) as error:
                service.files(item)
        self.assertIn('RAR', str(error.exception))

    def test_download_rejects_private_destination_and_redirect(self):
        with patch.object(module, 'resolve_addresses', return_value=[(None, None, None, None, ('127.0.0.1', 80))]):
            with self.assertRaises(ValueError):
                OnlineSubtitles._check_url('http://localhost/sub.srt')
        response = Mock(is_redirect=True, headers={'Location': 'http://localhost/private'})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        service = OnlineSubtitles()
        with patch.object(service, '_check_url', side_effect=[None, ValueError('private')]), patch.object(module, 'bounded_request', return_value=response) as get:
            with self.assertRaises(ValueError):
                service._download('https://example.com/subtitle')
            self.assertEqual(get.call_count, 1)

    def test_proxy_fake_ip_is_only_allowed_for_known_providers(self):
        with patch.object(module, 'resolve_addresses', return_value=[(None, None, None, None, ('198.18.0.38', 80))]):
            OnlineSubtitles._check_url('https://subtitle.v.geilijiasu.com/file.srt')
            with self.assertRaises(ValueError):
                OnlineSubtitles._check_url('https://untrusted.example/file.srt')
        with patch.object(module, 'resolve_addresses', return_value=[(None, None, None, None, ('192.168.1.1', 80))]):
            with self.assertRaises(ValueError):
                OnlineSubtitles._check_url('https://subtitle.v.geilijiasu.com/file.srt')

    def test_request_failure_does_not_expose_token(self):
        with patch.object(module, 'bounded_request', side_effect=module.requests.RequestException('url?token=SECRET')):
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

    def test_opensubtitles_ticket_requires_explicit_quota_confirmation_before_download(self):
        service = Mock()
        self.ns['_online_subtitle_service'] = lambda: service
        self.ns['_online_subtitle_media_path'] = lambda path: path
        ticket = self.ns['_online_subtitle_signer']().dumps({
            'user': 'user', 'path': '/library/movie.mkv', 'item': {'provider': 'opensubtitles', 'remote_id': '42'}
        })
        with self.app.test_request_context(json={'ticket': ticket}):
            result = self.ns['library_online_subtitle_download']()
        self.assertEqual(result['code'], -1)
        self.assertIn('配额', result['msg'])
        service.files.assert_not_called()

    def test_opensubtitles_download_defers_quota_until_idempotent_queue_admission(self):
        service = Mock()
        service.files.return_value = ('movie.zh-cn.srt', b'content')
        manager = Mock()
        manager.submit_download_upload.return_value = ({'task_id': 'existing-task'}, True)
        self.ns.update(_online_subtitle_service=lambda: service,
                       _online_subtitle_media_path=lambda path: path,
                       _subtitle_tasks=lambda: manager,
                       _get_all_media_library_root_paths=lambda: ['/library'],
                       _path_authorization_snapshot=lambda path, roots: {'real_path': path},
                       _subtitle_task_owner=lambda: 'user',
                       _subtitle_task_response=lambda task, reused, message: {'code': 0, 'task_id': task['task_id'], 'reused': reused},
                       Config=lambda: types.SimpleNamespace(get_config=lambda key: {'media_server': 'jellyfin'}))
        ticket = self.ns['_online_subtitle_signer']().dumps({
            'user': 'user', 'path': '/library/movie.mkv', 'item': {'provider': 'opensubtitles', 'remote_id': '42'}
        })
        with self.app.test_request_context(json={'ticket': ticket, 'confirm_quota': True}):
            result = self.ns['library_online_subtitle_download']()
        self.assertTrue(result['reused'])
        service.files.assert_not_called()
        kwargs = manager.submit_download_upload.call_args.kwargs
        self.assertEqual(kwargs['owner'], 'user')
        self.assertEqual(kwargs['payload']['canonical_media_file'], '/library/movie.mkv')
        with self.app.test_request_context(json={'ticket': ticket, 'confirm_quota': True, 'member': 'ignored.srt'}):
            self.ns['library_online_subtitle_download']()
        self.assertEqual(manager.submit_download_upload.call_args.kwargs['request_id'], kwargs['request_id'])
        files = kwargs['files_factory']()
        self.assertEqual(files[0].filename, 'movie.zh-cn.srt')
        service.files.assert_called_once_with({'provider': 'opensubtitles', 'remote_id': '42'})


class OnlineDownloadAdmissionTest(unittest.TestCase):
    def manager(self):
        # Exercise the production admission method and per-owner reentrant lock
        # without importing unrelated NAS services or a configured database.
        tree = ast.parse((ROOT / 'app/helper/subtitle_tasks.py').read_text())
        klass = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'SubtitleTaskManager')
        methods = [node for node in klass.body if isinstance(node, ast.FunctionDef)
                   and node.name in ('submit_download_upload', '_owner_submission')]
        namespace = {'contextmanager': contextmanager, 'threading': threading,
                     'SubtitleTaskError': ValueError, '_STANDARD_STAGING_FACTOR': 2}
        exec(compile(ast.Module(body=methods, type_ignores=[]), '<download-admission>', 'exec'), namespace)
        manager = types.SimpleNamespace(_lock=threading.RLock(), _owner_submit_locks={})
        manager._owner_submission = types.MethodType(namespace['_owner_submission'], manager)
        manager.submit_download_upload = types.MethodType(namespace['submit_download_upload'], manager)
        stored = {}
        manager.start = Mock()
        manager._find_reusable = lambda owner, kind, request, dedupe: stored.get((owner, request))
        manager._task_dict = lambda task, **kwargs: task
        manager.acquire_upload_admission = Mock(return_value='lease')
        manager.release_upload_admission = Mock()
        def submit_upload(**kwargs):
            task = {'task_id': 'task-1'}
            stored[(kwargs['owner'], kwargs['request_id'])] = task
            return task, False
        manager.submit_upload = Mock(side_effect=submit_upload)
        return manager

    def test_concurrent_retries_download_once_and_reuse_the_recorded_task(self):
        manager = self.manager()
        fetch = Mock(return_value=['subtitle'])
        def submit(_):
            return manager.submit_download_upload('user', fetch, {}, 'signed-request', 'jellyfin')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(sorted(reused for _, reused in results), [False, True])
        fetch.assert_called_once()
        manager.acquire_upload_admission.assert_called_once()
        manager.release_upload_admission.assert_called_once_with('lease')
        self.assertEqual(manager._owner_submit_locks, {})

    def test_full_queue_never_calls_quota_consuming_fetch(self):
        manager = self.manager()
        manager.acquire_upload_admission.side_effect = ValueError('queue full')
        fetch = Mock()
        with self.assertRaisesRegex(ValueError, 'queue full'):
            manager.submit_download_upload('user', fetch, {}, 'signed-request')
        fetch.assert_not_called()
        manager.release_upload_admission.assert_not_called()

    def test_failed_download_releases_admission_without_creating_a_task(self):
        manager = self.manager()
        fetch = Mock(side_effect=ValueError('HTTP 403'))
        with self.assertRaisesRegex(ValueError, 'HTTP 403'):
            manager.submit_download_upload('user', fetch, {}, 'signed-request')
        manager.release_upload_admission.assert_called_once_with('lease')
        manager.submit_upload.assert_not_called()
