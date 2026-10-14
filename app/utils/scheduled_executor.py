"""APScheduler adapters share bounded admission and retain scheduler ownership."""
from concurrent.futures import CancelledError
from threading import Condition, Lock

from apscheduler.executors.base import run_job
from apscheduler.executors.pool import BasePoolExecutor

from app.utils.workload import BoundedExecutor
from config import Config


_pool = None
_pool_lock = Lock()


def scheduled_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                config = Config()
                _pool = BoundedExecutor(config.get_workload_limit('scheduler_workers'),
                                        config.get_workload_limit('scheduler_queue_size'),
                                        'nastool-scheduled')
    return _pool


class SharedScheduledExecutor(BasePoolExecutor):
    """Keep native run-time/misfire/max_instances semantics and a finite queue."""
    def __init__(self, interactive=False):
        if interactive:
            from app.helper.thread_helper import ThreadHelper
            pool = ThreadHelper().executor_for('interactive')
        else:
            pool = scheduled_pool()
        super().__init__(pool)
        self._condition = Condition()
        self._futures = set()
        self._closing = False

    def _do_submit_job(self, job, run_times):
        with self._condition:
            if self._closing:
                raise RuntimeError('Scheduler executor is shutting down')
            future = self._pool.submit(run_job, job, job._jobstore_alias,
                                       run_times, self._logger.name)
            self._futures.add(future)

        def completed(done):
            try:
                if done.cancelled():
                    self._run_job_error(job.id, CancelledError('Scheduled work canceled'), None)
                else:
                    error = done.exception()
                    if error is not None:
                        self._run_job_error(job.id, error, error.__traceback__)
                    else:
                        self._run_job_success(job.id, done.result())
            finally:
                with self._condition:
                    self._futures.discard(done)
                    self._condition.notify_all()
        future.add_done_callback(completed)

    def shutdown(self, wait=True):
        # Reconfiguring RSS must not shut down transfer/playback scheduling.
        # Each adapter waits/cancels only futures that it owns.
        with self._condition:
            self._closing = True
            futures = list(self._futures)
        if not wait:
            for future in futures:
                future.cancel()
            return
        with self._condition:
            while self._futures:
                self._condition.wait()
