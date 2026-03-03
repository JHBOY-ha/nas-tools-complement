import json
import re

import log
from app.utils import ExceptionUtils, StringUtils
from app.utils.commons import singleton
from app.utils.types import MediaType
from config import Config

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
            system_prompt = (
                "你是媒体文件名解析助手。"
                "请严格返回 JSON 对象，不要输出任何额外文本。"
                "字段仅允许："
                "type,cn_name,en_name,year,begin_season,end_season,begin_episode,end_episode,"
                "part,resource_type,resource_effect,resource_pix,resource_team,video_encode,audio_encode,"
                "confidence,field_confidence。"
                "其中 type 只允许 movie/tv/anime。confidence 和 field_confidence 取值范围 0~1。"
            )
            user_prompt = (
                "请解析下列媒体名称并提取结构化信息。\n"
                f"title: {title}\n"
                f"subtitle: {subtitle or ''}\n"
                f"type_hint: {hint}\n"
                "若字段无法判断请省略该字段。"
            )
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
                return {}
            return self.__normalize_result(parsed)
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            log.error("【Meta】LLM 识别失败：%s" % str(err))
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
