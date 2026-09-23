"""Offline regression tests: execute production methods without starting app services.

AST loading avoids global Config/database initialization and third-party clients.
Run with: python -m unittest tests.test_sync_reliability
"""
import ast
import copy
import difflib
import json
import os
import re
import tempfile
import threading
import time
import traceback
import unittest
import uuid
from enum import Enum
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch
from urllib.parse import urlencode, urlparse, parse_qs

ROOT = Path(__file__).resolve().parents[1]


class MediaType(Enum):
    MOVIE = '电影'
    TV = '电视剧'
    ANIME = '动漫'


def load_class(path, name, methods, namespace):
    tree = ast.parse((ROOT / path).read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    cls.decorator_list = []
    cls.bases = []
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), path, 'exec'), namespace)
    return namespace[name]


def env():
    return dict(os=os, re=re, time=time, uuid=uuid, json=json, traceback=traceback,
                MediaType=MediaType, Enum=Enum, MatchMode=NS(NORMAL='normal'), EpisodeFormat=object,
                log=Mock(), ExceptionUtils=Mock(), lock=threading.Lock(), urlencode=urlencode,
                Config=lambda: NS(get_config=lambda key: {}),
                DEFAULT_EPISODE_MAPPINGS=[], DEFAULT_NAME_ALIASES={},
                RmtMode=NS(LINK='link', MOVE='move', RCLONE='rclone', MINIO='minio'),
                SyncType=NS(MON='monitor'), RMT_MEDIAEXT=['.mkv'], RMT_FAVTYPE='Favorites')


