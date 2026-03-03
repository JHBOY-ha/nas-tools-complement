import json
import re
import time
from copy import deepcopy
from urllib.parse import quote

import log
from app.media.tmdbv3api import TMDb, Search, TMDbException
from app.utils import ExceptionUtils, RequestUtils, StringUtils
from app.utils.commons import singleton
from app.utils.types import MediaType
from config import Config, DEFAULT_TMDB_PROXY

try:
    from openai import OpenAI
except Exception:
    OpenAI = None


@singleton
class LLMMetaParser(object):
    """
    基于 OpenAI 兼容接口的媒体识别增强器
    """
    _allowed_modes = {"rule_first", "llm_first", "hybrid"}
    _llm_fields = [
        "type",
        "cn_name",
        "en_name",
        "year",
        "begin_season",
        "end_season",
        "total_seasons",
        "begin_episode",
        "end_episode",
        "total_episodes",
        "part",
        "resource_type",
        "resource_effect",
        "resource_pix",
        "resource_team",
        "video_encode",
        "audio_encode"
    ]

    def __init__(self):
        self._client = None
        self._enabled = False
        self._mode = "rule_first"
        self._base_url = ""
        self._api_key = ""
        self._model = ""
        self._timeout = 20
        self._confidence_threshold = 0.75
        self._search_context_enable = False
        self._search_max_results = 3
        self._search_timeout = 8
        self._parse_cache = {}
        self._parse_cache_ttl = 60
        self.init_config()

    def init_config(self):
        config = Config().get_config("llm") or {}
        self._enabled = StringUtils.to_bool(
            config.get("enable", config.get("enabled")), False
        )
        mode = str(config.get("mode") or "rule_first").strip().lower()
        if mode not in self._allowed_modes:
            mode = "rule_first"
        self._mode = mode
        self._base_url = str(config.get("base_url") or config.get("api_base") or "").strip()
        self._api_key = str(config.get("api_key") or "").strip()
        self._model = str(config.get("model") or "").strip()
        self._timeout = self.__parse_int(config.get("timeout"), min_val=1, default=20)
        self._confidence_threshold = self.__parse_float(
            config.get("confidence_threshold"), min_val=0, max_val=1, default=0.75
        )
        self._search_context_enable = StringUtils.to_bool(
            config.get("search_context_enable"), False
        )
        self._search_max_results = self.__parse_int(
            config.get("search_max_results"), min_val=1, max_val=10, default=3
        )
        self._search_timeout = self.__parse_int(
            config.get("search_timeout"), min_val=1, max_val=30, default=8
        )
        self._parse_cache = {}
        self._client = None

    def get_status(self):
        """
        测试连通性（用于设置页测试按钮）
        """
        if not self.__is_client_ready(require_enable=False):
            return False
        try:
            client = self.__get_client()
            if not client:
                return False
            response = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": "You are a health-check assistant."},
                    {"role": "user", "content": "OK"}
                ],
                max_tokens=1,
                temperature=0
            )
            return True if response and getattr(response, "choices", None) else False
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            log.error("【Meta】LLM 连接测试失败：%s" % str(err))
            return False

    def parse(self, title, subtitle=None, mtype_hint=None):
        """
        调用 LLM 解析媒体名称并返回标准化字段
        """
        if not title or not self.__is_client_ready(require_enable=True):
            return {}
        cache_key = self.__make_parse_cache_key(title=title, subtitle=subtitle, mtype_hint=mtype_hint)
        cached_result = self.__get_cached_parse_result(cache_key)
        if cached_result is not None:
            return cached_result
        try:
            client = self.__get_client()
            if not client:
                return {}
            hint = ""
            if mtype_hint:
                if mtype_hint == MediaType.MOVIE:
                    hint = "movie"
                elif mtype_hint == MediaType.ANIME:
                    hint = "anime"
                else:
                    hint = "tv"
            external_candidates = self.__build_external_candidates(
                title=title,
                subtitle=subtitle,
                mtype_hint=mtype_hint
            )
            system_prompt = (
                "你是媒体文件名解析助手。"
                "请严格返回 JSON 对象，不要输出任何额外文本。"
                "字段仅允许："
                "type,cn_name,en_name,year,begin_season,end_season,begin_episode,end_episode,"
                "part,resource_type,resource_effect,resource_pix,resource_team,video_encode,audio_encode。"
                "其中 type 只允许 movie/tv/anime，不要返回任何其他字段。"
                "你可能会收到 external_candidates 字段，包含 TMDB/Bangumi 检索候选，仅供参考。"
            )
            user_prompt = (
                "请解析下列媒体名称并提取结构化信息。\n"
                f"title: {title}\n"
                f"subtitle: {subtitle or ''}\n"
                f"type_hint: {hint}\n"
            )
            if external_candidates:
                user_prompt += f"external_candidates: {external_candidates}\n"
            user_prompt += "若字段无法判断请省略该字段。"
            response = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0,
                max_tokens=512
            )
            content = self.__extract_content(response)
            if content:
                log.info("【Meta】LLM原始返回：%s" % self.__shorten_text(content, 2000))
            else:
                log.info("【Meta】LLM原始返回为空")
            parsed = self.__parse_json(content)
            if not parsed:
                self.__set_cached_parse_result(cache_key, {})
                return {}
            result = self.__normalize_result(parsed)
            self.__set_cached_parse_result(cache_key, result)
            return result
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            log.error("【Meta】LLM 识别失败：%s" % str(err))
            self.__set_cached_parse_result(cache_key, {})
            return {}

    def merge_into(self, meta_info, title, subtitle=None, mtype_hint=None):
        """
        将 LLM 识别结果与规则识别结果合并
        """
        if not meta_info:
            return meta_info

        llm_result = self.parse(title=title, subtitle=subtitle, mtype_hint=mtype_hint)

        note = dict(meta_info.note or {})
        note["llm"] = {
            "enabled": self._enabled,
            "mode": self._mode,
            "applied": False
        }

        if llm_result:
            self.__apply_result(meta_info, llm_result)
            note["llm"].update({
                "applied": True,
                "confidence": llm_result.get("confidence", 0),
                "field_confidence": llm_result.get("field_confidence", {})
            })

        meta_info.note = note

        # 外部强制指定类型优先
        if mtype_hint:
            meta_info.type = mtype_hint

        return meta_info

    def __apply_result(self, meta_info, llm_result):
        for field in self._llm_fields:
            if field not in llm_result:
                continue
            value = llm_result.get(field)
            if self.__is_empty(value):
                continue
            if not self.__should_apply(field, getattr(meta_info, field, None), llm_result):
                continue
            setattr(meta_info, field, value)

    def __should_apply(self, field, current_value, llm_result):
        if self._mode == "llm_first":
            return True
        if self._mode == "rule_first":
            return self.__is_empty(current_value)

        # hybrid：空值直接补齐，非空值按置信度覆盖
        if self.__is_empty(current_value):
            return True
        field_confidence = llm_result.get("field_confidence", {}).get(field)
        if field_confidence is None:
            field_confidence = llm_result.get("confidence", 0)
        return field_confidence >= self._confidence_threshold

    def __normalize_result(self, parsed):
        result = {}
        media_type = self.__normalize_type(parsed.get("type"))
        if media_type:
            result["type"] = media_type

        for key in [
            "cn_name",
            "en_name",
            "part",
            "resource_type",
            "resource_effect",
            "resource_pix",
            "resource_team",
            "video_encode",
            "audio_encode"
        ]:
            text = self.__clean_text(parsed.get(key))
            if text:
                result[key] = text

        year = self.__parse_int(parsed.get("year"), min_val=1900, max_val=2100)
        if year:
            result["year"] = str(year)

        begin_season = self.__parse_int(parsed.get("begin_season"), min_val=1, max_val=999)
        end_season = self.__parse_int(parsed.get("end_season"), min_val=1, max_val=999)
        if begin_season and end_season and end_season < begin_season:
            begin_season, end_season = end_season, begin_season
        if begin_season:
            result["begin_season"] = begin_season
        if end_season and end_season != begin_season:
            result["end_season"] = end_season
        if begin_season:
            result["total_seasons"] = (result.get("end_season") or begin_season) - begin_season + 1

        begin_episode = self.__parse_int(parsed.get("begin_episode"), min_val=1, max_val=99999)
        end_episode = self.__parse_int(parsed.get("end_episode"), min_val=1, max_val=99999)
        if begin_episode and end_episode and end_episode < begin_episode:
            begin_episode, end_episode = end_episode, begin_episode
        if begin_episode:
            result["begin_episode"] = begin_episode
        if end_episode and end_episode != begin_episode:
            result["end_episode"] = end_episode
        if begin_episode:
            result["total_episodes"] = (result.get("end_episode") or begin_episode) - begin_episode + 1

        result["confidence"] = self.__parse_float(
            parsed.get("confidence"), min_val=0, max_val=1, default=0
        )
        field_confidence = {}
        raw_field_confidence = parsed.get("field_confidence")
        if isinstance(raw_field_confidence, dict):
            for key in self._llm_fields:
                if key not in raw_field_confidence:
                    continue
                val = self.__parse_float(raw_field_confidence.get(key), min_val=0, max_val=1)
                if val is not None:
                    field_confidence[key] = val
        result["field_confidence"] = field_confidence
        return result

    def __build_external_candidates(self, title, subtitle=None, mtype_hint=None):
        if not self._search_context_enable:
            return ""
        query_list = self.__build_search_queries(title=title, subtitle=subtitle)
        if not query_list:
            return ""
        query = query_list[0]

        payload = {}
        tmdb_candidates = self.__search_candidates_by_queries(
            search_func=lambda q: self.__search_tmdb_candidates(query=q, mtype_hint=mtype_hint),
            query_list=query_list
        )
        bangumi_candidates = self.__search_candidates_by_queries(
            search_func=self.__search_bangumi_candidates,
            query_list=query_list
        )

        if tmdb_candidates:
            payload["tmdb"] = tmdb_candidates
        if bangumi_candidates:
            payload["bangumi"] = bangumi_candidates

        log.info(
            "【Meta】LLM检索增强候选：query=%s, tmdb=%s, bangumi=%s"
            % (
                self.__shorten_text(query, 80),
                len(tmdb_candidates),
                len(bangumi_candidates)
            )
        )
        if not payload:
            return ""
        return self.__shorten_text(json.dumps(payload, ensure_ascii=False), 3000)

    @classmethod
    def __build_search_query(cls, title, subtitle=None):
        text = "%s %s" % (title or "", subtitle or "")
        return cls.__normalize_query_for_search(text)

    @classmethod
    def __build_search_queries(cls, title, subtitle=None):
        query_set = []
        raw_title = str(title or "").strip()
        base_query = cls.__build_search_query(title=title, subtitle=subtitle)
        if base_query:
            query_set.append(base_query)
        normalized_title = cls.__normalize_query_for_search(raw_title)
        if normalized_title:
            query_set.append(normalized_title)

        # 标题含中英文双名时，尝试分别检索每一段
        for segment in re.split(r"[／/|]+", raw_title):
            segment_query = cls.__normalize_query_for_search(segment)
            if segment_query:
                query_set.append(segment_query)

        # 额外构造去季集号候选
        reduced_candidates = []
        for query in list(query_set):
            reduced = re.sub(r"第\s*\d+\s*季", " ", query, flags=re.IGNORECASE)
            reduced = re.sub(r"第\s*\d+\s*[集话回]", " ", reduced, flags=re.IGNORECASE)
            reduced = re.sub(r"\bS\d{1,2}\b", " ", reduced, flags=re.IGNORECASE)
            reduced = re.sub(r"\b(?:E|EP)\d{1,4}\b", " ", reduced, flags=re.IGNORECASE)
            reduced = re.sub(r"\s+", " ", reduced).strip()
            reduced = cls.__normalize_query_for_search(reduced)
            if reduced:
                reduced_candidates.append(reduced)
        query_set.extend(reduced_candidates)

        # 去重并保持顺序
        query_list = []
        for query in query_set:
            if query and query not in query_list:
                query_list.append(query)
        return query_list[:6]

    @staticmethod
    def __normalize_query_for_search(text):
        text = str(text or "").strip()
        if not text:
            return ""
        text = re.sub(r"\[[^\]]*]", " ", text)
        text = re.sub(r"[【】\[\]\(\)\{\}]+", " ", text)
        text = re.sub(r"[._\-]+", " ", text)
        text = re.sub(
            r"\b(?:S\d{1,2}E\d{1,4}|S\d{1,2}|E\d{1,4}|EP\d{1,4}|WEB[- ]?DL|WEBRIP|BLURAY|2160P|1080P|720P|HEVC|H265|X265|X264|AAC)\b",
            " ",
            text,
            flags=re.IGNORECASE
        )
        text = re.sub(r"\s+", " ", text).strip()
        return text[:120]

    def __search_candidates_by_queries(self, search_func, query_list):
        candidates = []
        seen = set()
        for query in query_list:
            if len(candidates) >= self._search_max_results:
                break
            query_candidates = search_func(query) or []
            for item in query_candidates:
                item_key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if item_key in seen:
                    continue
                seen.add(item_key)
                candidates.append(item)
                if len(candidates) >= self._search_max_results:
                    break
        return candidates

    def __make_parse_cache_key(self, title, subtitle=None, mtype_hint=None):
        mtype = ""
        if mtype_hint:
            mtype = str(getattr(mtype_hint, "value", mtype_hint))
        return "%s|%s|%s|%s|%s|%s" % (
            str(title or "").strip(),
            str(subtitle or "").strip(),
            mtype,
            self._mode,
            self._model,
            str(self._search_context_enable)
        )

    def __get_cached_parse_result(self, cache_key):
        cache_item = self._parse_cache.get(cache_key)
        if not cache_item:
            return None
        ts = cache_item.get("ts", 0)
        if time.time() - ts > self._parse_cache_ttl:
            self._parse_cache.pop(cache_key, None)
            return None
        return deepcopy(cache_item.get("result", {}))

    def __set_cached_parse_result(self, cache_key, result):
        self._parse_cache[cache_key] = {
            "ts": time.time(),
            "result": deepcopy(result or {})
        }

    def __search_tmdb_candidates(self, query, mtype_hint=None):
        app_conf = Config().get_config("app") or {}
        tmdb_key = str(app_conf.get("rmt_tmdbkey") or "").strip()
        if not tmdb_key:
            return []
        try:
            tmdb = TMDb()
            laboratory_conf = Config().get_config("laboratory") or {}
            if laboratory_conf.get("tmdb_proxy"):
                tmdb.domain = DEFAULT_TMDB_PROXY
            else:
                tmdb.domain = app_conf.get("tmdb_domain")
            tmdb.cache = True
            tmdb.api_key = tmdb_key
            tmdb.language = "zh"
            tmdb.proxies = Config().get_proxies()

            search = Search()
            params = {"query": query, "page": 1}
            if mtype_hint == MediaType.MOVIE:
                raw_results = search.movies(params)
            elif mtype_hint in [MediaType.TV, MediaType.ANIME]:
                raw_results = search.tv_shows(params)
            else:
                raw_results = search.multi(params)

            candidates = []
            for item in (raw_results or [])[:self._search_max_results]:
                name = self.__clean_text(
                    getattr(item, "title", None) or getattr(item, "name", None),
                    max_len=120
                )
                if not name:
                    continue
                release_date = str(
                    getattr(item, "release_date", "") or getattr(item, "first_air_date", "")
                ).strip()
                year = release_date[:4] if len(release_date) >= 4 and release_date[:4].isdigit() else ""
                item_type = self.__clean_text(getattr(item, "media_type", ""), max_len=20).lower()
                if not item_type:
                    if mtype_hint == MediaType.MOVIE:
                        item_type = "movie"
                    elif mtype_hint in [MediaType.TV, MediaType.ANIME]:
                        item_type = "tv"
                candidate = {
                    "id": getattr(item, "id", None),
                    "name": name
                }
                if year:
                    candidate["year"] = year
                if item_type:
                    candidate["type"] = item_type
                candidates.append(candidate)
            return candidates
        except TMDbException as err:
            log.debug("【Meta】TMDB候选检索失败：%s" % str(err))
            return []
        except Exception as err:
            log.debug("【Meta】TMDB候选检索异常：%s" % str(err))
            return []

    def __search_bangumi_candidates(self, query):
        try:
            req_url = "https://api.bgm.tv/search/subject/%s" % quote(query)
            response = RequestUtils(
                proxies=Config().get_proxies(),
                timeout=self._search_timeout
            ).get_res(
                url=req_url,
                params={
                    "type": 2,
                    "responseGroup": "small",
                    "max_results": self._search_max_results
                }
            )
            if not response or not response.ok:
                return []
            data = response.json()
            items = data.get("list") or data.get("data") or []
            candidates = []
            for item in items[:self._search_max_results]:
                name = self.__clean_text(item.get("name"), max_len=120)
                name_cn = self.__clean_text(item.get("name_cn"), max_len=120)
                if not name and not name_cn:
                    continue
                air_date = str(item.get("air_date") or item.get("date") or "").strip()
                year = air_date[:4] if len(air_date) >= 4 and air_date[:4].isdigit() else ""
                candidate = {
                    "id": item.get("id")
                }
                if name_cn:
                    candidate["name_cn"] = name_cn
                if name:
                    candidate["name"] = name
                if year:
                    candidate["year"] = year
                candidates.append(candidate)
            return candidates
        except Exception as err:
            log.debug("【Meta】Bangumi候选检索异常：%s" % str(err))
            return []

    def __extract_content(self, response):
        if not response or not getattr(response, "choices", None):
            return ""
        message = response.choices[0].message
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_list = []
            for item in content:
                if isinstance(item, str):
                    text_list.append(item)
                elif isinstance(item, dict) and item.get("text"):
                    text_list.append(str(item.get("text")))
            return "\n".join(text_list).strip()
        return str(content).strip()

    @staticmethod
    def __parse_json(content):
        if not content:
            return {}
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(content[start:end + 1])
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    @staticmethod
    def __normalize_type(value):
        if not value:
            return None
        text = str(value).strip().lower()
        if text in ["movie", "mov", "film", "电影"]:
            return MediaType.MOVIE
        if text in ["tv", "series", "电视剧"]:
            return MediaType.TV
        if text in ["anime", "ani", "动漫", "动画"]:
            return MediaType.ANIME
        return None

    @staticmethod
    def __clean_text(value, max_len=200):
        if value is None:
            return ""
        text = str(value).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            return ""
        return text[:max_len]

    @staticmethod
    def __is_empty(value):
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (int, float)):
            return value == 0
        if isinstance(value, (list, dict, tuple, set)):
            return len(value) == 0
        return False

    @staticmethod
    def __parse_int(value, min_val=None, max_val=None, default=None):
        if value is None or str(value).strip() == "":
            return default
        try:
            number = int(float(value))
        except Exception:
            return default
        if min_val is not None and number < min_val:
            return default
        if max_val is not None and number > max_val:
            return default
        return number

    @staticmethod
    def __parse_float(value, min_val=None, max_val=None, default=None):
        if value is None or str(value).strip() == "":
            return default
        try:
            number = float(value)
        except Exception:
            return default
        if min_val is not None and number < min_val:
            return default
        if max_val is not None and number > max_val:
            return default
        return number

    def __is_client_ready(self, require_enable=True):
        if require_enable and not self._enabled:
            return False
        if not self._base_url or not self._api_key or not self._model:
            return False
        if OpenAI is None:
            log.error("【Meta】openai 依赖未安装，无法启用 LLM 识别")
            return False
        return True

    def __get_client(self):
        if not self.__is_client_ready(require_enable=False):
            return None
        if not self._client:
            self._client = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
                timeout=self._timeout
            )
        return self._client

    @staticmethod
    def __shorten_text(text, max_len):
        if not text:
            return ""
        text = str(text).strip()
        if len(text) <= max_len:
            return text
        return "%s ...[truncated]" % text[:max_len]
