"""Carry only the LLM log label across workers, never the Flask request or DB session."""
from contextlib import contextmanager
from contextvars import ContextVar

from flask import has_request_context, request


_source = ContextVar("llm_source", default=None)


def current_llm_source():
    source = _source.get()
    if source:
        return source
    if has_request_context() and request.url_rule:
        # Route templates omit credentials, IDs and query parameters in actual URLs.
        return f"Web {request.url_rule.rule}"
    return None


@contextmanager
def llm_source(source):
    token = _source.set(source)
    try:
        yield
    finally:
        _source.reset(token)


def llm_log_message(category, message):
    source = current_llm_source()
    text = f"【{category}】{message}"
    return f"{text}  ·  来源：{source}" if source else text
