"""Offline LLM-primary contracts through real title/file recognition entry points."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.llm_parser import LLMMetaParser
from app.media.meta.metainfo import prepare_title
from app.utils.types import MediaType
from app.utils.episode_format import EpisodeFormat


class LLMPrimaryTest(unittest.TestCase):
    def setUp(self):
        self.parser = LLMMetaParser()
        state = dict(self.parser.__dict__)
        self.addCleanup(lambda: (self.parser.__dict__.clear(), self.parser.__dict__.update(state)))
        self.parser._enabled, self.parser._mode = True, 'balanced'
        self.parser._base_url, self.parser._api_key, self.parser._model = 'https://example.invalid', 'test', 'test'
        self.parser._parse_cache = {}
        self.client = Mock()
        self.patch(self.parser, '_LLMMetaParser__get_client', return_value=self.client)
        self.search = self.patch(self.parser, '_LLMMetaParser__search_tmdb_candidates')
        self.extra = self.patch(self.parser, '_LLMMetaParser__fetch_tv_candidate_extra', return_value={})
        self.media = Media()
        self.patch(self.media, 'tmdb', True)
        self.cache = self.patch(self.media, 'meta', Mock())
        self.info = {'id': 86031, 'name': '石纪元', 'media_type': MediaType.TV, 'genres': [{'id': 16}],
                     'seasons': [{'season_number': 4, 'name': '科学与未来', 'episode_count': 37}]}
        self.candidate = {'id': 86031, 'type': 'tv', 'name': '石纪元',
                          'seasons': [{'n': 4, 'name': '科学与未来', 'eps': 37}]}
        self.search.return_value = [self.candidate]
        self.fetch = self.patch(self.media, 'get_tmdb_info', side_effect=lambda **_: copy.deepcopy(self.info))
        self.details = self.patch(self.media, 'get_tmdb_tv_season_detail', side_effect=lambda _id, season: {
            'season_number': season,
            'episodes': [{'episode_number': i, 'season_number': season, 'name': 'Episode %s' % i}
                         for i in range(1, 38)]})
        self.rule_mapping = self.patch(self.media, '_apply_episode_mapping', side_effect=AssertionError('rule mapping in primary path'))
        words = patch('app.media.meta.metainfo.WordsHelper')
        self.addCleanup(words.stop)
        self.words = words.start().return_value
        self.words.process.side_effect = lambda title: (title, [], {})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def patch(self, obj, name, *args, **kwargs):
        patcher = patch.object(obj, name, *args, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    @staticmethod
    def filename(number):
        return '[CheeseAni] Dr.STONE SCIENCE FUTURE Cour 3 [%s][BDRip 1080p HEVC].mkv' % number

    def responses(self, title, begin=37, end=None, season=4, release=None, decision=None, extra=()):
        extraction = {'status': 'understood', 'type': 'anime', 'en_name': 'Dr.STONE',
                      'queries': ['Dr.STONE'], 'begin_episode': begin, 'end_episode': end}
        extraction.update(release or {})
        final = {'status': 'matched', 'tmdb_id': self.candidate['id'], 'tmdb_type': self.candidate['type'],
                 'targets': [{'season': season, 'begin_episode': begin, 'end_episode': end}],
                 'evidence': [title], 'reason': '根据原始篇章名及候选季信息对应'}
        final.update(decision or {})
        self.client.complete_text.side_effect = [json.dumps(item, ensure_ascii=False) for item in (extraction, final, *extra)]

    def file(self, name, context=None, **kwargs):
        path = Path(self.tmp.name) / name
        path.touch(exist_ok=True)
        return self.media.get_media_info_on_files([str(path)], download_context=context, **kwargs)[str(path)]

    def test_all_cour_files_use_model_episode_and_provider_season(self):
        for episode in range(25, 38):
            title = self.filename(episode)
            with self.subTest(episode=episode):
                self.responses(title, begin=episode)
                meta = self.file(title, {'tmdb_info': self.info, 'seasons': [4],
                                        'episodes': list(range(25, 38)), 'numbering': 'tmdb'})
                self.assertFalse(meta.skip_reason)
                self.assertEqual((86031, [4], [episode]), (meta.tmdb_id, meta.get_season_list(), meta.get_episode_list()))
        self.rule_mapping.assert_not_called()

    def test_cross_language_arc_reaches_correct_season_without_literal_match(self):
        title = "[UHA-WINGS][JoJo's Bizarre Adventure Steel Ball Run][01][1080p HEVC].mkv"
        self.candidate.update(id=45790, name='JOJO的奇妙冒险', seasons=[{'n': 1, 'name': '幻影之血篇', 'eps': 26},
                                                               {'n': 6, 'name': '飙马野郎篇', 'eps': 12}])
        self.info.update(id=45790, name='JOJO的奇妙冒险', seasons=[{'season_number': 1}, {'season_number': 6}])
        self.responses(title, begin=1, season=6, release={'en_name': "JoJo's Bizarre Adventure", 'queries': ["JoJo's Bizarre Adventure"]},
                       decision={'evidence': ['Steel Ball Run']})
        meta = self.media.get_media_info(title)
        self.assertFalse(meta.skip_reason)
        self.assertEqual((45790, [6], [1]), (meta.tmdb_id, meta.get_season_list(), meta.get_episode_list()))
        self.cache.get_meta_data_by_key.assert_not_called()

    def test_multiseason_pack_preserves_all_seasons(self):
        title = 'Example.Show.S01-S02.1080p'
        self.candidate.update(seasons=[{'n': 1, 'name': 'Season 1'}, {'n': 2, 'name': 'Season 2'}])
        self.info['seasons'] = [{'season_number': 1}, {'season_number': 2}]
        self.responses(title, begin=None, release={'begin_season': 1, 'end_season': 2},
                       decision={'targets': [{'season': 1}, {'season': 2}]})
        meta = self.media.get_media_info(title)
        self.assertFalse(meta.skip_reason)
        self.assertEqual([1, 2], meta.get_season_list())
        self.assertEqual([], meta.get_episode_list())

    def test_semantic_queries_not_rule_stripping_drive_retrieval(self):
        title = '不可按规则切词的续作名 12.mkv'
        self.responses(title, begin=12, release={'queries': ['真正的原作名'], 'en_name': 'Real Work'})
        with patch('app.media.meta.metainfo.MetaVideo', side_effect=AssertionError('rule parser')), \
                patch('app.media.meta.metainfo.MetaAnime', side_effect=AssertionError('anime parser')):
            meta = self.file(title)
        self.assertFalse(meta.skip_reason)
        self.assertEqual('真正的原作名', self.search.call_args_list[0].args[0])

    def test_correction_replaces_whole_identity_including_stale_ends(self):
        title = 'Example S03-S04E01-E37'
        meta = MetaInfo(title, use_llm=False)
        self.responses(title, begin=12, release={'year': 2025, 'en_name': 'Correct Work', 'resource_pix': '2160p'})
        self.parser.merge_into(meta, title)
        self.assertEqual([12], meta.get_episode_list())
        self.assertIsNone(meta.end_episode)
        self.assertIsNone(meta.end_season)
        self.assertEqual(('Correct Work', '2025', '2160p'), (meta.get_name(), meta.year, meta.resource_pix))

    def test_ordinal_and_numeric_movie_take_primary_path(self):
        self.candidate = {'id': 530915, 'type': 'movie', 'name': '1917'}
        self.search.return_value = [self.candidate]
        self.info = {'id': 530915, 'media_type': MediaType.MOVIE, 'title': '1917'}
        title = '1917.mkv'
        self.responses(title, begin=None, release={'type': 'movie', 'en_name': '1917', 'queries': ['1917']},
                       decision={'targets': []})
        meta = self.file(title, tmdb_info=self.info, media_type=MediaType.MOVIE)
        self.assertFalse(meta.skip_reason)
        self.assertEqual((530915, MediaType.MOVIE, []), (meta.tmdb_id, meta.type, meta.get_episode_list()))

    def test_range_mapping_preserves_cardinality(self):
        title = 'Show S04E01-E03.mkv'
        self.responses(title, begin=1, end=3,
                       decision={'targets': [{'season': 4, 'begin_episode': 25, 'end_episode': 27}]})
        meta = self.file(title)
        self.assertFalse(meta.skip_reason)
        self.assertEqual([25, 26, 27], meta.get_episode_list())

    def test_range_collapse_is_rejected(self):
        title = self.filename('25-37')
        self.responses(title, begin=25, end=37, decision={'targets': [{'season': 4, 'begin_episode': 25}]})
        meta = self.file(title)
        self.assertTrue(meta.skip_reason)
        self.assertFalse(meta.tmdb_id)

    def test_bad_decisions_never_fall_back_to_rule_identity(self):
        cases = [ {'status': 'unresolved'}, {'tmdb_id': 999}, {'tmdb_type': 'movie'},
                  {'evidence': ['模型自己生成的译名']}, {'targets': []},
                  {'targets': [{'season': 8, 'begin_episode': 1}]},
                  {'targets': [{'season': 4, 'begin_episode': 38}]},
                  {'targets': [{'season': 4, 'begin_episode': 13.5}]},
                  {'targets': [{'season': True, 'begin_episode': 1}]},
                  {'targets': [{'season': 4, 'begin_episode': 3, 'end_episode': 1}]} ]
        for decision in cases:
            with self.subTest(decision=decision):
                self.parser._parse_cache = {}
                self.responses('Show S04E01.mkv', begin=1, decision=decision)
                meta = self.file('Show S04E01.mkv')
                self.assertTrue(meta.skip_reason)
                self.assertFalse(meta.tmdb_id)

    def test_timeout_malformed_response_and_ambiguous_input_keep_file(self):
        for response in (TimeoutError(), 'not json', '{}', '{"status":"ambiguous"}'):
            with self.subTest(response=type(response).__name__):
                self.parser._parse_cache = {}
                self.client.complete_text.side_effect = response if isinstance(response, Exception) else None
                self.client.complete_text.return_value = response
                meta = self.file('Show.S04E37.mkv')
                self.assertTrue(meta.skip_reason)
                self.assertFalse(meta.tmdb_id)
                self.assertTrue((Path(self.tmp.name) / 'Show.S04E37.mkv').exists())
        self.rule_mapping.assert_not_called()

    def test_incomplete_credentials_do_not_disable_primary_safety(self):
        self.patch(self.parser, '_LLMMetaParser__get_client', return_value=None)
        meta = self.file('Show.S04E37.mkv')
        self.assertTrue(meta.skip_reason)
        self.assertFalse(meta.tmdb_id)

    def test_model_is_not_given_paths_or_unrelated_context_secrets(self):
        title = '37.mkv'
        self.responses(title)
        context = {'tmdb_info': self.info, 'seasons': [4], 'episodes': [37], 'numbering': 'tmdb',
                   'password': 'do-not-send', 'save_path': '/secret/path', 'source_title': 'Dr.STONE Cour 3'}
        meta = self.file(title, context)
        self.assertFalse(meta.skip_reason)
        for call in self.client.complete_text.call_args_list:
            prompt = call.kwargs['user_prompt']
            self.assertNotIn('do-not-send', prompt)
            self.assertNotIn('/secret/path', prompt)
            self.assertNotIn(self.tmp.name, prompt)
        self.assertIn('Dr.STONE Cour 3', self.client.complete_text.call_args.kwargs['user_prompt'])

    def test_same_filename_different_task_has_distinct_parse_cache(self):
        title = '37.mkv'
        self.responses(title)
        first = self.file(title, {'tmdb_info': self.info, 'seasons': [4], 'numbering': 'tmdb'})
        self.responses(title, decision={'status': 'unresolved'})
        second = self.file(title, {'tmdb_info': self.info, 'seasons': [1], 'numbering': 'tmdb'})
        self.assertFalse(first.skip_reason)
        self.assertTrue(second.skip_reason)
        self.assertEqual(4, self.client.complete_text.call_count)

    def test_download_scope_is_checked_without_rewriting_model_number(self):
        title = self.filename(37)
        self.responses(title)
        meta = self.file(title, {'tmdb_info': self.info, 'seasons': [4], 'episodes': [3], 'numbering': 'tmdb'})
        self.assertTrue(meta.skip_reason)
        self.assertEqual([37], meta.get_episode_list())
        self.assertFalse(meta.tmdb_id)

    def test_provider_failure_missing_episode_and_wrong_season_stop_publication(self):
        for detail in ({}, {'season_number': 3, 'episodes': [{'episode_number': 37}]},
                       {'episodes': [{'episode_number': 1}]}, {'episodes': [{'episode_number': 37}] * 2}):
            with self.subTest(detail=detail):
                self.parser._parse_cache = {}
                self.details.side_effect = None
                self.details.return_value = detail
                title = self.filename(37)
                self.responses(title)
                meta = self.file(title)
                self.assertTrue(meta.skip_reason)
                self.assertFalse(meta.tmdb_id)

    def test_special_uses_real_episode_evidence_not_regex_name_match(self):
        title = 'Show [13.5] - The Lost Day.mkv'
        self.details.side_effect = None
        self.details.return_value = {'season_number': 4, 'episodes': [
            {'episode_number': n, 'season_number': 4,
             'name': '失落的一天' if n == 7 else 'Episode %s' % n, 'overview': '总集篇' if n == 7 else ''}
            for n in range(1, 38)]}
        self.responses(title, begin=None, decision={'targets': [{'season': 4, 'begin_episode': 7}]}, extra=[
            {'status': 'confirmed', 'source_evidence': 'The Lost Day', 'provider_evidence': '失落的一天'}])
        meta = self.file(title)
        self.assertFalse(meta.skip_reason)
        self.assertEqual([7], meta.get_episode_list())
        self.assertEqual('confirmed', meta.note['fractional_episode']['status'])
        self.assertEqual(3, self.client.complete_text.call_count)

    def test_special_missing_grounded_episode_evidence_remains_pending(self):
        title = 'Show [OVA01].mkv'
        self.responses(title, begin=1, extra=[{'status': 'confirmed', 'source_evidence': 'OVA01',
                                              'provider_evidence': 'invented evidence'}])
        meta = self.file(title)
        self.assertTrue(meta.skip_reason)
        self.assertFalse(meta.tmdb_id)

    def test_unknown_file_episode_not_filled_from_single_task_episode(self):
        title = 'Show S04.mkv'
        self.responses(title, begin=None)
        meta = self.file(title, {'tmdb_info': self.info, 'seasons': [4], 'episodes': [37], 'numbering': 'tmdb'})
        self.assertTrue(meta.skip_reason)
        self.assertFalse(meta.tmdb_id)

    def test_manual_numbering_remains_an_explicit_override(self):
        self.client.complete_text.side_effect = AssertionError('manual override should not invoke LLM')
        self.patch(self.media, 'save_rename_cache')
        meta = self.file('Show.S04E03.mkv', tmdb_info=self.info, season=4,
                         episode_format=EpisodeFormat('', details='37'))
        self.assertFalse(meta.skip_reason)
        self.assertEqual([37], meta.get_episode_list())

    def test_legacy_rule_path_still_works_when_llm_disabled(self):
        self.parser._enabled = False
        meta = MetaInfo('Show.S04E37.mkv')
        self.assertEqual(([4], [37]), (meta.get_season_list(), meta.get_episode_list()))
        self.client.complete_text.assert_not_called()

    def test_multiseason_collapse_requires_complete_source_season_mapping(self):
        title = 'Example.S01-S02'
        self.candidate['seasons'] = [{'n': 1}, {'n': 2}]
        self.info['seasons'] = [{'season_number': 1}, {'season_number': 2}]
        for targets, accepted in (([{'season': 1}], False),
                                  ([{'season': 1, 'source_seasons': [1, 2]}], True),
                                  ([{'season': 1, 'source_seasons': [1, 1]}], False)):
            with self.subTest(targets=targets):
                self.parser._parse_cache = {}
                self.responses(title, begin=None, release={'begin_season': 1, 'end_season': 2},
                               decision={'targets': targets})
                meta = self.media.get_media_info(title)
                self.assertEqual(accepted, not bool(meta.skip_reason))
                self.assertEqual(accepted, bool(meta.tmdb_id))

    def test_rss_rules_cannot_skip_primary_recognition(self):
        from app.rss import Rss
        rss = Rss.__new__(Rss)
        with patch('app.rss.MetaInfo', side_effect=AssertionError('rule prefilter')):
            self.assertFalse(rss._Rss__should_skip_rss_article_before_identify(
                'Show S01E03', 'offline', {'1': {'name': 'Show', 'season': 'S01'}}))

    def test_download_selection_uses_verified_targets_not_release_numbers(self):
        from app.downloader.downloader import Downloader
        from app.utils.types import DownloaderType
        downloader = object.__new__(type(Downloader()))
        downloader.media = self.media
        pack = MetaInfo('Dr.STONE S04', use_llm=False)
        pack.set_tmdb_info(self.info)
        client = Mock()
        client.get_files.return_value = [{'index': 0, 'name': 'Show.S04E01.mkv'},
                                        {'index': 1, 'name': 'Show.S04E02.mkv'}]
        answers = []
        for source, target in ((1, 25), (2, 26)):
            title = 'Show.S04E%02d.mkv' % source
            self.responses(title, begin=source, decision={'targets': [{'season': 4, 'begin_episode': target}]})
            answers.extend(list(self.client.complete_text.side_effect))
        self.client.complete_text.side_effect = answers
        with patch.object(downloader, '_Downloader__get_client', return_value=client):
            selected = downloader.set_files_status('test', [25], DownloaderType.QB, media_info=pack)
        self.assertEqual([25], selected)
        client.set_files.assert_called_once_with(torrent_hash='test', file_ids=[1], priority=0)
        self.assertEqual(2, self.details.call_count)

    def test_download_context_persists_model_release_seasons(self):
        from app.downloader.downloader import Downloader
        from app.utils.types import DownloaderType
        downloader = object.__new__(type(Downloader()))
        downloader.dbhelper = Mock()
        meta = MetaInfo('Show S04', use_llm=False)
        meta.note['llm'] = {'primary': True, 'release_fields': {'begin_season': 4}}
        meta.begin_season = 1
        meta.set_tmdb_info(self.info)
        with patch('app.downloader.downloader.MetaInfo', side_effect=AssertionError('rule reparse')):
            downloader._create_download_context(meta, DownloaderType.QB)
        payload = downloader.dbhelper.save_download_context.call_args.args[2]
        self.assertEqual([4], payload['release_seasons'])
        self.assertEqual([1], payload['seasons'])

    def test_indexer_filters_against_formal_model_numbering(self):
        import datetime
        from types import SimpleNamespace
        from app.indexer.client._base import _IIndexClient
        from app.filter import Filter
        title = 'Show.S04E01.mkv'
        self.responses(title, begin=1, decision={'targets': [{'season': 4, 'begin_episode': 25}]})
        client = SimpleNamespace(media=self.media, filter=Mock(), progress=Mock(),
                                 _reverse_title_sites=[], index_type='Offline')
        client.filter.check_torrent_filter.return_value = (True, 0, '')
        client.filter.is_torrent_match_sey.side_effect = Filter().is_torrent_match_sey
        result = _IIndexClient.filter_search_results(client, [{'title': title, 'enclosure': 'offline'}], 0,
            SimpleNamespace(id='offline', name='Offline', public=False), {'season': 4, 'episode': 25},
            None, datetime.datetime.now())
        self.assertEqual(1, len(result))
        self.assertEqual([25], result[0].get_episode_list())
        self.assertTrue(result[0].note['llm']['provider_verified'])

    def test_user_replacement_reaches_model_and_original_filename_is_retained(self):
        original, corrected = 'WrongTitle.S04E37.mkv', 'Dr.STONE.S04E37.mkv'
        self.words.process.side_effect = lambda title: (title.replace('WrongTitle', 'Dr.STONE'), [], {})
        self.responses(corrected)
        meta = self.file(original)
        self.assertFalse(meta.skip_reason)
        self.assertEqual(original, meta.org_string)
        self.assertIn(corrected, self.client.complete_text.call_args_list[0].kwargs['user_prompt'])

    def test_primary_uncertainty_reaches_transfer_pending_history(self):
        from app.filetransfer import FileTransfer
        from app.utils.types import RmtMode, SyncType
        self.client.complete_text.return_value = '{"status":"ambiguous"}'
        self.client.complete_text.side_effect = None
        filename = self.filename(37)
        meta = self.file(filename)
        transfer = FileTransfer.__new__(FileTransfer)
        transfer.progress, transfer.dbhelper, transfer.media, transfer.message = Mock(), Mock(), Mock(), Mock()
        transfer._ignore_config_error = ''
        path = str(Path(self.tmp.name) / filename)
        transfer.media.get_media_info_on_files.return_value = {path: meta}
        with patch.object(transfer, 'check_ignore', return_value=([path], '')), \
                patch.object(transfer, '_FileTransfer__transfer_command') as publish:
            success, message = transfer.transfer_media(in_from=SyncType.MAN, in_path=self.tmp.name,
                files=[path], rmt_mode=RmtMode.MOVE, target_dir=self.tmp.name)
        self.assertFalse(success)
        publish.assert_not_called()
        transfer.dbhelper.insert_transfer_history.assert_not_called()
        transfer.dbhelper.insert_transfer_blacklist.assert_not_called()
        transfer.dbhelper.insert_transfer_unknown.assert_called_once()
        self.assertTrue(Path(path).exists())

    def test_conservative_unverified_arc_cannot_default_to_first_season(self):
        self.parser._mode = 'conservative'
        title = 'JoJo Steel Ball Run 01'
        result = {'type': MediaType.ANIME, 'tmdb_id': 86031, 'tmdb_type': 'tv', 'tmdb_season': 6}
        result.update(self.parser._LLMMetaParser__verify_candidate_binding(result, {'tmdb': [self.candidate]}, title))
        meta = MetaInfo(title, use_llm=False)
        with patch.object(self.parser, 'parse', return_value=result):
            self.parser.merge_into(meta, title)
        self.assertFalse(self.media._prepare_media_identity(meta, self.info))
        self.assertFalse(meta.tmdb_id)

    def test_conservative_multiseason_pack_never_gets_scalar_binding(self):
        self.parser._mode = 'conservative'
        title = 'Example.S01-S02'
        self.candidate['seasons'] = [{'n': 1, 'name': 'Season 1'}, {'n': 2, 'name': 'Season 2'}]
        self.info['seasons'] = [{'season_number': 1}, {'season_number': 2}]
        result = {'type': MediaType.TV, 'tmdb_id': 86031, 'tmdb_type': 'tv'}
        result.update(self.parser._LLMMetaParser__verify_candidate_binding(result, {'tmdb': [self.candidate]}, title))
        meta = MetaInfo(title, use_llm=False)
        with patch.object(self.parser, 'parse', return_value=result), \
                patch.object(self.media, '_apply_episode_mapping', return_value=False):
            self.parser.merge_into(meta, title)
            self.assertTrue(self.media._prepare_media_identity(meta, self.info))
        self.assertEqual([1, 2], meta.get_season_list())

    def test_rss_fuzzy_subscription_cannot_accept_unresolved_decision(self):
        from app.rss import Rss
        title = 'Show.S04E37.mkv'
        self.responses(title, decision={'status': 'unresolved'})
        meta = self.media.get_media_info(title)
        rss = Rss.__new__(Rss)
        matched, messages, subscription = rss.check_torrent_rss(meta, {},
            {'1': {'name': 'Show', 'fuzzy_match': True}}, {}, None, None, None, None)
        self.assertFalse(matched)
        self.assertEqual([meta.skip_reason], messages)
        self.assertEqual({}, subscription)
