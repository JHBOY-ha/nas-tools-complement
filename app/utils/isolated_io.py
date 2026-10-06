"""Reusable I/O processes with bounded admission and real elapsed-time limits."""
import atexit
import errno
import json
import queue
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager


class IsolatedIOError(OSError):
    """A safe, credential-free worker failure."""


class IsolatedIOTimeout(IsolatedIOError):
    """The result is uncertain; do not automatically replay a mutation."""


class IsolationPool:
    def __init__(self, workers=2, pending=16):
        self._maximum = workers
        self._admission = threading.BoundedSemaphore(workers + pending)
        self._condition = threading.Condition()
        self._workers = set()
        self._idle = []
        self._orphans = set()
        self._closed = False
        self._local = threading.local()
        atexit.register(self.close)

    def _spawn(self):
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name('isolated_worker.py'))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=(os.name != 'nt')
        )
        # NAS/Linux and macOS pipes are selectable; the parent never performs
        # an unbounded read or waits indefinitely for a killed worker.
        if os.name != 'nt':
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
        else:
            # Anonymous pipes are not selectable on Windows. One reader and
            # writer per bounded child provide persistent framing without
            # allocating another blocked thread for each deadline expiry.
            process._io_writes = queue.Queue(maxsize=1)
            process._io_replies = queue.Queue(maxsize=1)
            def writer():
                try:
                    while True:
                        payload = process._io_writes.get()
                        if payload is None: return
                        process.stdin.write(payload); process.stdin.flush()
                except OSError as error:
                    process._io_replies.put(error)
            def reader():
                try:
                    while True:
                        response = process.stdout.readline(32 * 1024 * 1024 + 1)
                        process._io_replies.put(response)
                        if not response: return
                except OSError as error:
                    process._io_replies.put(error)
            threading.Thread(target=writer, daemon=True, name='nastool-io-write').start()
            threading.Thread(target=reader, daemon=True, name='nastool-io-read').start()
        self._workers.add(process)
        return process

    def execute(self, operation, *, timeout=10, **arguments):
        deadline = time.monotonic() + max(float(timeout), 0.01)
        session = getattr(self._local, 'session', None)
        if session is not None:
            if not session['healthy']:
                raise IsolatedIOError(errno.EPIPE, 'I/O 会话结果未确认')
            payload = json.dumps({'operation': operation, 'arguments': arguments}, ensure_ascii=True).encode() + b'\n'
            try:
                response = self._exchange(session['process'], payload, deadline)
            except BaseException:
                session['healthy'] = False
                raise
            return self._decode(operation, response)
        if not self._admission.acquire(blocking=False):
            raise IsolatedIOError(errno.EBUSY, 'I/O 任务繁忙，请稍后重试')
        process = None
        healthy = False
        try:
            with self._condition:
                while process is None:
                    if self._closed:
                        raise IsolatedIOError(errno.EPIPE, 'I/O 服务已关闭')
                    if self._idle:
                        process = self._idle.pop()
                    elif len(self._workers) < self._maximum:
                        process = self._spawn()
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise IsolatedIOTimeout(errno.ETIMEDOUT, 'I/O 等待超过总时限')
                        self._condition.wait(remaining)
            payload = json.dumps({'operation': operation, 'arguments': arguments},
                                 ensure_ascii=True).encode() + b'\n'
            if len(payload) > 2 * 1024 * 1024:
                raise IsolatedIOError(errno.E2BIG, 'I/O 请求超过字节限制')
            response = self._exchange(process, payload, deadline)
            healthy = True
            return self._decode(operation, response)
        finally:
            if process is not None:
                if healthy and not self._closed:
                    with self._condition:
                        self._idle.append(process)
                        self._condition.notify_all()
                else:
                    self._retire(process)
            self._admission.release()

    @staticmethod
    def _decode(operation, response):
        if not response.get('ok'):
            if response.get('kind') == 'TimeoutExpired':
                raise IsolatedIOTimeout(errno.ETIMEDOUT, '外部操作超过总时限，结果未确认')
            error_type = IsolatedIOError if operation in ('http', 'resolve') else OSError
            error = error_type(response.get('errno') or errno.EIO,
                               'I/O 操作失败：%s' % response.get('kind', 'Unknown'))
            error.worker_kind = response.get('kind')
            raise error
        return response['value']

    @contextmanager
    def session(self, timeout=10):
        """Retain a worker's OS locks across a parent's persistence callback."""
        if getattr(self._local, 'session', None) is not None:
            yield self
            return
        if not self._admission.acquire(blocking=False):
            raise IsolatedIOError(errno.EBUSY, 'I/O 任务繁忙，请稍后重试')
        process = None
        deadline = time.monotonic() + timeout
        session = {'process': None, 'healthy': True}
        try:
            with self._condition:
                while process is None:
                    if self._closed:
                        raise IsolatedIOError(errno.EPIPE, 'I/O 服务已关闭')
                    if self._idle:
                        process = self._idle.pop()
                    elif len(self._workers) < self._maximum:
                        process = self._spawn()
                    else:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise IsolatedIOTimeout(errno.ETIMEDOUT, 'I/O 锁会话等待超时')
                        self._condition.wait(remaining)
            session['process'] = process
            self._local.session = session
            yield self
        finally:
            self._local.session = None
            if process is not None:
                if session['healthy'] and not self._closed:
                    with self._condition:
                        self._idle.append(process)
                        self._condition.notify_all()
                else:
                    self._retire(process)
            self._admission.release()

    @staticmethod
    def _exchange(process, payload, deadline):
        if os.name == 'nt':
            try:
                process._io_writes.put(payload, timeout=max(deadline - time.monotonic(), 0.01))
                output = process._io_replies.get(timeout=max(deadline - time.monotonic(), 0.01))
            except (queue.Full, queue.Empty):
                raise IsolatedIOTimeout(errno.ETIMEDOUT, 'I/O 超过总时限，结果未确认') from None
            if isinstance(output, OSError):
                raise output
            if not output:
                raise IsolatedIOError(errno.EPIPE, 'I/O 子进程退出，结果未确认')
            if len(output) > 32 * 1024 * 1024:
                raise IsolatedIOError(errno.E2BIG, 'I/O 响应超过字节限制')
            return json.loads(output)
        sent = 0
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IsolatedIOTimeout(errno.ETIMEDOUT, 'I/O 超过总时限，结果未确认，请检查后重试')
                ready = selector.select(remaining)
                for key, events in ready:
                    if key.fileobj is process.stdin and events & selectors.EVENT_WRITE:
                        written = os.write(process.stdin.fileno(), payload[sent:sent + 65536])
                        sent += written
                        if sent == len(payload):
                            selector.unregister(process.stdin)
                    elif events & selectors.EVENT_READ:
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            raise IsolatedIOError(errno.EPIPE, 'I/O 子进程退出，结果未确认')
                        output.extend(chunk)
                        if len(output) > 32 * 1024 * 1024:
                            raise IsolatedIOError(errno.E2BIG, 'I/O 响应超过字节限制')
                        if output.endswith(b'\n'):
                            return json.loads(output)

    def _retire(self, process):
        try:
            if os.name != 'nt':
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:
            # A D-state process can survive SIGKILL until its kernel call ends.
            # Keep its capacity charged; replacement spawning must stay bounded.
            with self._condition:
                self._orphans.add(process)
            threading.Thread(target=self._reap, args=(process,), daemon=True,
                             name='nastool-io-reaper').start()
        else:
            if self._group_alive(process):
                # A killed leader can leave an uninterruptible descendant.
                # Keep the group's budget until every member has disappeared.
                with self._condition:
                    self._orphans.add(process)
                threading.Thread(target=self._reap, args=(process,), daemon=True,
                                 name='nastool-io-group-reaper').start()
            else:
                self._finished(process)

    @staticmethod
    def _group_alive(process):
        if os.name == 'nt':
            return False
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False

    def _reap(self, process):
        process.wait()
        while self._group_alive(process):
            time.sleep(0.1)
        self._finished(process)

    def _finished(self, process):
        if os.name == 'nt':
            try:
                process._io_writes.put_nowait(None)
            except queue.Full:
                pass
        for stream in (process.stdin, process.stdout):
            if not stream.closed:
                stream.close()
        with self._condition:
            self._workers.discard(process)
            self._orphans.discard(process)
            self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            return {'processes': len(self._workers), 'idle': len(self._idle),
                    'unreaped': len(self._orphans), 'limit': self._maximum}

    def close(self):
        with self._condition:
            self._closed = True
            idle, self._idle = self._idle, []
            self._condition.notify_all()
        for process in idle:
            self._retire(process)


