"""Offline operation-count regressions; no timing thresholds or real NAS/network."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.filetransfer import FileTransfer
from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta import fractional, special
from app.media.meta.special_resolver import SpecialResolver
from app.media.tmdbv3api import TMDb, TV
from app.utils.types import MediaType, RmtMode


class EvidenceCacheTest(unittest.TestCase):
    def setUp(self):
        for module, name in ((special, 'special_references'), (fractional, 'release_references')):
            getattr(module, '_cached_' + name).cache_clear()

    def test_bounds_mutation_and_changed_text(self):
        for module, name, sample in ((special, 'special_references', 'OVA 1'),
                                     (fractional, 'release_references', 'Episode 1.5')):
            with self.subTest(name=name):
                public = getattr(module, name)
                cached = getattr(module, '_cached_' + name)
                parse = getattr(module, '_parse_' + name)
                with patch.object(module, '_parse_' + name, wraps=parse) as counted:
                    expected = public(sample)
                    public(sample).clear()
                    self.assertEqual(expected, public(sample))
                    self.assertEqual(1, counted.call_count)
                    self.assertNotEqual(expected, public(sample.replace('1', '2')))
                    # Long text is never truncated, even when evidence is at its end.
                    long_text = 'x' * 2049 + ' ' + sample
                    self.assertEqual(expected, public(long_text))
                    self.assertEqual(expected, public(long_text))
                    self.assertEqual(4, counted.call_count)
                    for n in range(2048):
                        public('unique %d; %s' % (n, sample))
                    before = counted.call_count
                    public(sample)
                    self.assertEqual(before + 1, counted.call_count)
                    self.assertEqual(2048, cached.cache_info().currsize)

    def test_thirty_resources_share_thousand_episode_parses(self):
        for decimal in (False, True):
            with self.subTest(decimal=decimal):
                module, name = (fractional, 'release_references') if decimal else (special, 'special_references')
                info = {'id': 123, 'media_type': MediaType.TV, 'name': 'Show',
                        'seasons': [{'season_number': 1, 'episode_count': 1000}]}
                episodes = [{'id': n, 'show_id': 123, 'season_number': 1, 'episode_number': n,
                             'name': 'Story %d' % n,
                             'overview': ('Episode %d.5' if decimal else 'OVA %d') % n}
                            for n in range(1, 1001)]
                media = Media.__new__(Media)
                media.tmdb = SimpleNamespace(language='zh-CN')
                media.get_tmdb_tv_season_detail = MagicMock(return_value={'episodes': episodes})
                cache = {}
                parse = getattr(module, '_parse_' + name)
                with patch.object(module, '_parse_' + name, wraps=parse) as counted:
                    for n in range(1, 31):
                        title = ('Show E%d.5.mkv' if decimal else 'Show [OVA%d].mkv') % n
                        meta = MetaInfo(title, use_llm=False)
                        if decimal:
                            self.assertTrue(media._confirm_fractional_episode(meta, info, cache))
                        else:
                            SpecialResolver(media, cache).resolve(meta, bound=info)
                            self.assertIsNone(meta.skip_reason)
                        self.assertEqual((1, n), (meta.begin_season, meta.begin_episode))
                    self.assertEqual(1000, counted.call_count)
                    self.assertEqual(1, media.get_tmdb_tv_season_detail.call_count)

    def test_validated_episode_batch_cache_and_recovery(self):
        info = {'id': 1, 'seasons': [{'season_number': 1, 'episode_count': 1}]}
        ep = {'episode_number': 1, 'show_id': 1, 'season_number': 1}
        media = SimpleNamespace(tmdb=SimpleNamespace(language='zh-CN'),
                                get_tmdb_tv_season_detail=MagicMock(return_value={'episodes': []}))
        cache = {}
        with self.assertRaises(ValueError):
            SpecialResolver(media, cache).episodes(info)
        self.assertFalse(any('validated_episodes' in key for key in cache))
        media.get_tmdb_tv_season_detail.return_value = {'episodes': [ep]}
        resolver = SpecialResolver(media, {})
        with patch.object(resolver, 'validate_episode', wraps=resolver.validate_episode) as validate:
            self.assertEqual(((1, ep),), resolver.episodes(info))
            self.assertEqual(((1, ep),), resolver.episodes(info))
            self.assertEqual(1, validate.call_count)
        # A changed manifest cannot reuse a previously successful completeness check.
        info['seasons'][0]['episode_count'] = 2
        with self.assertRaises(ValueError):
            resolver.episodes(info)


class ExtraDirectoryCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'library'
        self.folder = self.root / 'Show'
        self.folder.mkdir(parents=True)
        self.witness = self.folder / 'episode.mkv'
        self.witness.touch()
        self.ft = FileTransfer.__new__(FileTransfer)
        self.ft._movie_dir_rmt_format = self.ft._tv_dir_rmt_format = '{title}'
        self.ft._movie_category_flag = self.ft._tv_category_flag = self.ft._anime_category_flag = False
        self.ft.get_format_dict = lambda _: {'title': 'Show'}
        self.rows = [self.row(self.witness)]
        self.ft.dbhelper = SimpleNamespace(get_media_transfer_history=MagicMock(side_effect=lambda *_: self.rows))
        config = patch('app.filetransfer.Config')
        self.addCleanup(config.stop)
        config.start().return_value.get_config.return_value = {
            'extras': {'enabled': True, 'server_profile': 'jellyfin'}}

    def row(self, path, root=None):
        return SimpleNamespace(DEST=str(root or self.root), DEST_PATH=str(path.parent), DEST_FILENAME=path.name)

    def meta(self):
        return SimpleNamespace(type=MediaType.TV, tmdb_id=123, category='',
                               note={'extra': {'category': 'other'}})

    def prepare(self, source, meta, cache=None):
        root, destination = self.ft._extra_destination(source, meta, None, cache)
        meta.note['extra'].update(library_root=root, destination=destination)
        return destination

    def test_one_history_query_and_one_witness_per_batch(self):
        self.rows *= 1000
        cache = {}
        with patch('app.filetransfer.os.path.isfile', wraps=os.path.isfile) as probe:
            metas = [self.meta() for _ in range(30)]
            for n, meta in enumerate(metas):
                self.prepare('/source/NCOP%d.mkv' % n, meta, cache)
            self.assertEqual(1, probe.call_count)
            self.assertEqual(1, self.ft.dbhelper.get_media_transfer_history.call_count)
            for n, meta in enumerate(metas):
                self.ft._recheck_extra_destination('/source/NCOP%d.mkv' % n, meta, None)
            self.assertEqual(31, probe.call_count)
            self.assertEqual(1, self.ft.dbhelper.get_media_transfer_history.call_count)
        self.prepare('/source/NCOP.mkv', self.meta(), {})
        self.assertEqual(2, self.ft.dbhelper.get_media_transfer_history.call_count)

    def test_preflight_shares_cache_for_all_extras(self):
        self.rows *= 1000
        metas = {}
        for n in range(30):
            meta = self.meta()
            meta.skip_reason = None
            meta.tmdb_info = {"id": 123}
            meta.note["extra"]["status"] = "confirmed"
            metas['/source/NCOP%d.mkv' % n] = meta
        with patch('app.filetransfer.os.path.isfile', wraps=os.path.isfile) as probe:
            self.ft._check_fractional_destinations(metas, None, RmtMode.LINK)
            self.assertEqual(1, probe.call_count)
            self.assertEqual(1, self.ft.dbhelper.get_media_transfer_history.call_count)
        self.assertTrue(all(not meta.skip_reason and meta.note['extra'].get('destination')
                            for meta in metas.values()))

    def test_missing_witness_refreshes_and_missing_all_fails(self):
        meta = self.meta()
        self.prepare('/source/NCOP.mkv', meta)
        replacement = self.folder / 'replacement.mkv'
        replacement.touch()
        self.rows.append(self.row(replacement))
        self.witness.unlink()
        self.ft._recheck_extra_destination('/source/NCOP.mkv', meta, None)
        self.assertEqual(str(replacement), meta.note['extra']['directory_evidence']['witness'])
        replacement.unlink()
        with self.assertRaises(ValueError):
            self.ft._recheck_extra_destination('/source/NCOP.mkv', meta, None)

    def test_other_valid_directory_remains_ambiguous(self):
        other = Path(self.tmp.name) / 'other'
        (other / 'Show').mkdir(parents=True)
        ep = other / 'Show' / 'episode.mkv'
        ep.touch()
        self.rows.insert(0, self.row(self.folder / 'missing.mkv'))
        self.rows.append(self.row(ep, other))
        with self.assertRaises(ValueError):
            self.prepare('/source/NCOP.mkv', self.meta())
        ep.unlink()
        self.prepare('/source/NCOP.mkv', self.meta())

    def test_publication_rejects_changed_root_and_symlink_escape(self):
        meta = self.meta()
        self.prepare('/source/NCOP.mkv', meta)
        outside = Path(self.tmp.name) / 'outside'
        outside.mkdir()
        (self.folder / 'other').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.ft._recheck_extra_destination('/source/NCOP.mkv', meta, None)
        (self.folder / 'other').unlink()
        self.witness.unlink()
        (outside / 'Show').mkdir()
        replacement = outside / 'Show' / 'episode.mkv'
        replacement.touch()
        self.rows[:] = [self.row(replacement, outside)]
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.ft._recheck_extra_destination('/source/NCOP.mkv', meta, None)


class HttpCacheCountTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {'TMDB_API_KEY': 'offline', 'TMDB_LANGUAGE': 'zh-CN',
                                      'TMDB_CACHE_ENABLED': 'True', 'TMDB_DEBUG_ENABLED': 'False',
                                      'TMDB_PROXIES': '{}', 'TMDB_DOMAIN': 'https://tmdb.invalid/3'})
        env.start()
        self.addCleanup(env.stop)
        self.sdk = TMDb()
        self.sdk.cache_clear()
        self.addCleanup(self.sdk.cache_clear)
        response = MagicMock()
        response.headers = {}
        response.json.return_value = {'id': 1, 'name': 'Show', 'genres': [], 'episodes': [],
                                      'alternative_titles': {'results': [{'iso_3166_1': 'CN', 'title': '作品'}]}}
        request = patch('app.media.tmdbv3api.tmdb.requests.request', return_value=response)
        self.http = request.start()
        self.addCleanup(request.stop)
        self.media = Media.__new__(Media)
        self.media.tmdb, self.media.tv = self.sdk, TV()

    def test_details_seasons_language_and_parameters_count_real_boundary(self):
        for _ in range(2):
            detail = self.media.get_tmdb_info(MediaType.TV, 1)
            self.assertEqual('作品', detail['name'])
            self.media.get_tmdb_tv_season_detail(1, 1)
        # Chinese title conversion reads the appended data; no extra HTTP request.
        self.assertEqual(2, self.http.call_count)
        self.media.get_tmdb_info(MediaType.TV, 1, language='en-US')
        self.assertEqual(3, self.http.call_count)
        self.media.get_tmdb_info(MediaType.TV, 1, append_to_response='alternative_titles')
        self.assertEqual(4, self.http.call_count)

    def test_lru_eviction_after_256_distinct_requests(self):
        for tmdbid in range(257):
            self.media.tv.season_details(tmdbid, 1)
        self.assertEqual(257, self.http.call_count)
        self.media.tv.season_details(256, 1)
        self.assertEqual(257, self.http.call_count)
        self.media.tv.season_details(0, 1)
        self.assertEqual(258, self.http.call_count)
