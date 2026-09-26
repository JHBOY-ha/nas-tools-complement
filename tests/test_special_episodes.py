"""Offline special-content recognition and local extra publication regressions."""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.filetransfer import FileTransfer
from app.downloader.downloader import Downloader
from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.special import extract_special, special_references
from app.media.meta.special_resolver import SpecialResolver
from app.media.meta.extra_transfer import publish_extra
from app.utils.types import MediaType, RmtMode, SyncType, SearchType

SAMPLE = '[Kamigami&VCB-Studio] Yahari Ore no Seishun Lovecome wa Machigatte Iru. [OVA][Ma10p_1080p][x265_flac].mkv'


class SpecialRecognitionTest(unittest.TestCase):
    def setUp(self):
        self.media = Media.__new__(Media)
        self.media.tmdb = object()
        self.info = {"id": 65676, "media_type": MediaType.TV, "name": "我的青春恋爱物语果然有问题",
                     "original_name": "Yahari Ore no Seishun Lovecome wa Machigatte Iru", "genre_ids": [16],
                     "first_air_date": "2013-04-05", "seasons": [{"season_number": 0, "episode_count": 1},
                                                                    {"season_number": 1, "episode_count": 1}]}
        self.episodes = {0: [{"id": 800, "episode_number": 2, "season_number": 0, "show_id": 65676,
                             "name": "Bonus Story", "overview": "Season 1 OVA 01"}],
                         1: [{"id": 900, "episode_number": 1, "season_number": 1, "show_id": 65676,
                             "name": "Beginning", "overview": ""}]}
        self.cfg = patch('app.media.meta.special_resolver.Config')
        self.config = self.cfg.start()
        self.config.return_value.get_config.return_value = {}
        self.addCleanup(self.cfg.stop)
        self.query = patch.object(self.media, 'get_tmdb_infos', return_value=[self.info]).start()
        self.detail = patch.object(self.media, 'get_tmdb_info', return_value=self.info).start()
        self.season = patch.object(self.media, 'get_tmdb_tv_season_detail',
                                  side_effect=lambda tmdbid, season: {"episodes": self.episodes[season]}).start()
        self.addCleanup(patch.stopall)

    def resolve(self, name, **kwargs):
        return SpecialResolver(self.media).resolve(MetaInfo(name, use_llm=False), **kwargs)

    def test_actual_sample_cleans_metadata_but_does_not_guess_episode(self):
        meta = MetaInfo(SAMPLE, use_llm=False)
        self.assertEqual('Yahari Ore No Seishun Lovecome Wa Machigatte Iru', meta.get_name())
        self.assertEqual('Kamigami&VCB-Studio', meta.resource_team)
        self.assertEqual(('1080p', 'X265', 'flac'), (meta.resource_pix, meta.video_encode, meta.audio_encode))
        result = SpecialResolver(self.media).resolve(meta)
        self.assertEqual(65676, result.tmdb_id)
        self.assertIsNone(result.begin_episode)
        self.assertIn('无唯一证据', result.skip_reason)

    def test_release_numbers_are_not_formal_episode_ordinals(self):
        meta = self.resolve('Show S01 [OVA01].mkv', bound=self.info)
        self.assertIsNone(meta.skip_reason)
        self.assertEqual((0, 2), (meta.begin_season, meta.begin_episode))
        self.assertEqual(800, meta.note['special_episode']['episode_id'])
        self.assertEqual('tmdb', meta.note['episode_mapping']['provider'])
        self.assertTrue(self.resolve('Show S02 [OVA01].mkv', bound=self.info).skip_reason)
        self.assertTrue(self.resolve('Show [OVA03].mkv', bound=self.info).skip_reason)

    def test_title_and_formal_number_and_regular_season(self):
        meta = self.resolve('Show [OVA] - Bonus Story [1080p].mkv', bound=self.info)
        self.assertEqual((0, 2), (meta.begin_season, meta.begin_episode))
        self.assertEqual('Show', meta.get_name())
        self.assertIsNone(self.resolve('Show S00E02.mkv', bound=self.info).skip_reason)
        self.assertTrue(self.resolve('Show S00E99.mkv', bound=self.info).skip_reason)
        self.episodes[1][0]['overview'] = 'OAD 02'
        meta = self.resolve('Show [OAD02].mkv', bound=self.info)
        self.assertEqual((1, 1), (meta.begin_season, meta.begin_episode))

    def test_conflicting_evidence_duplicates_and_partial_lists(self):
        self.assertTrue(self.resolve('Show [OVA02] - Bonus Story.mkv', bound=self.info).skip_reason)
        self.episodes[1][0]['overview'] = 'OVA01'
        self.assertTrue(self.resolve('Show [OVA01].mkv', bound=self.info).skip_reason)
        self.episodes[1] = []
        self.assertTrue(self.resolve('Show [OVA01].mkv', bound=self.info).skip_reason)

    def test_names_not_removed_unless_release_fields(self):
        for name in ('A Special Day 2024.mkv', 'Nova 2024.mkv', 'The OVA Story.mkv',
                     'Movie [SPIDER].mkv', 'Rambo Extended Cut 2008 BluRay.mkv'):
            self.assertIsNone(extract_special(name)[1])
        self.assertIsNone(extract_special('Show [NCOP01].mkv')[1])
        self.assertEqual('other', extract_special('Show [NCOP&ED].mkv', True)[1]['category'])
        self.assertIsNone(MetaInfo('Show [SP01].mkv', use_llm=False).begin_episode)
        self.assertIn('reason', extract_special('Show [OVA01-02].mkv')[1])
        self.assertEqual([1, 2, 3], MetaInfo('S00E01-E03', use_llm=False).get_episode_list())

    def test_search_rejects_live_action_and_ambiguous_types(self):
        self.query.return_value = [dict(self.info, genre_ids=[18])]
        self.detail.return_value = self.query.return_value[0]
        with patch('app.media.meta.special_resolver.LLMMetaParser') as llm:
            llm.return_value.get_alias_candidates.return_value = []
            self.assertTrue(self.resolve(SAMPLE).skip_reason)
        movie = {"id": 65676, "media_type": MediaType.MOVIE, "title": self.info['original_name'],
                 "genre_ids": [16]}
        self.query.return_value = [self.info, movie]
        self.detail.side_effect = lambda mtype, **kw: movie if mtype == MediaType.MOVIE else self.info
        self.assertTrue(self.resolve(SAMPLE).skip_reason)

    def test_bangumi_aliases_are_revalidated_in_tmdb(self):
        self.query.side_effect = lambda title: [self.info] if title == self.info['name'] else []
        with patch('app.media.meta.special_resolver.LLMMetaParser') as llm:
            llm.return_value.get_alias_candidates.return_value = [self.info['name']]
            meta = self.resolve('Unknown Show [OVA01].mkv')
        self.assertEqual(65676, meta.tmdb_id)
        self.assertEqual(2, meta.begin_episode)

    def test_llm_cannot_set_target_numbering(self):
        with patch('app.media.meta.metainfo.LLMMetaParser') as llm:
            llm.return_value.parse.return_value = {'cn_name': self.info['name'], 'begin_season': 0,
                                                  'begin_episode': 99, 'tmdb_id': 999}
            meta = MetaInfo(SAMPLE)
        self.assertIsNone(meta.begin_episode)
        self.assertEqual(0, meta.tmdb_id)
        self.assertEqual([self.info['name']], meta.note['special_episode']['candidate_names'])

    def test_standalone_video_series_and_movie(self):
        video = dict(self.info, type='Video')
        meta = self.resolve('Show [OVA01].mkv', bound=video)
        # A standalone series ordinal cannot override a competing special reference.
        self.assertTrue(meta.skip_reason)
        self.episodes[0][0]['overview'] = ''
        meta = self.resolve('Show [OVA01].mkv', bound=video)
        self.assertEqual((1, 1), (meta.begin_season, meta.begin_episode))
        movie = {'id': 80, 'media_type': MediaType.MOVIE, 'title': 'Standalone Story',
                 'release_date': '2020-01-01', 'genre_ids': [16]}
        self.query.return_value = [movie]
        self.detail.return_value = movie
        meta = self.resolve('Standalone Story 2020 [OVA].mkv')
        self.assertIsNone(meta.skip_reason)
        self.assertEqual((MediaType.MOVIE, 80, None), (meta.type, meta.tmdb_id, meta.begin_episode))
        self.assertTrue(self.resolve('Show [OVA].mkv', bound=movie).skip_reason)

    def test_config_mapping_validates_existence_and_rejects_duplicates(self):
        rule = {'source_type': 'tv', 'source_tmdb_id': 65676, 'kind': 'OVA',
                'source_filename': 'Show [OVA].mkv',
                'target': {'media_type': 'tv', 'tmdb_id': 65676, 'season': 0, 'episode': 2}}
        self.config.return_value.get_config.return_value = {'special_episode_mappings': [rule]}
        self.assertIsNone(self.resolve('Show [OVA].mkv', bound=self.info).skip_reason)
        self.assertTrue(self.resolve('Other [OVA].mkv', bound=self.info).skip_reason)
        self.config.return_value.get_config.return_value['special_episode_mappings'].append(rule)
        self.assertTrue(self.resolve('Show [OVA].mkv', bound=self.info).skip_reason)
        self.config.return_value.get_config.return_value['special_episode_mappings'] = [dict(rule, target=dict(rule['target'], episode=99))]
        self.assertTrue(self.resolve('Show [OVA].mkv', bound=self.info).skip_reason)
        self.config.return_value.get_config.return_value['special_episode_mappings'] = [dict(rule, target={})]
        self.assertIn('缺少有效目标', self.resolve('Show [OVA].mkv', bound=self.info).skip_reason)

    def test_manual_movie_mapping_and_cross_type_task_conflict(self):
        movie = {'id': 80, 'media_type': MediaType.MOVIE, 'title': 'Standalone Story',
                 'release_date': '2020-01-01', 'genre_ids': [16]}
        self.detail.return_value = movie
        target = {'media_type': 'movie', 'tmdb_id': 80}
        meta = self.resolve('Show [OVA].mkv', bound=self.info, manual=target)
        self.assertIsNone(meta.skip_reason)
        self.assertEqual(MediaType.MOVIE, meta.type)
        self.assertEqual([], meta.get_episode_list())
        self.assertTrue(self.resolve('Show [OVA].mkv', bound=self.info, manual=target,
                                    context={'tmdb_info': self.info}).skip_reason)

    def test_release_year_does_not_have_to_equal_series_premiere(self):
        self.episodes[0][0]['air_date'] = '2016-01-01'
        meta = self.resolve(self.info['original_name'] + ' 2016 [OVA01].mkv')
        self.assertIsNone(meta.skip_reason)
        self.assertEqual(2, meta.begin_episode)

    def test_batch_cache_failure_retries_next_batch(self):
        cache = {}
        self.season.side_effect = RuntimeError('offline')
        for _ in range(2):
            meta = MetaInfo('Show [OVA01].mkv', use_llm=False)
            self.assertTrue(SpecialResolver(self.media, cache).resolve(meta, self.info).skip_reason)
        self.assertEqual(1, self.season.call_count)
        self.season.side_effect = lambda tmdbid, season: {'episodes': self.episodes[season]}
        self.assertIsNone(self.resolve('Show [OVA01].mkv', bound=self.info).skip_reason)

    def test_entrypoints_context_and_manual_selection(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'Show S01 [OVA01].mkv')
            Path(path).write_bytes(b'media')
            context = {'tmdb_info': self.info, 'seasons': [1], 'numbering': 'tmdb',
                       'scope': 'season_pack', 'release_seasons': [1]}
            result = self.media.get_media_info_on_files([path], download_context=context)[path]
            self.assertIsNone(result.skip_reason)
            for changes in ({'scope': None}, {'episodes': [2]}, {'release_seasons': [2]}):
                self.assertTrue(self.media.get_media_info_on_files(
                    [path], download_context=dict(context, **changes))[path].skip_reason)
            movie = dict(self.info, media_type=MediaType.MOVIE)
            self.assertTrue(self.media.get_media_info_on_files(
                [path], tmdb_info=movie, download_context=context)[path].skip_reason)
        with patch('app.media.meta.metainfo.LLMMetaParser') as llm:
            llm.return_value.parse.return_value = {}
            self.assertEqual(65676, self.media.get_media_info(SAMPLE).tmdb_id)

    def test_task_target_constrains_each_file_without_overwriting_it(self):
        # Both specials really exist; task evidence must not turn OVA02 into OVA01.
        self.info['seasons'][0]['episode_count'] = 2
        self.episodes[0].append(dict(self.episodes[0][0], id=801, episode_number=3,
                                     name='Another Story', overview='Season 1 OVA02'))
        saved = {'media_type': 'TV', 'tmdb_id': 65676, 'season': 0, 'episode': 2}
        with tempfile.TemporaryDirectory() as root:
            for selected in ([2], []):
                context = {'tmdb_info': self.info, 'numbering': 'tmdb', 'seasons': [0],
                           'episodes': selected, 'special_target': saved}
                for name in ('Show S01 [OVA02].mkv', 'Show S00E03.mkv', 'Show [OVA].mkv'):
                    with self.subTest(name=name, selected=selected):
                        path = Path(root) / name
                        path.touch()
                        meta = self.media.get_media_info_on_files(
                            [str(path)], download_context=context)[str(path)]
                        self.assertTrue(meta.skip_reason)
                        self.assertNotEqual(2, meta.begin_episode)
                for name in ('Show S01 [OVA01].mkv', 'Show S00E02.mkv'):
                    path = Path(root) / name
                    path.touch()
                    meta = self.media.get_media_info_on_files(
                        [str(path)], download_context=context)[str(path)]
                    self.assertIsNone(meta.skip_reason)
                    self.assertEqual((0, 2), (meta.begin_season, meta.begin_episode))

    def test_standalone_movie_task_still_requires_matching_file_identity(self):
        movie = {'id': 80, 'media_type': MediaType.MOVIE, 'title': 'Standalone Story',
                 'release_date': '2020-01-01', 'genre_ids': [16]}
        self.query.return_value = [movie]
        self.detail.return_value = movie
        context = {'tmdb_info': movie, 'special_target': {
            'media_type': 'MOVIE', 'tmdb_id': 80, 'season': None, 'episode': None}}
        with tempfile.TemporaryDirectory() as root:
            for name, allowed in (('Standalone Story 2020 [OVA].mkv', True),
                                  ('Other Story 2020 [OVA].mkv', False), ('Show [OVA].mkv', False)):
                path = Path(root) / name
                path.touch()
                meta = self.media.get_media_info_on_files(
                    [str(path)], download_context=context)[str(path)]
                self.assertEqual(allowed, not bool(meta.skip_reason))

    def test_decimal_and_range_references_are_not_integer_evidence(self):
        for label in ('OVA 1.5', 'OVA 1.5A', 'OVA 1-2', 'OVA 1-OVA 2',
                      'OVA 1 ～ 2', 'OVA 1 to 2', 'OVA 1 & 2', 'OVA 12.5',
                      'OVA 1-OVA 2-OVA 3', 'OVA 1.5-OVA 2', 'OVA 1, OVA 2, OVA 3'):
            with self.subTest(label=label):
                self.assertEqual(set(), special_references(label))
                self.episodes[0][0]['overview'] = label
                self.assertTrue(self.resolve('Show [OVA01].mkv', bound=self.info).skip_reason)
        self.assertEqual({('OVA', '1', 1)}, special_references('Season 1 OVA01. Bonus story.'))
        self.assertEqual({('OVA', '3', None)}, special_references('OVA 1-OVA 2; OVA 3'))

    def test_unconfirmed_content_cannot_download_or_clear_missing_episodes(self):
        downloader = object.__new__(type(Downloader()))
        downloader._download_order = None
        downloader.media = MagicMock()
        downloader.mediaserver = MagicMock()
        downloader.filetransfer = MagicMock()
        downloader.message = MagicMock()
        meta = self.resolve('Show S01 [OVA].mkv', bound=self.info)
        self.assertTrue(meta.skip_reason)
        missing = {65676: [{'season': 1, 'episodes': [], 'total_episodes': 12}]}
        # Unknown is distinct from missing; even direct downloads must stop before IO.
        exists, remaining, _ = downloader.check_exists_medias(meta, no_exists=missing)
        self.assertIsNone(exists)
        self.assertIs(missing, remaining)
        self.assertIsNone(downloader.download(meta)[0])
        downloader.media.get_tmdb_info.assert_not_called()
        downloader.mediaserver.get_no_exists_episodes.assert_not_called()
        with patch.object(downloader, 'download') as start, \
                patch.object(downloader, 'get_torrent_episodes') as inspect_torrent:
            downloaded, remaining = downloader.batch_download(SearchType.RSS, [meta], missing)
        self.assertEqual([], downloaded)
        self.assertEqual({65676: [{'season': 1, 'episodes': [], 'total_episodes': 12}]}, remaining)
        start.assert_not_called()
        inspect_torrent.assert_not_called()

        # Cached/parser-only objects have no skip_reason yet; their status still gates downloads.
        meta.skip_reason = None
        confirmed = self.resolve('Show S01 [OVA01].mkv', bound=self.info)
        ordinary = MetaInfo('Show S01', use_llm=False)
        ordinary.set_tmdb_info(self.info)
        for note in ({'special_episode': {'status': 'unconfirmed'}},
                     {'fractional_episode': {'status': 'unconfirmed'}},
                     {'extra': {'status': 'confirmed'}}):
            meta.note = note
            self.assertEqual([ordinary], downloader.get_download_list([meta, ordinary]))
            self.assertIsNone(downloader.download(meta)[0])
        self.assertEqual([confirmed], downloader.get_download_list([confirmed]))

    def test_work_cache_does_not_bypass_special_confirmation(self):
        self.media.meta = MagicMock()
        cached = {'id': 65676, 'type': MediaType.TV, 'title': 'Show'}
        self.media.meta.get_meta_data_by_key.return_value = cached
        for name in ('Show S00E02.mkv', 'Show S01 [OVA01].mkv', 'Show S01E1.5.mkv'):
            self.assertEqual({}, self.media.get_cache_info(MetaInfo(name, use_llm=False)))
        self.media.meta.get_meta_data_by_key.assert_not_called()
        self.assertEqual(cached, self.media.get_cache_info(MetaInfo('Show S01E01.mkv', use_llm=False)))


class ExtraTransferTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = {'extras': {'enabled': True, 'server_profile': 'jellyfin'}}
        for name in ('app.media.meta.metainfo.Config', 'app.filetransfer.Config'):
            patcher = patch(name)
            patcher.start().return_value.get_config.return_value = self.cfg
            self.addCleanup(patcher.stop)
        self.info = {'id': 65676, 'media_type': MediaType.TV, 'name': 'Show', 'first_air_date': '2013-04-05'}
        self.t = FileTransfer.__new__(FileTransfer)
        for attr in ('media', 'dbhelper', 'progress', 'message', 'threadhelper', 'mediaserver', 'scraper'):
            setattr(self.t, attr, MagicMock())
        self.t._filesize_cover = self.t._scraper_flag = self.t._refresh_mediaserver = False
        self.t._tv_category_flag = self.t._anime_category_flag = self.t._movie_category_flag = False
        self.t._tv_dir_rmt_format = '{title} ({year})'
        self.t._movie_dir_rmt_format = '{title} ({year})'
        self.t._tv_season_rmt_format = 'Season {season}'
        self.t._tv_file_rmt_format = '{title} - {season_episode}'
        self.t.check_ignore = lambda file_list: (file_list, '')
        self.t.media.get_episode_title.return_value = None
        self.t.dbhelper.insert_extra_transfer_history.return_value = True
        patcher = patch.object(Media, 'get_tmdb_en_title', return_value='Show')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = self.root / 'Show [NCOP01].mkv'
        self.source.write_bytes(b'extra-video')
        self.target = self.root / 'library'
        self.target.mkdir()
        self.meta = MetaInfo(self.source.name, use_llm=False)
        self.meta.set_tmdb_info(self.info)
        self.meta.note['extra']['status'] = 'confirmed'
        self.t.media.get_media_info_on_files.return_value = {str(self.source): self.meta}

    def run_transfer(self, mode=RmtMode.LINK):
        return self.t.transfer_media(in_from=SyncType.MAN, in_path=str(self.source),
            files=[str(self.source)], target_dir=str(self.target), rmt_mode=mode)

    def test_server_profiles_and_no_regular_media_side_effects(self):
        for profile, folder in (('jellyfin', 'other'), ('plex', 'Other'), ('emby', 'extras')):
            self.cfg['extras']['server_profile'] = profile
            _, dest = self.t._extra_destination(str(self.source), self.meta, str(self.target))
            self.assertEqual(folder, Path(dest).parent.name)
        self.assertTrue(self.run_transfer()[0])
        self.t.dbhelper.insert_transfer_history.assert_not_called()
        self.t.threadhelper.start_thread.assert_not_called()
        self.t.scraper.gen_scraper_files.assert_not_called()
        self.t.dbhelper.insert_extra_transfer_history.assert_called_once()
        self.assertEqual([], self.meta.get_episode_list())

    def test_unknown_profile_and_multiple_libraries_preserve_source(self):
        self.cfg['extras']['server_profile'] = 'unknown'
        self.assertFalse(self.run_transfer()[0])
        self.assertTrue(self.source.exists())
        self.t.dbhelper.insert_transfer_blacklist.assert_not_called()
        self.cfg['extras']['server_profile'] = 'jellyfin'
        rows = []
        for lib in ('one', 'two'):
            parent = self.root / lib / 'Show (2013)' / 'Season 1'
            parent.mkdir(parents=True)
            (parent / 'Show S01E01.mkv').write_bytes(b'main')
            rows.append(SimpleNamespace(DEST=str(self.root / lib), DEST_PATH=str(parent), DEST_FILENAME='Show S01E01.mkv'))
        self.t.dbhelper.get_media_transfer_history.return_value = rows
        with self.assertRaisesRegex(ValueError, '不唯一'):
            self.t._extra_destination(str(self.source), self.meta, None)
        self.t.dbhelper.get_media_transfer_history.return_value = rows[:1]
        _, dest = self.t._extra_destination(str(self.source), self.meta, None)
        self.assertEqual(self.root / 'one' / 'Show (2013)' / 'other' / self.source.name, Path(dest))

    def test_target_conflict_and_symlink_escape(self):
        parent = self.target / 'Show (2013)' / 'other'
        parent.mkdir(parents=True)
        (parent / self.source.name).write_bytes(b'different')
        self.assertFalse(self.run_transfer()[0])
        self.assertEqual(b'different', (parent / self.source.name).read_bytes())
        self.t.dbhelper.insert_transfer_blacklist.assert_not_called()
        shutil.rmtree(parent)
        parent.symlink_to(self.root, target_is_directory=True)
        self.meta.skip_reason = None
        self.assertFalse(self.run_transfer()[0])
        self.assertTrue(self.source.exists())

    def test_copy_and_move_retry_after_record_failure(self):
        for mode in (RmtMode.COPY, RmtMode.MOVE):
            source = self.root / (mode.name + '.mkv')
            source.write_bytes(b'preserve until persisted')
            dest = self.target / source.name
            with self.assertRaises(OSError):
                publish_extra(str(source), str(dest), mode, self.t._FileTransfer__transfer_command, lambda: False)
            self.assertTrue(source.exists())
            self.assertTrue(dest.exists())
            publish_extra(str(source), str(dest), mode, self.t._FileTransfer__transfer_command, lambda: True)
            self.assertEqual(mode != RmtMode.MOVE, source.exists())
            self.assertEqual([], list(self.target.glob('*.extra-pending')))

    def test_concurrent_publish_cannot_rewrite_another_tasks_staging_inode(self):
        for mode in (RmtMode.COPY, RmtMode.MOVE):
            source = self.root / (mode.name + '-concurrent.mkv')
            source.write_bytes(b'complete original content')
            dest = self.target / source.name
            started, release, attempted, published, second_done = [threading.Event() for _ in range(5)]
            outcomes, copies = {}, []

            def transfer(src, pending, staging_mode):
                if threading.current_thread().name == 'second-extra':
                    attempted.set()
                return self.t._FileTransfer__transfer_command(src, pending, staging_mode)

            def copy(src, pending):
                copies.append(threading.current_thread().name)
                if threading.current_thread().name == 'first-extra':
                    started.set()
                    if not release.wait(3):
                        raise TimeoutError('first copy was not released')
                    shutil.copy2(src, pending)
                    return 0, ''
                if not published.wait(3):
                    raise TimeoutError('first copy was not published')
                # Before the fix this truncates the inode already linked to dest.
                Path(pending).write_bytes(b'')
                return -1, 'simulated interrupted second copy'

            def record():
                published.set()
                second_done.wait(0.2)
                return True

            def run():
                name = threading.current_thread().name
                try:
                    publish_extra(str(source), str(dest), mode, transfer, record)
                    outcomes[name] = True
                except Exception as err:
                    outcomes[name] = err
                finally:
                    if name == 'second-extra':
                        second_done.set()

            with patch('app.filetransfer.SystemUtils.copy', side_effect=copy):
                first = threading.Thread(target=run, name='first-extra')
                second = threading.Thread(target=run, name='second-extra')
                first.start()
                self.assertTrue(started.wait(3))
                second.start()
                # A protected second publisher cannot enter staging while the first owns it.
                attempted.wait(0.2)
                release.set()
                first.join(4)
                second.join(4)
            self.assertFalse(first.is_alive() or second.is_alive())
            self.assertIs(True, outcomes['first-extra'])
            self.assertEqual(['first-extra'], copies)
            self.assertEqual(b'complete original content', dest.read_bytes())
            self.assertEqual(mode != RmtMode.MOVE, source.exists())

    def test_other_process_cannot_publish_while_recording(self):
        dest = self.target / 'process-lock.mkv'
        script = '''
import sys
from app.media.meta.extra_transfer import publish_extra
from app.filetransfer import FileTransfer
from app.utils.types import RmtMode
try:
    publish_extra(sys.argv[1], sys.argv[2], RmtMode.MOVE,
                  FileTransfer._FileTransfer__transfer_command, lambda: True)
except OSError:
    print('publication locked')
else:
    sys.exit(2)
'''
        def record():
            child = subprocess.run([sys.executable, '-c', script, str(self.source), str(dest)],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(0, child.returncode, child.stderr)
            self.assertIn('publication locked', child.stdout)
            self.assertTrue(self.source.exists())
            return True
        publish_extra(str(self.source), str(dest), RmtMode.MOVE,
                      self.t._FileTransfer__transfer_command, record)
        self.assertEqual(b'extra-video', dest.read_bytes())
        self.assertFalse(self.source.exists())

    def test_copy_interrupted_by_process_exit_recovers_on_retry(self):
        # os._exit deliberately bypasses Python cleanup and releases the OS lock.
        script = '''
import os
import sys
from pathlib import Path
from app.media.meta.extra_transfer import publish_extra
from app.utils.types import RmtMode
def interrupted_copy(source, pending, mode):
    Path(pending).write_bytes(b'partial')
    os._exit(73)
publish_extra(sys.argv[1], sys.argv[2], RmtMode[sys.argv[3]], interrupted_copy, lambda: True)
'''
        for mode in (RmtMode.COPY, RmtMode.MOVE):
            source = self.root / (mode.name + '-crash.mkv')
            source.write_bytes(b'complete original content')
            dest = self.target / source.name
            child = subprocess.run([sys.executable, '-c', script, str(source), str(dest), mode.name],
                                   capture_output=True, text=True, timeout=10)
            self.assertEqual(73, child.returncode, child.stderr)
            self.assertFalse(dest.exists())
            self.assertEqual(b'complete original content', source.read_bytes())
            publish_extra(str(source), str(dest), mode, self.t._FileTransfer__transfer_command, lambda: True)
            self.assertEqual(b'complete original content', dest.read_bytes())
            self.assertEqual(mode != RmtMode.MOVE, source.exists())
            self.assertEqual([], list(self.target.glob('*.extra-pending')))

    def test_published_staging_receipt_is_never_rebuilt(self):
        dest = self.target / 'published.mkv'
        with self.assertRaises(OSError):
            publish_extra(str(self.source), str(dest), RmtMode.MOVE,
                          self.t._FileTransfer__transfer_command, lambda: False)
        dest.write_bytes(b'damaged published content')
        record = MagicMock(return_value=True)
        with self.assertRaises(ValueError):
            publish_extra(str(self.source), str(dest), RmtMode.MOVE,
                          self.t._FileTransfer__transfer_command, record)
        self.assertEqual(b'damaged published content', dest.read_bytes())
        self.assertEqual(b'extra-video', self.source.read_bytes())
        record.assert_not_called()

    def test_same_inode_retry_and_history_failure(self):
        self.t.dbhelper.insert_extra_transfer_history.return_value = False
        self.assertFalse(self.run_transfer()[0])
        self.assertTrue(self.source.exists())
        self.t.dbhelper.insert_transfer_blacklist.assert_not_called()
        self.t.dbhelper.insert_extra_transfer_history.return_value = True
        self.assertTrue(self.run_transfer()[0])
        self.t.dbhelper.insert_transfer_blacklist.assert_called_once()

    def test_disappearing_collision_group_does_not_abort_unrelated_file(self):
        second = self.root / 'second' / self.source.name
        second.parent.mkdir()
        second.write_bytes(b'different')
        meta = MetaInfo(second.name, use_llm=False)
        meta.set_tmdb_info(self.info)
        meta.note['extra']['status'] = 'confirmed'
        medias = {str(self.source): self.meta, str(second): meta}
        second.unlink()
        self.t._check_fractional_destinations(medias, str(self.target), RmtMode.LINK)
        self.assertTrue(all(m.skip_reason for m in medias.values()))

    def test_mixed_batch_preserves_unknown_special_and_publishes_extra(self):
        unknown = self.root / 'Show [OVA].mkv'
        unknown.write_bytes(b'unknown')
        pending = MetaInfo(unknown.name, use_llm=False)
        pending.skip_reason = '特殊集无唯一证据'
        self.t.media.get_media_info_on_files.return_value = {str(unknown): pending, str(self.source): self.meta}
        success, _ = self.t.transfer_media(in_from=SyncType.MAN, in_path=str(self.root),
            files=[str(unknown), str(self.source)], target_dir=str(self.target), rmt_mode=RmtMode.LINK)
        self.assertFalse(success)
        self.assertTrue(unknown.exists())
        self.assertTrue((self.target / 'Show (2013)' / 'other' / self.source.name).exists())
        self.t.dbhelper.insert_transfer_blacklist.assert_called_once_with(str(self.source))

    def test_small_extras_survive_directory_size_filter_only_when_enabled(self):
        self.t._min_filesize = 150 * 1024 * 1024
        with patch('app.filetransfer.PathUtils.get_bluray_dir', return_value=None), \
                patch('app.filetransfer.PathUtils.is_invalid_path', return_value=False):
            self.assertTrue(self.t.transfer_media(in_from=SyncType.MAN, in_path=str(self.root),
                target_dir=str(self.target), rmt_mode=RmtMode.LINK)[0])
        passed = self.t.media.get_media_info_on_files.call_args
        self.assertIn(str(self.source), passed.args[0])

    def test_softlink_and_blacklist_failure_are_recoverable(self):
        self.t.dbhelper.insert_transfer_blacklist.return_value = False
        self.assertFalse(self.run_transfer(RmtMode.SOFTLINK)[0])
        self.assertTrue(self.source.exists())
        self.t.dbhelper.insert_transfer_blacklist.return_value = True
        self.assertTrue(self.run_transfer(RmtMode.SOFTLINK)[0])
        self.assertTrue((self.target / 'Show (2013)' / 'other' / self.source.name).is_symlink())

    def test_disabled_extras_keep_existing_filter_behavior(self):
        self.cfg['extras']['enabled'] = False
        meta = MetaInfo('Show S01E01 [NCOP01].mkv', use_llm=False)
        self.assertNotIn('extra', meta.note)
        self.assertEqual(1, meta.begin_episode)


class ExtraMigrationTest(unittest.TestCase):
    def test_real_history_persistence_is_idempotent_and_separate(self):
        from sqlalchemy.orm import sessionmaker
        from app.db import MainDb
        from app.db.models import EXTRATRANSFERHISTORY, TRANSFERHISTORY
        from app.helper.db_helper import DbHelper
        engine = sa.create_engine('sqlite://')
        EXTRATRANSFERHISTORY.__table__.create(engine)
        TRANSFERHISTORY.__table__.create(engine)
        session = sessionmaker(bind=engine)()
        meta = SimpleNamespace(tmdb_id=65676, tmdb_info={'media_type': MediaType.TV})
        with patch.object(MainDb, 'session', new_callable=PropertyMock, return_value=session):
            helper = DbHelper()
            for _ in range(2):
                self.assertTrue(helper.insert_extra_transfer_history(
                    '/source/extra.mkv', '/library/Show/other/extra.mkv', meta, 'other', RmtMode.COPY))
            self.assertEqual(1, session.query(EXTRATRANSFERHISTORY).count())
            self.assertEqual(0, session.query(TRANSFERHISTORY).count())
            meta.tmdb_id = 999
            self.assertFalse(helper.insert_extra_transfer_history(
                '/source/extra.mkv', '/library/Show/other/extra.mkv', meta, 'other', RmtMode.COPY))
        session.close()
        engine.dispose()

    def test_migration_is_repeatable_and_history_is_independent(self):
        module_path = Path(__file__).resolve().parents[1] / 'db_scripts/versions/c28f63a419de_extra_transfer_history.py'
        spec = importlib.util.spec_from_file_location('extra_migration', module_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine('sqlite://')
        with engine.begin() as connection:
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()
            migration.upgrade()
            inspector = sa.inspect(connection)
            self.assertEqual(['EXTRA_TRANSFER_HISTORY'], inspector.get_table_names())
            self.assertEqual(['DEST_PATH'], inspector.get_pk_constraint('EXTRA_TRANSFER_HISTORY')['constrained_columns'])
            migration.downgrade()
            migration.downgrade()
        engine.dispose()