class RecognitionTests(unittest.TestCase):
    def setUp(self):
        self.ns = env()
        cls = load_class('app/media/media.py', 'Media', [
            '__search_media_with_name', '__extract_llm_tmdb_target', '__resolve_tmdb_mtype',
            'get_media_info_on_files', 'get_cache_info', '__make_cache_key', '_valid_media_identity',
            '_prepare_media_identity', '_apply_episode_mapping', '_apply_llm_season',
            '__search_tv_by_name'], self.ns)
        self.media = cls()

    def test_explicit_work_mapping_is_verified_and_not_applied_twice(self):
        rules = [{'tmdb_id': 65942, 'source_season': 4, 'source_begin': 1,
                  'source_end': 19, 'target_season': 1, 'offset': 66}]
        self.ns['Config'] = lambda: NS(get_config=lambda key: {'episode_mappings': rules})
        meta = NS(type=MediaType.ANIME, begin_season=4, begin_episode=14, note={})
        meta.get_episode_list = lambda: [meta.begin_episode]
        meta.get_season_list = lambda: [meta.begin_season]
        info = {'id': 65942, 'media_type': MediaType.TV,
                'genres': [{'id': 16}], 'seasons': [{'season_number': 1}]}
        self.media.get_tmdb_tv_season_detail = Mock(return_value={'episodes': [{'episode_number': 80}]})
        self.assertTrue(self.media._prepare_media_identity(meta, info))
        self.assertEqual((1, 80), (meta.begin_season, meta.begin_episode))
        self.assertTrue(self.media._prepare_media_identity(meta, info))
        self.assertEqual((1, 80), (meta.begin_season, meta.begin_episode))

    def test_mapping_does_not_guess_when_tmdb_target_is_missing(self):
        rules = [{'tmdb_id': 65942, 'source_season': 4, 'source_begin': 1,
                  'source_end': 19, 'target_season': 1, 'offset': 66}]
        self.ns['Config'] = lambda: NS(get_config=lambda key: {'episode_mappings': rules})
        meta = NS(type=MediaType.ANIME, begin_season=4, begin_episode=14, note={},
                  get_episode_list=lambda: [14], get_season_list=lambda: [4])
        info = {'id': 65942, 'media_type': MediaType.TV, 'genres': [{'id': 16}]}
        self.media.get_tmdb_tv_season_detail = Mock(return_value={})
        self.assertFalse(self.media._prepare_media_identity(meta, info))
        self.assertEqual((4, 14), (meta.begin_season, meta.begin_episode))

    def test_rejected_inferred_year_can_retry_in_strict_mode(self):
        self.media._rmt_match_mode = 'strict'
        self.media._Media__search_tmdb = Mock(side_effect=[{}, {'id': 309974}])
        meta = NS(type=MediaType.ANIME, year='2025', begin_season=1,
                  note={'llm': {'inferred_year': True}})
        result = self.media._Media__search_media_with_name(meta, '透明之夜', strict=True)
        self.assertEqual(309974, result['id'])
        self.assertNotIn('first_media_year', self.media._Media__search_tmdb.call_args.kwargs)

    def test_anime_searches_tv_with_or_without_year(self):
        for year in ['2024', None]:
            self.media._Media__search_tmdb = Mock(return_value={'id': 123})
            self.media._Media__search_media_with_name(
                NS(type=MediaType.ANIME, year=year, begin_season=2), 'Example')
            self.assertEqual(MediaType.TV, self.media._Media__search_tmdb.call_args.kwargs['search_type'])

    def test_llm_id_requires_candidate_and_matching_type(self):
        for verified, kind, expected in [(False, 'tv', None), (True, 'movie', None), (True, 'tv', 123)]:
            meta = NS(type=MediaType.ANIME, note={'llm': {
                'tmdb_id': 123, 'tmdb_type': kind, 'candidate_verified': verified}})
            self.assertEqual(expected, self.media._Media__extract_llm_tmdb_target(meta)[0])

    def test_conflicting_old_cache_is_ignored_but_tv_anime_are_compatible(self):
        meta = NS(type=MediaType.ANIME, get_name=lambda: 'Anime', year='2024', begin_season=1)
        self.media.meta = Mock()
        self.media.meta.get_meta_data_by_key.return_value = {'id': 123, 'type': MediaType.MOVIE}
        self.assertEqual({}, self.media.get_cache_info(meta))
        self.media.meta.get_meta_data_by_key.return_value = {'id': 456, 'type': MediaType.TV}
        self.assertEqual({}, self.media.get_cache_info(meta))
        self.media.meta.get_meta_data_by_key.return_value = {'id': 456, 'type': MediaType.ANIME}
        self.assertEqual(456, self.media.get_cache_info(meta)['id'])

    def test_anime_rejects_live_action_and_absent_season(self):
        meta = NS(type=MediaType.ANIME, begin_season=2, get_season_list=lambda: [2])
        info = {'media_type': MediaType.TV, 'genres': [{'id': 18}],
                'seasons': [{'season_number': 1}]}
        self.assertFalse(self.media._valid_media_identity(meta, info))
        info['genres'] = [{'id': 16}]
        self.assertFalse(self.media._valid_media_identity(meta, info))
        info['seasons'].append({'season_number': 2})
        self.assertTrue(self.media._valid_media_identity(meta, info))

    def test_verified_tv_can_correct_default_movie_but_not_explicit_hint(self):
        meta = NS(type=MediaType.MOVIE, note={'llm': {
            'tmdb_id': 123, 'tmdb_type': 'tv', 'candidate_verified': True}})
        self.assertEqual(123, self.media._Media__extract_llm_tmdb_target(meta)[0])
        self.assertEqual((None, None), self.media._Media__extract_llm_tmdb_target(meta, MediaType.MOVIE))

    def test_anime_search_skips_same_named_live_action(self):
        self.ns['TMDbException'] = RuntimeError
        self.media.search = Mock(total_results=2)
        self.media.search.tv_shows.return_value = [
            {'id': 7030, 'name': '花样少男少女', 'genre_ids': [18]},
            {'id': 123, 'name': '花样少男少女', 'genre_ids': [16]}]
        self.media._Media__compare_tmdb_names = lambda a, b, **kwargs: a == b
        found = self.media._Media__search_tv_by_name('花样少男少女', None, anime_only=True)
        self.assertEqual(123, found['id'])

    def test_short_chinese_title_cannot_match_different_work_prefix(self):
        ns = env()
        ns.update(difflib=difflib, StringUtils=NS(
            handler_special_chars=lambda s: s,
            is_chinese=lambda s: bool(re.search('[\u4e00-\u9fff]', s))))
        cls = load_class('app/media/media.py', 'Media', ['__compare_tmdb_names'], ns)
        self.assertFalse(cls._Media__compare_tmdb_names('海贼王', '海贼王女'))
        self.assertTrue(cls._Media__compare_tmdb_names('海贼王', '海贼王'))

    def test_movie_result_does_not_mutate_episode_identity(self):
        cls = load_class('app/media/meta/_base.py', 'MetaBase', ['set_tmdb_info'], self.ns)
        meta = cls()
        meta.type, meta.begin_episode, meta.tmdb_id = MediaType.ANIME, 3, 456
        meta.set_tmdb_info({'media_type': MediaType.MOVIE, 'id': 123})
        self.assertEqual(456, meta.tmdb_id)
        self.assertEqual(MediaType.ANIME, meta.type)

    def identify_file(self, name, episodes, seasons=(2,), parsed_episode=3, tmdb_seasons=None):
        info = {'media_type': MediaType.TV, 'id': 456}
        if tmdb_seasons is not None:
            info['seasons'] = tmdb_seasons
        meta = NS(type=MediaType.TV, begin_episode=parsed_episode, begin_season=1,
                  set_tmdb_info=Mock())
        meta.get_episode_list = lambda: [meta.begin_episode] if meta.begin_episode else []
        self.ns['MetaInfo'] = Mock(return_value=meta)
        self.ns['PathUtils'] = NS(get_parent_paths=lambda p, n: str(Path(p).parents[n - 1]),
                                  get_bluray_dir=lambda p: None)
        self.media.tmdb = True
        self.media.save_rename_cache = Mock()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / name)
            Path(path).touch()
            result = self.media.get_media_info_on_files([path], info, MediaType.TV,
                download_context={'tmdb_info': info, 'seasons': list(seasons), 'episodes': episodes})
        return result, meta

    def test_monitored_batch_keeps_each_file_identity(self):
        self.ns['PathUtils'] = NS(get_parent_paths=lambda p, n: str(Path(p).parents[n - 1]),
                                  get_bluray_dir=lambda p: None)
        self.media.tmdb = True
        self.media.save_rename_cache = Mock()
        metas = [NS(type=MediaType.MOVIE, set_tmdb_info=Mock()) for _ in range(3)]
        self.ns['MetaInfo'] = Mock(side_effect=metas)
        with tempfile.TemporaryDirectory() as directory:
            paths = [str(Path(directory) / ('film%s.mkv' % i)) for i in range(3)]
            for path in paths:
                Path(path).touch()
            infos = [{'media_type': MediaType.MOVIE, 'id': i} for i in [101, 202, 303]]
            contexts = {paths[i]: {'tmdb_info': infos[i]} for i in range(2)}
            result = self.media.get_media_info_on_files(paths, infos[2], MediaType.MOVIE,
                                                       download_contexts=contexts)
            self.assertEqual(3, len(result))
            for meta, info in zip(metas, infos):
                meta.set_tmdb_info.assert_called_once_with(info)
            self.media.save_rename_cache.assert_called_once_with('film2.mkv', infos[2])

    def test_rss_identity_survives_different_file_title(self):
        result, meta = self.identify_file('abbreviated - 03.mkv', [3])
        self.assertEqual(1, len(result))
        self.assertEqual(2, meta.begin_season)
        meta.set_tmdb_info.assert_called_once_with({'media_type': MediaType.TV, 'id': 456})
        self.assertFalse(self.ns['MetaInfo'].call_args.kwargs['use_llm'])
        self.media.save_rename_cache.assert_not_called()

    def test_season_conflict_is_not_silently_overwritten(self):
        result, _ = self.identify_file('Other S01E03.mkv', [3])
        self.assertEqual({}, result)

    def test_absolute_episode_conflict_requires_mapping(self):
        result, _ = self.identify_file('Name - 15.mkv', [3], parsed_episode=15)
        self.assertEqual({}, result)

    def test_absolute_episode_maps_only_with_verified_previous_seasons(self):
        result, meta = self.identify_file('Name - 15.mkv', [3], parsed_episode=15,
            tmdb_seasons=[{'season_number': 1, 'episode_count': 12}])
        self.assertEqual(1, len(result))
        self.assertEqual(3, meta.begin_episode)
        result, _ = self.identify_file('Name S02E15.mkv', [3], parsed_episode=15,
            tmdb_seasons=[{'season_number': 1, 'episode_count': 12}])
        self.assertEqual({}, result)

    def test_single_file_can_use_rss_episode(self):
        result, meta = self.identify_file('abbreviation.mkv', [3], parsed_episode=None)
        self.assertEqual(1, len(result))
        self.assertEqual(3, meta.begin_episode)


