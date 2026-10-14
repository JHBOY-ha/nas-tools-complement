"""Verify queue admission, isolated capacity, coalescing and actual I/O bounds."""
import threading
import time
from concurrent.futures import as_completed
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.utils.workload import BoundedExecutor, FairConcurrencyGate, TaskQueueFull


class ExecutorAdmissionTest(TestCase):
    def make_executor(self, workers=1, pending=2):
        executor = BoundedExecutor(workers, pending, 'workload-test')
        # Every test releases blocked workers before waiting for their shutdown.
        self.addCleanup(executor.shutdown)
        return executor

    def hold_worker(self, executor):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hold():
            started.set()
            release.wait(5)

        future = executor.submit(hold)
        self.assertTrue(started.wait(2))
        return future, release

    def test_full_queue_rejects_before_work_and_preserves_fifo(self):
        executor = self.make_executor(pending=2)
        running, release = self.hold_worker(executor)
        order = []
        first = executor.submit(order.append, 1)
        second = executor.submit(order.append, 2)
        with self.assertRaises(TaskQueueFull):
            executor.submit(order.append, 3)
        self.assertEqual(executor.snapshot()['pending'], 2)
        release.set()
        for future in (running, first, second):
            future.result(2)
        self.assertEqual(order, [1, 2])

    def test_cancellation_discards_payload_and_notifies_completion_waiters(self):
        executor = self.make_executor(pending=1)
        _, release = self.hold_worker(executor)
        # Repeated cancellation while the worker is blocked must not grow an
        # internal unbounded queue or prevent a subsequent real task's admission.
        for _ in range(100):
            canceled = executor.submit(lambda: None)
            self.assertTrue(canceled.cancel())
            self.assertEqual(list(as_completed([canceled], timeout=1)), [canceled])
            self.assertEqual(executor.snapshot()['pending'], 0)
        future = executor.submit(lambda: 42)
        release.set()
        self.assertEqual(future.result(2), 42)

    def test_batch_rejection_is_atomic(self):
        executor = self.make_executor(pending=2)
        _, release = self.hold_worker(executor)
        calls = []
        existing = executor.submit(calls.append, 'existing')
        with self.assertRaises(TaskQueueFull):
            executor.submit_many([(calls.append, ('a',), {}), (calls.append, ('b',), {})])
        self.assertEqual(executor.snapshot()['pending'], 1)
        release.set()
        existing.result(2)
        self.assertEqual(calls, ['existing'])

    def test_exception_does_not_lose_worker_or_later_jobs(self):
        executor = self.make_executor()

        def fail():
            raise ValueError('expected')

        with self.assertRaises(ValueError):
            executor.submit(fail).result(2)
        self.assertEqual(executor.submit(lambda: 7).result(2), 7)

    def test_shutdown_cancels_queued_jobs_but_allows_running_work_to_finish(self):
        executor = self.make_executor()
        running, release = self.hold_worker(executor)
        side_effect = Mock()
        queued = executor.submit(side_effect)
        executor.shutdown(wait=False, cancel_futures=True)
        self.assertEqual(list(as_completed([queued], timeout=1)), [queued])
        self.assertTrue(queued.cancelled())
        with self.assertRaises(RuntimeError):
            executor.submit(side_effect)
        release.set()
        running.result(2)
        side_effect.assert_not_called()

    def test_nested_admission_fails_fast_instead_of_waiting_for_its_own_worker(self):
        executor = self.make_executor(pending=1)
        _, release = self.hold_worker(executor)
        seen = []

        def nested():
            executor.submit(seen.append, 'child')
            with self.assertRaises(TaskQueueFull):
                executor.submit(seen.append, 'overflow')

        parent = executor.submit(nested)
        release.set()
        parent.result(2)
        executor.shutdown()
        self.assertEqual(seen, ['child'])


