"""Offline regressions for candidate selection and atomic season/episode recognition."""

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.media.media import Media
from app.media.meta import MetaInfo
from app.utils.episode_format import EpisodeFormat
from app.utils.types import MediaType


class MediaRecognitionIntegrityTest(unittest.TestCase):
    def test_null_or_missing_animation_genres_fail_closed(self):
        # Empty normalized genre_ids must not fall through to iterating null genres.
        for genres in ({}, {"genres": None}, {"genres": [], "genre_ids": []},
                       {"genres": None, "genre_ids": []}):
            info = dict(self._info())
            info.pop("genres")
            info.update(genres)
            for hint in (None, MediaType.ANIME):
                meta = MetaInfo("Show S01E01", mtype=MediaType.ANIME, use_llm=False)
                with self.subTest(genres=genres, hint=hint):
                    self.assertFalse(self.media._prepare_media_identity(meta, info, mtype_hint=hint))
                    self.assertEqual([1], meta.get_episode_list())

    def test_invalid_manual_season_fails_before_file_processing(self):
        for season in ("S01", "abc", -1, "-1", True, False, 1.5, [], {}):
            with self.subTest(season=season), patch.object(self.media, "save_rename_cache") as save:
                with self.assertRaisesRegex(ValueError, "季号参数无效"):
                    self.media.get_media_info_on_files([], season=season)
                save.assert_not_called()

    def test_manual_season_strings_preserve_zero_and_auto_mode(self):
        for raw, expected in ((None, None), ("", None), (0, 0), ("0", 0),
                              ("01", 1), (" 2 ", 2)):
            with self.subTest(season=raw):
                self.assertEqual(expected, EpisodeFormat.normalize_season(raw))

    def _patch(self, target):
        patcher = patch(target)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def setUp(self):
        self.media = Media.__new__(Media)
        self.media.tmdb = object()
        # Recognition tests need neither persistent custom words nor live LLM requests.
        words = self._patch("app.media.meta.metainfo.WordsHelper")
        words.return_value.process.side_effect = lambda title: (title, [], {})
        llm = self._patch("app.media.meta.metainfo.LLMMetaParser")
        llm.return_value.merge_into.side_effect = lambda **kwargs: kwargs["meta_info"]
        config = self._patch("app.media.media.Config")
        config.return_value.get_config.return_value = {"episode_mappings": []}
        self.config = config
        self._patch("app.media.media.log")

    @staticmethod
    def _info():
        return {"id": 65942, "media_type": MediaType.TV,
                "name": "Re：从零开始的异世界生活", "genres": [{"id": 16}],
                "first_air_date": "2016-04-04",
                "seasons": [{"season_number": 1, "episode_count": 85}]}

    @staticmethod
    def _unverified_episode_meta():
        meta = MetaInfo("Re：从零开始的异世界生活 S04E20 [1080p]", use_llm=False)
        meta.note = {"llm": {"tmdb_id": 65942, "tmdb_type": "tv",
                             "candidate_verified": True, "tmdb_season": 1,
                             "season_verified": True, "season_evidence": "llm_only",
                             "release_season": 4}}
        return meta

    def test_rejected_llm_season_restores_release_numbers_and_evidence(self):
        meta = self._unverified_episode_meta()
        original_note = deepcopy(meta.note)
        episodes = {"episodes": [{"episode_number": number} for number in range(1, 86)]}
        with patch.object(self.media, "get_tmdb_tv_season_detail", return_value=episodes):
            self.assertFalse(self.media._prepare_media_identity(meta, self._info()))
        self.assertEqual((4, 20), (meta.begin_season, meta.begin_episode))
        self.assertEqual(original_note, meta.note)

    def test_public_name_retry_cannot_accept_a_previously_rejected_remap(self):
        meta = self._unverified_episode_meta()
        original_note = deepcopy(meta.note)
        episodes = {"episodes": [{"episode_number": number} for number in range(1, 86)]}

        def search_name(**kwargs):
            # A retry must receive the filename's release numbering, never a failed candidate's.
            self.assertEqual((4, 20), (kwargs["meta_info"].begin_season,
                                      kwargs["meta_info"].begin_episode))
            return self._info()

        with patch("app.media.media.MetaInfo", return_value=meta), \
                patch.object(self.media, "get_tmdb_info", return_value=self._info()), \
                patch.object(self.media, "get_tmdb_tv_season_detail", return_value=episodes), \
                patch.object(self.media, "_Media__search_media_with_name", side_effect=search_name) as search, \
                patch.object(self.media, "_Media__insert_media_cache") as cache:
            result = self.media.get_media_info(meta.org_string, cache=False)
        search.assert_called_once()
        cache.assert_not_called()
        self.assertFalse(result.tmdb_id)
        self.assertEqual((4, 20), (result.begin_season, result.begin_episode))
        self.assertEqual(original_note, result.note)

    def test_late_candidate_rejection_restores_the_original_episode_range(self):
        meta = MetaInfo("Show S04E01-E03.mkv", use_llm=False)
        meta.note["release"] = {"title": "Show"}
        original_note = deepcopy(meta.note)
        self.config.return_value.get_config.return_value = {"episode_mappings": [{
            "tmdb_id": 65942, "source_season": 4, "source_begin": 1, "source_end": 19,
            "target_season": 1, "offset": 66}]}
        with patch.object(self.media, "get_tmdb_tv_season_detail", return_value={
                "episodes": [{"episode_number": number} for number in range(67, 70)]}), \
                patch.object(self.media, "_valid_media_identity", return_value=False):
            self.assertFalse(self.media._prepare_media_identity(meta, self._info()))
        self.assertEqual((4, 1, 3, 3), (meta.begin_season, meta.begin_episode,
                                       meta.end_episode, meta.total_episodes))
        self.assertEqual(original_note, meta.note)

    def test_mapping_error_restores_nested_evidence(self):
        meta = MetaInfo("Show S04E01.mkv", use_llm=False)
        meta.note["release"] = {"season": 4}

        def failed_mapping(candidate, _info):
            candidate.begin_season = 1
            candidate.note["release"]["season"] = 1
            raise ValueError("incomplete episode evidence")

        with patch.object(self.media, "_apply_episode_mapping", side_effect=failed_mapping):
            self.assertFalse(self.media._prepare_media_identity(meta, self._info()))
        self.assertEqual(4, meta.begin_season)
        self.assertEqual({"release": {"season": 4}}, meta.note)

    def test_exact_movie_title_beats_an_earlier_fuzzy_candidate(self):
        for year in (None, "2020"):
            for title_field in ("title", "original_title"):
                with self.subTest(year=year, title_field=title_field):
                    self.media.search = Mock(total_results=2)
                    self.media.search.movies.return_value = [
                        {"id": 1, "title": "Example Movie II", "release_date": "2020-01-01"},
                        {"id": 2, title_field: "Example Movie", "release_date": "2020-01-01"}]
                    result = self.media._Media__search_movie_by_name("Example Movie", year)
                    self.assertEqual(2, result["id"])

    def test_exact_movie_title_still_respects_the_requested_year(self):
        self.media.search = Mock(total_results=2)
        self.media.search.movies.return_value = [
            {"id": 1, "title": "Example Movie II", "release_date": "2020-01-01"},
            {"id": 2, "title": "Example Movie", "release_date": "2021-01-01"}]
        result = self.media._Media__search_movie_by_name("Example Movie", "2020")
        self.assertEqual(1, result["id"])

    def _manual_file(self, episode_format, filename="Show S01E01-E03.mkv", context=None, season=None):
        info = {"id": 42, "name": "Show", "media_type": MediaType.TV,
                "seasons": [{"season_number": 1}, {"season_number": 2}]}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(self.media, "save_rename_cache"), \
                patch("app.media.media.PathUtils.get_bluray_dir", return_value=None):
            path = Path(directory) / filename
            path.write_bytes(b"source video")
            results = self.media.get_media_info_on_files(
                [str(path)], tmdb_info=info, episode_format=episode_format,
                download_context=context, season=season)
            # Recognition never consumes the source, including a rejected manual override.
            self.assertEqual(b"source video", path.read_bytes())
            return results.get(str(path))

    def test_manual_single_episode_replaces_the_entire_parsed_range(self):
        for episode in (2, 8):
            with self.subTest(episode=episode):
                meta = self._manual_file(EpisodeFormat(None, str(episode)))
                self.assertEqual([episode], meta.get_episode_list())
                self.assertIsNone(meta.end_episode)
                self.assertEqual(1, meta.total_episodes)

    def test_manual_range_updates_total_episodes(self):
        meta = self._manual_file(EpisodeFormat(None, "8-11"))
        self.assertEqual([8, 9, 10, 11], meta.get_episode_list())
        self.assertEqual(4, meta.total_episodes)

    def test_empty_manual_format_preserves_the_parsed_range(self):
        meta = self._manual_file(EpisodeFormat(None))
        self.assertEqual([1, 2, 3], meta.get_episode_list())
        self.assertEqual(3, meta.total_episodes)

    def test_manual_episode_must_still_match_the_download_task(self):
        for episode, accepted in ((2, True), (5, False)):
            with self.subTest(episode=episode):
                meta = self._manual_file(EpisodeFormat(None, str(episode)),
                                         filename="Show S01E02.mkv",
                                         context={"seasons": [1], "episodes": [2]})
                if accepted:
                    self.assertEqual([2], meta.get_episode_list())
                else:
                    self.assertIsNone(meta)

    def test_manual_episode_can_change_within_download_task_constraints(self):
        meta = self._manual_file(EpisodeFormat(None, "3"), filename="Show S01E02.mkv",
                                 context={"seasons": [1], "episodes": [2, 3]})
        self.assertEqual([3], meta.get_episode_list())
        self.assertEqual(1, meta.total_episodes)

    def test_manual_season_cannot_override_the_download_task(self):
        meta = self._manual_file(EpisodeFormat(None), filename="Show S01E02.mkv", season=2,
                                 context={"seasons": [1], "episodes": [2]})
        self.assertIsNone(meta)

    def test_manual_season_replaces_the_original_season_range(self):
        for filename in ("某剧 第1-3季 第02集.mkv", "Show S01-S03"):
            with self.subTest(filename=filename):
                meta = self._manual_file(EpisodeFormat(None, "2"), filename=filename, season=5)
                self.assertEqual([5], meta.get_season_list())
                self.assertIsNone(meta.end_season)
                self.assertEqual(1, meta.total_seasons)

    def test_manual_season_zero_is_explicit_and_bypasses_automatic_mapping(self):
        for season in (0, "0"):
            with self.subTest(season=season), \
                    patch.object(self.media, "_apply_episode_mapping",
                                 side_effect=ValueError("automatic target unavailable")) as mapping:
                meta = self._manual_file(EpisodeFormat(None), filename="Show S01E02.mkv", season=season)
                mapping.assert_not_called()
                self.assertEqual([0], meta.get_season_list())
                self.assertIsNone(meta.end_season)
                self.assertEqual(1, meta.total_seasons)
                self.assertEqual([2], meta.get_episode_list())

    def test_empty_or_absent_manual_season_preserves_the_original_range(self):
        for season in (None, ""):
            with self.subTest(season=season):
                meta = self._manual_file(EpisodeFormat(None, "2"),
                                         filename="某剧 第1-3季 第02集.mkv", season=season)
                self.assertEqual([1, 2, 3], meta.get_season_list())
                self.assertEqual(3, meta.total_seasons)

    def test_manual_season_zero_still_respects_download_task_constraints(self):
        meta = self._manual_file(EpisodeFormat(None), filename="Show S01E02.mkv", season=0,
                                 context={"seasons": [1], "episodes": [2]})
        self.assertIsNone(meta)


if __name__ == "__main__":
    unittest.main()
