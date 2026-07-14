# -*- coding: utf-8 -*-

import os
import importlib.util
import sys
import tempfile
import types
from enum import Enum
from unittest import TestCase
from unittest.mock import patch


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
        try:
            from app.subtitle import Subtitle
            from app.utils.types import MediaType
        except ModuleNotFoundError as error:
            self.skipTest("optional NAS-Tools runtime dependency unavailable: %s" % error)

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
            subtitle = Subtitle()
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
