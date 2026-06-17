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

if "bencode" not in sys.modules:
    bencode_stub = types.ModuleType("bencode")
    bencode_stub.bdecode = lambda value: {}
    sys.modules["bencode"] = bencode_stub

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

from app.subtitle import Subtitle
from app.utils.types import RmtMode


class _UploadFile:
    def __init__(self, filename, content=b"subtitle"):
        self.filename = filename
        self._content = content

    def save(self, path):
        with open(path, "wb") as file_obj:
            file_obj.write(self._content)


class SubtitleUploadTest(TestCase):
    def test_upload_uses_target_media_basename_when_syncing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_dir = os.path.join(tmpdir, "src")
            target_dir = os.path.join(tmpdir, "target")
            os.makedirs(src_dir)
            os.makedirs(target_dir)
            src_movie = os.path.join(src_dir, "Original.mkv")
            target_movie = os.path.join(target_dir, "Movie (2024).mkv")
            open(src_movie, "wb").close()
            open(target_movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("subtitle.eng.srt"),
                src_movie,
                target_movie,
                RmtMode.COPY
            )

            self.assertTrue(success, msg)
            self.assertTrue(data["synced"])
            self.assertTrue(os.path.exists(os.path.join(src_dir, "Original.eng.srt")))
            self.assertTrue(os.path.exists(os.path.join(target_dir, "Movie (2024).eng.srt")))

    def test_upload_without_target_only_saves_source_subtitle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(_UploadFile("Movie.zh-cn.ass"), movie)

            self.assertTrue(success, msg)
            self.assertFalse(data["synced"])
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN.ass")))

    def test_upload_rejects_unsupported_subtitle_extension(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(_UploadFile("Movie.txt"), movie)

            self.assertFalse(success)
            self.assertIn("仅支持", msg)

    def test_upload_defaults_unknown_language_to_zh_cn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(_UploadFile("Movie.subtitle.srt"), movie)

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN.srt")))

    def test_upload_detects_traditional_chinese(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(_UploadFile("Movie.zh-tw.ssa"), movie)

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-TW.ssa")))

    def test_upload_adds_number_when_subtitle_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            open(os.path.join(tmpdir, "Movie.zh-CN.srt"), "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(_UploadFile("Movie.zh-cn.srt"), movie)

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN(1).srt")))

    def test_upload_does_not_duplicate_when_target_is_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("Movie.eng.srt"),
                movie,
                movie,
                RmtMode.COPY
            )

            self.assertTrue(success, msg)
            self.assertTrue(data["synced"])
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.eng.srt")))
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.eng(1).srt")))

    def test_plex_upload_preserves_chinese_region_tag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, data = Subtitle().upload_subtitle(
                _UploadFile("Movie.zh-cn.srt"),
                movie,
                server_type="plex"
            )

            self.assertTrue(success, msg)
            self.assertEqual(data["language"], "zh-CN")
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN.srt")))

    def test_plex_upload_preserves_forced_and_sdh_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(
                _UploadFile("Movie.en.forced.sdh.vtt"),
                movie,
                server_type="plex"
            )

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie.en.forced.sdh.vtt")))

    def test_plex_upload_collision_keeps_language_token_clean(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            movie = os.path.join(tmpdir, "Movie.mkv")
            open(movie, "wb").close()
            open(os.path.join(tmpdir, "Movie.zh-TW.srt"), "wb").close()

            success, msg, _ = Subtitle().upload_subtitle(
                _UploadFile("Movie.zh-tw.srt"),
                movie,
                server_type="plex"
            )

            self.assertTrue(success, msg)
            self.assertTrue(os.path.exists(os.path.join(tmpdir, "Movie(1).zh-TW.srt")))
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.zh-TW(1).srt")))
