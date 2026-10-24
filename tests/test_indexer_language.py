"""Offline regressions for Chinese Telegram searches across mixed-language sites."""
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.indexer.client.builtin import BuiltinIndexer
from app.indexer.indexer import Indexer
from app.media import Media
from app.media.tmdbv3api import TMDb, Movie, TV
from app.searcher import Searcher
from app.utils.types import IndexerType, MediaType, SearchType
from app.utils.workload import BoundedExecutor, TaskQueueFull


class IndexerLanguageTest(TestCase):
    def setUp(self):
        # Exercise real dispatch and the built-in Chinese guard; only provider
        # I/O and result recognition are stubbed, so no browser/network starts.
        self.client = BuiltinIndexer.__new__(BuiltinIndexer)
        self.client.media = Mock()
        self.title_provider = self.client.media
        self.title_provider.get_tmdb_en_title.return_value = "Dune"
        self.client.progress = Mock()
        self.spider = Mock(side_effect=lambda **kw: [kw["keyword"]])
        self.client._BuiltinIndexer__spider_search = self.spider
        self.client.filter_search_results = Mock(side_effect=lambda **kw: kw["result_array"])
        indexer_type = Indexer.__closure__[0].cell_contents
        self.indexer = indexer_type.__new__(indexer_type)
        self.indexer._client = self.client
        self.indexer._client_type = IndexerType.BUILTIN
        self.indexer.progress = Mock()
        self.executor = BoundedExecutor(2, 8, "language-test")
        self.addCleanup(self.executor.shutdown)
        self.indexer._search_executor = self.executor
        self.sites = [SimpleNamespace(id=n, name=name, language=language,
                                      pri=0, rule=None, parser=None)
                      for n, (name, language) in enumerate([
                          ("中文站", "zh"), ("英文站一", "en"), ("英文站二", "en")])]
        self.indexer.get_indexers = lambda: self.sites
        self.media = SimpleNamespace(
            keyword=None, cn_name="沙丘", title="沙丘", en_name=None,
            original_title="Dune", original_language="en", tmdb_id=438631,
            type=MediaType.MOVIE, year="2021", begin_season=None,
            get_episode_list=lambda: [])
        config = patch("app.indexer.client.builtin.Config")
        config.start().return_value.get_config.return_value = {}
        self.addCleanup(config.stop)

    def search(self, keyword="沙丘", filters=None, media=True):
        return self.indexer.search_by_keyword(
            keyword, filters, self.media if media else None, SearchType.TG)

    def test_mixed_sites_search_both_titles_and_preserve_filters_and_identity(self):
        filters = {"year": "2021", "type": MediaType.MOVIE, "season": [2], "episode": [3]}
        before = vars(self.media).copy()
        self.assertCountEqual(self.search(filters=filters), ["沙丘", "Dune", "Dune"])
        self.title_provider.get_tmdb_en_title.assert_not_called()
        for call in self.client.filter_search_results.call_args_list:
            self.assertEqual(call.kwargs["filter_args"], filters)
            self.assertIs(call.kwargs["match_media"], self.media)
        self.assertEqual(vars(self.media), before)

    def test_known_english_name_takes_precedence(self):
        self.media.en_name = "Dune English"
        self.assertCountEqual(self.search(), ["沙丘", "Dune English", "Dune English"])
        self.title_provider.get_tmdb_en_title.assert_not_called()

    def test_non_english_original_resolves_english_once_for_all_sites(self):
        self.media.original_language = "zh"
        self.media.original_title = "沙丘"
        self.assertCountEqual(self.search(), ["沙丘", "Dune", "Dune"])
        self.title_provider.get_tmdb_en_title.assert_called_once_with(self.media)

    def test_translation_uses_selected_tmdb_id_and_type_in_english_details(self):
        # Use the real title accessor: a title search or another same-name film
        # must not replace the identity already selected in Telegram.
        self.media.original_language = "zh"
        self.media.original_title = "沙丘"
        self.title_provider.get_tmdb_en_title.side_effect = lambda media: Media.get_tmdb_en_title(
            self.title_provider, media)
        for media_type, field in ((MediaType.MOVIE, "title"), (MediaType.TV, "name")):
            with self.subTest(media_type=media_type):
                self.media.type = media_type
                client = self.title_provider.movie if media_type == MediaType.MOVIE else self.title_provider.tv
                client.details.reset_mock()
                client.details.return_value = {field: "Dune"}
                self.assertCountEqual(self.search(), ["沙丘", "Dune", "Dune"])
                client.details.assert_called_once_with(438631, language="en-US")
                self.title_provider.get_tmdb_info.assert_not_called()

    def test_telegram_candidate_choice_preserves_selected_movie_identity(self):
        from web.backend import search_torrents

        older_movie = SimpleNamespace(**vars(self.media))
        older_movie.tmdb_id = 841
        older_movie.year = "1984"
        self.media.douban_id = None
        user_id = "language-regression-user"
        # Stub only the post-selection boundary; no messages or downloads are
        # sent while checking which of the same-name movies reaches search.
        with patch.object(search_torrents, "SEARCH_MEDIA_CACHE", {user_id: [older_movie, self.media]}), \
                patch.object(search_torrents, "SEARCH_MEDIA_TYPE", {user_id: "SEARCH"}), \
                patch.object(search_torrents, "__search_media") as selected_search:
            search_torrents.search_media_by_message("2", SearchType.TG, user_id)
        selected_search.assert_called_once_with(
            in_from=SearchType.TG, media_info=self.media, user_id=user_id, user_name=None)
        selected = selected_search.call_args.kwargs["media_info"]
        self.assertEqual((selected.tmdb_id, selected.year), (438631, "2021"))

    def test_invalid_english_field_uses_valid_original_title(self):
        self.media.en_name = "沙丘 Dune"
        self.assertCountEqual(self.search(), ["沙丘", "Dune", "Dune"])

    def test_missing_or_chinese_translation_keeps_chinese_sites_working(self):
        self.media.original_language = "zh"
        for title in (None, "", "   ", "沙丘"):
            with self.subTest(title=title):
                self.title_provider.get_tmdb_en_title.return_value = title
                self.spider.reset_mock()
                self.assertEqual(self.search(), ["沙丘"])
                self.spider.assert_called_once()

    def test_translation_failure_does_not_abort_chinese_search(self):
        self.media.original_language = "zh"
        self.title_provider.get_tmdb_en_title.side_effect = RuntimeError("TMDB unavailable")
        self.assertEqual(self.search(), ["沙丘"])

    def test_unrecognized_quick_search_does_not_guess_translation(self):
        self.assertEqual(self.search(media=False), ["沙丘"])
        self.title_provider.get_tmdb_en_title.assert_not_called()

    def test_explicit_subscription_keyword_is_not_replaced(self):
        self.media.keyword = "沙丘 导演剪辑版"
        self.assertEqual(self.search(keyword=self.media.keyword), [self.media.keyword])
        self.title_provider.get_tmdb_en_title.assert_not_called()

    def test_site_filter_avoids_unneeded_translation_lookup(self):
        self.media.original_language = "zh"
        self.assertEqual(self.search(filters={"site": ["中文站"]}), ["沙丘"])
        self.title_provider.get_tmdb_en_title.assert_not_called()

    def test_english_query_is_unchanged(self):
        self.assertEqual(self.search(keyword="Dune 2021"), ["Dune 2021"] * 3)
        self.title_provider.get_tmdb_en_title.assert_not_called()

    def test_missing_tmdb_identity_does_not_fetch_details(self):
        self.media.original_language = "zh"
        self.media.tmdb_id = None
        self.assertEqual(self.search(), ["沙丘"])
        self.title_provider.get_tmdb_en_title.assert_not_called()

    def test_external_indexers_keep_original_keyword(self):
        self.indexer._client_type = IndexerType.JACKETT
        self.indexer._client = Mock()
        self.indexer._client.search.return_value = []
        self.search()
        for call in self.indexer._client.search.call_args_list:
            self.assertEqual(call.args[2], "沙丘")

    def test_telegram_search_includes_english_sites_when_chinese_site_has_results(self):
        searcher = Searcher.__new__(Searcher)
        searcher.indexer = self.indexer
        searcher.media = self.title_provider
        searcher.progress = Mock()
        searcher.message = Mock()
        searcher.message.get_search_types.return_value = [SearchType.TG]
        searcher.dbhelper = Mock()
        searcher.downloader = Mock()
        searcher._search_auto = False
        # Represent recognized torrent results so the normal Telegram sorting
        # and persistence branch executes without initiating a download.
        self.client.filter_search_results.side_effect = lambda **kw: [SimpleNamespace(
            title=kw["result_array"][0], res_order=1, site_order=1, seeders=1)]
        with patch("app.searcher.Config") as config:
            config.return_value.get_config.return_value = {}
            result = searcher.search_one_media(self.media, SearchType.TG, {})
        self.assertEqual(result, (None, {}, 3, None))
        stored = searcher.dbhelper.insert_search_results.call_args.args[0]
        self.assertCountEqual([item.title for item in stored], ["沙丘", "Dune", "Dune"])
        self.assertEqual(self.spider.call_count, 3)
        searcher.downloader.batch_download.assert_not_called()

    def make_searcher(self):
        # Keep actual search orchestration; isolate messages and downloads.
        searcher = Searcher.__new__(Searcher)
        searcher.indexer = self.indexer
        searcher.media = self.title_provider
        searcher.progress = Mock()
        searcher.message = Mock()
        searcher.message.get_search_types.return_value = [SearchType.TG]
        searcher.dbhelper = Mock()
        searcher.downloader = Mock()
        searcher._search_auto = False
        return searcher

    def assert_fallback_queries(self):
        calls = Counter((call.kwargs['indexer'].name, call.kwargs['keyword'])
                        for call in self.spider.call_args_list)
        self.assertEqual(calls, {('中文站', '沙丘'): 1, ('中文站', 'Dune'): 1,
                                 ('英文站一', 'Dune'): 1, ('英文站二', 'Dune'): 1})

    def test_empty_telegram_results_do_not_repeat_english_sites_in_either_order(self):
        self.spider.side_effect = None
        self.spider.return_value = []
        for english_first in (False, True):
            with self.subTest(english_first=english_first), patch('app.searcher.Config') as config:
                config.return_value.get_config.return_value = {'search_en_title': english_first}
                self.spider.reset_mock()
                result = self.make_searcher().search_one_media(self.media, SearchType.TG, {})
                self.assertEqual(result, (None, {}, 0, 0))
                self.assert_fallback_queries()

    def test_new_logical_search_is_not_suppressed(self):
        self.spider.side_effect = None
        self.spider.return_value = []
        searcher = self.make_searcher()
        with patch('app.searcher.Config') as config:
            config.return_value.get_config.return_value = {}
            for _ in range(2):
                self.spider.reset_mock()
                searcher.search_one_media(self.media, SearchType.TG, {})
                self.assert_fallback_queries()

    def test_non_english_title_is_resolved_once_across_both_rounds(self):
        self.media.original_language = 'zh'
        self.media.original_title = '沙丘'
        self.spider.side_effect = None
        self.spider.return_value = []
        with patch('app.searcher.Config') as config:
            config.return_value.get_config.return_value = {}
            self.make_searcher().search_one_media(self.media, SearchType.TG, {})
        self.title_provider.get_tmdb_en_title.assert_called_once_with(self.media)
        self.assert_fallback_queries()

    def test_failed_title_resolution_is_not_repeated_by_each_round(self):
        self.media.original_language = 'zh'
        self.title_provider.get_tmdb_en_title.return_value = None
        self.spider.side_effect = None
        self.spider.return_value = []
        with patch('app.searcher.Config') as config:
            config.return_value.get_config.return_value = {}
            self.make_searcher().search_one_media(self.media, SearchType.TG, {})
        self.title_provider.get_tmdb_en_title.assert_called_once_with(self.media)
        self.spider.assert_called_once()

    def test_empty_web_results_share_title_and_query_context(self):
        from web.backend import search_torrents
        self.media.tmdb_info = {'id': self.media.tmdb_id}
        self.media.get_title_string = lambda: '沙丘 (2021)'
        self.media.original_language = 'zh'
        self.title_provider.get_media_info.return_value = self.media
        self.spider.side_effect = None
        self.spider.return_value = []
        for english_first in (False, True):
            with self.subTest(english_first=english_first), \
                    patch.object(search_torrents, 'Media', return_value=self.title_provider), \
                    patch.object(search_torrents, 'Searcher', return_value=self.make_searcher()), \
                    patch.object(search_torrents, 'DbHelper'), \
                    patch.object(search_torrents, 'ProgressHelper'), \
                    patch.object(search_torrents, 'Config') as config:
                config.return_value.get_config.return_value = {'search_en_title': english_first}
                self.spider.reset_mock()
                self.title_provider.get_tmdb_en_title.reset_mock()
                code, _ = search_torrents.search_medias_for_web('沙丘 2021')
                self.assertEqual(code, 1)
                self.assert_fallback_queries()
                self.title_provider.get_tmdb_en_title.assert_called_once_with(self.media)

    def test_rejected_submission_can_be_retried_in_second_round(self):
        self.sites = self.sites[1:2]
        context = {}
        with patch.object(self.executor, 'submit', side_effect=TaskQueueFull('busy')):
            self.assertEqual(self.indexer.search_by_keyword('沙丘', {}, self.media,
                                                           search_context=context), [])
        self.assertFalse(context['submitted'])
        self.assertEqual(self.indexer.search_by_keyword('Dune', {}, self.media,
                                                        search_context=context), ['Dune'])
        self.spider.assert_called_once()

    def test_canceled_queued_search_does_not_block_fallback(self):
        executor = BoundedExecutor(1, 8, 'language-cancel-test')
        self.addCleanup(executor.shutdown)
        self.indexer._search_executor = executor
        self.sites = self.sites[1:2]
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def hold():
            started.set()
            release.wait(3)
        running = executor.submit(hold)
        self.assertTrue(started.wait(2))
        context = {}
        with patch('app.indexer.indexer.SEARCH_TOTAL_TIMEOUT_SECONDS', 0.02):
            self.assertEqual(self.indexer.search_by_keyword('沙丘', {}, self.media,
                                                           search_context=context), [])
        self.assertFalse(context['submitted'])
        self.spider.assert_not_called()
        release.set()
        running.result(2)
        self.assertEqual(self.indexer.search_by_keyword('Dune', {}, self.media,
                                                        search_context=context), ['Dune'])
        self.spider.assert_called_once()


