"""Cooperative search cancellation across nested recognition and LLM calls."""
from contextlib import contextmanager
from contextvars import ContextVar


_search_events = ContextVar('search_cancel_events', default=())
_disable_llm_retries = ContextVar('search_disable_llm_retries', default=False)


class SearchCancelled(Exception):
    """Stopped before a download or subscription crossed its submission boundary."""


@contextmanager
def search_cancellation(*events, disable_llm_retries=False):
    # Bind explicitly inside each pool worker; ContextVars do not cross threads.
    # Reset on exit so a reused worker cannot cancel another user's next job.
    token = _search_events.set(_search_events.get() + tuple(event for event in events if event is not None))
    retry_token = _disable_llm_retries.set(_disable_llm_retries.get() or disable_llm_retries)
    try:
        yield
    finally:
        _disable_llm_retries.reset(retry_token)
        _search_events.reset(token)


def search_is_canceled():
    return any(event.is_set() for event in _search_events.get())


def search_disables_llm_retries():
    return _disable_llm_retries.get()
