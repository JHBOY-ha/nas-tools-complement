# -*- coding: utf-8 -*-

import os
import sys
import tempfile
import types
from unittest import TestCase

if not os.environ.get("NASTOOL_CONFIG"):
    _ROOT_PATH = os.path.dirname(os.path.dirname(__file__))
    os.environ["NASTOOL_CONFIG"] = os.path.join(_ROOT_PATH, "config", "config.yaml")

if "undetected_chromedriver" not in sys.modules:
    uc_stub = types.ModuleType("undetected_chromedriver")
    uc_stub.find_chrome_executable = lambda: None
    uc_stub.ChromeOptions = object
    uc_stub.Chrome = object
    sys.modules["undetected_chromedriver"] = uc_stub

if "webdriver_manager.chrome" not in sys.modules:
    webdriver_manager_stub = types.ModuleType("webdriver_manager")
    chrome_stub = types.ModuleType("webdriver_manager.chrome")

    class _ChromeDriverManager:
        def install(self):
            return ""

    chrome_stub.ChromeDriverManager = _ChromeDriverManager
    sys.modules["webdriver_manager"] = webdriver_manager_stub
    sys.modules["webdriver_manager.chrome"] = chrome_stub

if "parse" not in sys.modules:
    parse_stub = types.ModuleType("parse")
    parse_stub.parse = lambda *args, **kwargs: None
    sys.modules["parse"] = parse_stub

if "dateparser" not in sys.modules:
    dateparser_stub = types.ModuleType("dateparser")
    dateparser_stub.parse = lambda value: None
    sys.modules["dateparser"] = dateparser_stub

if "cn2an" not in sys.modules:
    cn2an_stub = types.ModuleType("cn2an")
    cn2an_stub.cn2an = lambda value, mode=None: int(value)
    sys.modules["cn2an"] = cn2an_stub

if "cacheout" not in sys.modules:
    cacheout_stub = types.ModuleType("cacheout")

    class _Cache:
        def __init__(self, *args, **kwargs):
            self._values = {}

        def get(self, key, default=None):
            return self._values.get(key, default)

        def set(self, key, value, *args, **kwargs):
            self._values[key] = value

        def delete(self, key):
            self._values.pop(key, None)

    class _CacheManager:
        def __init__(self, *args, **kwargs):
            pass

    cacheout_stub.Cache = _Cache
    cacheout_stub.LRUCache = _Cache
    cacheout_stub.CacheManager = _CacheManager
    sys.modules["cacheout"] = cacheout_stub

if "bencode" not in sys.modules:
    bencode_stub = types.ModuleType("bencode")
    bencode_stub.bdecode = lambda value: {}
    sys.modules["bencode"] = bencode_stub

if "pyvirtualdisplay" not in sys.modules:
    pyvirtualdisplay_stub = types.ModuleType("pyvirtualdisplay")

    class _Display:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def stop(self):
            return None

    pyvirtualdisplay_stub.Display = _Display
    sys.modules["pyvirtualdisplay"] = pyvirtualdisplay_stub

if "pyquery" not in sys.modules:
    pyquery_stub = types.ModuleType("pyquery")

    class _PyQuery:
        def __init__(self, *args, **kwargs):
            pass

    pyquery_stub.PyQuery = _PyQuery
    sys.modules["pyquery"] = pyquery_stub

if "zhconv" not in sys.modules:
    zhconv_stub = types.ModuleType("zhconv")
    zhconv_stub.convert = lambda value, locale=None: value
    sys.modules["zhconv"] = zhconv_stub

from app.library import MediaLibrary


