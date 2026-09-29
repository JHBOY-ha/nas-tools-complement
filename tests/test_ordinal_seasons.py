"""Ordinal words in titles must not become unverified seasons or season packs."""
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

from app.media.media import Media
from app.media.meta import MetaInfo
from app.media.meta.special import download_block_reason
from app.utils.types import MediaType


class OrdinalSeasonTest(TestCase):
    def setUp(self):
        self.media = Media.__new__(Media)
        self.media.tmdb = True
        self.media.meta = Mock()
        self.media.meta.get_meta_data_by_key.return_value = {}
        self.media.get_tmdb_tv_season_detail = Mock(return_value={
            "episodes": [{"episode_number": n} for n in range(1, 25)]})
        config = patch('app.media.media.Config').start()
        config.return_value.get_config.return_value = {"episode_mappings": []}
        self.addCleanup(patch.stopall)

    @staticmethod
    def info(name, season, ident):
        return {"name": name, "id": ident, "media_type": MediaType.TV,
                "genres": [{"id": 16}], "first_air_date": "2020-01-01",
                "seasons": [{"season_number": season, "episode_count": 24}]}

    def candidates(self, *works):
        by_name = {w['name'].casefold(): w for w in works}
        self.media.get_tmdb_search_page = Mock(side_effect=lambda **kw:
            [by_name[kw['title'].casefold()]] if kw['title'].casefold() in by_name else [])
        self.media.get_tmdb_info = Mock(side_effect=lambda **kw:
            next((w for w in works if w['id'] == kw['tmdbid']), {}))

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

    def test_bare_number_is_preserved_but_not_downloadable_before_confirmation(self):
        for digits in ('5', '05', '12'):
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
        self.candidates(self.info('Major 2nd', 2, 2))
        resolved = self.media.get_media_info('Major 2nd Season 2', cache=False)
        self.assertEqual(('major 2nd', 2, []),
                         (resolved.get_name().casefold(), resolved.begin_season, resolved.get_episode_list()))
        self.assertEqual(2, resolved.tmdb_id)
        self.assertFalse(resolved.skip_reason)

    def test_public_lookup_confirms_single_episode_with_tmdb_evidence(self):
        self.candidates(self.info('Show', 4, 1))
        resolved = self.media.get_media_info('Show 4th Season 5 1080p', cache=False)
        self.assertEqual((1, 4, [5]), (resolved.tmdb_id, resolved.begin_season, resolved.get_episode_list()))
        self.assertFalse(download_block_reason(resolved))
        self.media.get_tmdb_tv_season_detail.return_value = {'episodes': [{'episode_number': 1}]}
        rejected = self.media.get_media_info('Show 4th Season 5 1080p', cache=False)
        self.assertTrue(download_block_reason(rejected))
        self.assertFalse(rejected.tmdb_id)

    def test_unbound_file_lookup_and_incomplete_episode_lists_fail_safely(self):
        self.candidates(self.info('Show', 4, 1))
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
