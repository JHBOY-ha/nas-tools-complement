import json
import re

import requests

import log
from app.utils import ExceptionUtils, StringUtils
from config import Config


class LLMClient:
    """
    统一封装 OpenAI 兼容协议和 Anthropic Messages API。
    """
    _providers = {"openai", "anthropic"}

    def __init__(self, config=None):
        self._config = config if config is not None else (Config().get_config("llm") or {})
        self._provider = str(self._config.get("provider") or "openai").strip().lower()
        if self._provider not in self._providers:
            self._provider = "openai"
        self._base_url = str(self._config.get("base_url") or self._config.get("api_base") or "").strip().rstrip("/")
        self._api_key = str(self._config.get("api_key") or "").strip()
        self._model = str(self._config.get("model") or "").strip()
        self._timeout = self.__parse_int(self._config.get("timeout"), min_val=1, default=20)
        self._max_tokens = self.__parse_int(self._config.get("max_tokens"), min_val=1, default=1024)
        self._anthropic_version = str(
            self._config.get("anthropic_version") or "2023-06-01"
        ).strip()
        self._enabled = StringUtils.to_bool(
            self._config.get("enable", self._config.get("enabled")), False
        )

    @property
    def provider(self):
        return self._provider

    def is_ready(self, require_enable=False):
        if require_enable and not self._enabled:
            return False
        return bool(self._base_url and self._api_key and self._model)

    def get_status(self):
        if not self.is_ready(require_enable=False):
            return False
        return bool(self.complete_text(
            system_prompt="You are a health-check assistant.",
            user_prompt="OK",
            max_tokens=4
        ))

    def complete_json(self, system_prompt, user_prompt, max_tokens=None):
        content = self.complete_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens
        )
        return self.parse_json(content)

    def complete_text(self, system_prompt, user_prompt, max_tokens=None):
        if not self.is_ready(require_enable=False):
            return ""
        try:
            if self._provider == "anthropic":
                return self.__complete_anthropic(system_prompt, user_prompt, max_tokens)
            return self.__complete_openai(system_prompt, user_prompt, max_tokens)
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            log.error("【LLM】请求失败：provider=%s, error=%s" % (self._provider, str(err)))
            return ""

    def __complete_openai(self, system_prompt, user_prompt, max_tokens=None):
        url = "%s/chat/completions" % self._base_url
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt or ""},
                {"role": "user", "content": user_prompt or ""}
            ],
            "max_tokens": max_tokens or self._max_tokens,
            "temperature": 0
        }
        response = requests.post(
            url,
            headers={
                "Authorization": "Bearer %s" % self._api_key,
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=self._timeout,
            verify=False
        )
        if not response or response.status_code >= 400:
            log.warn("【LLM】OpenAI兼容接口请求失败：status=%s" % (
                response.status_code if response else "none"
            ))
            return ""
        data = response.json()
        choices = data.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return self.extract_text_content(message.get("content"))

    def __complete_anthropic(self, system_prompt, user_prompt, max_tokens=None):
        url = "%s/messages" % self._base_url
        payload = {
            "model": self._model,
            "max_tokens": max_tokens or self._max_tokens,
            "system": system_prompt or "",
            "messages": [
                {"role": "user", "content": user_prompt or ""}
            ]
        }
        response = requests.post(
            url,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": self._anthropic_version,
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=self._timeout,
            verify=False
        )
        if not response or response.status_code >= 400:
            log.warn("【LLM】Anthropic接口请求失败：status=%s" % (
                response.status_code if response else "none"
            ))
            return ""
        data = response.json()
        return self.extract_text_content(data.get("content"))

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
