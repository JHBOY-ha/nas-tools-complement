"""Bound process-wide task queues and file I/O without changing transaction boundaries."""
from collections import deque
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from contextlib import contextmanager
from threading import Condition, Lock, local


class TaskQueueFull(RuntimeError):
    """Admission failed before a task or any of its side effects started."""


class BoundedExecutor(Executor):
    """FIFO work queue drained by a fixed number of standard executor workers.

    Only drain loops enter ThreadPoolExecutor's otherwise unbounded queue. Job
    cancellation removes its payload here, so repeated cancel/submit cycles
    cannot accumulate abandoned work items behind a blocked network request.
    """

    def __init__(self, max_workers, max_pending, thread_name_prefix="nastool"):
        if max_workers < 1 or max_pending < 1:
            raise ValueError("Worker and queue limits must be positive")
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix=thread_name_prefix
        )
        self._lock = Lock()
        self._pending = deque()
        self._workers = 0
        self._running = 0
        self._shutdown = False

    def submit(self, fn, /, *args, **kwargs):
        return self.submit_many([(fn, args, kwargs)])[0]

    def submit_many(self, calls):
        """Admit a batch atomically, including paired media-server callbacks."""
        calls = list(calls)
        if not calls:
            return []
        jobs = [(Future(), fn, args, kwargs) for fn, args, kwargs in calls]
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if len(self._pending) + len(jobs) > self._max_pending:
                raise TaskQueueFull("后台任务队列已满，请稍后重试")
            self._pending.extend(jobs)
            # Start at most one drain loop per available worker. All loops take
            # this lock before fetching work, keeping batch admission atomic.
            needed = min(len(self._pending), self._max_workers - self._workers)
            try:
                for _ in range(needed):
                    self._workers += 1
                    try:
                        self._executor.submit(self._drain)
                    except BaseException:
                        self._workers -= 1
                        raise
            except BaseException:
                for job in jobs:
                    self._pending.remove(job)
                raise
        futures = [job[0] for job in jobs]
        for future in futures:
            future.add_done_callback(self._discard_canceled)
        return futures

    def _discard_canceled(self, future):
        if not future.cancelled():
            return
        removed = False
        with self._lock:
            for job in self._pending:
                if job[0] is future:
                    self._pending.remove(job)
                    removed = True
                    break
        if removed:
            # Future.cancel() alone does not notify as_completed() waiters;
            # a removed job still needs the executor's cancellation transition.
            future.set_running_or_notify_cancel()

    def _drain(self):
        while True:
            with self._lock:
                if not self._pending:
                    self._workers -= 1
                    return
                future, fn, args, kwargs = self._pending.popleft()
                self._running += 1
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        result = fn(*args, **kwargs)
                    except BaseException as error:
                        future.set_exception(error)
                    else:
                        future.set_result(result)
            finally:
                with self._lock:
                    self._running -= 1
                # Release large request/media arguments before waiting for the
                # next item; completed jobs should not retain their payloads.
                del future, fn, args, kwargs
                result = None

    def snapshot(self):
        with self._lock:
            return {"running": self._running, "pending": len(self._pending),
                    "max_workers": self._max_workers, "max_pending": self._max_pending}

    def shutdown(self, wait=True, *, cancel_futures=False):
        with self._lock:
            self._shutdown = True
            canceled = list(self._pending) if cancel_futures else []
            if cancel_futures:
                self._pending.clear()
        # Callbacks execute outside the queue lock: they may inspect this pool
        # or release a task's coalescing key.
        for future, _, _, _ in canceled:
            future.cancel()
            future.set_running_or_notify_cancel()
        self._executor.shutdown(wait=wait)


class FairConcurrencyGate:
    """FIFO admission for transfer commands; nested calls share one slot."""

    def __init__(self, limit):
        if limit < 1:
            raise ValueError("Concurrency limit must be positive")
        self._limit = limit
        self._active = 0
        self._waiting = deque()
        self._condition = Condition()
        self._local = local()

    @contextmanager
    def slot(self):
        if getattr(self._local, "depth", 0):
            self._local.depth += 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        ticket = object()
        with self._condition:
            self._waiting.append(ticket)
            try:
                while self._waiting[0] is not ticket or self._active >= self._limit:
                    self._condition.wait()
            except BaseException:
                self._waiting.remove(ticket)
                self._condition.notify_all()
                raise
            self._waiting.popleft()
            self._active += 1
            self._local.depth = 1
            self._condition.notify_all()
        try:
            yield
        finally:
            self._local.depth = 0
            with self._condition:
                self._active -= 1
                self._condition.notify_all()


_transfer_gate = None
_transfer_gate_lock = Lock()


def get_transfer_gate():
    # Resolve settings once: replacing a live semaphore on configuration reload
    # would let old and new batches exceed the process-wide budget together.
    global _transfer_gate
    if _transfer_gate is None:
        # A cache alone is not single-flight: concurrent cold calls can create
        # different gates and exceed the intended budget before one is cached.
        with _transfer_gate_lock:
            if _transfer_gate is None:
                from config import Config
                _transfer_gate = FairConcurrencyGate(Config().get_workload_limit("transfer_concurrency"))
    return _transfer_gate
