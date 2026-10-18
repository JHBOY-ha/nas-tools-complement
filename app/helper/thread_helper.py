from threading import RLock

import log
from app.db.session_scope import with_db_session
from app.utils.commons import singleton
from app.utils.workload import BoundedExecutor
from config import Config


@singleton
class ThreadHelper:
    executor = None

    def __init__(self):
        config = Config()
        self._pools = {
            kind: BoundedExecutor(config.get_workload_limit(kind + '_workers'),
                                  config.get_workload_limit(kind + '_queue_size'),
                                  'nastool-' + kind)
            for kind in ('background', 'interactive')
        }
        # Telegram polling lives for the service lifetime and must not occupy
        # one of the finite job workers. Reconfiguration may queue replacements.
        self._pools['service'] = BoundedExecutor(1, 4, 'nastool-service')
        self.executor = self._pools['background']
        self._keys = {}
        self._key_lock = RLock()

    def init_config(self):
        pass

    def start_thread(self, func, kwargs, *, pool='background', task_key=None):
        # 池内线程常驻，必须在每个工作单元结束时归还数据库连接，否则每个
        # 用过的线程都会永久占用一条连接（见 app/db/session_scope.py）。
        executor = self._pools[pool]
        with self._key_lock:
            key = (pool, task_key) if task_key is not None else None
            prior = self._keys.get(key) if key is not None else None
            if prior is not None and not prior.done():
                return prior
            future = executor.submit(with_db_session(func), *kwargs)
            if key is not None:
                self._keys[key] = future
            future.add_done_callback(lambda done: self._completed(done, func, key))
            return future

    def start_threads(self, calls, *, pool='interactive'):
        """Admit related callbacks together, or reject all before any runs."""
        calls = list(calls)
        futures = self._pools[pool].submit_many([
            (with_db_session(func), args, {}) for func, args in calls
        ])
        for future, (func, _) in zip(futures, calls):
            future.add_done_callback(lambda done, target=func: self._completed(done, target))
        return futures

    def _completed(self, future, func, key=None):
        if key is not None:
            with self._key_lock:
                if self._keys.get(key) is future:
                    self._keys.pop(key, None)
        if not future.cancelled():
            error = future.exception()
            if error is not None:
                # Report task identity and exception class without logging its
                # arguments, which may contain signed URLs or credentials.
                log.error("【Task】后台任务 %s 失败：%s" % (
                    getattr(func, '__name__', type(func).__name__), type(error).__name__
                ))

    def get_state(self):
        """Inspect resource pressure without querying SQLite or the NAS."""
        return {kind: executor.snapshot() for kind, executor in self._pools.items()}

    def executor_for(self, kind):
        """Share admission capacity while the caller retains its task ownership."""
        return self._pools[kind]