class JellyfinTests(unittest.TestCase):
    def setUp(self):
        self.ns = env()
        cls = load_class('app/mediaserver/client/jellyfin.py', 'Jellyfin',
                         ['__get_jellyfin_tv_episodes', 'get_no_exists_episodes'], self.ns)
        self.client = cls()
        self.client._host, self.client._apikey, self.client._user = 'http://mock/', 'dummy', 'u'
        self.request = Mock(side_effect=self.respond)
        self.client._Jellyfin__request_utils = lambda: NS(get_res=self.request)
        self.fail_disk_b = False
        self.season = 1

    def respond(self, url):
        params = parse_qs(urlparse(url).query)
        if '/Items?' in url:
            self.assertEqual(['tmdb.123'], params['AnyProviderIdEquals'])
            offset = int(params['StartIndex'][0])
            # Simulate a server returning partial pages and different localized names.
            data = {'Items': [{'Id': 'A' if offset == 0 else 'B', 'Name': 'Localized',
                               'ProviderIds': {'Tmdb': '123'}}], 'TotalRecordCount': 2}
        elif '/Seasons?' in url:
            data = {'Items': [{'Id': 'season', 'IndexNumber': self.season}]}
        else:
            if self.fail_disk_b and '/B/' in url:
                return None
            data = {'Items': [{'IndexNumber': 1, 'IndexNumberEnd': 2}] if '/A/' in url
                             else [{'IndexNumber': 3}]}
        response = Mock()
        response.json.return_value = data
        return response

    def test_merge_all_entries_pages_and_episode_ranges(self):
        missing = self.client.get_no_exists_episodes(NS(title='Anime', year=2024, tmdb_id=123), 1, 4)
        self.assertEqual([4], missing)
        self.assertTrue(any('/B/Episodes?' in c.args[0] for c in self.request.call_args_list))

    def test_partial_failure_is_unknown_not_missing(self):
        self.fail_disk_b = True
        self.assertIsNone(self.client.get_no_exists_episodes(NS(title='Anime', year=2024, tmdb_id=123), 1, 4))

    def test_specials_season_zero_not_replaced_by_one(self):
        self.season = 0
        self.assertEqual([4], self.client.get_no_exists_episodes(NS(title='Anime', year=2024, tmdb_id=123), 0, 4))


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.ns = env()
        cls = load_class('app/downloader/downloader.py', 'Downloader',
                         ['transfer', '_get_download_context', '_create_download_context', 'check_exists_medias', 'get_monitored_download_contexts'], self.ns)
        self.d = cls()
        self.d.default_client = Mock()
        self.d._default_client_type = NS(value='QB')
        self.d._pt_monitor_only = False
        self.d._pt_rmt_mode = 'link'
        self.d.filetransfer = Mock()
        self.d.dbhelper = Mock()
        self.d.dbhelper.get_legacy_download_context.return_value = None
        self.d.media = Mock()
        self.d.default_client.get_transfer_task.return_value = [{'path': '/mock/file', 'id': 'hash'}]

    def test_failed_transfer_retries_without_organized_tag(self):
        self.d.filetransfer.transfer_media.return_value = False, 'temporary lookup error'
        self.d.transfer()
        self.d.default_client.set_torrents_status.assert_not_called()
        self.d.transfer()
        self.assertEqual(1, self.d.filetransfer.transfer_media.call_count)
        self.d._transfer_retries[('QB', 'hash')] = (1, 0)
        self.d.filetransfer.transfer_media.return_value = True, ''
        self.d.transfer()
        self.d.default_client.set_torrents_status.assert_called_once()

    def test_one_task_exception_does_not_block_next(self):
        self.d.default_client.get_transfer_task.return_value = [
            {'path': '/mock/one', 'id': 'one'}, {'path': '/mock/two', 'id': 'two'}]
        self.d.filetransfer.transfer_media.side_effect = [OSError('IO failure'), (True, '')]
        self.d.transfer()
        self.d.default_client.set_torrents_status.assert_called_once_with(ids='two', tags=None)

    def test_context_roundtrip_and_forward_to_transfer(self):
        records = {}
        def save(key, downloader, payload):
            records[(key, downloader)] = json.dumps(payload)
            return True
        self.d.dbhelper.save_download_context.side_effect = save
        self.d.dbhelper.get_download_context.side_effect = lambda k, d: json.loads(records[(k, d)])
        namespace = {'sys': __import__('sys'), 'TMDbException': RuntimeError}
        tree = ast.parse((ROOT / 'app/media/tmdbv3api/as_obj.py').read_text())
        tree.body = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        exec(compile(tree, 'as_obj.py', 'exec'), namespace)
        info = namespace['AsObj'](id=123, media_type=MediaType.TV,
                                  genres=[{'id': 16}], seasons=[{'season_number': 2}],
                                  external_ids={'nested': {'value': 'test'}})
        media = NS(tmdb_info=info, org_string='RSS title',
                   get_season_list=lambda: [2], get_episode_list=lambda: [3])
        tag = self.d._create_download_context(media, self.d._default_client_type)
        task = {'path': '/different/file.mkv', 'id': 'hash', 'tags': 'user-tag, ' + tag}
        self.d.default_client.get_transfer_task.return_value = [task]
        self.d.filetransfer.transfer_media.return_value = (True, '')
        self.d.transfer()
        context = self.d.filetransfer.transfer_media.call_args.kwargs['download_context']
        self.assertEqual(123, context['tmdb_info']['id'])
        self.assertEqual(MediaType.TV, context['tmdb_info']['media_type'])
        self.assertEqual([3], context['episodes'])
        self.assertEqual('test', context['tmdb_info']['external_ids']['nested']['value'])
        self.assertEqual([{'id': 16}], context['tmdb_info']['genres'])

    def test_missing_context_fails_closed(self):
        self.d.dbhelper.get_download_context.return_value = None
        self.d.default_client.get_transfer_task.return_value = [{'id': 'hash', 'tags': ['NASTOOL_CTX_missing']}]
        self.d.transfer()
        self.d.filetransfer.transfer_media.assert_not_called()
        self.d.default_client.set_torrents_status.assert_not_called()

    def test_legacy_context_uses_exact_rss_metadata_when_tmdb_is_temporarily_down(self):
        self.d.dbhelper.get_legacy_download_context.return_value = {
            'tmdb_info': {'id': 285743, 'media_type': MediaType.TV},
            'legacy_type': MediaType.ANIME.value,
            'title': '和青梅竹马之间不会有恋爱喜剧', 'year': '2026',
            'seasons': [1], 'episodes': []}
        self.d.media.get_tmdb_info.return_value = None
        value = self.d._get_download_context(
            {'id': 'f99ef0a1705d43ac1ca19c59727e7d5b534cc9b4', 'tags': 'NASTOOL'},
            self.d._default_client_type)
        self.assertEqual(285743, value['tmdb_info']['id'])
        self.assertEqual('和青梅竹马之间不会有恋爱喜剧', value['tmdb_info']['name'])
        self.assertEqual([16], value['tmdb_info']['genre_ids'])

    def test_jellyfin_empty_result_still_checks_local_movies(self):
        self.d.mediaserver = Mock()
        self.d.mediaserver.get_movies.return_value = []
        self.d.filetransfer.get_no_exists_medias.return_value = [{'title': 'Film', 'year': 2024}]
        meta = NS(type=MediaType.MOVIE, title='Film', year=2024,
                  begin_season=None, get_episode_list=lambda: [])
        self.assertTrue(self.d.check_exists_medias(meta)[0])

    def test_tv_missing_is_intersection_of_server_and_disks(self):
        self.d.mediaserver = Mock()
        self.d.mediaserver.get_no_exists_episodes.return_value = [2, 3]
        self.d.filetransfer.get_no_exists_medias.return_value = [1, 3]
        self.d.media = Mock()
        self.d.media.get_tmdb_info.return_value = {'id': 123}
        self.d.media.get_tmdb_tv_season_detail.return_value = {'episodes': [{'episode_number': e} for e in [1, 2, 3]]}
        meta = NS(type=MediaType.ANIME, title='Anime', year=2024, tmdb_id=123,
                  begin_season=1, get_season_list=lambda: [1], get_episode_list=lambda: [2],
                  get_title_string=lambda: 'Anime', get_season_episode_string=lambda: 'S01E02')
        exists, missing, _ = self.d.check_exists_medias(meta, total_ep={1: 3})
        self.assertTrue(exists)
        self.assertEqual([3], missing[123][0]['episodes'])

    def test_absolute_episode_uses_tmdb_numbers_not_season_count(self):
        self.d.media = Mock()
        self.d.media.get_tmdb_info.return_value = {'id': 37854, 'seasons': [{'season_number': 23}]}
        numbers = list(range(1156, 1182))
        self.d.media.get_tmdb_tv_season_detail.return_value = {
            'episodes': [{'episode_number': ep} for ep in numbers]}
        self.d.media.get_tmdb_season_episodes_num.return_value = 26
        self.d.mediaserver = Mock()
        self.d.mediaserver.get_no_exists_episodes.return_value = [1179]
        self.d.filetransfer.get_no_exists_medias.return_value = [1179]
        meta = NS(type=MediaType.ANIME, title='航海王', tmdb_id=37854, begin_season=23,
                  get_season_list=lambda: [23], get_episode_list=lambda: [1179],
                  get_title_string=lambda: '航海王', get_season_episode_string=lambda: 'S23E1179')
        exists, missing, _ = self.d.check_exists_medias(meta)
        self.assertFalse(exists)
        self.assertEqual([1179], missing[37854][0]['episodes'])
        self.assertEqual(numbers, self.d.mediaserver.get_no_exists_episodes.call_args.kwargs['episode_numbers'])

    def test_out_of_range_and_missing_season_are_unknown(self):
        self.d.media = Mock()
        self.d.media.get_tmdb_info.return_value = {'id': 245842}
        self.d.media.get_tmdb_tv_season_detail.return_value = {'episodes': [{'episode_number': e} for e in range(1, 13)]}
        self.d.mediaserver = Mock()
        meta = NS(type=MediaType.ANIME, tmdb_id=245842, begin_season=1,
                  get_season_list=lambda: [1], get_episode_list=lambda: list(range(13, 25)),
                  get_title_string=lambda: '杖与剑的魔剑谭')
        for count in [12, 0]:
            self.d.media.get_tmdb_season_episodes_num.return_value = count
            self.assertIsNone(self.d.check_exists_medias(meta)[0])
        self.d.mediaserver.get_no_exists_episodes.assert_not_called()

    def test_monitor_exact_membership_completion_and_conflicts(self):
        self.ns['DownloaderType'] = NS(QB=self.d._default_client_type)
        client = self.d.default_client
        client.get_replace_path.side_effect = lambda p: p
        torrent = {'hash': 'a', 'save_path': '/downloads', 'progress': 1,
                   'tags': 'NASTOOL_CTX_one'}
        client.get_torrents.return_value = ([torrent], False)
        client.get_files.return_value = [{'name': 'show/01.mkv'}, {'name': 'show/02.mkv'}]
        self.d.dbhelper.get_download_context.side_effect = lambda *args: {
            'tmdb_info': {'id': 285743, 'media_type': 'TV'}, 'episodes': [1, 2]}
        wanted = ['/downloads/show/01.mkv', '/downloads/show/other.mkv', '/downloads2/show/01.mkv']
        contexts = self.d.get_monitored_download_contexts(wanted)
        self.assertEqual([wanted[0]], list(contexts))
        self.assertFalse(contexts[wanted[0]]['allow_episode_fallback'])
        torrent['progress'] = .5
        with self.assertRaises(ValueError):
            self.d.get_monitored_download_contexts(wanted)
        torrent['progress'] = 1
        client.get_torrents.return_value = ([torrent, torrent], False)
        with self.assertRaises(ValueError):
            self.d.get_monitored_download_contexts(wanted)
        client.get_torrents.return_value = ([], True)
        with self.assertRaises(ValueError):
            self.d.get_monitored_download_contexts(wanted)

    def test_monitor_recovers_legacy_rss_identity_by_hash(self):
        self.ns['DownloaderType'] = NS(QB=self.d._default_client_type)
        client = self.d.default_client
        client.get_replace_path.side_effect = lambda p: p
        client.get_torrents.return_value = ([{
            'hash': 'f99ef0a1705d43ac1ca19c59727e7d5b534cc9b4',
            'save_path': '/downloads', 'content_path': '/downloads/show',
            'progress': 1, 'tags': 'NASTOOL'
        }], False)
        client.get_files.return_value = [{'name': 'show/05.mkv'}, {'name': 'show/06.mkv'}]
        client.get_legacy_download_context = Mock()
        self.d.dbhelper.get_legacy_download_context.return_value = {
            'tmdb_info': {'id': 285743, 'media_type': MediaType.TV},
            'source_title': 'RSS title', 'seasons': [1], 'episodes': []}
        self.d.media = Mock()
        self.d.media.get_tmdb_info.return_value = {
            'id': 285743, 'media_type': MediaType.TV, 'name': '和青梅竹马之间不会有恋爱喜剧',
            'genre_ids': [16], 'seasons': [{'season_number': 1, 'episode_count': 12}]
        }
        path = '/downloads/show/05.mkv'
        contexts = self.d.get_monitored_download_contexts([path])
        self.assertEqual(285743, contexts[path]['tmdb_info']['id'])
        self.assertFalse(contexts[path]['allow_episode_fallback'])
        self.d.dbhelper.get_legacy_download_context.assert_called_once_with(
            'f99ef0a1705d43ac1ca19c59727e7d5b534cc9b4')
        self.d.media.get_tmdb_info.assert_called_once_with(mtype=MediaType.TV, tmdbid=285743)


