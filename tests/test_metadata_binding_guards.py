"""Offline regressions for work identity and release-number preservation."""
import datetime
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.filter import Filter
from app.indexer.client._base import _IIndexClient
from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.llm_parser import LLMMetaParser
from app.media.meta.special_resolver import SpecialResolver
from app.utils.types import MediaType


class MetadataBindingGuardsTest(TestCase):
    def setUp(self):
        self.info = {'id': 42, 'name': 'Show', 'media_type': MediaType.TV,
                     'genres': [{'id': 16}], 'first_air_date': '2020-01-01',
                     'seasons': [{'season_number': 1, 'episode_count': 12}]}
        self.media = Media.__new__(Media)
        self.media.tmdb = object()
        self.media.meta = Mock()
        self.cache = {}
        self.media.meta.get_meta_data_by_key.side_effect = lambda key: self.cache.get(key, {})
        self.media.meta.update_meta_data.side_effect = self.cache.update
        self.media._Media__search_media_with_name = Mock(return_value=self.info)
        self.media.get_tmdb_info = Mock(side_effect=lambda **kw: deepcopy(self.info))
        self.media.get_tmdb_tv_season_detail = Mock(return_value={
            'episodes': [{'episode_number': n} for n in range(1, 13)]})
        cfg = patch('app.media.media.Config').start()
        self.config = cfg.return_value
        self.config.get_config.return_value = {'episode_mappings': []}
        self.addCleanup(patch.stopall)

    def mapping(self, offset, target=1):
        self.config.get_config.return_value = {'episode_mappings': [
            {'tmdb_id': 42, 'source_season': 1, 'source_begin': 1, 'source_end': 10,
             'target_season': target, 'offset': offset}]}

    def test_mapping_fresh_cached_and_repeated_are_identical(self):
        for offset, episode in ((1, 1), (-1, 3)):
            with self.subTest(offset=offset):
                self.cache.clear()
                self.mapping(offset)
                title = 'Show S01E%02d 1080p' % episode
                first = self.media.get_media_info(title, mtype=MediaType.TV)
                second = self.media.get_media_info(title, mtype=MediaType.TV)
                self.assertEqual([episode + offset], first.get_episode_list())
                self.assertEqual(first.get_episode_list(), second.get_episode_list())
                count = self.media.get_tmdb_tv_season_detail.call_count
                self.assertTrue(self.media._prepare_media_identity(first, self.info))
                self.assertEqual([episode + offset], first.get_episode_list())
                self.assertEqual(count, self.media.get_tmdb_tv_season_detail.call_count)

    def test_file_mapping_matches_cached_and_bound_paths(self):
        self.mapping(1)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'Show S01E01.mkv'
            path.touch()
            for kwargs in ({}, {}, {'tmdb_info': self.info}):
                result = self.media.get_media_info_on_files([str(path)], **kwargs)[str(path)]
                self.assertEqual([2], result.get_episode_list())

    def test_llm_scalar_does_not_collapse_range(self):
        for title in ('Show S01E01-E03.mkv', '[Group] Show - 01-03 [1080p].mkv'):
            meta = MetaInfo(title, use_llm=False)
            self.assertEqual([1, 2, 3], meta.get_episode_list())
            meta.note['llm'] = {'tmdb_id': 42, 'tmdb_season': 1,
                                'tmdb_episode': 1, 'season_verified': True}
            self.assertTrue(self.media._prepare_media_identity(meta, self.info))
            self.assertEqual([1, 2, 3], meta.get_episode_list())

    def test_llm_merge_preserves_rule_range_before_binding(self):
        parser = LLMMetaParser()
        meta = MetaInfo('Show S01E01-E03.mkv', use_llm=False)
        with patch.object(parser, 'parse', return_value={'begin_episode': 2, 'end_episode': 2,
                'total_episodes': 1, 'confidence': 1, 'field_confidence': {}}):
            result = parser.merge_into(meta, meta.org_string)
        self.assertEqual([1, 2, 3], result.get_episode_list())

    def index_results(self, resource, wanted, imdb=None):
        self.media.get_media_info = Mock(return_value=resource)
        wanted.get_poster_image = Mock(return_value='')
        wanted.get_backdrop_image = Mock(return_value='')
        wanted.imdb_id = imdb or ''
        client = SimpleNamespace(media=self.media, filter=Mock(), progress=Mock(),
                                 _reverse_title_sites=[], index_type='Offline')
        client.filter.check_torrent_filter.return_value = (True, 0, '')
        client.filter.is_torrent_match_sey.side_effect = Filter().is_torrent_match_sey
        return _IIndexClient.filter_search_results(client,
            [{'title': resource.org_string, 'enclosure': 'offline', 'imdbid': imdb}], 0,
            SimpleNamespace(id='offline', name='Offline', public=False),
            {'type': MediaType.TV, 'season': 1}, wanted, datetime.datetime.now())

    def test_equal_numeric_id_cannot_merge_movie_into_tv(self):
        resource = MetaInfo('Other Movie 2020 1080p', use_llm=False)
        resource.set_tmdb_info({'id': 42, 'media_type': MediaType.MOVIE,
                               'title': 'Other Movie', 'genres': [{'id': 18}]})
        wanted = MetaInfo('Show S01', use_llm=False)
        wanted.set_tmdb_info(self.info)
        self.assertEqual([], self.index_results(resource, wanted))
        self.assertEqual(MediaType.MOVIE, resource.type)

    def special_setup(self):
        self.info['seasons'][0]['episode_count'] = 1
        self.media.get_tmdb_search_page = Mock(return_value=[self.info])
        self.media.get_tmdb_tv_season_detail.return_value = {'episodes': [
            {'episode_number': 1, 'name': 'OVA 1', 'season_number': 1, 'show_id': 42}]}

    def test_special_title_respects_explicit_movie_type(self):
        self.special_setup()
        result = self.media.get_media_info('Show OVA 1', mtype=MediaType.MOVIE)
        self.assertTrue(result.skip_reason)
        self.assertEqual('unconfirmed', result.note['special_episode']['status'])

    def test_special_file_respects_explicit_movie_type(self):
        self.special_setup()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'Show OVA 1.mkv'
            path.touch()
            result = self.media.get_media_info_on_files([str(path)], media_type=MediaType.MOVIE)[str(path)]
            self.assertTrue(result.skip_reason)

    def test_mapping_proof_does_not_apply_to_changed_identity_or_numbers(self):
        self.mapping(1)
        meta = MetaInfo('Show S01E01', use_llm=False)
        self.assertTrue(self.media._prepare_media_identity(meta, self.info))
        proof = deepcopy(meta.note)
        self.assertFalse(self.media._prepare_media_identity(meta, dict(self.info, id=99)))
        self.assertEqual(proof, meta.note)
        self.assertFalse(self.media._prepare_media_identity(meta, dict(self.info, media_type=MediaType.MOVIE)))
        self.assertEqual(proof, meta.note)
        meta.begin_episode = 5
        self.assertFalse(self.media._prepare_media_identity(meta, self.info))
        self.assertEqual(5, meta.begin_episode)

    def test_mapping_rejection_restores_source_before_next_candidate(self):
        self.mapping(1, target=2)
        meta = MetaInfo('Show S01E01', use_llm=False)
        self.assertFalse(self.media._prepare_media_identity(meta, self.info))
        self.assertEqual((1, [1], {}), (meta.begin_season, meta.get_episode_list(), meta.note))
        valid = dict(self.info, seasons=[{'season_number': 2}])
        self.assertTrue(self.media._prepare_media_identity(meta, valid))
        self.assertEqual((2, [2]), (meta.begin_season, meta.get_episode_list()))
        count = self.media.get_tmdb_tv_season_detail.call_count
        self.assertTrue(self.media._prepare_media_identity(meta, valid))
        self.assertEqual(count, self.media.get_tmdb_tv_season_detail.call_count)

    def test_llm_range_cross_season_requires_complete_absolute_conversion(self):
        info = dict(self.info, seasons=[{'season_number': 1, 'episode_count': 12},
                                      {'season_number': 2, 'episode_count': 12}])
        for title, accepted, expected in (('Show S01E13-E15', True, [1, 2, 3]),
                                           ('Show S01E01-E03', False, [1, 2, 3])):
            meta = MetaInfo(title, use_llm=False)
            meta.note['llm'] = {'tmdb_id': 42, 'tmdb_season': 2,
                                'tmdb_episode': 1, 'season_verified': True}
            original = deepcopy(meta.note)
            self.assertEqual(accepted, self.media._prepare_media_identity(meta, info))
            self.assertEqual(expected, meta.get_episode_list())
            self.assertEqual(2 if accepted else 1, meta.begin_season)
            if not accepted:
                self.assertEqual(original, meta.note)

    def test_llm_range_missing_target_member_is_rejected(self):
        meta = MetaInfo('Show S01E01-E03', use_llm=False)
        meta.note['llm'] = {'tmdb_id': 42, 'tmdb_season': 1,
                            'tmdb_episode': 1, 'season_verified': True}
        self.media.get_tmdb_tv_season_detail.return_value = {'episodes': [{'episode_number': 1}]}
        self.assertFalse(self.media._prepare_media_identity(meta, self.info))
        self.assertEqual([1, 2, 3], meta.get_episode_list())

    def test_same_tv_identity_and_imdb_default_movie_fallback_remain_supported(self):
        for title, imdb in (('Show S01E02', None), ('Show', 'tt0042')):
            resource = MetaInfo(title, use_llm=False)
            resource.set_tmdb_info(self.info)
            wanted = MetaInfo('Show S01', use_llm=False)
            wanted.set_tmdb_info(dict(self.info, genres=[{'id': 18}]))
            self.assertEqual(1, len(self.index_results(resource, wanted, imdb)))

    def test_imdb_cannot_override_explicit_episode_type(self):
        wanted = MetaInfo('Movie', use_llm=False)
        wanted.set_tmdb_info({'id': 42, 'media_type': MediaType.MOVIE, 'title': 'Movie'})
        self.media.get_tmdb_info.side_effect = lambda **kw: deepcopy(wanted.tmdb_info)
        resource = MetaInfo('Show S01E01', use_llm=False)
        self.assertEqual([], self.index_results(resource, wanted, 'tt0042'))

    def test_special_final_type_matrix_with_manual_and_configured_targets(self):
        self.special_setup()
        resolver = SpecialResolver(self.media)
        for manual in (False, True):
            for hint, genres, allowed in ((None, [18], True), (MediaType.TV, [18], True),
                    (MediaType.ANIME, [16], True), (MediaType.ANIME, [18], False),
                    (MediaType.MOVIE, [16], False)):
                with self.subTest(manual=manual, hint=hint, genres=genres):
                    resolver.cache.clear()  # Each matrix case is a separate recognition batch.
                    self.info['genres'] = [{'id': g} for g in genres]
                    target = {'media_type': 'tv', 'tmdb_id': 42, 'season': 1, 'episode': 1}
                    with patch.object(resolver, 'configured_target', return_value=None if manual else target):
                        result = resolver.resolve(MetaInfo('Show OVA 1', use_llm=False),
                            bound=self.info, manual=target if manual else None, mtype_hint=hint)
                    self.assertEqual(allowed, result.skip_reason is None)
                    self.assertEqual('confirmed' if allowed else 'unconfirmed',
                                     result.note['special_episode']['status'])

    def test_tv_parent_can_resolve_independent_movie_when_hint_allows(self):
        self.special_setup()
        movie = {'id': 99, 'media_type': MediaType.MOVIE, 'title': 'Bonus Film',
                 'genres': [{'id': 16}], 'release_date': '2020-01-01'}
        resolver = SpecialResolver(self.media)
        self.media.get_tmdb_info.return_value = movie
        self.media.get_tmdb_info.side_effect = None
        target = {'media_type': 'movie', 'tmdb_id': 99}
        for hint, allowed in ((None, True), (MediaType.MOVIE, True),
                              (MediaType.TV, False), (MediaType.ANIME, False)):
            with patch.object(resolver, 'configured_target', return_value=target):
                result = resolver.resolve(MetaInfo('Show OVA 1', use_llm=False),
                                          bound=self.info, mtype_hint=hint)
            self.assertEqual(allowed, result.skip_reason is None)
            if allowed:
                self.assertEqual((MediaType.MOVIE, 99), (result.type, result.tmdb_id))

    def test_mapping_each_entry_validates_once(self):
        self.mapping(1)
        with patch.object(self.media, '_prepare_media_identity', wraps=self.media._prepare_media_identity) as prepare:
            self.media.get_media_info('Show S01E01', mtype=MediaType.TV)
            self.assertEqual(1, prepare.call_count)
            self.media.get_media_info('Show S01E01', mtype=MediaType.TV)
            self.assertEqual(2, prepare.call_count)
        self.assertEqual(2, self.media.get_tmdb_tv_season_detail.call_count)

    def test_configured_range_mapping_preserves_cardinality(self):
        self.mapping(1)
        result = self.media.get_media_info('Show S01E01-E03', mtype=MediaType.TV)
        self.assertEqual([2, 3, 4], result.get_episode_list())
        self.assertEqual([1, 2, 3], result.note['episode_mapping']['source_episodes'])

    def test_special_automatic_type_acceptance_on_title_and_file_entries(self):
        self.special_setup()
        for hint in (None, MediaType.TV, MediaType.ANIME):
            self.assertIsNone(self.media.get_media_info('Show OVA 1', mtype=hint).skip_reason)
            with tempfile.TemporaryDirectory() as root:
                path = Path(root) / 'Show OVA 1.mkv'
                path.touch()
                result = self.media.get_media_info_on_files([str(path)], media_type=hint)[str(path)]
                self.assertIsNone(result.skip_reason)

    def test_verified_llm_type_conflict_blocks_imdb_shortcut(self):
        resource = MetaInfo('Show', use_llm=False)
        wanted = MetaInfo('Show S01', use_llm=False)
        wanted.set_tmdb_info(self.info)
        resource.note['llm'] = {'candidate_verified': True, 'tmdb_type': 'movie'}
        with patch('app.indexer.client._base.MetaInfo', return_value=resource):
            self.assertEqual([], self.index_results(resource, wanted, 'tt0042'))
