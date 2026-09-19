import json
import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import log
from app.utils import ExceptionUtils, StringUtils
from config import Config

try:
    from openai import APIConnectionError, APIError, APIStatusError, APITimeoutError, OpenAI
    OPENAI_IMPORT_ERROR = None
except Exception as err:
    APIConnectionError = None
    APIError = None
    APIStatusError = None
    APITimeoutError = None
    OpenAI = None
    OPENAI_IMPORT_ERROR = err


class LLMClient:
    """
    统一封装 OpenAI Chat Completions 兼容协议。

    业务层只依赖本类；协议传输、有限重试和异常结构由 OpenAI SDK 负责。
    Base URL 优先填写 API 根地址，也容忍完整的 /chat/completions 请求地址。
    """

    def __init__(self, config=None):
        self._config = config if config is not None else (Config().get_config("llm") or {})
        self._base_url = str(self._config.get("base_url") or self._config.get("api_base") or "").strip()
        self._api_base_url, self._base_query = self.__parse_base_url(self._base_url)
        self._api_key = str(self._config.get("api_key") or "").strip()
        self._model = str(self._config.get("model") or "").strip()
        self._timeout = self.__parse_int(self._config.get("timeout"), min_val=1, default=20)
        self._max_retries = self.__parse_int(self._config.get("max_retries"), min_val=0, default=2)
        self._max_tokens = self.__parse_int(self._config.get("max_tokens"), min_val=1, default=1024)
        self._thinking = self.__parse_thinking(self._config.get("thinking"))
        self._extra_body = self.__parse_mapping(self._config.get("extra_body"))
        self._extra_headers = self.__parse_mapping(self._config.get("extra_headers"))
        self._extra_query = self.__parse_mapping(self._config.get("extra_query"))
        self._client = None
        self._enabled = StringUtils.to_bool(
            self._config.get("enable", self._config.get("enabled")), False
        )

    def is_ready(self, require_enable=False):
        if require_enable and not self._enabled:
            return False
        return bool(OpenAI and self._api_base_url and self._api_key and self._model)

    def get_status(self):
        if not self.is_ready(require_enable=False):
            if OPENAI_IMPORT_ERROR:
                log.warn("【LLM】连接测试失败，OpenAI SDK 加载异常：%s" % str(OPENAI_IMPORT_ERROR))
            else:
                log.warn("【LLM】连接测试配置不完整，请检查 Base URL、API Key 和 Model")
            return False
        try:
            response = self.__request_openai(
                system_prompt="You are a health-check assistant.",
                user_prompt="OK",
                max_tokens=4
            )
            choices = self.__get_value(response, "choices", []) or []
            if not choices:
                log.warn("【LLM】连接测试响应成功，但响应中不含 choices")
                return False
            self.__log_empty_content(choices[0], context="连接测试")
            return True
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            self.__log_request_error(err, context="连接测试")
            return False

    def complete_json(self, system_prompt, user_prompt, max_tokens=None,
                      timeout=None, max_retries=None):
        content = self.complete_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
            timeout=timeout,
            max_retries=max_retries
        )
        return self.parse_json(content)

    def complete_text(self, system_prompt, user_prompt, max_tokens=None,
                      timeout=None, max_retries=None):
        if not self.is_ready(require_enable=False):
            return ""
        try:
            return self.__complete_openai(
                system_prompt,
                user_prompt,
                max_tokens,
                timeout=timeout,
                max_retries=max_retries
            )
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            self.__log_request_error(err, context="补全请求")
            return ""

    def __complete_openai(self, system_prompt, user_prompt, max_tokens=None,
                          timeout=None, max_retries=None):
        response = self.__request_openai(
            system_prompt,
            user_prompt,
            max_tokens,
            timeout=timeout,
            max_retries=max_retries
        )
        if not response:
            return ""
        choices = self.__get_value(response, "choices", []) or []
        if not choices:
            log.warn("【LLM】OpenAI兼容接口响应成功，但响应中不含 choices")
            return ""
        choice = choices[0]
        message = self.__get_value(choice, "message") or {}
        content = self.extract_text_content(self.__get_value(message, "content"))
        if not content:
            self.__log_empty_content(choice, context="补全请求")
        return content

    def __request_openai(self, system_prompt, user_prompt, max_tokens=None,
                         timeout=None, max_retries=None):
        kwargs = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt or ""},
                {"role": "user", "content": user_prompt or ""}
            ],
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": 0
        }
        extra_body = dict(self._extra_body)
        # thinking 并非 OpenAI 标准字段，仅在用户明确配置时通过 SDK extra_body 发送。
        if self._thinking:
            extra_body["thinking"] = {"type": self._thinking}
        if extra_body:
            kwargs["extra_body"] = extra_body
        if self._extra_headers:
            kwargs["extra_headers"] = self._extra_headers
        extra_query = dict(self._base_query)
        extra_query.update(self._extra_query)
        if extra_query:
            kwargs["extra_query"] = extra_query
        client = self.__get_client()
        if timeout is not None or max_retries is not None:
            client = client.with_options(
                timeout=self._timeout if timeout is None else timeout,
                max_retries=self._max_retries if max_retries is None else max_retries
            )
        return client.chat.completions.create(**kwargs)

    def __get_client(self):
        if not self._client:
            self._client = OpenAI(
                api_key=self._api_key,
                base_url=self._api_base_url,
                timeout=self._timeout,
                max_retries=self._max_retries
            )
        return self._client

    @classmethod
    def __log_empty_content(cls, choice, context):
        message = cls.__get_value(choice, "message") or {}
        content = cls.extract_text_content(cls.__get_value(message, "content"))
        if content:
            return
        reasoning_content = cls.extract_text_content(cls.__get_value(message, "reasoning_content"))
        finish_reason = cls.__get_value(choice, "finish_reason", "") or ""
        log.info(
            "【LLM】%s最终内容为空：reasoning_content_len=%s, finish_reason=%s"
            % (context, len(reasoning_content), finish_reason)
        )

    @staticmethod
    def __parse_base_url(base_url):
        """
        将完整 /chat/completions 地址还原为 SDK 所需的 API 根地址，
        同时保留原 URL 查询参数并在请求时通过 extra_query 传递。
        """
        value = str(base_url or "").strip()
        if not value:
            return "", {}
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in ["http", "https"] or not parsed.netloc:
            return "", {}
        path = (parsed.path or "").rstrip("/")
        suffix = "/chat/completions"
        if path.lower().endswith(suffix):
            path = path[:-len(suffix)]
        path = "%s/" % path.rstrip("/") if path else "/"
        return (
            urlunsplit((parsed.scheme, parsed.netloc, path, "", "")),
            dict(parse_qsl(parsed.query, keep_blank_values=True))
        )

    @classmethod
    def parse_json(cls, content):
        if not content:
            return None
        content = str(content).strip()
        content = cls.__strip_json_fence(content)
        for candidate in [content, cls.__extract_json_object(content), cls.__extract_json_array(content)]:
            if not candidate:
                continue
            try:
                return json.loads(candidate)
            except Exception:
                pass
        return None

    @classmethod
    def extract_text_content(cls, content):
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_list = []
            for item in content:
                if isinstance(item, str):
                    text_list.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        text_list.append(str(text))
                else:
                    text = getattr(item, "text", None) or getattr(item, "content", None)
                    if text:
                        text_list.append(str(text))
            return "\n".join(text_list).strip()
        return str(content).strip() if content else ""

    @staticmethod
    def __get_value(obj, key, default=None):
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def __parse_mapping(value):
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        return {}

    @staticmethod
    def __log_request_error(err, context):
        category = "unexpected"
        detail = str(err)
        if APITimeoutError is not None and isinstance(err, APITimeoutError):
            category = "timeout"
        elif APIConnectionError is not None and isinstance(err, APIConnectionError):
            category = "connection"
        elif APIStatusError is not None and isinstance(err, APIStatusError):
            category = "http_status"
            detail = "status=%s, %s" % (getattr(err, "status_code", "unknown"), detail)
        elif APIError is not None and isinstance(err, APIError):
            category = "api"
        detail = re.sub(r"\s+", " ", detail or "").strip()[:1000]
        log.error(
            "【LLM】%s失败：category=%s, error_type=%s%s"
            % (
                context,
                category,
                err.__class__.__name__,
                ", detail=%s" % detail if detail else ""
            )
        )

    @staticmethod
    def __strip_json_fence(content):
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return match.group(1).strip()
        return content

    @staticmethod
    def __extract_json_object(content):
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            return content[start:end + 1]
        return ""

    @staticmethod
    def __extract_json_array(content):
        start = content.find("[")
        end = content.rfind("]")
        if start != -1 and end != -1 and end > start:
            return content[start:end + 1]
        return ""

    @staticmethod
    def __parse_int(value, min_val=None, default=None):
        if value is None or str(value).strip() == "":
            return default
        try:
            number = int(float(value))
        except Exception:
            return default
        if min_val is not None and number < min_val:
            return default
        return number

    @staticmethod
    def __parse_thinking(value):
        if isinstance(value, dict):
            value = value.get("type")
        if isinstance(value, bool):
            return "enabled" if value else "disabled"
        value = str(value or "").strip().lower()
        if value in ["enabled", "enable", "on", "true", "1"]:
            return "enabled"
        if value in ["disabled", "disable", "off", "false", "0"]:
            return "disabled"
        return ""