class QueueTests(unittest.TestCase):
    def test_failed_queue_is_retained_and_io_runs_outside_lock(self):
        ns = env()
        ns['PathUtils'] = NS(get_bluray_dir=lambda p: None)
        cls = load_class('app/sync.py', 'Sync', ['transfer_mon_files'], ns)
        sync = cls()
        sync._synced_files = ['/mock/one.mkv']
        sync._need_sync_paths = {'/mock': {'files': ['/mock/one.mkv']}}
        def transfer(**kwargs):
            self.assertFalse(ns['lock'].locked())
            return False, 'temporary error'
        sync.filetransfer = NS(transfer_media=transfer)
        sync.transfer_mon_files()
        self.assertIn('/mock', sync._need_sync_paths)
        sync._need_sync_paths['/mock']['retry_at'] = 0
        sync.filetransfer.transfer_media = lambda **kwargs: (True, '')
        sync.transfer_mon_files()
        self.assertEqual({}, sync._need_sync_paths)
        self.assertEqual([], sync._synced_files)


class HardlinkTests(unittest.TestCase):
    def setUp(self):
        self.ns = env()
        self.mode = MediaType.ANIME  # Enum-shaped transfer mode; no app imports needed.
        self.ns['RmtMode'].LINK = self.mode
        cls = load_class('app/filetransfer.py', 'FileTransfer', [
            '__transfer_file', '__transfer_origin_file', '__get_best_target_path', '_existing_media_files', 'transfer_media'], self.ns)
        self.transfer = cls()
        self.transfer.dbhelper = Mock()
        self.transfer._FileTransfer__transfer_subtitles = Mock(return_value=0)

    def test_history_failure_retries_same_hardlink_without_overwriting(self):
        self.ns['Subtitle'] = Mock()
        t = self.transfer
        t.progress, t.message, t.threadhelper = Mock(), Mock(), Mock()
        t._filesize_cover = t._refresh_mediaserver = t._scraper_flag = t._movie_category_flag = False
        t.check_ignore = lambda file_list: (file_list, '')
        t._existing_media_files = Mock(return_value=[])
        t.media = Mock()
        t.dbhelper.insert_transfer_history.side_effect = [False, True]
        t.dbhelper.insert_transfer_blacklist.return_value = True
        t._FileTransfer__transfer_command = Mock()
        meta = NS(tmdb_id=1, type=MediaType.MOVIE, category='', title='Film', year='2026',
                  en_name='Film', cn_name='', begin_season=None, begin_episode=None,
                  imdb_id=None, set_tmdb_info=Mock(), tmdb_info={'id': 1},
                  get_title_string=lambda: 'Film (2026)')
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / 'source.mkv', Path(folder) / 'target.mkv'
            source.write_bytes(b'published content')
            os.link(source, target)
            t.media.get_media_info_on_files.return_value = {str(source): meta}
            t._FileTransfer__is_media_exists = Mock(return_value=(True, folder, True, str(target)))
            kwargs = dict(in_from='manual', in_path=str(source), files=[str(source)],
                          rmt_mode=self.mode, target_dir=folder)
            self.assertFalse(t.transfer_media(**kwargs)[0])
            t.dbhelper.insert_transfer_blacklist.assert_not_called()
            t.message.send_transfer_movie_message.assert_not_called()
            t.threadhelper.start_thread.assert_not_called()
            self.assertTrue(t.transfer_media(**kwargs)[0])
            self.assertEqual(source.stat().st_ino, target.stat().st_ino)
            t._FileTransfer__transfer_command.assert_not_called()
            t.dbhelper.insert_transfer_blacklist.assert_called_once_with(str(source))

    def test_failed_replacement_preserves_old_media(self):
        with tempfile.TemporaryDirectory() as folder:
            old = Path(folder) / 'old.mkv'
            old.write_bytes(b'original')
            self.transfer._FileTransfer__transfer_command = Mock(return_value=-1)
            ret = self.transfer._FileTransfer__transfer_file('/missing/new', str(old), self.mode, True, str(old))
            self.assertEqual(-1, ret)
            self.assertEqual(b'original', old.read_bytes())

    def test_successful_replacement_is_a_real_hardlink(self):
        with tempfile.TemporaryDirectory() as folder:
            old, source = Path(folder) / 'old.mkv', Path(folder) / 'source.mkv'
            old.write_bytes(b'original')
            source.write_bytes(b'replacement')
            def link(file_item, target_file, rmt_mode):
                os.link(file_item, target_file)
                return 0
            self.transfer._FileTransfer__transfer_command = link
            self.assertEqual(0, self.transfer._FileTransfer__transfer_file(str(source), str(old), self.mode, True, str(old)))
            self.assertEqual(source.stat().st_ino, old.stat().st_ino)
            self.assertEqual([], list(Path(folder).glob('*.linking')))

    def test_unknown_link_does_not_blacklist_recognition_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'episode.mkv'
            source.write_bytes(b'data')
            self.transfer._FileTransfer__transfer_command = Mock(return_value=0)
            result = self.transfer._FileTransfer__transfer_origin_file(str(source), str(Path(folder) / 'unknown'), self.mode)
            self.assertEqual(0, result)
            self.transfer.dbhelper.insert_transfer_blacklist.assert_not_called()

    def test_history_proves_existence_only_while_target_file_exists(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'episode.mkv'
            path.write_bytes(b'data')
            self.transfer.dbhelper.get_media_transfer_history.return_value = [
                NS(DEST_PATH=folder, DEST_FILENAME='episode.mkv', SEASON_EPISODE='S02E03')]
            media = NS(tmdb_id=123, type=MediaType.ANIME)
            self.assertEqual([(str(path), 'S02E03')], self.transfer._existing_media_files(media))
            path.unlink()
            self.assertEqual([], self.transfer._existing_media_files(media))

    def test_selects_matching_device_not_longest_path_prefix(self):
        self.transfer._anime_path = ['/disk1/library', '/disk2/library']
        def stat(path):
            return NS(st_dev=2 if path in ['/downloads/file', '/disk2/library'] else 1)
        with patch.object(os, 'stat', side_effect=stat), patch.object(os.path, 'isdir', return_value=True):
            dest = self.transfer._FileTransfer__get_best_target_path(
                MediaType.ANIME, '/downloads/file', rmt_mode=self.mode)
        self.assertEqual('/disk2/library', dest)


if __name__ == '__main__':
    unittest.main()
