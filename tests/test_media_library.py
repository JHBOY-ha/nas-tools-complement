# -*- coding: utf-8 -*-

import json
import os
import sys
import tempfile
import types
from unittest import TestCase
from unittest.mock import patch

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
from app.helper.subtitle_health import SubtitleHealth


class MediaLibraryTest(TestCase):
    class _AuditCategory:
        @staticmethod
        def get_movie_categorys():
            return ["国产电影", "外国电影", "动画电影"]

        @staticmethod
        def get_tv_categorys():
            return ["国产剧", "欧美剧"]

        @staticmethod
        def get_anime_categorys():
            return ["动漫"]

    def test_subtitle_audit_scans_selected_movie_subcategory_only(self):
        library = MediaLibrary.__new__(MediaLibrary)
        library.category = self._AuditCategory()
        with patch("app.library.Config") as config_cls, \
                patch.object(SubtitleHealth, "audit_roots", return_value={"code": 0}) as audit_roots, \
                patch.object(MediaLibrary, "_MediaLibrary__save_subtitle_audit", return_value=[]):
            config_cls.return_value.get_config.return_value = {
                "media_server": "jellyfin",
                "movie_path": ["/media/movies"]
            }
            ret = library.audit_external_subtitles("movie", "动画电影")

        audit_roots.assert_called_once_with([os.path.join("/media/movies", "动画电影")], "jellyfin")
        self.assertEqual(ret["subcategory"], "动画电影")
        self.assertEqual(ret["scope_name"], "电影 / 动画电影")

    def test_subtitle_audit_rejects_unknown_subcategory(self):
        library = MediaLibrary.__new__(MediaLibrary)
        library.category = self._AuditCategory()
        with patch("app.library.Config") as config_cls, \
                patch.object(SubtitleHealth, "audit_roots") as audit_roots:
            config_cls.return_value.get_config.return_value = {
                "media_server": "jellyfin",
                "movie_path": ["/media/movies"]
            }
            ret = library.audit_external_subtitles("movie", "../other")

        self.assertEqual(ret["code"], -1)
        self.assertIn("小分类无效", ret["msg"])
        audit_roots.assert_not_called()

    def test_subtitle_audit_categories_follow_category_yaml(self):
        library = MediaLibrary.__new__(MediaLibrary)
        library.category = self._AuditCategory()

        ret = library.get_external_subtitle_audit_categories()

        self.assertEqual(ret["categories"]["movie"], ["国产电影", "外国电影", "动画电影"])
        self.assertEqual(ret["categories"]["tv"], ["国产剧", "欧美剧"])

    def test_subcategory_audits_preserve_other_movie_subcategory_labels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            domestic_root = os.path.join(tmpdir, "国产电影")
            foreign_root = os.path.join(tmpdir, "外国电影")
            os.makedirs(domestic_root)
            os.makedirs(foreign_root)
            domestic_movie = os.path.join(domestic_root, "Domestic.mkv")
            foreign_movie = os.path.join(foreign_root, "Foreign.mkv")
            library = MediaLibrary.__new__(MediaLibrary)
            library.category = self._AuditCategory()

            def audit_result(roots, server):
                media_path = domestic_movie if roots[0] == domestic_root else foreign_movie
                key = os.path.normcase(os.path.normpath(media_path))
                return {
                    "code": 0,
                    "server": server,
                    "roots": roots,
                    "inaccessible_roots": [],
                    "summary": {"total": 1, "ok": 1, "warning": 0, "error": 0},
                    "issues": [],
                    "issues_truncated": 0,
                    "probe_available": True,
                    "media_statuses": {key: {"media_path": media_path, "status": "ok"}}
                }

            with patch("app.library.Config") as config_cls, \
                    patch.object(SubtitleHealth, "audit_roots", side_effect=audit_result):
                config_cls.return_value.get_config.return_value = {
                    "media_server": "jellyfin",
                    "movie_path": [tmpdir]
                }
                config_cls.return_value.get_config_path.return_value = tmpdir
                library.audit_external_subtitles("movie", "国产电影")
                library.audit_external_subtitles("movie", "外国电影")
                snapshot = library._MediaLibrary__latest_audit_snapshot("movie", "jellyfin")

            self.assertEqual(len(snapshot["media_statuses"]), 2)
            self.assertIn(os.path.normcase(os.path.normpath(domestic_movie)), snapshot["media_statuses"])
            self.assertIn(os.path.normcase(os.path.normpath(foreign_movie)), snapshot["media_statuses"])

    def test_subtitle_audit_scans_only_selected_category(self):
        media_config = {
            "media_server": "jellyfin",
            "movie_path": ["/media/movies"],
            "tv_path": ["/media/tv"],
            "anime_path": ["/media/anime"]
        }
        audit_result = {
            "code": 0,
            "summary": {"total": 0, "ok": 0, "warning": 0, "error": 0}
        }
        library = MediaLibrary.__new__(MediaLibrary)
        with patch("app.library.Config") as config_cls, \
                patch.object(SubtitleHealth, "audit_roots", return_value=audit_result) as audit_roots, \
                patch.object(MediaLibrary, "_MediaLibrary__save_subtitle_audit", return_value=[]):
            config_cls.return_value.get_config.return_value = media_config
            ret = library.audit_external_subtitles("tv")

        audit_roots.assert_called_once_with(["/media/tv"], "jellyfin")
        self.assertEqual(ret["category"], "tv")
        self.assertEqual(ret["category_name"], "电视剧")

    def test_subtitle_audit_requires_a_category(self):
        library = MediaLibrary.__new__(MediaLibrary)
        with patch("app.library.Config") as config_cls, \
                patch.object(SubtitleHealth, "audit_roots") as audit_roots:
            config_cls.return_value.get_config.return_value = {"media_server": "jellyfin"}
            ret = library.audit_external_subtitles("")

        self.assertEqual(ret["code"], -1)
        self.assertIn("请选择", ret["msg"])
        audit_roots.assert_not_called()

    def test_anime_subtitle_audit_falls_back_to_tv_path(self):
        library = MediaLibrary.__new__(MediaLibrary)
        with patch("app.library.Config") as config_cls, \
                patch.object(SubtitleHealth, "audit_roots", return_value={"code": 0}) as audit_roots, \
                patch.object(MediaLibrary, "_MediaLibrary__save_subtitle_audit", return_value=[]):
            config_cls.return_value.get_config.return_value = {
                "media_server": "emby",
                "tv_path": ["/media/tv"],
                "anime_path": []
            }
            ret = library.audit_external_subtitles("anime")

        audit_roots.assert_called_once_with(["/media/tv"], "emby")
        self.assertEqual(ret["category"], "anime")

    def test_full_library_subtitle_audit_reports_server_recognition_states(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            defined = os.path.join(tmpdir, "Movie.chi.zh-cn.srt")
            undefined = os.path.join(tmpdir, "Movie.zh-CN.srt")
            orphan = os.path.join(tmpdir, "Other.zh-CN.srt")
            open(movie, "wb").close()
            for subtitle_file in [defined, undefined, orphan]:
                with open(subtitle_file, "wb") as file_obj:
                    file_obj.write(b"1\n00:00:01,000 --> 00:00:02,000\nHello\n")

            original_validate = SubtitleHealth.__dict__["validate_subtitle"]
            try:
                SubtitleHealth.validate_subtitle = classmethod(
                    lambda cls, path: {
                        "valid": True,
                        "probe_available": True,
                        "message": "ffprobe 解析通过"
                    }
                )
                ret = SubtitleHealth.audit_roots([tmpdir], "jellyfin")
            finally:
                SubtitleHealth.validate_subtitle = original_validate

            self.assertEqual(ret["summary"], {"total": 3, "ok": 1, "warning": 1, "error": 1})
            issues = {os.path.basename(item["path"]): item for item in ret["issues"]}
            self.assertEqual(issues["Movie.zh-CN.srt"]["status"], "warning")
            self.assertIn("语言未定义", issues["Movie.zh-CN.srt"]["reason"])
            self.assertEqual(issues["Other.zh-CN.srt"]["status"], "error")
            self.assertIn("未找到", issues["Other.zh-CN.srt"]["reason"])
            media_status = ret["media_statuses"][os.path.normcase(os.path.normpath(movie))]
            self.assertEqual(media_status["status"], "warning")
            self.assertEqual(media_status["subtitle_count"], 2)

    def test_subtitle_audit_keeps_only_latest_three_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            library = MediaLibrary.__new__(MediaLibrary)
            counter = {"value": 0}

            def audit_result(*_):
                counter["value"] += 1
                value = counter["value"]
                return {
                    "code": 0,
                    "server": "jellyfin",
                    "roots": [tmpdir],
                    "summary": {"total": value, "ok": value, "warning": 0, "error": 0},
                    "issues": [],
                    "issues_truncated": 0,
                    "probe_available": True,
                    "media_statuses": {}
                }

            with patch("app.library.Config") as config_cls, \
                    patch.object(SubtitleHealth, "audit_roots", side_effect=audit_result):
                config_cls.return_value.get_config.return_value = {
                    "media_server": "jellyfin",
                    "movie_path": [tmpdir]
                }
                config_cls.return_value.get_config_path.return_value = tmpdir
                for _ in range(4):
                    library.audit_external_subtitles("movie")
                history = library.get_external_subtitle_audit_history()["history"]

            self.assertEqual(len(history), 3)
            self.assertEqual([item["summary"]["total"] for item in history], [4, 3, 2])
            self.assertTrue(os.path.isfile(os.path.join(tmpdir, "subtitle-audit-history.json")))

    def test_movie_card_receives_latest_external_subtitle_audit_label(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            key = os.path.normcase(os.path.normpath(movie))
            item = {
                "media_type": "movie",
                "target_path": movie,
                "path": movie,
                "media_streams": [],
                "linked_episodes": []
            }
            snapshot = {
                "checked_at": "2026-07-13T20:00:00+08:00",
                "media_statuses": {
                    key: {"status": "error", "reason": "Invalid data", "subtitle_count": 2}
                }
            }

            MediaLibrary.__new__(MediaLibrary)._MediaLibrary__fill_subtitle_summary(
                item,
                allow_ffprobe=False,
                audit_snapshot=snapshot
            )

            self.assertEqual(item["subtitle_audit_status"], "error")
            self.assertEqual(item["subtitle_audit_label"], "外挂字幕无法识别")
            self.assertEqual(item["subtitle_audit_count"], 2)
            self.assertEqual(item["subtitle_audit_checked_at"], snapshot["checked_at"])

    def test_single_movie_recheck_updates_latest_status_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            key = os.path.normcase(os.path.normpath(movie))
            history_file = os.path.join(tmpdir, "subtitle-audit-history.json")
            with open(history_file, "w", encoding="utf-8") as file_obj:
                json.dump({
                    "version": 1,
                    "latest": {
                        "movie": {
                            "checked_at": "2026-07-13T20:00:00+08:00",
                            "server": "jellyfin",
                            "media_statuses": {key: {"status": "warning", "subtitle_count": 1}}
                        }
                    },
                    "history": [{"scope_name": "全部电影", "summary": {"warning": 1}}]
                }, file_obj, ensure_ascii=False)

            MediaLibrary._subtitle_audit_store_cache = None
            aggregate = {
                "media_path": movie,
                "status": "ok",
                "reason": "文件名关联、语言标签和字幕内容均可识别",
                "subtitle_count": 2
            }
            with patch("app.library.Config") as config_cls, \
                    patch.object(SubtitleHealth, "inspect_media_subtitles", return_value=[{"status": "ok"}]), \
                    patch.object(SubtitleHealth, "aggregate_media_subtitles", return_value=aggregate):
                config_cls.return_value.get_config_path.return_value = tmpdir
                ret = MediaLibrary.update_external_subtitle_audit_status(movie, "jellyfin")

            MediaLibrary._subtitle_audit_store_cache = None
            with open(history_file, "r", encoding="utf-8") as file_obj:
                store = json.load(file_obj)
            self.assertEqual(ret["status"], "ok")
            self.assertEqual(store["latest"]["movie"]["media_statuses"][key]["subtitle_count"], 2)
            self.assertEqual(store["history"], [{"scope_name": "全部电影", "summary": {"warning": 1}}])

    def test_full_library_subtitle_audit_reports_ffprobe_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            subtitle = os.path.join(tmpdir, "Movie.chi.zh-cn.srt")
            open(movie, "wb").close()
            open(subtitle, "wb").close()

            original_validate = SubtitleHealth.__dict__["validate_subtitle"]
            try:
                SubtitleHealth.validate_subtitle = classmethod(
                    lambda cls, path: {
                        "valid": False,
                        "probe_available": True,
                        "message": "Invalid data found when processing input"
                    }
                )
                ret = SubtitleHealth.audit_roots([tmpdir], "jellyfin")
            finally:
                SubtitleHealth.validate_subtitle = original_validate

            self.assertEqual(ret["summary"]["error"], 1)
            self.assertIn("Invalid data", ret["issues"][0]["reason"])

    def test_jellyfin_two_letter_language_is_defined_but_region_only_tag_is_not(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            short_tag = os.path.join(tmpdir, "Movie.zh.srt")
            region_tag = os.path.join(tmpdir, "Movie.zh-CN.srt")
            open(movie, "wb").close()
            for subtitle_file in [short_tag, region_tag]:
                open(subtitle_file, "wb").close()

            old_validate = SubtitleHealth.__dict__["validate_subtitle"]
            try:
                SubtitleHealth.validate_subtitle = classmethod(
                    lambda cls, path: {"valid": True, "probe_available": True, "message": "ok"}
                )
                short_result = SubtitleHealth.inspect_external_subtitle(short_tag, movie, "jellyfin")
                region_result = SubtitleHealth.inspect_external_subtitle(region_tag, movie, "jellyfin")
            finally:
                SubtitleHealth.validate_subtitle = old_validate

            self.assertEqual(short_result["status"], "ok")
            self.assertEqual(region_result["status"], "warning")

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

    def test_list_items_prefers_linked_transfer_history_but_keeps_synced_items(self):
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
        unlinked_ret = library.list_items({})
        self.assertEqual(unlinked_ret["total"], 1)
        self.assertFalse(unlinked_ret["items"][0]["linked"])
        self.assertEqual(unlinked_ret["items"][0]["path"], "/server/raw/Movie.mkv")
        self.assertEqual(unlinked_ret["items"][0]["target_path"], "")
        self.assertFalse(unlinked_ret["items"][0]["can_upload"])

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
            self.assertTrue(ret["items"][0]["can_upload"])

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

    def test_subtitle_filter_does_not_ffprobe_all_items(self):
        class _ServerType:
            value = "Jellyfin"

        class _MediaServer:
            def get_type(self):
                return _ServerType()

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

        with tempfile.TemporaryDirectory() as tmpdir:
            target_file = os.path.join(tmpdir, "Movie (2024).mkv")
            open(target_file, "wb").close()

            class _History:
                ID = 1
                MODE = "link"
                TYPE = "电影"
                CATEGORY = ""
                TMDBID = 100
                TITLE = "Movie"
                YEAR = "2024"
                SEASON_EPISODE = ""
                DEST_PATH = tmpdir
                DEST_FILENAME = "Movie (2024).mkv"

            library = MediaLibrary.__new__(MediaLibrary)
            library.mediadb = _MediaDb()
            library.dbhelper = _DbHelper([_History()])
            library.media_server = _MediaServer()
            library.category = _Category()
            old_ffprobe = MediaLibrary._MediaLibrary__ffprobe_subtitle_streams
            try:
                MediaLibrary._MediaLibrary__ffprobe_subtitle_streams = classmethod(
                    lambda cls, media_file: (_ for _ in ()).throw(AssertionError("ffprobe should not run"))
                )

                ret = library.list_items({"subtitle": "missing"})

                self.assertEqual(ret["code"], 0)
                self.assertEqual(ret["total"], 1)
                self.assertEqual(ret["items"][0]["subtitle_status"], "missing_chinese")
            finally:
                MediaLibrary._MediaLibrary__ffprobe_subtitle_streams = old_ffprobe

    def test_list_items_sorts_by_internal_external_and_audit_status(self):
        class _ServerType:
            value = "Jellyfin"

        class _MediaServer:
            @staticmethod
            def get_type():
                return _ServerType()

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

        with tempfile.TemporaryDirectory() as tmpdir:
            definitions = [
                ("1", "Internal", [{"Type": "Subtitle", "Language": "eng"}]),
                ("2", "External", []),
                ("3", "Broken", []),
                ("4", "Warning", []),
                ("5", "Passed", [])
            ]
            rows = []
            histories = []
            target_paths = {}
            for item_id, title, streams in definitions:
                target_file = os.path.join(tmpdir, "%s.mkv" % title)
                open(target_file, "wb").close()
                target_paths[title] = target_file
                rows.append(types.SimpleNamespace(
                    ITEM_ID=item_id,
                    LIBRARY="lib",
                    ITEM_TYPE="Movie",
                    TITLE=title,
                    ORGIN_TITLE="",
                    YEAR="2024",
                    TMDBID=item_id,
                    IMDBID="",
                    PATH="/server/%s.mkv" % title,
                    JSON=json.dumps({"MediaStreams": streams})
                ))
                histories.append(types.SimpleNamespace(
                    ID=int(item_id),
                    MODE="link",
                    TYPE="电影",
                    CATEGORY="",
                    TMDBID=item_id,
                    TITLE=title,
                    YEAR="2024",
                    SEASON_EPISODE="",
                    DEST_PATH=tmpdir,
                    DEST_FILENAME="%s.mkv" % title
                ))
            with open(os.path.join(tmpdir, "External.eng.srt"), "wb") as file_obj:
                file_obj.write(b"subtitle")

            class _MediaDb:
                @staticmethod
                def list_items(server_type=None):
                    return rows

            class _DbHelper:
                @staticmethod
                def get_transfer_histories_with_dest():
                    return histories

            audit_snapshot = {
                "server": "jellyfin",
                "media_statuses": {
                    os.path.normcase(os.path.normpath(target_paths["Broken"])): {
                        "status": "error"
                    },
                    os.path.normcase(os.path.normpath(target_paths["Warning"])): {
                        "status": "warning"
                    },
                    os.path.normcase(os.path.normpath(target_paths["Passed"])): {
                        "status": "ok"
                    }
                }
            }
            library = MediaLibrary.__new__(MediaLibrary)
            library.mediadb = _MediaDb()
            library.dbhelper = _DbHelper()
            library.media_server = _MediaServer()
            library.category = _Category()

            with patch.object(
                    MediaLibrary,
                    "_MediaLibrary__latest_audit_snapshots",
                    return_value={"movie": audit_snapshot, "tv": {}, "anime": {}}
            ), \
                    patch.object(MediaLibrary, "_MediaLibrary__ffprobe_subtitle_streams",
                                 side_effect=AssertionError("list sorting must not run ffprobe")):
                internal_desc = library.list_items({"sort_by": "internal", "sort_order": "desc"})
                internal_asc = library.list_items({"sort_by": "internal", "sort_order": "asc"})
                external_desc = library.list_items({"sort_by": "external", "sort_order": "desc"})
                audit_desc = library.list_items({"sort_by": "audit", "sort_order": "desc"})
                audit_asc = library.list_items({"sort_by": "audit", "sort_order": "asc"})

            self.assertEqual(internal_desc["items"][0]["title"], "Internal")
            self.assertEqual(internal_asc["items"][-1]["title"], "Internal")
            self.assertEqual(external_desc["items"][-1]["title"], "Internal")
            self.assertEqual(
                [item["title"] for item in audit_desc["items"]],
                ["Broken", "Warning", "External", "Internal", "Passed"]
            )
            self.assertEqual(
                [item["title"] for item in audit_asc["items"]],
                ["Passed", "External", "Internal", "Warning", "Broken"]
            )

    def test_external_subtitle_directory_cache_and_invalidation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            subtitle = os.path.join(tmpdir, "Movie.eng.srt")
            open(movie, "wb").close()
            open(subtitle, "wb").close()
            MediaLibrary.invalidate_subtitle_directory_cache()

            with patch("app.library.os.listdir", wraps=os.listdir) as listdir:
                self.assertTrue(MediaLibrary.has_external_subtitle(movie))
                self.assertTrue(MediaLibrary.has_external_subtitle(movie))
                self.assertEqual(listdir.call_count, 1)
                MediaLibrary.invalidate_subtitle_directory_cache(movie)
                self.assertTrue(MediaLibrary.has_external_subtitle(movie))
                self.assertEqual(listdir.call_count, 2)

    def test_default_library_page_does_not_run_ffprobe(self):
        class _ServerType:
            value = "Jellyfin"

        class _MediaServer:
            @staticmethod
            def get_type():
                return _ServerType()

        class _MediaDb:
            @staticmethod
            def list_items(server_type=None):
                return []

        class _DbHelper:
            @staticmethod
            def get_transfer_histories_with_dest():
                return []

        library = MediaLibrary.__new__(MediaLibrary)
        library.media_server = _MediaServer()
        library.mediadb = _MediaDb()
        library.dbhelper = _DbHelper()
        with patch.object(MediaLibrary, "_MediaLibrary__ffprobe_subtitle_streams",
                          side_effect=AssertionError("default page must not run ffprobe")):
            ret = library.list_items({})

        self.assertEqual(ret["code"], 0)

    def test_local_poster_prefers_poster_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media_dir = os.path.join(tmpdir, "Movie")
            os.makedirs(media_dir)
            poster = os.path.join(media_dir, "poster.jpg")
            open(poster, "wb").close()

            self.assertEqual(MediaLibrary._MediaLibrary__find_local_poster(media_dir), poster)