class MediaLibraryTest(TestCase):
    def test_list_items_uses_media_server_enum_value_for_query(self):
        class _ServerType:
            value = "Jellyfin"

        class _MediaServer:
            def get_type(self):
                return _ServerType()

        class _MediaDb:
            queried_server = None

            def list_items(self, server_type=None):
                self.queried_server = server_type
                return []

        class _DbHelper:
            @staticmethod
            def get_transfer_histories_with_dest():
                return []

        library = MediaLibrary.__new__(MediaLibrary)
        library.mediadb = _MediaDb()
        library.dbhelper = _DbHelper()
        library.media_server = _MediaServer()

        ret = library.list_items({})

        self.assertEqual(ret["code"], 0)
        self.assertEqual(library.mediadb.queried_server, "Jellyfin")

    def test_list_items_only_returns_linked_transfer_history_items(self):
        class _ServerType:
            value = "Jellyfin"

        class _MediaServer:
            def get_type(self):
                return _ServerType()

        class _Row:
            ITEM_ID = "item1"
            LIBRARY = "lib"
            ITEM_TYPE = "Movie"
            TITLE = "Movie"
            ORGIN_TITLE = ""
            YEAR = "2024"
            TMDBID = "100"
            IMDBID = ""
            PATH = "/server/raw/Movie.mkv"
            JSON = "{}"

        class _MediaDb:
            @staticmethod
            def list_items(server_type=None):
                return [_Row()]

        class _DbHelper:
            def __init__(self, histories):
                self.histories = histories

            def get_transfer_histories_with_dest(self):
                return self.histories

        class _Category:
            @staticmethod
            def get_movie_categorys():
                return []

            @staticmethod
            def get_tv_categorys():
                return []

            @staticmethod
            def get_anime_categorys():
                return []

        library = MediaLibrary.__new__(MediaLibrary)
        library.mediadb = _MediaDb()
        library.media_server = _MediaServer()
        library.category = _Category()

        library.dbhelper = _DbHelper([])
        self.assertEqual(library.list_items({})["total"], 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            target_dir = os.path.join(tmpdir, "外语电影", "Movie (2024)")
            target_file = os.path.join(target_dir, "Movie (2024).mkv")
            os.makedirs(target_dir)
            open(target_file, "wb").close()

            class _History:
                ID = 1
                MODE = "link"
                TYPE = "电影"
                CATEGORY = "外语电影"
                TMDBID = 100
                TITLE = "Movie"
                YEAR = "2024"
                SEASON_EPISODE = ""
                DEST_PATH = target_dir
                DEST_FILENAME = "Movie (2024).mkv"

            library.dbhelper = _DbHelper([_History()])
            ret = library.list_items({})

            self.assertEqual(ret["total"], 1)
            self.assertEqual(ret["items"][0]["target_path"], target_file)
            self.assertEqual(ret["items"][0]["category"], "外语电影")
            self.assertEqual(ret["items"][0]["poster_url"], "/library/image/item1")

    def test_classify_path_uses_media_root_and_second_level_category(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie_root = os.path.join(tmpdir, "movie")
            tv_root = os.path.join(tmpdir, "tv")
            anime_root = os.path.join(tmpdir, "anime")
            os.makedirs(os.path.join(movie_root, "华语电影", "Movie"))
            os.makedirs(os.path.join(tv_root, "欧美剧", "Show"))
            os.makedirs(os.path.join(anime_root, "动漫", "Anime"))

            old_media_paths = MediaLibrary._MediaLibrary__media_paths
            old_category_names = MediaLibrary._MediaLibrary__category_names
            try:
                MediaLibrary._MediaLibrary__media_paths = staticmethod(
                    lambda media_type: {"movie": [movie_root], "tv": [tv_root], "anime": [anime_root]}[media_type]
                )
                MediaLibrary._MediaLibrary__category_names = lambda self, media_type: {
                    "movie": ["华语电影"],
                    "tv": ["欧美剧"],
                    "anime": ["动漫"]
                }[media_type]
                library = MediaLibrary.__new__(MediaLibrary)

                self.assertEqual(
                    library.classify_path(os.path.join(movie_root, "华语电影", "Movie", "Movie.mkv"), "Movie"),
                    ("movie", "华语电影")
                )
                self.assertEqual(
                    library.classify_path(os.path.join(tv_root, "欧美剧", "Show"), "Series"),
                    ("tv", "欧美剧")
                )
                self.assertEqual(
                    library.classify_path(os.path.join(anime_root, "动漫", "Anime"), "Series"),
                    ("anime", "动漫")
                )
            finally:
                MediaLibrary._MediaLibrary__media_paths = staticmethod(old_media_paths)
                MediaLibrary._MediaLibrary__category_names = old_category_names

    def test_external_chinese_subtitle_is_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            sub = os.path.join(tmpdir, "Movie.zh-CN.srt")
            open(movie, "wb").close()
            open(sub, "wb").close()

            status = MediaLibrary.detect_subtitle_status(movie, [])

            self.assertEqual(status["status"], "has_chinese_external")

    def test_internal_chinese_subtitle_stream_is_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            streams = [{"Type": "Subtitle", "Language": "zh-CN", "DisplayTitle": "Chinese Simplified"}]

            status = MediaLibrary.detect_subtitle_status(movie, streams)

            self.assertEqual(status["status"], "has_chinese_internal")

    def test_non_chinese_internal_subtitle_is_missing_chinese(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            streams = [{"Type": "Subtitle", "Language": "eng", "DisplayTitle": "English"}]

            status = MediaLibrary.detect_subtitle_status(movie, streams)

            self.assertEqual(status["status"], "missing_chinese")

    def test_ffprobe_fallback_is_used_when_media_streams_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            old_ffprobe = MediaLibrary._MediaLibrary__ffprobe_subtitle_streams
            try:
                MediaLibrary._MediaLibrary__ffprobe_subtitle_streams = classmethod(lambda cls, media_file: (True, True))

                status = MediaLibrary.detect_subtitle_status(movie, [])

                self.assertEqual(status["status"], "has_chinese_internal")
            finally:
                MediaLibrary._MediaLibrary__ffprobe_subtitle_streams = old_ffprobe

    def test_local_poster_prefers_poster_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_dir = os.path.join(tmpdir, "Movie")
            os.makedirs(media_dir)
            poster = os.path.join(media_dir, "poster.jpg")
            open(poster, "wb").close()

            self.assertEqual(MediaLibrary._MediaLibrary__find_local_poster(media_dir), poster)