class ThreadHelperBudgetTest(TestCase):
    def make_helper(self):
        from app.helper.thread_helper import ThreadHelper
        helper_type = ThreadHelper.__closure__[0].cell_contents
        limits = {'background_workers': 1, 'background_queue_size': 2,
                  'interactive_workers': 1, 'interactive_queue_size': 2}
        with patch('app.helper.thread_helper.Config') as config:
            config.return_value.get_workload_limit.side_effect = limits.__getitem__
            helper = helper_type()
        self.addCleanup(lambda: [pool.shutdown() for pool in helper._pools.values()])
        return helper

    def test_interactive_and_service_capacity_is_independent_of_background(self):
        helper = self.make_helper()
        release, background_started, service_started = (
            threading.Event(), threading.Event(), threading.Event()
        )
        self.addCleanup(release.set)

        def hold(started):
            started.set()
            release.wait(5)

        helper.start_thread(hold, (background_started,))
        helper.start_thread(hold, (service_started,), pool='service')
        self.assertTrue(background_started.wait(2))
        self.assertTrue(service_started.wait(2))
        self.assertEqual(helper.start_thread(lambda: 'interactive', (), pool='interactive').result(2),
                         'interactive')

    def test_repeated_service_trigger_coalesces_and_key_is_released(self):
        helper = self.make_helper()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        calls = []

        def task():
            calls.append(1)
            started.set()
            release.wait(5)

        first = helper.start_thread(task, (), task_key='same-service')
        self.assertTrue(started.wait(2))
        self.assertIs(helper.start_thread(task, (), task_key='same-service'), first)
        release.set()
        first.result(2)
        helper.start_thread(task, (), task_key='same-service').result(2)
        self.assertEqual(calls, [1, 1])

    def test_worker_cleanup_runs_on_task_failure(self):
        helper = self.make_helper()
        cleaned = threading.Event()

        def fail():
            raise ValueError('expected')

        with patch('app.db.session_scope.release_db_connections', side_effect=cleaned.set):
            with self.assertRaises(ValueError):
                helper.start_thread(fail, ()).result(2)
        self.assertTrue(cleaned.is_set())

    def test_related_callbacks_are_rejected_together_when_interactive_queue_is_full(self):
        helper = self.make_helper()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hold():
            started.set()
            release.wait(5)

        helper.start_thread(hold, (), pool='interactive')
        self.assertTrue(started.wait(2))
        helper.start_thread(lambda: None, (), pool='interactive')
        first, second = Mock(), Mock()
        with self.assertRaises(TaskQueueFull):
            helper.start_threads([(first, ()), (second, ())])
        release.set()
        helper._pools['interactive'].shutdown()
        first.assert_not_called()
        second.assert_not_called()


