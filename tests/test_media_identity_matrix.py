"""Offline identity integration matrix using real parsers and public Media entry points.

The IDs, aliases, genres and episode inventories below are controlled provider
fixtures, not live TMDB records. Only TMDB, LLM and persistent identity-cache I/O
are replaced; parsing, candidate selection, type classification and cache keys run
the production code. Run with ``python3 -m tests.run_recognition
tests.test_media_identity_matrix`` to obtain an isolated database and no network.
"""

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.media.media import Media
from app.media.meta.llm_parser import LLMMetaParser
from app.media.tmdbv3api import TMDbException
from app.utils.types import MatchMode, MediaType


def movie_fixture(identifier, title, original, year, genres):
    return {"id": identifier, "title": title, "original_title": original,
            "release_date": "%s-01-01" % year,
            "genres": [{"id": genre} for genre in genres],
            "alternative_titles": {"titles": []}, "translations": {"translations": []}}


def series_fixture(identifier, title, original, year, genres, season_count):
    return {"id": identifier, "name": title, "original_name": original,
            "first_air_date": "%s-01-01" % year,
            "genres": [{"id": genre} for genre in genres],
            "seasons": [{"season_number": season, "episode_count": 12,
                         "air_date": "%s-01-01" % (year + season - 1)}
                        for season in range(1, season_count + 1)],
            "alternative_titles": {"results": []}, "translations": {"translations": []}}


FIXTURES = {
    910001: movie_fixture(910001, "降临", "Arrival", 2016, [18, 878]),
    910002: movie_fixture(910002, "1917", "1917", 2019, [18, 10752]),
    910003: movie_fixture(910003, "第39号案件", "Case39", 2009, [27]),
    910004: movie_fixture(910004, "寻梦环游记", "Coco", 2017, [16, 10751]),
    910005: movie_fixture(910005, "千与千寻", "千と千尋の神隠し", 2001, [16, 14]),
    920001: series_fixture(920001, "苍穹浩瀚", "The Expanse", 2015, [18, 10765], 3),
    930001: series_fixture(930001, "孤独摇滚", "Bocchi the Rock!", 2022, [16, 35], 1),
    930002: series_fixture(930002, "86", "86", 2021, [16, 10765], 2),
}


@dataclass(frozen=True)
class IdentityCase:
    name: str
    filename: str
    identifier: int
    media_type: MediaType
    seasons: tuple = ()
    episodes: tuple = ()
    hint: MediaType = None


