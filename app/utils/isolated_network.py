"""Requests-compatible, bounded network transport without worker-thread DNS leaks."""
import base64
import time
from contextvars import ContextVar
from functools import wraps

import requests

from app.utils.isolated_io import get_io_pool, IsolatedIOError, IsolatedIOTimeout

_deadline = ContextVar('subtitle_network_deadline', default=None)


def current_deadline():
    return _deadline.get() or NetworkDeadline()


def network_operation(function):
    """Nested API/login/download stages share the enclosing operation budget."""
    @wraps(function)
    def bounded(*args, **kwargs):
        if _deadline.get() is not None:
            return function(*args, **kwargs)
        token = _deadline.set(NetworkDeadline())
        try:
            return function(*args, **kwargs)
        finally:
            _deadline.reset(token)
    return bounded


class NetworkDeadline:
    """One elapsed-time budget shared by DNS, redirects and retries."""
    def __init__(self, seconds=90):
        self.ends_at = time.monotonic() + float(seconds)

    def remaining(self, maximum=None):
        value = self.ends_at - time.monotonic()
        if value <= 0:
            raise requests.Timeout('字幕请求超过总时限')
        return min(value, maximum) if maximum else value


def resolve_addresses(host, port, *, deadline=None):
    try:
        values = get_io_pool('network').execute(
            'resolve', host=host, port=port,
            timeout=deadline.remaining(5) if deadline else 5
        )
        return [tuple(value[:4]) + (tuple(value[4]),) for value in values]
    except IsolatedIOTimeout:
        raise requests.Timeout('字幕地址解析超过总时限') from None
    except IsolatedIOError:
        raise requests.ConnectionError('字幕地址解析失败') from None


def bounded_request(url, *, deadline=None, max_bytes=20 * 1024 * 1024, **kwargs):
    """Return a fully bounded response; transport errors never include a URL."""
    budget = deadline or current_deadline()
    timeout = kwargs.pop('timeout', (5, 20))
    timeout = list(timeout) if isinstance(timeout, (tuple, list)) else [timeout, timeout]
    # Redirect decisions belong to the caller, which validates each new URL.
    allow_redirects = bool(kwargs.pop('allow_redirects', False))
    kwargs.pop('stream', None)
    # Preserve requests' bool/private-CA contract across the process boundary.
    kwargs.setdefault('verify', True)
    try:
        result = get_io_pool('network').execute(
            'http', url=url, max_bytes=max_bytes, timeout=budget.remaining(),
            **dict(kwargs, request_timeout=timeout, allow_redirects=allow_redirects)
        )
    except IsolatedIOTimeout:
        raise requests.Timeout('字幕请求超过总时限') from None
    except IsolatedIOError as error:
        kinds = {'SSLError': requests.exceptions.SSLError,
                 'ProxyError': requests.exceptions.ProxyError,
                 'ConnectTimeout': requests.ConnectTimeout,
                 'ReadTimeout': requests.ReadTimeout,
                 'Timeout': requests.Timeout}
        if getattr(error, 'worker_kind', None) == 'ValueError':
            raise ValueError('字幕响应超过字节限制') from None
        raise kinds.get(getattr(error, 'worker_kind', None), requests.ConnectionError)(
            '字幕请求失败'
        ) from None
    response = requests.Response()
    response.status_code = result['status']
    response.headers.update(result['headers'])
    response._content = base64.b64decode(result['body'])
    response._content_consumed = True
    # Response.raise_for_status uses its URL in exception text. Keep this
    # response URL-free while preserving status/JSON/content behavior.
    response.url = ''
    return response