class TransferBudgetTest(TestCase):
    def test_concurrent_cold_start_publishes_only_one_transfer_gate(self):
        import app.utils.workload as workload
        barrier = threading.Barrier(8)
        gates = []

        def read_limit(_name):
            # Hold the first initialization while the other cold callers arrive.
            time.sleep(0.05)
            return 2

        def get_gate():
            barrier.wait(2)
            gates.append(workload.get_transfer_gate())

        with patch.object(workload, '_transfer_gate', None), patch('config.Config') as config:
            config.return_value.get_workload_limit.side_effect = read_limit
            threads = [threading.Thread(target=get_gate) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(len(gates), 8)
            self.assertEqual(len({id(gate) for gate in gates}), 1)
            config.return_value.get_workload_limit.assert_called_once()

    def test_transfer_commands_share_a_process_budget_and_release_on_failure(self):
        import app.filetransfer as transfer_module
        from app.utils.types import RmtMode

        gate = FairConcurrencyGate(2)
        lock = threading.Lock()
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        active = peak = calls = 0
        errors = []

        def copy(_source, _destination):
            nonlocal active, peak, calls
            with lock:
                active += 1
                calls += 1
                peak = max(peak, active)
                if active == 2:
                    started.set()
            release.wait(5)
            with lock:
                active -= 1
            return 0, ''

        def transfer(number):
            try:
                transfer_module.FileTransfer._FileTransfer__transfer_command(
                    'source', '/media/budget-%s.mkv' % number, RmtMode.COPY
                )
            except Exception as error:
                errors.append(error)

        with patch.object(transfer_module, 'get_transfer_gate', return_value=gate), \
                patch.object(transfer_module.SystemUtils, 'copy', side_effect=copy):
            threads = [threading.Thread(target=transfer, args=(n,)) for n in range(5)]
            try:
                for thread in threads:
                    thread.start()
                self.assertTrue(started.wait(2))
                self.assertEqual(calls, 2)
            finally:
                release.set()
                for thread in threads:
                    thread.join(2)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls, 5)
        self.assertEqual(peak, 2)
        with self.assertRaises(ValueError):
            with gate.slot():
                raise ValueError('expected')
        with gate.slot(), gate.slot():
            self.assertEqual(gate._active, 1, 'Nested protected publication reuses one slot')
        self.assertEqual(gate._active, 0)
        self.assertEqual(len(transfer_module._target_locks), 0)

    def test_waiting_transfers_take_slots_in_fifo_order(self):
        gate = FairConcurrencyGate(1)
        order, threads = [], []
        with gate.slot():
            for number in range(3):
                thread = threading.Thread(target=lambda n=number: self._record(gate, order, n))
                threads.append(thread)
                thread.start()
                # Observe the actual wait queue instead of using execution time
                # as evidence for fairness on a busy CI or NAS machine.
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    with gate._condition:
                        if len(gate._waiting) == number + 1:
                            break
                    time.sleep(0.005)
                else:
                    self.fail('Transfer did not enter admission queue')
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(order, [0, 1, 2])

    @staticmethod
    def _record(gate, order, number):
        with gate.slot():
            order.append(number)


class SharedSearchBudgetTest(TestCase):
    def make_indexer(self, executor, search):
        from app.indexer.indexer import Indexer
        # Isolate the orchestration method without initializing real providers.
        indexer_type = Indexer.__closure__[0].cell_contents
        instance = indexer_type.__new__(indexer_type)
        instance._search_executor = executor
        instance._client = SimpleNamespace(search=search)
        instance._client_type = SimpleNamespace(value='test')
        instance.progress = Mock()
        instance.get_indexers = lambda: [SimpleNamespace(pri=0, id=n) for n in range(6)]
        return instance

    def test_repeated_timed_out_searches_cannot_spawn_more_running_site_requests(self):
        executor = BoundedExecutor(2, 16, 'search-budget-test')
        self.addCleanup(executor.shutdown)
        release, two_started = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        lock = threading.Lock()
        active = peak = calls = 0

        def search(*_args):
            nonlocal active, peak, calls
            with lock:
                active += 1
                calls += 1
                peak = max(peak, active)
                if active == 2:
                    two_started.set()
            release.wait(5)
            with lock:
                active -= 1
            return ['result']

        indexer = self.make_indexer(executor, search)
        with patch('app.indexer.indexer.SEARCH_TOTAL_TIMEOUT_SECONDS', 0.1):
            self.assertEqual(indexer.search_by_keyword('first', {}), [])
            self.assertTrue(two_started.wait(2))
            self.assertEqual(indexer.search_by_keyword('second', {}), [])
        self.assertEqual(calls, 2)
        self.assertEqual(peak, 2)
        self.assertEqual(executor.snapshot()['pending'], 0)
        release.set()

    def test_search_call_leaves_other_callers_queued_tasks_intact(self):
        executor = BoundedExecutor(1, 16, 'search-isolation-test')
        self.addCleanup(executor.shutdown)
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def hold():
            started.set()
            release.wait(5)

        executor.submit(hold)
        self.assertTrue(started.wait(2))
        other = executor.submit(lambda: 'other caller')
        indexer = self.make_indexer(executor, lambda *_: ['result'])
        with patch('app.indexer.indexer.SEARCH_TOTAL_TIMEOUT_SECONDS', 0.05):
            self.assertEqual(indexer.search_by_keyword('mine', {}), [])
        self.assertFalse(other.cancelled())
        release.set()
        self.assertEqual(other.result(2), 'other caller')

    def test_site_failure_does_not_discard_other_results(self):
        executor = BoundedExecutor(2, 16, 'search-results-test')
        self.addCleanup(executor.shutdown)

        def search(_order, index, *_args):
            if index.id == 1:
                raise ValueError('failed site')
            return [index.id]

        result = self.make_indexer(executor, search).search_by_keyword('query', {})
        self.assertEqual(sorted(result), [0, 2, 3, 4, 5])