# Expected identities and numbering are authored from each filename's explicit
# meaning and the fixture catalog, never obtained by recording parser output.
SUCCESS_CASES = (
    IdentityCase("movie_dotted", "Arrival.2016.1080p.BluRay.x264.mkv", 910001, MediaType.MOVIE),
    IdentityCase("movie_chinese", "降临 (2016) 1080p.mkv", 910001, MediaType.MOVIE),
    IdentityCase("movie_no_year", "Arrival.mkv", 910001, MediaType.MOVIE),
    IdentityCase("movie_cut", "Arrival.Extended.Cut.2016.1080p.BluRay.mkv", 910001, MediaType.MOVIE),
    IdentityCase("movie_title_digits", "Case39.2009.1080p.mkv", 910003, MediaType.MOVIE),
    IdentityCase("animated_movie", "Coco.2017.1080p.BluRay.mkv", 910004, MediaType.MOVIE),
    IdentityCase("animated_movie_chinese", "千与千寻.2001.1080p.mkv", 910005, MediaType.MOVIE),
    IdentityCase("tv_first_season", "The.Expanse.S01E02.1080p.WEB-DL.mkv", 920001, MediaType.TV, (1,), (2,)),
    IdentityCase("tv_later_season", "The Expanse S02E03.mkv", 920001, MediaType.TV, (2,), (3,)),
    IdentityCase("tv_x_notation", "The Expanse 2x03.mkv", 920001, MediaType.TV, (2,), (3,)),
    IdentityCase("tv_written_markers", "The Expanse Season 2 Episode 03.mkv", 920001, MediaType.TV, (2,), (3,)),
    IdentityCase("tv_episode_range", "The Expanse S01E01-E03.mkv", 920001, MediaType.TV, (1,), (1, 2, 3)),
    IdentityCase("tv_chinese", "苍穹浩瀚 第2季 第03集.mkv", 920001, MediaType.TV, (2,), (3,)),
    IdentityCase("tv_season_pack", "The Expanse S01 Complete.mkv", 920001, MediaType.TV, (1,), ()),
    IdentityCase("anime_release_number", "[Group] Bocchi the Rock! - 01 [1080p][HEVC].mkv", 930001, MediaType.ANIME, (1,), (1,)),
    IdentityCase("anime_final", "[Group] Bocchi the Rock! [12 END] [1080p].mkv", 930001, MediaType.ANIME, (1,), (12,)),
    IdentityCase("anime_tv_notation", "Bocchi the Rock! S01E02.mkv", 930001, MediaType.ANIME, (1,), (2,)),
    IdentityCase("anime_episode_range", "Bocchi the Rock! S01E01-E03.mkv", 930001, MediaType.ANIME, (1,), (1, 2, 3)),
    IdentityCase("anime_chinese", "孤独摇滚 S01E02.mkv", 930001, MediaType.ANIME, (1,), (2,)),
    IdentityCase("anime_numeric_title", "[Group] 86 - 01 [1080p].mkv", 930002, MediaType.ANIME, (1,), (1,)),
    IdentityCase("anime_numeric_title_later_season", "[Group] 86 S02E02 [1080p].mkv", 930002, MediaType.ANIME, (2,), (2,)),
)


class OfflineProvider:
    """A deterministic TMDB boundary; all Media search and detail methods remain real."""

    def __init__(self):
        self.records = deepcopy(FIXTURES)
        self.search_mode = "normal"
        self.detail_mode = "normal"
        self.search = Mock(total_results=0)
        self.search.movies.side_effect = lambda params: self._search(params, "movie")
        self.search.tv_shows.side_effect = lambda params: self._search(params, "tv")
        self.search.multi.side_effect = lambda params: self._search(params, None)
        self.movie = Mock()
        self.movie.details.side_effect = lambda identifier, *args: self._detail(identifier, "movie")
        self.tv = Mock()
        self.tv.details.side_effect = lambda identifier, *args: self._detail(identifier, "tv")
        self.tv.season_details.side_effect = self._season

    @staticmethod
    def normalized(value):
        return re.sub(r"[\W_]", "", value or "").casefold()

    def _search(self, params, kind):
        if self.search_mode == "error":
            raise TMDbException("offline fixture: search unavailable")
        results = []
        if self.search_mode != "empty":
            query = self.normalized(params["query"])
            for record in self.records.values():
                record_kind = "movie" if "title" in record else "tv"
                if kind and kind != record_kind:
                    continue
                names = (record.get("title"), record.get("original_title"),
                         record.get("name"), record.get("original_name"))
                if query not in {self.normalized(name) for name in names if name}:
                    continue
                # Real search responses omit full genres/seasons; details must be loaded.
                candidate = {key: deepcopy(value) for key, value in record.items()
                             if key in ("id", "title", "original_title", "release_date",
                                        "name", "original_name", "first_air_date")}
                candidate["genre_ids"] = [genre["id"] for genre in record["genres"]]
                candidate["media_type"] = record_kind
                results.append(candidate)
        self.search.total_results = len(results)
        return results

    def _detail(self, identifier, kind):
        if self.detail_mode == "error":
            raise TMDbException("offline fixture: details unavailable")
        if self.detail_mode == "empty":
            return {}
        record = self.records.get(int(identifier)) or {}
        if record and (("title" in record) != (kind == "movie")):
            return {}
        return deepcopy(record)

    def _season(self, identifier, season):
        record = self.records.get(int(identifier)) or {}
        if not any(item["season_number"] == season for item in record.get("seasons", [])):
            return {}
        return {"season_number": season,
                "episodes": [{"id": int(identifier) * 1000 + season * 100 + number,
                              "season_number": season, "episode_number": number,
                              "name": "Fixture episode %s" % number}
                             for number in range(1, 13)]}

    def search_count(self):
        return sum(method.call_count for method in
                   (self.search.movies, self.search.tv_shows, self.search.multi))


