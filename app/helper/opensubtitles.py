import os
import re
import struct
from difflib import SequenceMatcher

from app.utils.http_utils import RequestUtils
from app.utils.types import MediaType
from config import Config
from version import APP_VERSION


class OpenSubtitles:
    """OpenSubtitles.com REST API client.

    Search requests only require the consumer API key.  Download-link requests
    additionally require the short-lived user JWT obtained from /login.
    """

    API_ROOT = "https://api.opensubtitles.com/api/v1"
    DEFAULT_LANGUAGES = "zh-cn,ze,zh-tw"
    RELEASE_SIMILARITY_THRESHOLD = 0.75
    HASH_CHUNK_SIZE = 64 * 1024
    _MASK_64 = 0xFFFFFFFFFFFFFFFF

    def __init__(self, config=None):
        if config is None:
            config = (Config().get_config("subtitle") or {}).get("opensubtitles", {}) or {}
        self._api_key = str(config.get("api_key") or "").strip()
        self._username = str(config.get("username") or "").strip()
        self._password = str(config.get("password") or "")
        self._languages = self.parse_languages(config.get("languages"))
        self._token = None
        self._base_url = self.API_ROOT
        self._last_error = ""

    @staticmethod
    def parse_languages(value):
        values = value if isinstance(value, (list, tuple)) else str(value or OpenSubtitles.DEFAULT_LANGUAGES).split(",")
        result = []
        for language in values:
            language = str(language or "").strip().lower()
            if language and language not in result:
                result.append(language)
        return result or OpenSubtitles.DEFAULT_LANGUAGES.split(",")

    @property
    def languages(self):
        return list(self._languages)

    @staticmethod
    def _normalize_release(value):
        value = os.path.basename(str(value or ""))
        stem, extension = os.path.splitext(value)
        if extension.lower() in (".mkv", ".mp4", ".avi", ".mov", ".wmv", ".ts", ".m2ts",
                                 ".srt", ".ass", ".ssa", ".sub", ".vtt"):
            value = stem
        value = value.lower()
        return " ".join(re.findall(r"[a-z0-9]+", value))

    @classmethod
    def release_similarity(cls, release, filename):
        left = cls._normalize_release(release)
        right = cls._normalize_release(filename)
        if not left or not right:
            return 0.0
        return round(SequenceMatcher(None, left, right).ratio(), 4)

    @classmethod
    def calculate_moviehash(cls, file_path):
        """Calculate the standard OpenSubtitles 64-bit movie hash."""
        if not file_path or not os.path.isfile(file_path):
            return None
        file_size = os.path.getsize(file_path)
        if file_size < cls.HASH_CHUNK_SIZE * 2:
            return None
        value = file_size
        try:
            with open(file_path, "rb") as media_file:
                for offset in (0, file_size - cls.HASH_CHUNK_SIZE):
                    media_file.seek(offset)
                    chunk = media_file.read(cls.HASH_CHUNK_SIZE)
                    if len(chunk) != cls.HASH_CHUNK_SIZE:
                        return None
                    for number in struct.unpack("<8192Q", chunk):
                        value = (value + number) & cls._MASK_64
        except (OSError, IOError):
            return None
        return "%016x" % value

    def is_configured(self, require_login=False):
        if not self._api_key:
            return False, "未配置OpenSubtitles API Key"
        if require_login and (not self._username or not self._password):
            return False, "未配置OpenSubtitles用户名或密码"
        return True, ""

    @staticmethod
    def _safe_json(response):
        if response is None:
            return {}
        try:
            return response.json() or {}
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _response_error(response, payload, default):
        if response is None:
            return "%s：网络连接失败" % default
        message = payload.get("message") or payload.get("error") or payload.get("errors")
        if isinstance(message, list):
            message = "；".join(str(item) for item in message)
        return "%s：HTTP %s%s" % (
            default,
            response.status_code,
            "，%s" % message if message else ""
        )

    def _headers(self, authenticated=False):
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Api-Key": self._api_key,
            "User-Agent": "NAS-Tools %s" % APP_VERSION,
        }
        if authenticated and self._token:
            headers["Authorization"] = "Bearer %s" % self._token
        return headers

    def _raw_request(self, method, url, params=None, json_data=None, authenticated=False):
        request = RequestUtils(
            headers=self._headers(authenticated=authenticated),
            proxies=Config().get_proxies(),
            timeout=15
        )
        if method == "POST":
            return request.post_res(url, json=json_data)
        return request.get_res(url, params=params)

    def _request(self, method, path, params=None, json_data=None, authenticated=False,
                 retry_server_error=True):
        if authenticated:
            ok, error = self.login()
            if not ok:
                return None, {}, error
        url = "%s/%s" % (self._base_url.rstrip("/"), path.lstrip("/"))
        response = self._raw_request(method, url, params=params, json_data=json_data,
                                     authenticated=authenticated)
        if authenticated and response is not None and response.status_code == 401:
            ok, error = self.login(force=True)
            if not ok:
                return response, self._safe_json(response), error
            url = "%s/%s" % (self._base_url.rstrip("/"), path.lstrip("/"))
            response = self._raw_request(method, url, params=params, json_data=json_data,
                                         authenticated=True)
        if retry_server_error and response is not None and response.status_code >= 500:
            response = self._raw_request(method, url, params=params, json_data=json_data,
                                         authenticated=authenticated)
        payload = self._safe_json(response)
        if response is None or not response.ok:
            return response, payload, self._response_error(response, payload, "OpenSubtitles请求失败")
        return response, payload, ""

    def login(self, force=False):
        configured, error = self.is_configured(require_login=True)
        if not configured:
            return False, error
        if self._token and not force:
            return True, ""
        response = self._raw_request(
            "POST",
            "%s/login" % self.API_ROOT,
            json_data={"username": self._username, "password": self._password}
        )
        payload = self._safe_json(response)
        if response is None or not response.ok or not payload.get("token"):
            self._token = None
            self._last_error = self._response_error(response, payload, "OpenSubtitles登录失败")
            return False, self._last_error
        self._token = payload.get("token")
        base_url = str(payload.get("base_url") or "api.opensubtitles.com").strip().rstrip("/")
        if not base_url.startswith("http"):
            base_url = "https://%s" % base_url
        self._base_url = "%s/api/v1" % base_url if not base_url.endswith("/api/v1") else base_url
        return True, ""

    def get_user_info(self):
        _, payload, error = self._request("GET", "/infos/user", authenticated=True)
        if error:
            return None, error
        return payload.get("data") or {}, ""

    @staticmethod
    def _imdb_number(value):
        value = str(value or "").lower().replace("tt", "").strip()
        return int(value) if value.isdigit() else None

    @staticmethod
    def _media_path(item):
        file_path = str(item.get("file") or "")
        extension = str(item.get("file_ext") or "")
        if file_path and extension and not file_path.lower().endswith(extension.lower()):
            file_path += extension
        return file_path

    def _search(self, params):
        configured, error = self.is_configured()
        if not configured:
            return [], error
        params = {key: value for key, value in params.items() if value not in (None, "")}
        params["languages"] = ",".join(sorted(self._languages))
        _, payload, error = self._request("GET", "/subtitles", params=params)
        if error:
            return [], error
        return self._flatten_results(payload.get("data") or []), ""

    def _flatten_results(self, rows):
        candidates = []
        for row in rows:
            attributes = row.get("attributes") or {}
            if attributes.get("machine_translated"):
                continue
            language = str(attributes.get("language") or "").lower()
            if language not in self._languages:
                continue
            feature = attributes.get("feature_details") or {}
            for subtitle_file in attributes.get("files") or []:
                file_id = subtitle_file.get("file_id")
                if not file_id:
                    continue
                candidates.append({
                    "file_id": int(file_id),
                    "language": language,
                    "release": attributes.get("release") or subtitle_file.get("file_name") or "",
                    "file_name": subtitle_file.get("file_name") or "",
                    "moviehash_match": bool(attributes.get("moviehash_match") or row.get("moviehash_match")),
                    "from_trusted": bool(attributes.get("from_trusted")),
                    "ai_translated": bool(attributes.get("ai_translated")),
                    "ratings": attributes.get("ratings") or 0,
                    "download_count": attributes.get("download_count") or 0,
                    "feature": feature,
                })
        return candidates

    def _identity_matches(self, item, candidate):
        feature = candidate.get("feature") or {}
        feature_type = str(feature.get("feature_type") or "").lower()
        expected_imdb = self._imdb_number(item.get("imdbid"))
        if item.get("type") == MediaType.TV:
            return bool(
                feature_type in ("", "episode")
                and
                expected_imdb
                and int(feature.get("parent_imdb_id") or 0) == expected_imdb
                and int(feature.get("season_number") or 0) == int(item.get("season") or 0)
                and int(feature.get("episode_number") or 0) == int(item.get("episode") or 0)
            )
        return bool(
            feature_type in ("", "movie")
            and expected_imdb
            and int(feature.get("imdb_id") or 0) == expected_imdb
        )

    def _score_candidates(self, item, candidates):
        media_path = self._media_path(item)
        language_rank = {language: index for index, language in enumerate(self._languages)}
        for candidate in candidates:
            similarity = self.release_similarity(candidate.get("release"), media_path)
            identity_match = self._identity_matches(item, candidate)
            hash_match = bool(candidate.get("moviehash_match"))
            candidate["similarity"] = similarity
            candidate["identity_match"] = identity_match
            candidate["high_confidence"] = hash_match or (
                identity_match and similarity >= self.RELEASE_SIMILARITY_THRESHOLD
            )
            candidate["match_type"] = "moviehash" if hash_match else (
                "metadata" if candidate["high_confidence"] else "candidate"
            )
        return sorted(candidates, key=lambda candidate: (
            1 if candidate.get("moviehash_match") else 0,
            1 if candidate.get("high_confidence") else 0,
            -language_rank.get(candidate.get("language"), 999),
            candidate.get("similarity") or 0,
            1 if candidate.get("from_trusted") else 0,
            0 if candidate.get("ai_translated") else 1,
            candidate.get("ratings") or 0,
            candidate.get("download_count") or 0,
        ), reverse=True)

    def search_subtitles(self, item):
        """Return ranked candidates without consuming a download quota."""
        media_path = self._media_path(item)
        moviehash = self.calculate_moviehash(media_path)
        candidates = []
        if moviehash:
            candidates, error = self._search({
                "moviehash": moviehash,
                "query": os.path.basename(media_path).lower(),
            })
            if error:
                return [], error
            hash_matches = [candidate for candidate in candidates if candidate.get("moviehash_match")]
            if hash_matches:
                return self._score_candidates(item, hash_matches), ""

        imdb_id = self._imdb_number(item.get("imdbid"))
        if item.get("type") == MediaType.TV:
            if not imdb_id:
                return [], "电视剧缺少IMDb ID，无法准确检索字幕"
            params = {
                "parent_imdb_id": imdb_id,
                "season_number": item.get("season"),
                "episode_number": item.get("episode"),
            }
        elif imdb_id:
            params = {"imdb_id": imdb_id}
        else:
            params = {"query": item.get("name"), "year": item.get("year")}
        candidates, error = self._search(params)
        if error:
            return [], error
        return self._score_candidates(item, candidates), ""

    def download(self, file_id):
        """Consume at most one quota unit by requesting one temporary URL."""
        _, payload, error = self._request(
            "POST", "/download",
            json_data={"file_id": int(file_id), "sub_format": "srt"},
            authenticated=True,
            retry_server_error=False
        )
        if error:
            return None, error
        if not payload.get("link"):
            return None, "OpenSubtitles未返回字幕下载链接"
        return payload, ""
