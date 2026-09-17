# -*- coding: utf-8 -*-

import os
from pathlib import Path
import importlib.util
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import types
from enum import Enum
from unittest import TestCase
from unittest.mock import Mock, patch


class FakeResponse:
    def __init__(self, status_code=200, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content
        self.headers = {}

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        return self._payload


class OpenSubtitlesApiTest(TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("NASTOOL_CONFIG"):
            root_path = os.path.dirname(os.path.dirname(__file__))
            os.environ["NASTOOL_CONFIG"] = os.path.join(root_path, "config", "config.yaml")
        cls.module = cls._load_client_module()
        cls.subtitle_module = cls._load_subtitle_module()

    @staticmethod
    def _load_client_module():
        """Load the isolated client even when optional NAS-Tools UI deps are absent."""
        root_path = os.path.dirname(os.path.dirname(__file__))

        class TestMediaType(Enum):
            TV = "电视剧"
            MOVIE = "电影"

        class TestConfig:
            def get_config(self, node=None):
                return {}

            def get_proxies(self):
                return None

        stubs = {
            "log": types.SimpleNamespace(info=lambda *args, **kwargs: None),
            "app.utils.http_utils": types.SimpleNamespace(RequestUtils=object),
            "app.utils.types": types.SimpleNamespace(MediaType=TestMediaType),
            "config": types.SimpleNamespace(Config=TestConfig),
            "version": types.SimpleNamespace(APP_VERSION="v2.10.3"),
        }
        originals = {name: sys.modules.get(name) for name in stubs}
        try:
            sys.modules.update(stubs)
            spec = importlib.util.spec_from_file_location(
                "_opensubtitles_api_under_test",
                os.path.join(root_path, "app", "helper", "opensubtitles.py")
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
        finally:
            for name, original in originals.items():
                if original is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = original

    @classmethod
    def _load_subtitle_module(cls):
        """Exercise real subtitle orchestration without unrelated browser/UI imports."""
        stubs = {
            "lxml": types.SimpleNamespace(etree=object),
            "log": types.SimpleNamespace(info=lambda *args, **kwargs: None),
            "app.conf": types.SimpleNamespace(SiteConf=object),
            "app.helper": types.SimpleNamespace(OpenSubtitles=cls.module.OpenSubtitles),
            "app.helper.subtitle_align": types.SimpleNamespace(SubtitleAligner=object),
            "app.utils": types.SimpleNamespace(**{name: object for name in (
                "RequestUtils", "PathUtils", "SystemUtils", "StringUtils", "ExceptionUtils")}),
            "app.utils.commons": types.SimpleNamespace(singleton=lambda klass: klass),
            "app.utils.types": types.SimpleNamespace(MediaType=cls.module.MediaType, RmtMode=object),
            "config": types.SimpleNamespace(Config=cls.module.Config, RMT_MEDIAEXT=[".mkv"],
                                            RMT_SUBEXT=[".srt", ".ass", ".ssa", ".smi", ".vtt", ".sub"]),
            "version": types.SimpleNamespace(APP_VERSION="test"),
        }
        with patch.dict(sys.modules, stubs):
            health_spec = importlib.util.spec_from_file_location(
                "app.helper.subtitle_health",
                Path(__file__).resolve().parents[1] / "app" / "helper" / "subtitle_health.py")
            health_module = importlib.util.module_from_spec(health_spec)
            health_spec.loader.exec_module(health_module)
            sys.modules["app.helper.subtitle_health"] = health_module
            spec = importlib.util.spec_from_file_location(
                "_subtitle_under_test", Path(__file__).resolve().parents[1] / "app" / "subtitle.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module

    def _subtitle(self):
        subtitle = object.__new__(self.subtitle_module.Subtitle)
        subtitle._server = "opensubtitles"
        subtitle._opensubtitles_enable = True
        subtitle.opensubtitles = Mock(languages=["zh-cn", "ze", "zh-tw"])
        subtitle.opensubtitles.is_configured.return_value = (True, "")
        return subtitle

    @staticmethod
    def _client():
        return OpenSubtitlesApiTest.module.OpenSubtitles({
            "api_key": "api-key",
            "username": "user",
            "password": "password",
            "languages": "zh-cn,ze,zh-tw",
        })

    def test_moviehash_matches_fixed_binary_sample(self):
        OpenSubtitles = self.module.OpenSubtitles
        with tempfile.NamedTemporaryFile(delete=False) as media_file:
            media_file.write(bytes(range(256)) * 512)
            media_path = media_file.name
        try:
            self.assertEqual("a0601fdf9f610000", OpenSubtitles.calculate_moviehash(media_path))
        finally:
            os.remove(media_path)

    def test_movie_search_uses_numeric_imdb_id(self):
        MediaType = self.module.MediaType
        client = self._client()
        calls = []

        def fake_search(params):
            calls.append(params)
            return [], ""

        item = {
            "type": MediaType.MOVIE,
            "file": "missing-video",
            "file_ext": ".mkv",
            "name": "Movie",
            "imdbid": "tt1234567",
        }
        with patch.object(client, "_search", side_effect=fake_search):
            candidates, error = client.search_subtitles(item)

        self.assertEqual([], candidates)
        self.assertEqual("", error)
        self.assertEqual([{"imdb_id": 1234567}], calls)

    def test_tv_search_uses_parent_imdb_and_exact_episode(self):
        MediaType = self.module.MediaType
        client = self._client()
        calls = []

        def fake_search(params):
            calls.append(params)
            return [], ""

        item = {
            "type": MediaType.TV,
            "file": "missing-video",
            "file_ext": ".mkv",
            "name": "Show",
            "imdbid": "tt7654321",
            "season": 2,
            "episode": 3,
        }
        with patch.object(client, "_search", side_effect=fake_search):
            client.search_subtitles(item)

        self.assertEqual([{
            "parent_imdb_id": 7654321,
            "season_number": 2,
            "episode_number": 3,
        }], calls)

    def test_hash_match_outranks_metadata_and_is_high_confidence(self):
        MediaType = self.module.MediaType
        client = self._client()
        item = {
            "type": MediaType.MOVIE,
            "file": "Movie.2026.1080p-GROUP",
            "file_ext": ".mkv",
            "imdbid": "tt1234567",
        }
        candidates = [
            {
                "file_id": 1, "language": "zh-cn", "release": "unrelated",
                "moviehash_match": True, "from_trusted": False, "ai_translated": False,
                "ratings": 0, "download_count": 1, "feature": {}
            },
            {
                "file_id": 2, "language": "zh-cn", "release": "Movie.2026.1080p-GROUP",
                "moviehash_match": False, "from_trusted": True, "ai_translated": False,
                "ratings": 10, "download_count": 999,
                "feature": {"imdb_id": 1234567}
            },
        ]

        ranked = client._score_candidates(item, candidates)

        self.assertEqual(1, ranked[0]["file_id"])
        self.assertTrue(ranked[0]["high_confidence"])
        self.assertEqual("moviehash", ranked[0]["match_type"])

    def test_release_normalization_keeps_dotted_release_tokens(self):
        OpenSubtitles = self.module.OpenSubtitles
        similarity = OpenSubtitles.release_similarity(
            "Movie.2026.1080p-GROUP",
            "Movie.2026.1080p-GROUP.mkv"
        )
        self.assertEqual(1.0, similarity)

    def test_wrong_feature_type_is_not_high_confidence(self):
        MediaType = self.module.MediaType
        client = self._client()
        ranked = client._score_candidates({
            "type": MediaType.MOVIE,
            "file": "Movie.2026.1080p-GROUP",
            "file_ext": ".mkv",
            "imdbid": "tt1234567",
        }, [{
            "file_id": 3, "language": "zh-cn", "release": "Movie.2026.1080p-GROUP",
            "moviehash_match": False, "from_trusted": True, "ai_translated": False,
            "ratings": 10, "download_count": 100,
            "feature": {"feature_type": "Episode", "imdb_id": 1234567},
        }])

        self.assertFalse(ranked[0]["identity_match"])
        self.assertFalse(ranked[0]["high_confidence"])

    def test_download_sends_api_key_and_fresh_bearer_once(self):
        client = self._client()
        client._token = "jwt-token"
        request_headers = []
        post_calls = []

        class FakeRequestUtils:
            def __init__(self, *args, **kwargs):
                request_headers.append(kwargs.get("headers") or {})

            def post_res(self, url, params=None, allow_redirects=True, files=None, json=None):
                post_calls.append((url, json))
                return FakeResponse(payload={"link": "https://download", "remaining": 4})

            def get_res(self, *args, **kwargs):
                raise AssertionError("download must use POST")

        with patch.object(self.module, "RequestUtils", FakeRequestUtils):
            payload, error = client.download(123)

        self.assertEqual("", error)
        self.assertEqual("https://download", payload["link"])
        self.assertEqual(1, len(post_calls))
        self.assertEqual({"file_id": 123, "sub_format": "srt"}, post_calls[0][1])
        self.assertEqual("api-key", request_headers[0]["Api-Key"])
        self.assertEqual("Bearer jwt-token", request_headers[0]["Authorization"])

    def test_download_5xx_does_not_request_a_second_link(self):
        client = self._client()
        client._token = "jwt-token"
        post_calls = []

        class FakeRequestUtils:
            def __init__(self, *args, **kwargs):
                pass

            def post_res(self, url, params=None, allow_redirects=True, files=None, json=None):
                post_calls.append((url, json))
                return FakeResponse(status_code=503, payload={"message": "unavailable"})

        with patch.object(self.module, "RequestUtils", FakeRequestUtils):
            payload, error = client.download(123)

        self.assertIsNone(payload)
        self.assertIn("HTTP 503", error)
        self.assertEqual(1, len(post_calls))

    def test_low_confidence_returns_candidates_without_download(self):
        MediaType = self.module.MediaType

        class FakeOpenSubtitles:
            languages = ["zh-cn"]
            download_calls = 0

            @staticmethod
            def is_configured(require_login=False):
                return True, ""

            @staticmethod
            def search_subtitles(item):
                return [{
                    "file_id": 99,
                    "language": "zh-cn",
                    "release": "different-release",
                    "match_type": "candidate",
                    "similarity": 0.2,
                    "high_confidence": False,
                    "from_trusted": False,
                    "ai_translated": False,
                    "ratings": 0,
                    "download_count": 1,
                }], ""

            def download(self, file_id):
                self.download_calls += 1
                return None, "should not be called"

        with tempfile.TemporaryDirectory() as temp_dir:
            base_path = os.path.join(temp_dir, "Movie")
            subtitle = self._subtitle()
            fake = FakeOpenSubtitles()
            subtitle.opensubtitles = fake
            subtitle._opensubtitles_enable = True
            ok, result = subtitle.download_subtitle(items=[{
                "type": MediaType.MOVIE,
                "file": base_path,
                "file_ext": ".mkv",
                "name": "Movie",
                "imdbid": "tt1234567",
            }])

        self.assertFalse(ok)
        self.assertEqual(0, fake.download_calls)
        self.assertEqual(99, result["candidates"][0]["file_id"])

    def test_existing_subtitles_respect_language_and_media_identity(self):
        subtitle = self._subtitle()
        cases = [
            (".eng.srt", "English subtitle text", False),
            (".en.forced.srt", "English subtitle text", False),
            (".srt", "This is an English subtitle with enough letters.", False),
            (".srt", "这是中文字幕内容", True),
            (".chi.zh-cn.srt", "字幕", True),
            (".CHT.ASS", "字幕", True),
            (".zh-Hans.srt", "字幕", True),
            (".chi.zh-tw.srt", "字幕", True),
            (".extended.chi.srt", "字幕", True),
        ]
        for suffix, content, expected in cases:
            with self.subTest(suffix=suffix, content=content), tempfile.TemporaryDirectory() as directory:
                base = Path(directory) / "Movie.English.Chinese"
                target = Path(str(base) + suffix)
                target.write_text(content, encoding="utf-8")
                actual = subtitle._Subtitle__existing_opensubtitles_target({"file": str(base)})
                self.assertEqual(str(target) if expected else None, actual)

    def test_traditional_subtitle_does_not_block_simplified_only(self):
        subtitle = self._subtitle()
        subtitle.opensubtitles.languages = ["zh-cn"]
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "Movie"
            Path(str(base) + ".chi.zh-tw.srt").write_text("字幕", encoding="utf-8")
            self.assertIsNone(subtitle._Subtitle__existing_opensubtitles_target({"file": str(base)}))

    def test_non_chinese_download_keeps_language_suffix(self):
        subtitle = self._subtitle()
        self.assertEqual("Movie.en.srt", subtitle._Subtitle__subtitle_target({"file": "Movie"}, "en"))

    def test_batch_continues_after_existing_subtitle_and_search_failure(self):
        subtitle = self._subtitle()
        with tempfile.TemporaryDirectory() as directory:
            items = [{"name": name, "file": str(Path(directory) / name)}
                     for name in ("Existing", "Failed", "New")]
            Path(items[0]["file"] + ".chi.zh-cn.srt").write_text("字幕", encoding="utf-8")
            subtitle.opensubtitles.search_subtitles.side_effect = [
                ([], "检索失败"),
                ([{"file_id": 42, "language": "zh-cn", "high_confidence": True}], ""),
            ]
            subtitle.opensubtitles.download.return_value = ({"link": "https://example.test/sub"}, "")
            with patch.object(subtitle, "_Subtitle__fetch_temporary_subtitle",
                              return_value=("1\n00:00:01,000 --> 00:00:02,000\n你好\n", "")):
                ok, result = subtitle.download_subtitle(items)
            self.assertFalse(ok)
            self.assertIn("检索失败", result)
            self.assertTrue(Path(items[2]["file"] + ".chi.zh-cn.srt").is_file())
            self.assertEqual(2, subtitle.opensubtitles.search_subtitles.call_count)
            subtitle.opensubtitles.download.assert_called_once_with(42)

    def test_manual_candidate_cannot_be_applied_to_multiple_media(self):
        subtitle = self._subtitle()
        ok, _ = subtitle.download_subtitle([
            {"name": "A", "file": "A"}, {"name": "B", "file": "B"}], selected_file_id=42)
        self.assertFalse(ok)
        subtitle.opensubtitles.search_subtitles.assert_not_called()
        subtitle.opensubtitles.download.assert_not_called()

    def test_temporary_link_retry_does_not_request_another_download(self):
        subtitle = self._subtitle()
        client = Mock()
        client.get_res.side_effect = [FakeResponse(status_code=503), FakeResponse(
            content=b"1\n00:00:01,000 --> 00:00:02,000\nHello\n")]
        with patch.object(self.subtitle_module, "RequestUtils", return_value=client):
            text, error = subtitle._Subtitle__fetch_temporary_subtitle("https://example.test/sub")
        self.assertEqual("", error)
        self.assertIn("Hello", text)
        self.assertEqual(2, client.get_res.call_count)
        subtitle.opensubtitles.download.assert_not_called()

    def test_numbered_subtitles_prevent_another_download(self):
        for suffix, languages, expected in [
            (".zh-CN(1).srt", ["zh-cn"], True),
            (".zh-TW(12).ass", ["zh-tw"], True),
            (".zh-TW(1).srt", ["zh-cn"], False),
            (".en(1).srt", ["zh-cn"], False),
        ]:
            with self.subTest(suffix=suffix, languages=languages), tempfile.TemporaryDirectory() as directory:
                subtitle = self._subtitle()
                subtitle.opensubtitles.languages = languages
                base = Path(directory) / "Movie"
                path = Path(str(base) + suffix)
                path.write_text("字幕内容", encoding="utf-8")
                item = {"name": "Movie", "file": str(base)}
                self.assertEqual(str(path) if expected else None,
                                 subtitle._Subtitle__existing_opensubtitles_target(item))
                if expected:
                    ok, _ = subtitle.download_subtitle([item])
                    self.assertTrue(ok)
                    subtitle.opensubtitles.search_subtitles.assert_not_called()
                    subtitle.opensubtitles.download.assert_not_called()

    def test_untagged_chinese_encodings_prevent_another_download(self):
        cases = [
            ("utf-8", "这是中文字幕内容，我们现在开始学习。", "zh-cn"),
            ("utf-16", "这是中文字幕内容，我们现在开始学习。", "zh-cn"),
            ("gb18030", "这是中文字幕内容，我们现在开始学习。", "zh-cn"),
            ("big5", "這是中文字幕內容，我們現在開始學習。", "zh-tw"),
        ]
        for encoding, dialogue, language in cases:
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as directory:
                subtitle = self._subtitle()
                subtitle.opensubtitles.languages = [language]
                base = Path(directory) / "Movie"
                path = Path(str(base) + ".srt")
                raw = ("1\n00:00:01,000 --> 00:00:02,000\n" + dialogue + "\n").encode(encoding)
                path.write_bytes(raw)
                ok, _ = subtitle.download_subtitle([{"name": "Movie", "file": str(base)}])
                self.assertTrue(ok)
                subtitle.opensubtitles.search_subtitles.assert_not_called()
                subtitle.opensubtitles.download.assert_not_called()
                self.assertEqual(raw, path.read_bytes())

    def test_utf16_english_does_not_block_chinese_download(self):
        subtitle = self._subtitle()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "Movie"
            Path(str(base) + ".srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nThis is an English subtitle.\n", encoding="utf-16")
            self.assertIsNone(subtitle._Subtitle__existing_opensubtitles_target({"file": str(base)}))

    def test_download_decodes_big5_without_garbled_text(self):
        text = "1\n00:00:01,000 --> 00:00:02,000\n這是中文字幕內容，我們現在開始學習。\n"
        for encoding in ("utf-8", "utf-16", "gb18030", "big5"):
            with self.subTest(encoding=encoding):
                valid, decoded = self.subtitle_module.Subtitle._Subtitle__valid_subtitle_content(
                    text.encode(encoding))
                self.assertTrue(valid)
                self.assertEqual(text, decoded)
        for content in (b"", b"<html>not a subtitle --> </html>", b'{"error":"-->"}'):
            self.assertFalse(self.subtitle_module.Subtitle._Subtitle__valid_subtitle_content(content)[0])

    def test_download_preserves_subtitle_published_during_write(self):
        subtitle = self._subtitle()
        original_link = self.subtitle_module.os.link
        with tempfile.TemporaryDirectory() as directory:
            item = {"file": str(Path(directory) / "Movie")}
            target = Path(item["file"] + ".chi.zh-cn.srt")

            def concurrent_publish(source, destination):
                target.write_text("manually uploaded subtitle", encoding="utf-8")
                return original_link(source, destination)

            with patch.object(self.subtitle_module.os, "link", side_effect=concurrent_publish):
                ok, message = subtitle._Subtitle__save_opensubtitles_content(
                    item, {"language": "zh-cn"}, "downloaded subtitle")
            self.assertFalse(ok)
            self.assertIn("未覆盖", message)
            self.assertEqual("manually uploaded subtitle", target.read_text(encoding="utf-8"))
            self.assertEqual([target.name], sorted(p.name for p in Path(directory).iterdir()))

    def test_concurrent_publications_keep_one_complete_file_and_clean_temps(self):
        subtitle = self._subtitle()
        original_link = self.subtitle_module.os.link
        barrier = threading.Barrier(2)
        sources = []
        with tempfile.TemporaryDirectory() as directory:
            item = {"file": str(Path(directory) / "Movie")}
            target = Path(item["file"] + ".chi.zh-cn.srt")

            def racing_link(source, destination):
                sources.append(source)
                barrier.wait(timeout=5)
                return original_link(source, destination)

            def publish(content):
                return subtitle._Subtitle__save_opensubtitles_content(item, {"language": "zh-cn"}, content)

            with patch.object(self.subtitle_module.os, "link", side_effect=racing_link):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(publish, content) for content in ("A" * 10000, "B" * 20000)]
                    results = [future.result(timeout=10) for future in futures]
            self.assertEqual(1, sum(ok for ok, _ in results))
            self.assertEqual(2, len(set(sources)))
            self.assertIn(target.read_text(encoding="utf-8"), ("A" * 10000, "B" * 20000))
            self.assertEqual([target.name], sorted(p.name for p in Path(directory).iterdir()))

    def test_concurrent_download_requests_spend_quota_once(self):
        subtitle = self._subtitle()
        subtitle.opensubtitles.search_subtitles.return_value = ([{
            "file_id": 42, "language": "zh-cn", "high_confidence": True
        }], "")
        subtitle.opensubtitles.download.return_value = ({"link": "https://example.test/sub"}, "")
        start = threading.Barrier(2)
        with tempfile.TemporaryDirectory() as directory:
            item = {"name": "Movie", "file": str(Path(directory) / "Movie")}

            def request():
                start.wait(timeout=5)
                return subtitle.download_subtitle([item])

            with patch.object(subtitle, "_Subtitle__fetch_temporary_subtitle", return_value=(
                    "1\n00:00:01,000 --> 00:00:02,000\n你好\n", "")):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(request) for _ in range(2)]
                    results = [future.result(timeout=10) for future in futures]
            self.assertTrue(all(ok for ok, _ in results))
            subtitle.opensubtitles.download.assert_called_once_with(42)
            subtitle.opensubtitles.search_subtitles.assert_called_once()

    def test_failed_publication_removes_its_temporary_file(self):
        subtitle = self._subtitle()
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(subtitle, "_Subtitle__publish_file_no_replace", side_effect=OSError("read-only")):
                ok, _ = subtitle._Subtitle__save_opensubtitles_content(
                    {"file": str(Path(directory) / "Movie")}, {"language": "zh-cn"}, "subtitle")
            self.assertFalse(ok)
            self.assertEqual([], list(Path(directory).iterdir()))
