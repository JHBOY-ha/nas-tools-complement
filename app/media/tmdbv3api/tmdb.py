# -*- coding: utf-8 -*-

import logging
import json
import os
import time
from functools import lru_cache
from urllib.parse import urlencode

import requests
import requests.exceptions

from .as_obj import AsObj
from .exceptions import TMDbException
from app.utils.security_utils import normalize_proxies, parse_rule_dict

logger = logging.getLogger(__name__)


class TMDb(object):
    TMDB_API_KEY = "TMDB_API_KEY"
    TMDB_LANGUAGE = "TMDB_LANGUAGE"
    TMDB_WAIT_ON_RATE_LIMIT = "TMDB_WAIT_ON_RATE_LIMIT"
    TMDB_DEBUG_ENABLED = "TMDB_DEBUG_ENABLED"
    TMDB_CACHE_ENABLED = "TMDB_CACHE_ENABLED"
    TMDB_PROXIES = "TMDB_PROXIES"
    TMDB_DOMAIN = "TMDB_DOMAIN"
    REQUEST_CACHE_MAXSIZE = 256
    # Cached and uncached SDK calls use the same timeout and TLS verification.
    REQUEST_TIMEOUT = 10

    def __init__(self, obj_cached=True, session=None):
        self._session = requests.Session() if session is None else session
        self._remaining = 40
        self._reset = None
        self.obj_cached = obj_cached
        if os.environ.get(self.TMDB_LANGUAGE) is None:
            os.environ[self.TMDB_LANGUAGE] = "zh-CN"
        if not os.environ.get(self.TMDB_DOMAIN):
            os.environ[self.TMDB_DOMAIN] = "https://api.themoviedb.org/3"

    @property
    def page(self):
        return os.environ["page"]

    @property
    def total_results(self):
        return os.environ["total_results"]

    @property
    def total_pages(self):
        return os.environ["total_pages"]

    @property
    def api_key(self):
        return os.environ.get(self.TMDB_API_KEY)

    @property
    def domain(self):
        return os.environ.get(self.TMDB_DOMAIN)

    @domain.setter
    def domain(self, domain):
        if domain:
            if not str(domain).startswith('http'):
                domain = "https://%s" % domain
            if not str(domain).endswith('/3'):
                domain = "%s/3" % domain
            os.environ[self.TMDB_DOMAIN] = str(domain)
        else:
            os.environ[self.TMDB_DOMAIN] = ''

    @property
    def proxies(self):
        return os.environ.get(self.TMDB_PROXIES) or "{}"

    @proxies.setter
    def proxies(self, proxies):
        # JSON safely escapes credentials and stays hashable for the HTTP LRU.
        try:
            normalized = normalize_proxies(proxies)
        except ValueError as err:
            # Keep startup available for configuration repair, but never silently
            # send requests directly when a configured proxy is invalid.
            # Search/Movie/TV use separate instances sharing this environment key.
            os.environ[self.TMDB_PROXIES] = "INVALID_PROXY"
            # The validator's field-specific error excludes URLs and passwords.
            logger.error("代理配置无效，请修正后重试：%s", err)
            return
        os.environ[self.TMDB_PROXIES] = json.dumps(normalized, sort_keys=True)

    @api_key.setter
    def api_key(self, api_key):
        os.environ[self.TMDB_API_KEY] = str(api_key)

    @property
    def language(self):
        return os.environ.get(self.TMDB_LANGUAGE)

    @language.setter
    def language(self, language):
        os.environ[self.TMDB_LANGUAGE] = language

    @property
    def wait_on_rate_limit(self):
        if os.environ.get(self.TMDB_WAIT_ON_RATE_LIMIT) == "False":
            return False
        else:
            return True

    @wait_on_rate_limit.setter
    def wait_on_rate_limit(self, wait_on_rate_limit):
        os.environ[self.TMDB_WAIT_ON_RATE_LIMIT] = str(wait_on_rate_limit)

    @property
    def debug(self):
        if os.environ.get(self.TMDB_DEBUG_ENABLED) == "True":
            return True
        else:
            return False

    @debug.setter
    def debug(self, debug):
        os.environ[self.TMDB_DEBUG_ENABLED] = str(debug)

    @property
    def cache(self):
        if os.environ.get(self.TMDB_CACHE_ENABLED) == "False":
            return False
        else:
            return True

    @cache.setter
    def cache(self, cache):
        os.environ[self.TMDB_CACHE_ENABLED] = str(cache)

    @staticmethod
    def _get_obj(result, key="results", all_details=False):
        if "success" in result and result["success"] is False:
            raise TMDbException(result["status_message"])
        if all_details is True or key is None:
            return AsObj(**result)
        else:
            return [AsObj(**res) for res in result[key]]

    @staticmethod
    def _validate_response(response):
        """Reject HTTP/API failures before they can enter the successful-response LRU."""
        status = response.status_code
        if not 200 <= status < 300:
            # Avoid HTTPError's full request URL, which includes the API key.
            reason = {
                401: "TMDB 认证失败，请检查 API Key",
                403: "TMDB 请求被拒绝",
                429: "TMDB 请求过于频繁，请稍后重试",
            }.get(status, "TMDB 服务暂时不可用" if status >= 500 else "TMDB 请求失败")
            raise TMDbException(f"{reason}（HTTP {status}）")
        try:
            result = response.json()
        except ValueError as err:
            raise TMDbException("TMDB 返回了无效的 JSON 响应") from err
        # Some configuration endpoints legitimately return a top-level array.
        if not isinstance(result, (dict, list)):
            raise TMDbException("TMDB 返回的数据格式无效")
        if isinstance(result, dict) and (result.get("success") is False or "errors" in result):
            # Report numeric API codes without reflecting arbitrary response text.
            code = result.get("status_code")
            detail = f"（TMDB {code}）" if type(code) is int else ""
            raise TMDbException("TMDB API 请求失败" + detail)
        return result

    @staticmethod
    def _request(method, url, data, proxies, session=None):
        """Share proxy parsing, TLS, timeout and response checks with API probes."""
        proxy_dict = normalize_proxies(proxies)
        transport = requests.request if session is None else session.request
        response = transport(method, url, data=data, proxies=proxy_dict,
                             verify=True, timeout=TMDb.REQUEST_TIMEOUT)
        TMDb._validate_response(response)
        return response

    @classmethod
    def test_connection(cls, api_key, proxies=None, domain="api.themoviedb.org"):
        """Probe the authenticated API without cache or shared environment mutations."""
        if not api_key:
            raise TMDbException("TMDB API Key 未配置")
        try:
            proxy_dict = normalize_proxies(proxies)
        except ValueError as err:
            raise TMDbException(str(err)) from err
        base = str(domain).rstrip("/")
        if not base.startswith(("http://", "https://")):
            base = "https://" + base
        if not base.endswith("/3"):
            base += "/3"
        url = base + "/configuration?" + urlencode({"api_key": str(api_key), "language": "zh"})
        response = cls._request("GET", url, None, proxy_dict)
        result = cls._validate_response(response)
        if not isinstance(result, dict) or not isinstance(result.get("images"), dict):
            raise TMDbException("TMDB 配置接口返回的数据不完整")
        return True

    @staticmethod
    @lru_cache(maxsize=REQUEST_CACHE_MAXSIZE)
    def cached_request(method, url, data, proxies):
        # Safely read legacy dict repr as well as the new canonical JSON value.
        proxy_dict = normalize_proxies(parse_rule_dict(proxies if proxies and proxies != "None" else "{}"))
        # lru_cache does not retain exceptions: retry a failed URL on its next use.
        return TMDb._request(method, url, data, proxy_dict)

    def cache_clear(self):
        return self.cached_request.cache_clear()

    def _call(
            self, action, append_to_response, call_cached=True, method="GET", data=None
    ):
        if self.proxies == "INVALID_PROXY":
            raise TMDbException("代理配置无效，请修正后重试")
        if self.api_key is None or self.api_key == "":
            raise TMDbException("No API key found.")

        url = "%s%s?api_key=%s&%s&language=%s" % (
            self.domain,
            action,
            self.api_key,
            append_to_response,
            self.language,
        )

        if self.cache and self.obj_cached and call_cached and method != "POST":
            req = self.cached_request(method, url, data, self.proxies)
        else:
            proxy_dict = normalize_proxies(parse_rule_dict(self.proxies if self.proxies != "None" else "{}"))
            req = self._request(method, url, data, proxy_dict, session=self._session)

        # Apply the same HTTP, authentication and payload checks to both paths.
        json = self._validate_response(req)
        headers = req.headers

        if "X-RateLimit-Remaining" in headers:
            self._remaining = int(headers["X-RateLimit-Remaining"])

        if "X-RateLimit-Reset" in headers:
            self._reset = int(headers["X-RateLimit-Reset"])

        if self._remaining < 1:
            current_time = int(time.time())
            sleep_time = self._reset - current_time

            if self.wait_on_rate_limit:
                logger.warning("Rate limit reached. Sleeping for: %d" % sleep_time)
                time.sleep(abs(sleep_time))
                self._call(action, append_to_response, call_cached, method, data)
            else:
                raise TMDbException(
                    "Rate limit reached. Try again in %d seconds." % sleep_time
                )

        if "page" in json:
            os.environ["page"] = str(json["page"])

        if "total_results" in json:
            os.environ["total_results"] = str(json["total_results"])

        if "total_pages" in json:
            os.environ["total_pages"] = str(json["total_pages"])

        if self.debug:
            logger.info(json)
            logger.info(self.cached_request.cache_info())

        return json
