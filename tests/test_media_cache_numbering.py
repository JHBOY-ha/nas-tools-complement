"""Cached identities must preserve the same numbering checks as fresh searches."""
import datetime
from copy import deepcopy
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.indexer.client._base import _IIndexClient
from app.media.media import Media
from app.media.meta import MetaInfo
from app.rss import Rss
from app.utils.types import MediaType
from tests.test_sync_reliability import load_class


class CacheNumberingTest(TestCase):
    def setUp(self):
        # Exercise real recognition and cache keys without a configured LLM or server.
        words = patch("app.media.meta.metainfo.WordsHelper").start()
        words.return_value.process.side_effect = lambda title: (title, [], {})
        llm = patch("app.media.meta.metainfo.LLMMetaParser").start()
        llm.return_value.merge_into.side_effect = lambda **kwargs: kwargs["meta_info"]
        self.addCleanup(patch.stopall)
        config = patch("app.media.media.Config").start()
        config.return_value.get_config.return_value = {"episode_mappings": [
            {"tmdb_id": 42, "source_season": 4, "source_begin": 1, "source_end": 19,
             "target_season": 1, "offset": 66}]}
        self.info = {"id": 42, "name": "Show", "media_type": MediaType.TV,
                     "genres": [{"id": 18}], "first_air_date": "2020-01-01",
                     "seasons": [{"season_number": 1, "episode_count": 85}]}
        self.media = Media.__new__(Media)
        self.media.tmdb = object()
        self.media.meta = Mock()
        self.cache = {}
        self.media.meta.get_meta_data_by_key.side_effect = lambda key: self.cache.get(key, {})
        self.media.meta.update_meta_data.side_effect = self.cache.update
        self.media._Media__search_media_with_name = Mock(return_value=self.info)
        self.media.get_tmdb_info = Mock(side_effect=lambda **kwargs: deepcopy(self.info))
        self.media.get_tmdb_tv_season_detail = Mock(return_value={
            "episodes": [{"episode_number": number} for number in range(1, 86)]})
        first = self.media.get_media_info("Show S04E01 1080p")
        self.assertEqual((1, 67), (first.begin_season, first.begin_episode))
        self.title = "Show S04E02 1080p"

    def assert_formal_numbering(self, meta):
        self.assertEqual((42, 1, 68), (meta.tmdb_id, meta.begin_season, meta.begin_episode))
        self.assertEqual([2], meta.note["episode_mapping"]["source_episodes"])
        # A cache hit still avoids the second work-name search.
        self.media._Media__search_media_with_name.assert_called_once()

    def test_rss_cached_resource_reaches_subscription_with_formal_numbering(self):
        rss = Rss.__new__(Rss)
        rss.media = self.media
        rss._sites = [{"name": "Offline", "rssurl": "https://example.invalid/rss"}]
        rss.subscribe = Mock()
        rss.subscribe.get_subscribe_movies.return_value = {}
        rss.subscribe.get_subscribe_tvs.return_value = {"1": {"name": "Show"}}
        rss.dbhelper = Mock()
        rss.dbhelper.is_torrent_rssd.return_value = False
        rss.parse_rssxml = Mock(return_value=[{"title": self.title, "enclosure": "offline"}])
        rss._Rss__should_skip_rss_article_before_identify = Mock(return_value=False)
        rss.check_torrent_rss = Mock(return_value=(False, [], {}))
        rss.download_rss_torrent = Mock()

        rss.rssdownload()

        self.assert_formal_numbering(rss.check_torrent_rss.call_args.kwargs["media_info"])

    def test_rss_checker_uses_the_same_cached_numbering(self):
        # Load just the actual preview method, avoiding the singleton's scheduler.
        checker_type = load_class("app/rsschecker.py", "RssChecker", ["test_rss_articles"],
                                  {"log": Mock(), "MediaType": MediaType})
        checker = checker_type()
        checker.media = self.media
        checker.get_rsstask_info = Mock(return_value={"uses": "D"})
        checker.filter = Mock()
        checker.filter.check_torrent_filter.return_value = (True, 0, "")
        checker.downloader = Mock()
        checker.downloader.check_exists_medias.return_value = (False, {}, None)

        result, _, _ = checker.test_rss_articles(1, self.title)

        self.assert_formal_numbering(result)
        self.assertIs(result, checker.downloader.check_exists_medias.call_args.kwargs["meta_info"])

    def indexer_results(self, imdbid=None, partial_details=False):
        match_media = MetaInfo("Show S01", use_llm=False)
        match_media.set_tmdb_info(self.info)
        if imdbid:
            match_media.imdb_id = imdbid
        if partial_details:
            match_media.tmdb_info = {key: value for key, value in self.info.items() if key != "seasons"}
        match_media.get_poster_image = Mock(return_value="")
        match_media.get_backdrop_image = Mock(return_value="")
        client = SimpleNamespace(media=self.media, filter=Mock(), progress=Mock(),
                                 _reverse_title_sites=[], index_type="Offline")
        client.filter.check_torrent_filter.return_value = (True, 0, "")
        client.filter.is_torrent_match_sey.side_effect = (
            lambda meta, season, episode, year: meta.begin_season == 1 and meta.begin_episode == 68)
        return _IIndexClient.filter_search_results(
            client, [{"title": self.title, "enclosure": "offline", "imdbid": imdbid}], 0,
            SimpleNamespace(id="offline", name="Offline", public=False),
            {"season": 1, "episode": 68}, match_media, datetime.datetime.now())

    def test_indexer_cached_resource_matches_formal_season_and_episode(self):
        results = self.indexer_results()
        self.assertEqual(1, len(results))
        self.assert_formal_numbering(results[0])

    def test_cached_identity_cannot_accept_an_unverified_mapping(self):
        # The work is cached, but the next episode is absent from the target season.
        self.media.get_tmdb_tv_season_detail.return_value = {"episodes": [{"episode_number": 67}]}
        self.assertEqual([], self.indexer_results())
        self.media._Media__search_media_with_name.assert_called_once()

    def test_imdb_identity_still_requires_formal_episode_mapping(self):
        for partial in (False, True):
            with self.subTest(partial_details=partial):
                results = self.indexer_results(imdbid="tt12345", partial_details=partial)
                self.assertEqual(1, len(results))
                self.assert_formal_numbering(results[0])

    def test_imdb_identity_cannot_accept_missing_target_episode(self):
        self.media.get_tmdb_tv_season_detail.return_value = {"episodes": [{"episode_number": 67}]}
        self.assertEqual([], self.indexer_results(imdbid="tt12345"))

    def test_imdb_match_does_not_bypass_fractional_confirmation(self):
        self.title = "Show S04E13.5"
        # A work-level IMDb ID cannot turn an unconfirmed decimal into a regular episode.
        self.assertEqual([], self.indexer_results(imdbid="tt12345"))