class EnglishTitleRequestTest(TestCase):
    def setUp(self):
        # Exercise real SDK URL building with a fake HTTP transport and no API key.
        self.environment = patch.dict(os.environ, {'TMDB_API_KEY': 'offline-test-key',
            'TMDB_LANGUAGE': 'zh-CN', 'TMDB_CACHE_ENABLED': 'False', 'TMDB_PROXIES': '{}',
            'TMDB_DOMAIN': 'https://api.themoviedb.org/3'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.provider = Media.__new__(Media)
        self.provider.movie = Movie()
        self.provider.tv = TV()
        self.media = SimpleNamespace(tmdb_id=438631, type=MediaType.MOVIE)

    @staticmethod
    def response():
        response = Mock(status_code=200, headers={})
        response.json.return_value = {'id': 438631, 'title': 'Dune', 'name': 'Dune'}
        return response

    def test_movie_and_tv_request_explicit_english_without_mutating_default(self):
        for media_type, path in ((MediaType.MOVIE, '/3/movie/438631'),
                                 (MediaType.TV, '/3/tv/438631')):
            with self.subTest(media_type=media_type), \
                    patch.object(TMDb, '_request', return_value=self.response()) as request:
                self.media.type = media_type
                self.assertEqual(self.provider.get_tmdb_en_title(self.media), 'Dune')
                url = urlparse(request.call_args.args[1])
                self.assertEqual(url.path, path)
                self.assertEqual(parse_qs(url.query)['language'], ['en-US'])
                self.assertEqual(TMDb().language, 'zh-CN')

    def test_parallel_default_request_cannot_translate_english_result_back(self):
        started, release = threading.Event(), threading.Event()
        requests = []
        def transport(_method, url, *_args, **_kwargs):
            language = parse_qs(urlparse(url).query)['language'][0]
            requests.append(language)
            if language == 'en-US':
                started.set()
                if not release.wait(3):
                    raise AssertionError('Concurrent request did not finish')
            return self.response()
        with patch.object(TMDb, '_request', side_effect=transport), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.provider.get_tmdb_en_title, self.media)
            try:
                self.assertTrue(started.wait(2))
                Movie().details(841)
                # Another task may change the process default mid-flight; this
                # must neither change the explicit URL nor post-process its title.
                TMDb().language = 'fr-FR'
            finally:
                release.set()
            self.assertEqual(future.result(2), 'Dune')
        self.assertEqual(requests, ['en-US', 'zh-CN'])
        self.assertEqual(TMDb().language, 'fr-FR')

    def test_failed_request_does_not_change_language(self):
        with patch.object(TMDb, '_request', side_effect=RuntimeError('offline failure')):
            self.assertIsNone(self.provider.get_tmdb_en_title(self.media))
        self.assertEqual(TMDb().language, 'zh-CN')

    def test_explicit_language_is_part_of_http_cache_key(self):
        self.provider.movie.cache_clear()
        self.addCleanup(self.provider.movie.cache_clear)
        with patch.dict(os.environ, {'TMDB_CACHE_ENABLED': 'True'}), \
                patch.object(TMDb, '_request', return_value=self.response()) as request:
            self.provider.movie.details(self.media.tmdb_id)
            for _ in range(2):
                self.assertEqual(self.provider.get_tmdb_en_title(self.media), 'Dune')
            self.provider.movie.details(self.media.tmdb_id)
        self.assertEqual(request.call_count, 2)
        languages = [parse_qs(urlparse(call.args[1]).query)['language'][0]
                     for call in request.call_args_list]
        self.assertEqual(languages, ['zh-CN', 'en-US'])