class MediaIdentityMatrixTest(unittest.TestCase):
    def setUp(self):
        # Keep real LLM merge behavior; an empty external answer leaves rule evidence intact.
        llm = LLMMetaParser()
        patcher = patch.object(llm, "parse", return_value={})
        self.addCleanup(patcher.stop)
        self.llm_parse = patcher.start()

    @staticmethod
    def make_media():
        provider = OfflineProvider()
        media = Media.__new__(Media)
        media.tmdb = SimpleNamespace(language="zh-CN")
        media.search, media.movie, media.tv = provider.search, provider.movie, provider.tv
        media._rmt_match_mode = MatchMode.NORMAL
        media._search_keyword = media._search_tmdbweb = False
        cache = {}
        media.meta = Mock()
        media.meta.get_meta_data_by_key.side_effect = lambda key: deepcopy(cache.get(key, {}))
        media.meta.update_meta_data.side_effect = lambda values: cache.update(deepcopy(values))
        return media, provider, cache

    def assert_identity(self, meta, case):
        self.assertIsNotNone(meta)
        fixture = FIXTURES[case.identifier]
        self.assertEqual(case.identifier, meta.tmdb_id)
        self.assertEqual(case.media_type, meta.type)
        self.assertEqual(fixture.get("title") or fixture["name"], meta.title)
        self.assertEqual(case.seasons, tuple(meta.get_season_list()))
        self.assertEqual(case.episodes, tuple(meta.get_episode_list()))
        self.assertFalse(meta.skip_reason)
        self.assertEqual(case.identifier, meta.tmdb_info["id"])

    @staticmethod
    def file_result(media, filename, **kwargs):
        # No parser or filesystem mocks: the file exists in a real temporary directory.
        with tempfile.TemporaryDirectory(prefix="identity-matrix-") as directory:
            path = Path(directory) / filename
            path.write_bytes(b"offline media fixture")
            result = media.get_media_info_on_files([str(path)], **kwargs).get(str(path))
            if path.read_bytes() != b"offline media fixture":
                raise AssertionError("recognition modified the source")
            return result

    def test_public_title_success_matrix(self):
        for case in SUCCESS_CASES:
            with self.subTest(case=case.name):
                media, provider, _ = self.make_media()
                self.assert_identity(media.get_media_info(case.filename, mtype=case.hint), case)
                self.assertGreater(provider.search_count(), 0)

    def test_real_file_success_matrix(self):
        for case in SUCCESS_CASES:
            with self.subTest(case=case.name):
                media, provider, _ = self.make_media()
                self.assert_identity(self.file_result(media, case.filename), case)
                self.assertGreater(provider.search_count(), 0)

    def test_title_and_file_cache_preserve_the_same_identity(self):
        for case in SUCCESS_CASES:
            with self.subTest(case=case.name):
                media, provider, cache = self.make_media()
                self.assert_identity(media.get_media_info(case.filename), case)
                search_count = provider.search_count()
                self.assertTrue(cache)
                self.assert_identity(media.get_media_info(case.filename), case)
                self.assert_identity(self.file_result(media, case.filename), case)
                self.assertEqual(search_count, provider.search_count(),
                                 "warm identity cache should avoid repeating title searches")

    def test_explicit_numeric_movie_identity_in_both_entry_points(self):
        case = IdentityCase("numeric_movie", "1917.mkv", 910002, MediaType.MOVIE, hint=MediaType.MOVIE)
        media, _, _ = self.make_media()
        self.assert_identity(media.get_media_info(case.filename, mtype=case.hint), case)
        self.assert_identity(self.file_result(media, case.filename, media_type=case.hint), case)
        bound = media.get_tmdb_info(MediaType.MOVIE, case.identifier)
        self.assert_identity(self.file_result(media, case.filename, tmdb_info=bound), case)

    def test_explicit_type_hints_keep_valid_catalog_identities(self):
        for case in (SUCCESS_CASES[0], SUCCESS_CASES[5], SUCCESS_CASES[7], SUCCESS_CASES[16]):
            with self.subTest(case=case.name):
                media, _, _ = self.make_media()
                self.assert_identity(media.get_media_info(case.filename, mtype=case.media_type), case)
                self.assert_identity(self.file_result(media, case.filename, media_type=case.media_type), case)

    def test_animation_genre_does_not_turn_movies_into_series(self):
        for case in (SUCCESS_CASES[5], SUCCESS_CASES[6], SUCCESS_CASES[16]):
            with self.subTest(case=case.name):
                media, _, _ = self.make_media()
                result = media.get_media_info(case.filename)
                self.assert_identity(result, case)
                self.assertIn(16, result.tmdb_info["genre_ids"])
                expected_provider_type = MediaType.MOVIE if case.media_type == MediaType.MOVIE else MediaType.TV
                self.assertEqual(expected_provider_type, result.tmdb_info["media_type"])

    def test_empty_and_failed_provider_leave_each_media_family_unidentified(self):
        for case in (SUCCESS_CASES[0], SUCCESS_CASES[7], SUCCESS_CASES[16]):
            for mode in ("empty_search", "failed_search", "empty_details", "failed_details"):
                for entry in ("title", "file"):
                    with self.subTest(case=case.name, mode=mode, entry=entry):
                        media, provider, cache = self.make_media()
                        provider.search_mode = {"empty_search": "empty", "failed_search": "error"}.get(mode, "normal")
                        provider.detail_mode = {"empty_details": "empty", "failed_details": "error"}.get(mode, "normal")
                        result = (media.get_media_info(case.filename) if entry == "title"
                                  else self.file_result(media, case.filename))
                        self.assertIsNotNone(result)
                        self.assertFalse(result.tmdb_id)
                        self.assertFalse(result.tmdb_info)
                        self.assertFalse(any(value.get("id") for value in cache.values()))

    def test_explicit_anime_hint_rejects_live_action_candidate(self):
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                media, provider, cache = self.make_media()
                provider.records[930001]["genres"] = [{"id": 18}]
                filename = "Bocchi the Rock! S01E02.mkv"
                result = (media.get_media_info(filename, mtype=MediaType.ANIME) if entry == "title"
                          else self.file_result(media, filename, media_type=MediaType.ANIME))
                self.assertIsNotNone(result)
                self.assertFalse(result.tmdb_id)
                self.assertFalse(result.tmdb_info)
                self.assertEqual(MediaType.ANIME, result.type)
                self.assertFalse(cache)

    def test_wrong_type_cache_cannot_replace_the_requested_series(self):
        cases = ((SUCCESS_CASES[0], "[电影]Arrival-2016-None", 920001, MediaType.TV),
                 (SUCCESS_CASES[7], "[电视剧]The Expanse-None-1", 910001, MediaType.MOVIE),
                 (SUCCESS_CASES[16], "[电视剧]Bocchi The Rock!-None-1", 910001, MediaType.MOVIE))
        for case, key, wrong_id, wrong_type in cases:
            for entry in ("title", "file"):
                with self.subTest(case=case.name, entry=entry):
                    media, provider, cache = self.make_media()
                    # The stale key is authored from the filename, not computed by the tested code.
                    cache[key] = {"id": wrong_id, "type": wrong_type, "title": "Stale", "year": "2016"}
                    provider.search_mode = "empty"
                    result = (media.get_media_info(case.filename) if entry == "title"
                              else self.file_result(media, case.filename))
                    media.meta.get_meta_data_by_key.assert_any_call(key)
                    self.assertFalse(result.tmdb_id)
                    self.assertFalse(result.tmdb_info)
                    provider.movie.details.assert_not_called()
                    provider.tv.details.assert_not_called()

    def test_cached_identity_does_not_hide_a_failed_details_request(self):
        for case in (SUCCESS_CASES[0], SUCCESS_CASES[7], SUCCESS_CASES[16]):
            for entry in ("title", "file"):
                with self.subTest(case=case.name, entry=entry):
                    media, provider, _ = self.make_media()
                    self.assert_identity(media.get_media_info(case.filename), case)
                    search_count = provider.search_count()
                    provider.detail_mode = "error"
                    result = (media.get_media_info(case.filename) if entry == "title"
                              else self.file_result(media, case.filename))
                    self.assertFalse(result.tmdb_id)
                    self.assertFalse(result.tmdb_info)
                    self.assertEqual(search_count, provider.search_count())

    def test_cached_anime_identity_rechecks_the_animation_genre(self):
        for entry in ("title", "file"):
            with self.subTest(entry=entry):
                media, provider, _ = self.make_media()
                case = SUCCESS_CASES[16]
                self.assert_identity(media.get_media_info(case.filename, mtype=MediaType.ANIME), case)
                provider.records[930001]["genres"] = [{"id": 18}]
                result = (media.get_media_info(case.filename, mtype=MediaType.ANIME) if entry == "title"
                          else self.file_result(media, case.filename, media_type=MediaType.ANIME))
                self.assertFalse(result.tmdb_id)
                self.assertFalse(result.tmdb_info)
                self.assertEqual(MediaType.ANIME, result.type)

    def test_explicit_movie_hint_does_not_bind_a_same_name_series(self):
        for filename in ("Arrival.2016.1080p.mkv", "Arrival.mkv"):
            for entry in ("title", "file"):
                with self.subTest(filename=filename, entry=entry):
                    media, provider, cache = self.make_media()
                    provider.records = {940001: series_fixture(940001, "降临", "Arrival", 2016, [18], 1)}
                    result = (media.get_media_info(filename, mtype=MediaType.MOVIE) if entry == "title"
                              else self.file_result(media, filename, media_type=MediaType.MOVIE))
                    self.assertIsNotNone(result)
                    self.assertFalse(result.tmdb_id)
                    self.assertFalse(result.tmdb_info)
                    self.assertEqual(MediaType.MOVIE, result.type)
                    self.assertFalse(cache)

    def test_unknown_type_can_still_discover_a_same_name_series(self):
        # A parser's default MOVIE does not mean the user explicitly selected a film.
        for filename in ("Arrival.2016.1080p.mkv", "Arrival.mkv"):
            for entry in ("title", "file"):
                with self.subTest(filename=filename, entry=entry):
                    media, provider, _ = self.make_media()
                    provider.records = {940001: series_fixture(940001, "降临", "Arrival", 2016, [18], 1)}
                    result = (media.get_media_info(filename) if entry == "title"
                              else self.file_result(media, filename))
                    self.assertEqual(940001, result.tmdb_id)
                    self.assertEqual(MediaType.TV, result.type)
                    self.assertEqual("降临", result.title)
                    self.assertEqual([1], result.get_season_list())
                    self.assertEqual([], result.get_episode_list())

    def test_trusted_download_context_recognizes_short_filenames(self):
        cases = (IdentityCase("bound_movie", "video.mkv", 910001, MediaType.MOVIE),
                 IdentityCase("bound_tv", "03.mkv", 920001, MediaType.TV, (2,), (3,)),
                 IdentityCase("bound_anime", "03.mkv", 930001, MediaType.ANIME, (1,), (3,)))
        for case in cases:
            with self.subTest(case=case.name):
                media, provider, _ = self.make_media()
                provider_type = MediaType.MOVIE if case.media_type == MediaType.MOVIE else MediaType.TV
                bound = media.get_tmdb_info(provider_type, case.identifier)
                context = {"tmdb_info": bound, "seasons": list(case.seasons),
                           "episodes": list(case.episodes), "numbering": "tmdb"}
                self.assert_identity(self.file_result(media, case.filename, download_context=context), case)
                self.assertEqual(0, provider.search_count())


if __name__ == "__main__":
    unittest.main()
