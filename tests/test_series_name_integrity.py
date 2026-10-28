"""Series-name regressions with real parsing/search and offline provider boundaries."""
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.media.meta import MetaInfo
from app.media.meta.llm_parser import LLMMetaParser
from app.media.meta.ordinal import Evidence
from app.media.meta.release_groups import ReleaseGroupsMatcher
from app.utils import EpisodeFormat
from app.utils.types import MediaType
from tests import test_media_identity_matrix as fixtures


class SeriesNameIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='series-name-integrity-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        parser = LLMMetaParser()
        self.enter(patch.object(parser, 'parse', return_value={}))
        self.enter(patch.object(parser, '_search_context_enable', False))

    def enter(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def file(self, media, relative, **kwargs):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'unchanged source')
        result = media.get_media_info_on_files([str(path)], **kwargs).get(str(path))
        self.assertEqual(b'unchanged source', path.read_bytes())
        return result

    def test_title_zero_cannot_override_explicit_episode(self):
        media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
        work = fixtures.series_fixture(1001, 'Class of 09', 'Class of 09', 2020, [18], 1)
        provider.records[1001] = work
        for marker, expected in (('S01E01', [1]), ('S01E02', [2]), ('S01E10', [10]),
                                 ('S01E01-E03', [1, 2, 3]), ('1x02', [2]),
                                 ('Episode 03', [3]), ('Episode(03)', [3]), ('第01集', [1])):
            raw = 'Class.of.09.%s.mkv' % marker
            with self.subTest(marker=marker):
                parsed = MetaInfo(raw, use_llm=False)
                self.assertEqual('Class Of 09', parsed.get_name())
                self.assertEqual(expected, parsed.get_episode_list())
                found = self.file(media, raw)
                self.assertEqual(1001, found.tmdb_id)
                self.assertEqual(expected, found.get_episode_list())
                self.assertIsNone(found.skip_reason)

    def test_existing_bare_and_numeric_title_rules_survive(self):
        for raw, title, episodes in (
                ('Show 09.mkv', 'Show', [9]),
                ('[Group] 86 - 01 [1080p].mkv', '86', [1]),
                ('[Group] Hundred 10 [BD 1920x1080 HEVC].mkv', 'Hundred', [10]),
                ('The.100.S01E01.mkv', 'The 100', [1]),
                ('1883.S01E01.mkv', '1883', [1])):
            with self.subTest(raw=raw):
                parsed = MetaInfo(raw, use_llm=False)
                self.assertEqual(title, parsed.get_name())
                self.assertEqual(episodes, parsed.get_episode_list())
        fractional = MetaInfo('Show S01E13.5.mkv', use_llm=False)
        self.assertIsNone(fractional.begin_episode)
        self.assertEqual('unconfirmed', fractional.note['fractional_episode']['status'])

    def test_non_episode_year_word_keeps_existing_bare_episode(self):
        # The look-ahead must agree with the parser: Episode 2020 is not an
        # explicit EP marker and cannot suppress the earlier bare episode.
        parsed = MetaInfo('Show 01 Episode 2020.mkv', use_llm=False)
        self.assertEqual('Show', parsed.get_name())
        self.assertEqual([1], parsed.get_episode_list())
        self.assertEqual('2020', parsed.year)

    def test_invalid_custom_group_pattern_keeps_builtin_group_and_title(self):
        matcher = ReleaseGroupsMatcher()
        # Confirming a group is new optional evidence; a malformed custom
        # pattern must not abort an otherwise valid existing anime parse.
        for pattern in ('(', '(Loli)(House)'):
            with self.subTest(pattern=pattern), \
                    patch.object(matcher, '_ReleaseGroupsMatcher__release_groups', pattern), \
                    patch('app.media.meta.metaanime.ReleaseGroupsMatcher', return_value=matcher):
                parsed = MetaInfo('[LoliHouse] Erased [01][1080p].mkv', use_llm=False)
                self.assertEqual('Erased', parsed.get_name())
                self.assertEqual([1], parsed.get_episode_list())
                self.assertEqual('LoliHouse', parsed.resource_team)

    def test_grouped_chinese_and_short_titles_remain_titles(self):
        for raw, title, group in (
                ('[VCB-Studio][孤独摇滚][01][1080p].mkv', '孤独摇滚', 'VCB-Studio'),
                ('[LoliHouse][孤独摇滚][01][1080p].mkv', '孤独摇滚', 'LoliHouse'),
                ('[VCB-Studio][孤独摇滚][01][1080p][GB].mkv', '孤独摇滚', 'VCB-Studio'),
                ('[动漫国字幕组][孤独摇滚][01][1080p].mkv', '孤独摇滚', '动漫国字幕组'),
                ('[VCB-Studio][命运石之门 0][01][1080p].mkv', '命运石之门 0', 'VCB-Studio'),
                ('[VCB-Studio] AIR [01][1080p].mkv', 'Air', 'VCB-Studio'),
                ('[LoliHouse] K - 01 [1080p].mkv', 'K', 'LoliHouse')):
            with self.subTest(raw=raw):
                meta = MetaInfo(raw, use_llm=False)
                self.assertEqual(title, meta.get_name())
                self.assertEqual(group, meta.resource_team)
                self.assertEqual([1], meta.get_episode_list())
                self.assertEqual('1080p', meta.resource_pix)

    def test_group_identity_never_crosses_works_through_manual_cache(self):
        media, provider, cache = fixtures.MediaIdentityMatrixTest.make_media()
        first = dict(deepcopy(provider.records[930001]), media_type=MediaType.TV)
        old_key = '[电视剧]Vcb-Studio-None-None'
        cache[old_key] = dict(id=930001, type=MediaType.ANIME, title='孤独摇滚')
        old_entry = deepcopy(cache[old_key])
        second = fixtures.series_fixture(1002, '另一部动画', 'Another Animation', 2022, [16], 1)
        provider.records[1002] = second
        self.assertEqual(930001, self.file(media, 'first/[VCB-Studio][孤独摇滚][01][1080p].mkv',
                                          tmdb_info=first).tmdb_id)
        found = self.file(media, 'second/[VCB-Studio][另一部动画][02][1080p].mkv')
        self.assertEqual(1002, found.tmdb_id)
        self.assertEqual(old_entry, cache[old_key])
        self.assertEqual([old_key], [key for key in cache if 'vcb-studio' in key.casefold()])
        self.assertGreater(provider.search_count(), 0)
        media.save_rename_cache('[VCB-Studio][01][1080p].mkv', first)
        self.assertEqual(old_entry, cache[old_key])
        self.assertEqual([old_key], [key for key in cache if 'vcb-studio' in key.casefold()])

    def test_group_key_is_ignored_without_changing_ordinary_manual_aliases(self):
        media, provider, cache = fixtures.MediaIdentityMatrixTest.make_media()
        legacy = MetaInfo('VCB-Studio S01E01.mkv', use_llm=False)
        legacy.resource_team = 'VCB-Studio'
        self.assertEqual({}, media.get_cache_info(legacy))
        media.meta.get_meta_data_by_key.assert_not_called()
        work = dict(deepcopy(provider.records[920001]), media_type=MediaType.TV)
        media.save_rename_cache('User Alias S01E01.mkv', work)
        found = media.get_media_info('User Alias S01E02.mkv', mtype=MediaType.TV)
        self.assertEqual(920001, found.tmdb_id)
        self.assertEqual(0, provider.search_count())

    def test_only_metadata_blocks_do_not_become_titles(self):
        for raw in ('[VCB-Studio][01][1080p].mkv', '[VCB-Studio][1080p][01].mkv'):
            meta = MetaInfo(raw, use_llm=False)
            self.assertNotEqual('vcb-studio', meta.get_name().casefold())
            self.assertNotEqual('1080p', meta.get_name().casefold())

    def test_later_same_name_candidate_is_checked_after_rejection(self):
        # A definite identity conflict (live action for an explicit anime request)
        # lets the next same-name candidate be checked, in either response order.
        rows = [fixtures.series_fixture(1001, 'Echo', 'Echo', 2000, [18], 3),
                fixtures.series_fixture(1002, 'Echo', 'Echo', 2020, [16], 3)]
        for ordered in (rows, list(reversed(rows))):
            with self.subTest(order=[row['id'] for row in ordered]):
                media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
                provider.records = {row['id']: deepcopy(row) for row in ordered}
                found = media.get_media_info('Echo S03E01.mkv', mtype=MediaType.ANIME, cache=False)
                self.assertEqual(1002, found.tmdb_id)
                self.assertEqual((3, [1]), (found.begin_season, found.get_episode_list()))
                self.assertIsNone(found.skip_reason)
        # A missing season is UNKNOWN, not a conflict: the namesake with S03 is
        # never selected instead, and the release numbering is untouched (C3).
        rows = [fixtures.series_fixture(1001, 'Echo', 'Echo', 2000, [18], 1),
                fixtures.series_fixture(1002, 'Echo', 'Echo', 2020, [18], 3)]
        for ordered in (rows, list(reversed(rows))):
            with self.subTest(order=[row['id'] for row in ordered], missing_season=True):
                media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
                provider.records = {row['id']: deepcopy(row) for row in ordered}
                found = media.get_media_info('Echo S03E01.mkv', mtype=MediaType.TV, cache=False)
                self.assertFalse(found.tmdb_id)
                self.assertTrue(found.skip_reason)
                self.assertEqual((3, [1]), (found.begin_season, found.get_episode_list()))

    def test_definite_rejection_preserves_selected_work_numbering(self):
        media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
        rows = [fixtures.series_fixture(1001, 'Echo', 'Echo', 2000, [18], 1),
                fixtures.series_fixture(1002, 'Echo', 'Echo', 2020, [16], 1)]
        provider.records = {row['id']: row for row in rows}
        with patch('app.media.media.Config') as config:
            # The first work is disproved by its genre, not by a missing season or
            # a failed mapping. The next work's explicit mapping must apply once.
            config.return_value.get_config.return_value = {'episode_mappings': [
                dict(tmdb_id=1002, source_season=3, source_begin=1, source_end=12, target_season=1, offset=2)]}
            found = media.get_media_info('Echo S03E01.mkv', mtype=MediaType.ANIME, cache=False)
        self.assertEqual(1002, found.tmdb_id)
        self.assertEqual((1, [3]), (found.begin_season, found.get_episode_list()))
        self.assertEqual([1], found.note['episode_mapping']['source_episodes'])

    def test_unavailable_first_candidate_is_not_evidence_for_switching_works(self):
        for missing in ('detail', 'season_manifest', 'null_season_manifest'):
            with self.subTest(missing=missing):
                media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
                rows = [fixtures.series_fixture(1001, 'Echo', 'Echo', 2000, [18], 3),
                        fixtures.series_fixture(1002, 'Echo', 'Echo', 2020, [18], 3)]
                provider.records = {row['id']: row for row in rows}
                def detail(identifier, *args):
                    value = deepcopy(provider.records[int(identifier)])
                    if int(identifier) == 1001:
                        if missing == 'detail':
                            return {}
                        if missing == 'null_season_manifest':
                            value['seasons'] = None
                        else:
                            value.pop('seasons')
                    return value
                provider.tv.details.side_effect = detail
                provider.records[1003] = fixtures.series_fixture(1003, 'Other Show', 'Other Show', 2020, [18], 3)
                for title in ('Echo S03E01.mkv', 'Echo 2022 S03E01.mkv'):
                    with patch.object(LLMMetaParser(), 'get_alias_candidates', return_value=['Other Show']) as aliases:
                        found = media.get_media_info(title, mtype=MediaType.TV, cache=False)
                    aliases.assert_not_called()
                    self.assertFalse(found.tmdb_id)
                    self.assertTrue(found.skip_reason)

    def test_file_numbering_preserves_source_and_checks_task_scope(self):
        media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
        for name in ('The Expanse S03 (2015)/S01E01.mkv', 'The Expanse S03 (2015)/Season 1/01.mkv',
                     'Bocchi the Rock (2022)/The Expanse/01.mkv', 'TV/The Expanse/01.mkv'):
            found = self.file(media, name)
            self.assertEqual((920001, [1], [1]), (found.tmdb_id, found.get_season_list(), found.get_episode_list()))
        info = dict(provider.records[920001], media_type=MediaType.TV)
        with patch('app.media.media.Config') as config:
            config.return_value.get_config.return_value = {'episode_mappings': [dict(
                tmdb_id=920001, source_season=1, source_begin=1, source_end=12, target_season=1, offset=1)]}
            found = self.file(media, 'The Expanse S01E01.mkv', tmdb_info=info,
                              episode_format=EpisodeFormat('Other.E{ep}.mkv'))
        self.assertEqual([2], found.get_episode_list())
        for formatter in (None, EpisodeFormat(None, '999')):
            missing = self.file(media, 'The Expanse S09E01.mkv', tmdb_info=info, episode_format=formatter)
            self.assertTrue(missing.skip_reason)
        manual = self.file(media, 'The Expanse S09E01.mkv', tmdb_info=info,
                           season=1, episode_format=EpisodeFormat(None, '999'))
        self.assertFalse(manual.skip_reason)
        self.assertEqual((1, [999]), (manual.begin_season, manual.get_episode_list()))
        for name in ('The Expanse 第1-3季 第01集.mkv', 'The Expanse S01-S03E01.mkv',
                     '[Group] The Expanse S01-S03 - 01 [1080p].mkv'):
            # Every parser must preserve the range before the transfer guard sees it.
            self.assertEqual([1, 2, 3], MetaInfo(name, use_llm=False).get_season_list())
            for target_season in (1, 2):
                found = self.file(media, name, download_context=dict(tmdb_info=info,
                    numbering='tmdb', seasons=[target_season], episodes=[1], source_title='The Expanse 第1季'))
                self.assertTrue(found is None or found.skip_reason)

    def test_unavailable_mapping_episodes_do_not_switch_work_and_can_retry(self):
        # A valid configured mapping identifies the first work; an unavailable
        # season endpoint is not proof that the next namesake is the right work.
        for response in (None, {}, {'episodes': None}, {'episodes': []},
                         {'episodes': [{'episode_number': 1}]}):
            with self.subTest(response=response):
                media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
                rows = [fixtures.series_fixture(1001, 'Echo', 'Echo', 2000, [18], 1),
                        fixtures.series_fixture(1002, 'Echo', 'Echo', 2020, [18], 3)]
                provider.records = {row['id']: row for row in rows}
                provider.tv.season_details.side_effect = lambda identifier, season: deepcopy(response)
                with patch('app.media.media.Config') as config:
                    config.return_value.get_config.return_value = {'episode_mappings': [
                        dict(tmdb_id=1001, source_season=3, source_begin=1, source_end=12,
                             target_season=1, offset=2)]}
                    unresolved = media.get_media_info('Echo S03E01.mkv', mtype=MediaType.TV)
                    self.assertFalse(unresolved.tmdb_id)
                    self.assertEqual((3, [1]), (unresolved.begin_season, unresolved.get_episode_list()))
                    self.assertNotIn('episode_mapping', unresolved.note)
                    self.assertNotIn(1002, [int(call.args[0]) for call in provider.tv.details.call_args_list])
                    provider.tv.season_details.side_effect = provider._season
                    recovered = media.get_media_info('Echo S03E01.mkv', mtype=MediaType.TV)
                    self.assertEqual((1001, 1, [3]),
                        (recovered.tmdb_id, recovered.begin_season, recovered.get_episode_list()))

    def test_unconfirmed_group_guess_does_not_promote_language_tags(self):
        for tag in ('简体', '繁体', 'GB', 'CHS', 'HEVC'):
            raw = '[孤独摇滚][%s][01][1080p].mkv' % tag
            for hint in (None, MediaType.ANIME):
                with self.subTest(tag=tag, hint=hint):
                    meta = MetaInfo(raw, mtype=hint, use_llm=False)
                    self.assertEqual('孤独摇滚', meta.get_name())
                    self.assertEqual([1], meta.get_episode_list())
                    self.assertEqual('1080p', meta.resource_pix)

    def test_identity_evidence_distinguishes_mapping_failure_and_work_mismatch(self):
        media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
        info = dict(fixtures.series_fixture(1001, 'Echo', 'Echo', 2000, [18], 1),
                    media_type=MediaType.TV)
        provider.records[1001] = info
        # A missing season and a failed mapping are UNKNOWN (C3); only a definite
        # identity conflict, here live action for an explicit anime request, is INVALID.
        for rules, mtype, expected in (
                ([], None, Evidence.UNKNOWN),
                ([dict(tmdb_id=1001, source_season=3, source_begin=1, source_end=12,
                       target_season=1, offset=100)], None, Evidence.UNKNOWN),
                ([], MediaType.ANIME, Evidence.INVALID)):
            with self.subTest(expected=expected, mtype=mtype), patch('app.media.media.Config') as config:
                config.return_value.get_config.return_value = {'episode_mappings': rules}
                meta = MetaInfo('Echo S03E01.mkv', mtype=mtype, use_llm=False)
                evidence = {}
                self.assertFalse(media._prepare_media_identity(meta, info, evidence=evidence))
                self.assertEqual(expected, evidence['state'])
                self.assertEqual((3, [1]), (meta.begin_season, meta.get_episode_list()))
                self.assertNotIn('episode_mapping', meta.note)

    def test_unavailable_llm_season_evidence_is_not_an_identity_rejection(self):
        media, _, _ = fixtures.MediaIdentityMatrixTest.make_media()
        info = dict(fixtures.series_fixture(1001, 'Echo', 'Echo', 2000, [18], 1),
                    media_type=MediaType.TV)
        meta = MetaInfo('Echo S03E01.mkv', use_llm=False)
        meta.note['llm'] = dict(tmdb_id=1001, tmdb_season=1, tmdb_episode=3, season_verified=True)
        with patch.object(media, 'get_tmdb_tv_season_detail', return_value={}):
            evidence = {}
            self.assertFalse(media._prepare_media_identity(meta, info, evidence=evidence))
            self.assertEqual(Evidence.UNKNOWN, evidence['state'])
            self.assertEqual((3, [1]), (meta.begin_season, meta.get_episode_list()))
            self.assertNotIn('season_binding', meta.note)

    def test_promoted_title_preserves_completion_and_resolution_metadata(self):
        for block in ('12 END', '12END', '12 FINAL'):
            meta = MetaInfo('[VCB-Studio][%s][1080p].mkv' % block, use_llm=False)
            self.assertEqual([12], meta.get_episode_list())
            self.assertFalse(meta.get_name())
        for tag in ('简体', '繁体'):
            meta = MetaInfo('[VCB-Studio][%s][01][1080p].mkv' % tag, use_llm=False)
            self.assertEqual([1], meta.get_episode_list())
            self.assertFalse(meta.get_name())
        meta = MetaInfo('[VCB-Studio][孤独摇滚][4K][01].mkv', mtype=MediaType.ANIME, use_llm=False)
        self.assertEqual('孤独摇滚', meta.get_name())
        self.assertEqual([1], meta.get_episode_list())
        self.assertEqual('2160p', meta.resource_pix)

    def test_missing_anime_genres_reach_existing_detail_policy(self):
        for genres, language, expected in (([], 'ja', 1005), (None, 'ja', 1005),
                                           ([], 'en', 0), ([18], 'ja', 0), ([16], 'en', 1005)):
            with self.subTest(genres=genres, language=language):
                media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
                record = fixtures.series_fixture(1005, 'New Animation', 'New Animation', 2025, genres or [], 1)
                record['original_language'] = language
                provider.records = {1005: record}
                if genres is None:
                    # Simulate a search response omitting IDs and a detail with explicit null genres.
                    provider.search.tv_shows.return_value = []
                    provider.search.tv_shows.side_effect = lambda params: [dict(id=1005, name='New Animation',
                        original_name='New Animation', first_air_date='2025-01-01', genre_ids=None, genres=None)]
                    record['genres'] = None
                found = media.get_media_info('New.Animation.S01E01.mkv', mtype=MediaType.ANIME, cache=False)
                self.assertEqual(expected, found.tmdb_id)
                if expected:
                    self.assertEqual(MediaType.ANIME, found.type)
                    self.assertEqual([1], found.get_episode_list())

    def test_missing_anime_details_do_not_authorize_a_candidate(self):
        media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
        record = fixtures.series_fixture(1005, 'New Animation', 'New Animation', 2025, [], 1)
        record['original_language'] = 'ja'
        provider.records = {1005: record}
        provider.detail_mode = 'empty'
        self.assertFalse(media.get_media_info('New.Animation.S01E01.mkv', mtype=MediaType.ANIME,
                                            cache=False).tmdb_id)

    def test_exact_and_year_priority_survive_candidate_retries(self):
        media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
        records = [fixtures.series_fixture(1001, 'Echo Night', 'Echo Night', 2020, [18], 3),
                   fixtures.series_fixture(1002, 'Echo Nights', 'Echo Nights', 2019, [18], 1),
                   fixtures.series_fixture(1003, 'Echo Nights', 'Echo Nights', 2020, [18], 3)]
        provider.records = {record['id']: record for record in records}
        candidates = [dict(id=record['id'], name=record['name'], original_name=record['original_name'],
                           first_air_date=record['first_air_date'], genre_ids=[18]) for record in records]
        provider.search.tv_shows.side_effect = lambda params: deepcopy(candidates)
        # An explicit year disambiguates same-name works; the fuzzy title never wins.
        for title, expected in (('Echo.Nights.2020.S01E01.mkv', 1003), ('Echo.Nights.2019.S01E01.mkv', 1002)):
            with self.subTest(title=title):
                found = media.get_media_info(title, mtype=MediaType.TV, cache=False)
                self.assertEqual(expected, found.tmdb_id)
                self.assertEqual((1, [1]), (found.begin_season, found.get_episode_list()))
        # The 2019 work lacks S03: that is UNKNOWN, so the S03 namesake is not chosen (C3).
        for title in ('Echo.Nights.S03E01.mkv', 'Echo.Nights.2022.S03E01.mkv'):
            with self.subTest(title=title):
                found = media.get_media_info(title, mtype=MediaType.TV, cache=False)
                self.assertFalse(found.tmdb_id)
                self.assertTrue(found.skip_reason)
                self.assertEqual((3, [1]), (found.begin_season, found.get_episode_list()))
        # An exact alias still beats a plausible fuzzy primary name.
        provider.records[1003].update(name='Official Title', original_name='Official Title',
                                     alternative_titles={'results': [{'title': 'Echo Nights'}]})
        candidates[2].update(name='Official Title', original_name='Official Title')
        found = media.get_media_info('Echo.Nights.2020.S01E01.mkv', mtype=MediaType.TV, cache=False)
        self.assertEqual(1003, found.tmdb_id)
        rows = [fixtures.series_fixture(1011, 'Echo Lights', 'Echo Lights', 2020, [18], 1),
                fixtures.series_fixture(1012, 'Echo Nights', 'Echo Nights', 2020, [18], 1)]
        provider.records = {row['id']: row for row in rows}
        provider.search.multi.side_effect = lambda params: [dict(row, media_type='tv') for row in deepcopy(rows)]
        found = media.get_media_info('Echo Night.mkv', cache=False)
        self.assertEqual(1012, found.tmdb_id)

    def test_missing_search_genres_do_not_bypass_known_live_action_details(self):
        media, provider, _ = fixtures.MediaIdentityMatrixTest.make_media()
        record = fixtures.series_fixture(1005, 'New Animation', 'New Animation', 2025, [18], 1)
        record['original_language'] = 'ja'
        provider.records = {1005: record}
        provider.search.tv_shows.side_effect = lambda params: [dict(id=1005, name='New Animation',
            original_name='New Animation', first_air_date='2025-01-01', genre_ids=[])]
        for title in ('New.Animation.S01E01.mkv', 'New.Animation.2025.S01E01.mkv'):
            self.assertFalse(media.get_media_info(title, mtype=MediaType.ANIME, cache=False).tmdb_id)