class WorkloadConfigTest(TestCase):
    def test_invalid_counts_use_safe_defaults_without_changing_config(self):
        from config import Config, WORKLOAD_DEFAULTS
        config = Config()
        original = config.get_config()
        for invalid in (True, 0, -1, 2.5, 'many', 10000):
            with self.subTest(value=invalid), patch.object(
                    config, '_config', {'app': {'workload': {'transfer_concurrency': invalid}}}):
                self.assertEqual(config.get_workload_limit('transfer_concurrency'),
                                 WORKLOAD_DEFAULTS['transfer_concurrency'])
                self.assertEqual(config.get_config('app')['workload']['transfer_concurrency'], invalid)
        self.assertIs(config.get_config(), original)

    def test_explicit_budget_and_legacy_missing_settings_are_supported(self):
        from config import Config, WORKLOAD_DEFAULTS
        config = Config()
        with patch.object(config, '_config', {'app': {'workload': {'transfer_concurrency': '1'}}}):
            self.assertEqual(config.get_workload_limit('transfer_concurrency'), 1)
            self.assertEqual(config.get_workload_limit('search_workers'), WORKLOAD_DEFAULTS['search_workers'])


class WorkloadIntegrationTest(TestCase):
    def test_action_and_api_report_rejected_admission_as_failure(self):
        from web.action import WebAction
        action = WebAction.__new__(WebAction)

        def reject(_data):
            raise TaskQueueFull('busy')

        action._actions = {'work': reject}
        self.assertEqual(action.action('work', {})['code'], -1)
        self.assertFalse(action.api_action('work', {})['success'])

    def test_webhook_returns_retryable_response_without_starting_callbacks(self):
        import web.main as web_module
        helper, event, limit = Mock(), Mock(), Mock()
        helper.start_threads.side_effect = TaskQueueFull('busy')
        with patch.object(web_module, 'ThreadHelper', return_value=helper):
            body, status, headers = web_module._queue_media_webhook(event, limit, {'id': 1})
        self.assertEqual((body, status), ('busy', 503))
        self.assertIn('Retry-After', headers)
        event.assert_not_called()
        limit.assert_not_called()

    def test_telegram_polling_exits_on_reconfiguration_event(self):
        from app.message.client.telegram import Telegram
        telegram = Telegram.__new__(Telegram)
        telegram._enabled = True
        telegram._telegram_token = 'test-token'
        stopped = threading.Event()

        def poll(_url):
            stopped.set()
            return None

        with patch('app.message.client.telegram.RequestUtils') as request_utils:
            request_utils.return_value.get_res.side_effect = poll
            thread = threading.Thread(target=telegram._Telegram__start_telegram_message_proxy,
                                      args=(stopped,))
            try:
                thread.start()
                thread.join(2)
                self.assertFalse(thread.is_alive(), 'Old polling loop must free its service worker')
                request_utils.return_value.get_res.assert_called_once()
            finally:
                stopped.set()
                telegram._enabled = False
                thread.join(2)
