"""Ordinal words in titles must not become unverified seasons or season packs."""
import copy
import datetime
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.special import download_block_reason
from app.utils import EpisodeFormat
from app.utils.types import MediaType, MatchMode


class OrdinalSeasonTest(TestCase):
    def setUp(self):
        self.media = Media.__new__(Media)
        self.media.tmdb = Mock(language="zh-CN")
        self.media._rmt_match_mode = MatchMode.NORMAL
        self.media.search = Mock()
        self.media.meta = Mock()
        self.cache = {}
        self.media.meta.get_meta_data_by_key.side_effect = lambda key: self.cache.get(key, {})
        self.media.meta.update_meta_data.side_effect = self.cache.update
        self.media.get_tmdb_tv_season_detail = Mock(return_value={
            "episodes": [{"episode_number": n} for n in range(1, 25)]})
        config = patch('app.media.media.Config').start()
        self.config = config.return_value
        self.config.get_config.return_value = {"episode_mappings": []}
        patch("app.media.meta.ordinal.Config", return_value=self.config).start()
        self.aliases = patch("app.media.meta.ordinal.LLMMetaParser").start()
        self.aliases.return_value.get_alias_candidates.return_value = []
        self.addCleanup(patch.stopall)

    @staticmethod
    def info(name, season, ident):
        return {"name": name, "id": ident, "media_type": MediaType.TV,
                "genres": [{"id": 16}], "first_air_date": "2020-01-01",
                "number_of_seasons": 1,
                "seasons": [{"season_number": season, "episode_count": 24}]}

    def candidates(self, *works):
        def found(name):
            return [copy.deepcopy(w) for w in works if w['name'].casefold() == name.casefold()]
        self.media.get_tmdb_search_page = Mock(side_effect=lambda **kw: found(kw['title']))
        self.media.search.tv_shows.side_effect = lambda args: [
            {k: v for k, v in w.items() if k not in ('genres', 'seasons')} for w in found(args['query'])]
        self.media.get_tmdb_info = Mock(side_effect=lambda **kw:
            copy.deepcopy(next((w for w in works if w['id'] == kw['tmdbid']), {})))

    def test_explicit_written_season_preserves_ordinal_title(self):
        # Both ordinal suffixes and explicit anime routing retain the full work name.
        for name in ('Major 2nd', 'Show 4th'):
            for suffix in ('Episode 5', 'E05'):
                for hint in (None, MediaType.ANIME):
                    with self.subTest(name=name, suffix=suffix, hint=hint):
                        meta = MetaInfo(f'{name} Season 2 {suffix} 1080p', mtype=hint, use_llm=False)
                        self.assertEqual(name.casefold(), meta.get_name().casefold())
                        self.assertEqual((2, [5]), (meta.begin_season, meta.get_episode_list()))
                        self.assertFalse(meta.skip_reason)

    def test_delimited_ordinal_accepts_all_single_digits(self):
        for number in range(1, 10):
            for digits in (str(number), f'{number:02}'):
                meta = MetaInfo(f'Show [4th Season][{digits}][1080p].mkv', use_llm=False)
                self.assertEqual((4, [number]), (meta.begin_season, meta.get_episode_list()))
                self.assertFalse(meta.skip_reason)
        pack = MetaInfo('Show [4th Season] 1080p', use_llm=False)
        self.assertEqual([], pack.get_episode_list())
        self.assertFalse(pack.skip_reason)
        for hint in (None, MediaType.ANIME):
            pack = MetaInfo('Food Wars 4th Season 1080p', mtype=hint, use_llm=False)
            self.assertEqual(('Food Wars', 4, []), (pack.get_name(), pack.begin_season, pack.get_episode_list()))
            self.assertFalse(pack.skip_reason)

    def test_bare_number_is_preserved_but_not_downloadable_before_confirmation(self):
        for digits in ('1', '5', '9'):
            meta = MetaInfo(f'Show 4th Season {digits} 1080p', use_llm=False)
            self.assertEqual([int(digits)], meta.get_episode_list())
            self.assertEqual(2, len(meta.note['ordinal_candidates']))
            self.assertTrue(download_block_reason(meta))

    def test_public_lookup_selects_full_title_instead_of_original_work(self):
        self.candidates(self.info('Major', 2, 1), self.info('Major 2nd', 2, 2))
        # Both the original work's S2E2 and the sequel's season pack are plausible;
        # candidate order must never choose between two valid interpretations.
        ambiguous = self.media.get_media_info('Major 2nd Season 2', cache=False)
        self.assertTrue(ambiguous.skip_reason)
        self.assertFalse(ambiguous.tmdb_id)
        # Exclusion requires actual contrary facts, not an empty search response.
        self.candidates(self.info('Major', 9, 1), self.info('Major 2nd', 2, 2))
        resolved = self.media.get_media_info('Major 2nd Season 2', cache=False)
        self.assertEqual(('major 2nd', 2, []),
                         (resolved.get_name().casefold(), resolved.begin_season, resolved.get_episode_list()))
        self.assertEqual(2, resolved.tmdb_id)
        self.assertFalse(resolved.skip_reason)

    def test_public_lookup_confirms_single_episode_with_tmdb_evidence(self):
        self.candidates(self.info('Show', 4, 1), self.info('Show 4th', 9, 2))
        resolved = self.media.get_media_info('Show 4th Season 5 1080p', cache=False)
        self.assertEqual((1, 4, [5]), (resolved.tmdb_id, resolved.begin_season, resolved.get_episode_list()))
        self.assertFalse(download_block_reason(resolved))
        self.media.get_tmdb_tv_season_detail.return_value = {'episodes': [{'episode_number': 1}]}
        rejected = self.media.get_media_info('Show 4th Season 5 1080p', cache=False)
        self.assertTrue(download_block_reason(rejected))
        self.assertFalse(rejected.tmdb_id)

    def test_unbound_file_lookup_and_incomplete_episode_lists_fail_safely(self):
        self.candidates(self.info('Show', 4, 1), self.info('Show 4th', 9, 2))
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'Show 4th Season 5.mkv')
            Path(path).touch()
            meta = self.media.get_media_info_on_files([path])[path]
            self.assertEqual((1, [5]), (meta.tmdb_id, meta.get_episode_list()))
            self.assertFalse(meta.skip_reason)
        self.candidates(self.info('Show', 4, 1), self.info('Show 4th', 5, 2))
        self.media.get_tmdb_tv_season_detail.return_value = {'episodes': []}
        self.assertTrue(self.media.get_media_info('Show 4th Season 5', cache=False).skip_reason)

    def test_detail_failure_does_not_prove_uniqueness(self):
        self.candidates(self.info('Show', 4, 1), self.info('Show 4th', 5, 2))
        self.media.get_tmdb_info.side_effect = [self.info('Show', 4, 1), {}]
        meta = self.media.get_media_info('Show 4th Season 5', cache=False)
        self.assertTrue(meta.skip_reason)
        self.assertFalse(meta.tmdb_id)

    def test_search_failure_and_same_name_duplicates_remain_unconfirmed(self):
        first, second = self.info('Show', 4, 1), self.info('Show', 4, 2)
        self.candidates(first, second)
        self.media.get_tmdb_search_page.side_effect = [[first, second], []]
        self.assertTrue(self.media.get_media_info('Show 4th Season 5', cache=False).skip_reason)
        self.media.get_tmdb_search_page.side_effect = [[first], RuntimeError('offline')]
        self.assertTrue(self.media.get_media_info('Show 4th Season 5', cache=False).skip_reason)

    def test_bound_files_use_identity_to_choose_and_still_check_task_episode(self):
        work = self.info('Major 2nd', 2, 2)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'Major 2nd Season 2.mkv')
            Path(path).touch()
            result = self.media.get_media_info_on_files([path], tmdb_info=work)
            self.assertEqual('major 2nd', result[path].get_name().casefold())
            self.assertFalse(result[path].skip_reason)
            other = self.media.get_media_info_on_files([path], tmdb_info=self.info('Unrelated', 2, 3))
            self.assertTrue(other[path].skip_reason)
            episode_path = str(Path(directory) / 'Show 4th Season 5.mkv')
            Path(episode_path).touch()
            context = {'tmdb_info': self.info('Show', 4, 4), 'seasons': [4], 'episodes': [6]}
            self.assertEqual({}, self.media.get_media_info_on_files([episode_path], download_context=context))

    def test_ambiguous_parse_cannot_be_overwritten_by_llm_or_work_cache(self):
        with patch('app.media.meta.metainfo.LLMMetaParser') as llm:
            meta = MetaInfo('Show 4th Season 5')
            llm.assert_not_called()
        self.media.meta.get_meta_data_by_key.return_value = {'id': 1, 'type': MediaType.TV}
        self.assertEqual({}, self.media.get_cache_info(meta))

    def test_two_digits_are_a_compatibility_default_without_extra_queries(self):
        # Width is an explicit compatibility policy, never a uniqueness claim.
        self.candidates(self.info('Show', 4, 1))
        with patch('requests.request') as http, patch('requests.sessions.Session.request') as session_http:
            for digits in ('01', '05', '09', '12'):
                meta = self.media.get_media_info(f'Show 4th Season {digits} 1080p')
                self.assertEqual((1, 4, [int(digits)]), (meta.tmdb_id, meta.begin_season, meta.get_episode_list()))
                self.assertFalse(meta.skip_reason)
                self.assertNotIn('ordinal_candidates', meta.note)
            self.media.get_tmdb_search_page.assert_not_called()
            self.media.get_tmdb_tv_season_detail.assert_not_called()
            self.assertEqual(1, self.media.search.tv_shows.call_count)
            self.assertEqual(4, self.media.get_tmdb_info.call_count)
            # Provider-interface calls above are distinct from actual HTTP calls (0).
            http.assert_not_called()
            session_http.assert_not_called()
        third = MetaInfo('Show 3rd Season 12 1080p', use_llm=False)
        self.assertEqual((3, [12]), (third.begin_season, third.get_episode_list()))
        self.assertFalse(third.skip_reason)

    def test_cache_isolation_for_public_and_unbound_file_lookup(self):
        short, full = self.info('Major', 2, 1), self.info('Major 2nd', 2, 2)
        short['seasons'][0]['episode_count'] = 1
        for file_lookup in (False, True):
            with self.subTest(file_lookup=file_lookup), tempfile.TemporaryDirectory() as root:
                self.cache.clear()
                self.candidates(short, full)
                self.media.get_tmdb_tv_season_detail.side_effect = lambda ident, season: {
                    'episodes': [{'episode_number': n} for n in range(1, 2 if ident == 1 else 25)]}
                title = 'Major 2nd Season 2'
                if file_lookup:
                    path = str(Path(root) / (title + '.mkv'))
                    Path(path).touch()
                    resolved = self.media.get_media_info_on_files([path])[path]
                else:
                    resolved = self.media.get_media_info(title)
                self.assertEqual(2, resolved.tmdb_id)
                self.assertFalse(self.cache)
                ordinary = self.media.get_media_info('Major S02E01 1080p')
                self.assertEqual(1, ordinary.tmdb_id)
                self.assertFalse(ordinary.skip_reason)

    def test_three_states_do_not_turn_missing_data_into_negative_evidence(self):
        for missing in ('short_seasons', 'full_seasons', 'manifest_count', 'count', 'truncated', 'empty_search', 'alias', 'exception'):
            with self.subTest(missing=missing):
                short, full = self.info('Show', 4, 1), self.info('Show 4th', 5, 2)
                if missing == 'short_seasons':
                    short.pop('seasons')
                elif missing == 'full_seasons':
                    full.pop('seasons')
                elif missing == 'count':
                    short['seasons'][0].pop('episode_count')
                elif missing == 'manifest_count':
                    full['seasons'][0]['season_number'] = 9
                    full.pop('number_of_seasons')
                self.candidates(short, full)
                self.media.get_tmdb_tv_season_detail = Mock(return_value={
                    'episodes': [{'episode_number': n} for n in range(1, 25)]})
                if missing == 'truncated':
                    self.media.get_tmdb_tv_season_detail.return_value = {'episodes': [{'episode_number': 1}]}
                elif missing == 'empty_search':
                    self.media.get_tmdb_search_page.side_effect = [[short], []]
                elif missing == 'alias':
                    full['name'] = 'Official Sequel'
                    self.candidates(short, full)
                    self.media.get_tmdb_search_page.side_effect = [[short], [full]]
                elif missing == 'exception':
                    self.media.get_tmdb_tv_season_detail.side_effect = RuntimeError('offline')
                meta = self.media.get_media_info('Show 4th Season 5', cache=False)
                self.assertTrue(meta.skip_reason)
                self.assertFalse(meta.tmdb_id)
                self.assertIn('未知', meta.note['ordinal_evidence'])

    def test_both_valid_and_both_invalid_stay_blocked(self):
        for seasons in ((4, 5), (9, 9)):
            self.candidates(self.info('Show', seasons[0], 1), self.info('Show 4th', seasons[1], 2))
            meta = self.media.get_media_info('Show 4th Season 5', cache=False)
            self.assertTrue(download_block_reason(meta))
            expected = '成立' if seasons == (4, 5) else '不成立'
            self.assertEqual([expected, expected], meta.note['ordinal_evidence'])

    def test_manual_targets_precede_name_ambiguity_but_require_target_evidence(self):
        from web.backend.special_confirmation import special_file
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'Show 4th Season 5.mkv')
            Path(path).touch()
            for name in ('Official translation', 'Show'):
                work = self.info(name, 4, 1)
                work['alternative_titles'] = {'results': [{'title': 'Show 4th'}]}
                work['seasons'].append({'season_number': 5, 'episode_count': 24})
                work['number_of_seasons'] = 2
                resolved = self.media.get_media_info_on_files([path], tmdb_info=work,
                    season=4, episode_format=EpisodeFormat('', '5'))[path]
                self.assertEqual((1, 4, [5]), (resolved.tmdb_id, resolved.begin_season, resolved.get_episode_list()))
                self.assertFalse(resolved.skip_reason)
                self.assertFalse(self.cache)
            # The ordinary manual UI is sufficient; the special-episode dialog need not claim this file.
            self.assertIsNone(special_file(path))
            self.media.get_tmdb_tv_season_detail.return_value = {}
            unknown = self.media.get_media_info_on_files([path], tmdb_info=work,
                season=4, episode_format=EpisodeFormat('', '5'))[path]
            self.assertTrue(unknown.skip_reason)
            self.media.get_tmdb_tv_season_detail.return_value = {
                'episodes': [{'episode_number': n} for n in range(1, 25)]}
            for invalid in ('0', '25'):
                result = self.media.get_media_info_on_files([path], tmdb_info=work,
                    season=4, episode_format=EpisodeFormat('', invalid))[path]
                self.assertTrue(result.skip_reason)

    def test_manual_targets_still_obey_download_identity_and_numbering(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'Show 4th Season 5.mkv')
            Path(path).touch()
            work = self.info('Show', 4, 1)
            context = dict(tmdb_info=work, seasons=[4], episodes=[6])
            self.assertEqual({}, self.media.get_media_info_on_files([path], tmdb_info=work,
                season=4, episode_format=EpisodeFormat('', '5'), download_context=context))
            other = self.info('Other', 4, 2)
            result = self.media.get_media_info_on_files([path], tmdb_info=other,
                season=4, episode_format=EpisodeFormat('', '6'), download_context=context)[path]
            self.assertTrue(result.skip_reason)

    def test_rss_prefilter_keeps_ambiguous_season_pack(self):
        from app.rss import Rss
        rss = Rss.__new__(Rss)
        rss.subscribe = Mock()
        rss.subscribe.get_subscribe_tv_episodes.return_value = [5]
        subscription = dict(id=1, name='Major 2nd', rss_sites=['Offline'], season='S02', over_edition=False)
        self.assertFalse(rss._Rss__should_skip_rss_article_before_identify(
            title='Major 2nd Season 2', site_name='Offline', rss_tvs={'1': subscription}))

    def test_imdb_constraint_resolves_before_download_gate(self):
        from app.filter import Filter
        from app.indexer.client._base import _IIndexClient
        from app.downloader.downloader import Downloader
        work = self.info('Show', 4, 1)
        wanted = MetaInfo('Show S04', use_llm=False)
        wanted.set_tmdb_info(work)
        wanted.imdb_id = 'tt123'
        wanted.get_poster_image = wanted.get_backdrop_image = Mock(return_value='')
        client = SimpleNamespace(media=self.media, filter=Mock(), progress=Mock(),
                                 _reverse_title_sites=[], index_type='Offline')
        client.filter.check_torrent_filter.return_value = (True, 0, '')
        client.filter.is_torrent_match_sey.side_effect = Filter().is_torrent_match_sey
        found = _IIndexClient.filter_search_results(client,
            [dict(title='Show 4th Season 5 1080p', enclosure='offline', imdbid='tt123')], 0,
            SimpleNamespace(id='offline', name='Offline', public=False),
            dict(type=MediaType.TV, season=4), wanted, datetime.datetime.now())
        self.assertEqual(1, len(found))
        self.assertFalse(download_block_reason(found[0]))
        self.assertEqual([5], found[0].get_episode_list())
        # Reach the real downloader past its evidence gate, stopping before any I/O.
        cls = next(c.cell_contents for c in Downloader.__closure__ if isinstance(c.cell_contents, type))
        downloader = object.__new__(cls)
        with patch('app.downloader.downloader.Torrent') as torrent:
            torrent.return_value.read_torrent_content.side_effect = RuntimeError('passed evidence gate')
            with self.assertRaisesRegex(RuntimeError, 'passed evidence gate'):
                downloader.download(found[0], torrent_file='/offline.torrent')

    def test_configured_and_external_names_are_verified_for_each_interpretation(self):
        for external in (False, True):
            with self.subTest(external=external):
                self.candidates(self.info('Official Show', 4, 1), self.info('Show 4th', 9, 2))
                self.config.get_config.return_value = {'episode_mappings': [], 'name_aliases':
                    {} if external else {'Show': 'Official Show'}}
                self.aliases.return_value.get_alias_candidates.side_effect = (
                    lambda **kw: ['Official Show'] if kw['title'] == 'Show' else [])
                meta = self.media.get_media_info('Show 4th Season 5', cache=False)
                self.assertEqual((1, [5]), (meta.tmdb_id, meta.get_episode_list()))
                self.assertFalse(meta.skip_reason)
                names = [c.kwargs['title'].casefold() for c in self.media.get_tmdb_search_page.call_args_list]
                self.assertIn('show 4th', names)

    def test_anime_route_keeps_completion_and_codec_behavior(self):
        from app.media.meta.metaanime import MetaAnime
        for marker in ('END', 'FINAL'):
            for hint in (None, MediaType.ANIME):
                meta = MetaInfo(f'[Group] Some Anime 4th Season [12 {marker}][1080p][HEVC].mkv',
                                mtype=hint, use_llm=False)
                self.assertIsInstance(meta, MetaAnime)
                self.assertEqual((4, [12]), (meta.begin_season, meta.get_episode_list()))
                self.assertEqual('Some Anime', meta.get_name())
                self.assertFalse(meta.skip_reason)
        pack = MetaInfo('[Group] Some Anime 4th Season [01-12][1080p][HEVC].mkv', use_llm=False)
        self.assertIsInstance(pack, MetaAnime)
        self.assertEqual((4, list(range(1, 13))), (pack.begin_season, pack.get_episode_list()))
        self.assertEqual('HEVC', pack.video_encode)
        movie = MetaInfo('Toy Story 4 1080p', use_llm=False)
        self.assertEqual(('Toy Story 4', MediaType.MOVIE, []),
                         (movie.get_name(), movie.type, movie.get_episode_list()))

    def test_batch_evidence_reuse_and_next_batch_retry(self):
        self.candidates(self.info('Show', 4, 1), self.info('Show 4th', 9, 2))
        with tempfile.TemporaryDirectory() as root:
            paths = [str(Path(root) / f'Show 4th Season {n}.mkv') for n in (5, 6, 7)]
            for path in paths:
                Path(path).touch()
            for batch in (1, 2):
                result = self.media.get_media_info_on_files(paths)
                self.assertTrue(all(m.tmdb_id == 1 and not m.skip_reason for m in result.values()))
                self.assertEqual(2 * batch, self.media.get_tmdb_search_page.call_count)
                self.assertEqual(2 * batch, self.media.get_tmdb_info.call_count)
                self.assertEqual(batch, self.media.get_tmdb_tv_season_detail.call_count)
            self.assertFalse(self.cache)
            self.media.get_tmdb_search_page.side_effect = RuntimeError('temporary failure')
            self.assertTrue(self.media.get_media_info_on_files(paths)[paths[0]].skip_reason)
            self.candidates(self.info('Show', 4, 1), self.info('Show 4th', 9, 2))
            self.assertFalse(self.media.get_media_info_on_files(paths)[paths[0]].skip_reason)

    def test_bound_identity_can_override_two_digit_compatibility(self):
        work = self.info('Major 2nd', 2, 2)
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'Major 2nd Season 02.mkv')
            Path(path).touch()
            meta = self.media.get_media_info_on_files([path], tmdb_info=work)[path]
            self.assertEqual(('major 2nd', 2, []),
                             (meta.get_name().casefold(), meta.begin_season, meta.get_episode_list()))
            self.assertFalse(meta.skip_reason)

    def test_positive_lookup_evidence_overrides_default_without_work_cache_pollution(self):
        full = self.info('Major 2nd', 2, 2)
        self.candidates(full)
        # The normal lookup can already have a verified full-title result (e.g. LLM ID).
        # Reuse it; do not add two name searches merely because the release has 02.
        with patch.object(self.media, '_Media__search_media_with_name', return_value=full):
            meta = self.media.get_media_info('Major 2nd Season 02 1080p')
        self.assertEqual((2, 2, []), (meta.tmdb_id, meta.begin_season, meta.get_episode_list()))
        self.assertEqual('Major 2Nd', meta.get_name())
        self.assertFalse(meta.skip_reason)
        self.assertFalse(self.cache)
        self.media.get_tmdb_search_page.assert_not_called()

    def test_batch_mapping_is_applied_once_and_reuses_target_detail(self):
        self.config.get_config.return_value = {'episode_mappings': [dict(
            tmdb_id=1, source_season=4, source_begin=1, source_end=9, target_season=1, offset=10)]}
        self.candidates(self.info('Show', 1, 1), self.info('Show 4th', 9, 2))
        with tempfile.TemporaryDirectory() as root:
            paths = [str(Path(root) / f'Show 4th Season {n}.mkv') for n in (5, 6)]
            for path in paths:
                Path(path).touch()
            result = self.media.get_media_info_on_files(paths)
            self.assertEqual([(1, [15]), (1, [16])],
                             [(result[p].begin_season, result[p].get_episode_list()) for p in paths])
            self.assertEqual(1, self.media.get_tmdb_tv_season_detail.call_count)
            self.assertFalse(self.cache)

    def test_contrary_llm_identity_validates_final_numbering_completeness(self):
        work = self.info('Major 2nd', 2, 2)
        work['number_of_seasons'] = 2
        work['seasons'].append({'season_number': 3, 'episode_count': 24})
        for complete in (False, True):
            meta = MetaInfo('Major 2nd Season 02', use_llm=False)
            meta.note['llm'] = dict(tmdb_id=2, season_verified=True, tmdb_season=3, tmdb_episode=7)
            self.media.get_tmdb_tv_season_detail.side_effect = lambda ident, season: {
                'episodes': [{'episode_number': n} for n in (range(1, 25) if complete or season == 2 else [7])]}
            self.assertEqual(complete, self.media._prepare_media_identity(meta, work))
            if complete:
                self.assertEqual((3, [7]), (meta.begin_season, meta.get_episode_list()))
                self.assertFalse(meta.skip_reason)
            else:
                self.assertTrue(meta.skip_reason)

    def test_original_rezero_still_uses_formal_episode_mapping(self):
        from app.media.meta.recognition_rules import DEFAULT_EPISODE_MAPPINGS
        self.config.get_config.return_value = {'episode_mappings': DEFAULT_EPISODE_MAPPINGS}
        self.candidates(self.info('Re:Zero Kara Hajimeru Isekai Seikatsu', 1, 65942))
        self.media.get_tmdb_tv_season_detail.return_value = {'episodes': [{'episode_number': 84}]}
        title = ('[晚街与灯][Re：从零开始的异世界生活 第四季 / Re:Zero kara Hajimeru Isekai Seikatsu 4th Season]'
                 '[18 - 总第84][WEB-DL Remux][1080P_AVC_AAC][简繁日内封PGS]')
        meta = self.media.get_media_info(title)
        self.assertEqual((65942, 1, [84]), (meta.tmdb_id, meta.begin_season, meta.get_episode_list()))
        self.assertFalse(meta.skip_reason)