_pool_lock = threading.Lock()
_pools = {}


def get_io_pool(kind='filesystem'):
    with _pool_lock:
        if kind not in _pools:
            # Separate file operations from external network requests so a slow
            # provider cannot consume capacity needed to publish task outputs.
            limits = {'filesystem': 2, 'bulk': 2, 'network': 4}
            _pools[kind] = IsolationPool(limits[kind])
        return _pools[kind]


def isolated_stat(path, *, follow_symlinks=True, timeout=10):
    value = get_io_pool().execute('stat', path=os.fspath(path), follow=follow_symlinks, timeout=timeout)
    return os.stat_result(value['values'], value['extra'])


def isolated_disk_usage(path):
    from collections import namedtuple
    value = get_io_pool().execute('disk_usage', path=os.fspath(path))
    return namedtuple('usage', 'total used free')(*value)


def isolated_hash(path, timeout=300):
    # Long file reads share bulk capacity with copies, leaving stat/free-space
    # checks able to run while media bytes are moving.
    return get_io_pool('bulk').execute('hash', path=os.fspath(path), timeout=timeout)


def isolated_remove_tree(path):
    try:
        before = isolated_stat(path, follow_symlinks=False)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return True
        raise
    return get_io_pool().execute('remove_tree', path=os.fspath(path),
                                 identity=[before.st_dev, before.st_ino], timeout=60)
