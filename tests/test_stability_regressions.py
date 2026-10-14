"""稳定性治理的回归测试：连接回收、锁边界与阻塞超时。

覆盖的修复：
- 数据库连接在每个工作单元结束时归还（B2）
- 字幕管理器的 NAS I/O 不再发生在全局锁内（B3）
- 上传入口与 start() 的加锁顺序一致（B1）
- 文件转移改为按目标加锁（B1）
- 外部转移命令超时、刷流缓存有界、全量同步互斥（B0/B1）
"""
import os
import tempfile
import threading
import time
import uuid
from collections import namedtuple
from unittest import TestCase
from unittest.mock import patch

import tests.test_subtitle_upload  # optional dependency stubs
import tests.test_media_library  # media/server dependency stubs
from sqlalchemy import create_engine, text
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import QueuePool

from app.db.models import Base, SUBTITLETASK, SUBTITLETASKITEM
from app.helper.subtitle_tasks import SubtitleTaskManager
from tests.test_subtitle_tasks import _MemoryDb

_Usage = namedtuple("usage", "total used free")


class _PoolHarness:
    """一个与生产同构的小连接池，用于验证连接是否被归还。"""

    def __init__(self, pool_size=2, max_overflow=0):
        self.engine = create_engine(
            "sqlite://",
            poolclass=QueuePool,
            pool_size=pool_size,
            max_overflow=max_overflow,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self._session = scoped_session(sessionmaker(bind=self.engine, expire_on_commit=False))

    @property
    def session(self):
        return self._session

    def remove_session(self):
        self._session.remove()

    def query_one(self):
        return self.session.execute(text("SELECT 1")).scalar()


class ConnectionLifecycleTest(TestCase):
    def test_read_only_query_pins_a_connection_until_removed(self):
        """记录机制本身：不归还时，存活线程会一直占用一条连接。"""
        harness = _PoolHarness(pool_size=2)
        self.addCleanup(harness.engine.dispose)
        harness.query_one()
        self.assertEqual(harness.engine.pool.checkedout(), 1)
        harness.remove_session()
        self.assertEqual(harness.engine.pool.checkedout(), 0)

    def test_wrapped_unit_releases_connection_while_thread_stays_alive(self):
        """修复后的行为：工作单元结束即归还，线程存活不再占用连接。"""
        from app.db.session_scope import with_db_session

        harness = _PoolHarness(pool_size=2)
        self.addCleanup(harness.engine.dispose)
        # 让 release_db_connections 走注入对象的清理方法
        with patch("app.db.session_scope.remove_main_session", harness.remove_session), \
                patch("app.db.session_scope.remove_media_session", lambda: None):
            hold = threading.Event()
            started = threading.Event()

            def worker():
                with_db_session(harness.query_one)()
                started.set()
                hold.wait(5)

            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(started.wait(5))
            self.assertEqual(harness.engine.pool.checkedout(), 0,
                             "工作单元结束后线程仍存活时不应占用连接")
            hold.set()
            thread.join(5)

    def test_pool_is_not_exhausted_by_more_workers_than_connections(self):
        """修复前 N 个常驻线程会打满池；修复后超过池容量的线程也能继续工作。"""
        from app.db.session_scope import with_db_session

        harness = _PoolHarness(pool_size=2, max_overflow=0)
        self.addCleanup(harness.engine.dispose)
        with patch("app.db.session_scope.remove_main_session", harness.remove_session), \
                patch("app.db.session_scope.remove_media_session", lambda: None):
            errors = []
            hold = threading.Event()

            def worker():
                try:
                    with_db_session(harness.query_one)()
                except Exception as error:  # pragma: no cover - 失败时记录
                    errors.append(error)
                hold.wait(5)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
            hold.set()
            self.assertEqual(errors, [], "不应出现连接池耗尽")
            self.assertEqual(harness.engine.pool.checkedout(), 0)


class SubtitleLockBoundaryTest(TestCase):
    def setUp(self):
        self.db = _MemoryDb()
        self.db.init_db()
        self.manager = SubtitleTaskManager(db=self.db)
        self.task_id = str(uuid.uuid4())
        now = time.time()
        self.db.insert(SUBTITLETASK(
            ID=self.task_id, TYPE="upload", OWNER="o", STATUS="queued", PRIORITY=100,
            SERVER="emby", DEDUPE_KEY="d", PAYLOAD="{}", POLICY="{}", PHASE="queued",
            COMPLETED=0, CANCEL_REQUESTED=0, CREATED_AT=now, QUEUED_AT=now, UPDATED_AT=now
        ))
        self.db.insert(SUBTITLETASKITEM(
            TASK_ID=self.task_id, ITEM_KEY="k", LOGICAL_INDEX=0, KIND="text",
            SOURCE_NAME="a.srt", STAGED_PATH="/nonexistent/a.srt", CONTENT_HASH="h",
            SIZE=1, STATUS="queued", STAGE="staged", RESULT="{}",
            CREATED_AT=now, UPDATED_AT=now
        ))
        self.db.commit()
        self.item_id = int(self.db.query(SUBTITLETASKITEM).first().ID)

    def _instrument(self):
        """给所有会做 NAS I/O 的入口加探针，记录是否在持锁期间被调用。"""
        violations = []
        for name in ("_file_matches", "_staged_identity_matches", "_cleanup_task_staging",
                     "_cleanup_upload_temp_artifacts", "_assert_target_capacity",
                     "_upload_output_owned"):
            original = getattr(self.manager, name)

            def make(name, original):
                def wrapper(*args, **kwargs):
                    if self.manager._lock._is_owned():
                        violations.append(name)
                    return original(*args, **kwargs)
                return wrapper

            setattr(self.manager, name, make(name, original))
        return violations

    def test_no_nas_io_while_manager_lock_is_held(self):
        violations = self._instrument()
        target_dir = tempfile.mkdtemp()
        with patch("app.helper.subtitle_tasks.isolated_disk_usage",
                   lambda *a, **k: _Usage(10 ** 12, 0, 10 ** 12)), \
                patch("app.helper.subtitle_tasks.isolated_remove_tree", lambda *a, **k: None), \
                patch("app.helper.subtitle_tasks.os.remove", lambda *a, **k: None):
            self.manager.verify_upload_item_output(self.task_id, self.item_id)
            self.manager.trusted_upload_item_hashes(self.task_id, self.item_id)
            self.manager.ensure_task_target_space(
                self.task_id, os.path.join(target_dir, "movie.mkv"))
            self.manager._reconcile_upload_outputs(self.task_id)
            self.manager._upload_staging_valid(self.task_id)
            self.manager.cancel_task(self.task_id, admin=True)
            self.manager._claim_next_interactive()
            self.manager._recover_tasks()
        self.assertEqual(violations, [], "NAS I/O 不得发生在 manager 锁内")

    def test_start_never_takes_submit_lock_while_holding_manager_lock(self):
        """加锁顺序统一为 _submit_lock -> _lock，避免与 submit_upload 形成 ABBA。"""
        violations = []

        class GuardedLock:
            def __init__(self, inner, owner):
                self._inner, self._owner = inner, owner

            def acquire(self, *args, **kwargs):
                if self._owner._lock._is_owned():
                    violations.append("持有 _lock 时申请 _submit_lock")
                return self._inner.acquire(*args, **kwargs)

            def release(self):
                return self._inner.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *exc):
                self.release()
                return False

        self.manager._submit_lock = GuardedLock(self.manager._submit_lock, self.manager)
        self.manager.start()
        self.addCleanup(self.manager.shutdown, False)
        self.assertEqual(violations, [])


class TransferLockTest(TestCase):
    def test_same_target_serialises_and_distinct_targets_run_in_parallel(self):
        from app.filetransfer import _transfer_lock, _target_locks

        order = []

        def worker(name, target, hold):
            with _transfer_lock(target):
                order.append((name, "enter"))
                time.sleep(hold)
                order.append((name, "exit"))

        first = threading.Thread(target=worker, args=("A", "/media/x.mkv", 0.2))
        second = threading.Thread(target=worker, args=("B", "/media/x.mkv", 0.02))
        first.start(); second.start(); first.join(); second.join()
        self.assertEqual([name for name, _ in order], ["A", "A", "B", "B"],
                         "同一目标的转移必须互斥")

        started = time.perf_counter()
        threads = [threading.Thread(target=worker, args=(n, f"/media/{n}.mkv", 0.2))
                   for n in ("C", "D", "E")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertLess(time.perf_counter() - started, 0.45,
                        "不同目标应并行，不应全局排队")
        self.assertEqual(len(_target_locks), 0, "无人等待时应清理锁表")


class BlockingGuardTest(TestCase):
    def test_external_transfer_timeout_terminates_instead_of_hanging(self):
        from app.utils.system_utils import SystemUtils

        with patch.object(SystemUtils, "get_external_transfer_timeout", return_value=1):
            started = time.perf_counter()
            retcode, message = SystemUtils._SystemUtils__run_external_transfer(["sleep", "30"])
            elapsed = time.perf_counter() - started
        self.assertEqual(retcode, -1)
        self.assertLess(elapsed, 5, "超时必须终止子进程，而不是等待其结束")
        self.assertIn("不完整文件", message)

    def test_brush_cache_is_bounded(self):
        from app.db import init_db
        init_db()
        from app.brushtask import BrushTask

        # BrushTask 由 @singleton 包装，取实例后通过 type() 访问类属性。
        instance = BrushTask()
        task_type = type(instance)
        original_cache = task_type._torrents_cache
        original_max = task_type._torrents_cache_max
        task_type._torrents_cache = type(original_cache)()
        task_type._torrents_cache_max = 64
        try:
            for index in range(5000):
                instance._BrushTask__remember_torrent("u%d" % index)
            self.assertEqual(len(task_type._torrents_cache), 64,
                             "缓存必须保持有界，不能随运行时间单调增长")
            self.assertFalse(instance._BrushTask__remember_torrent("u4999"),
                             "重复项仍应被识别为已处理")
        finally:
            task_type._torrents_cache = original_cache
            task_type._torrents_cache_max = original_max

    def test_transfer_all_sync_is_mutually_exclusive(self):
        from app.db import init_db
        init_db()
        import app.sync as sync_module

        instance = sync_module.Sync()
        calls = []
        original = instance._Sync__transfer_all_sync
        instance._Sync__transfer_all_sync = lambda sid=None: (
            calls.append(sid), time.sleep(0.2)
        )
        try:
            threads = [threading.Thread(target=instance.transfer_all_sync, args=("1",))
                       for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            instance._Sync__transfer_all_sync = original
        self.assertEqual(len(calls), 1, "并发触发全量同步应只执行一次")
        self.assertFalse(sync_module._transfer_all_lock.locked())


class DatabaseConfigTest(TestCase):
    def test_sqlite_busy_timeout_is_raised(self):
        from app.db.main_db import _Engine as main_engine
        from app.db.media_db import _Engine as media_engine

        for engine in (main_engine, media_engine):
            with engine.connect() as connection:
                self.assertEqual(
                    connection.execute(text("PRAGMA busy_timeout")).scalar(), 30000
                )

    def test_new_indexes_exist(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        expected = {
            "SUBTITLE_TASK": {"INDX_SUBTITLE_TASK_CREATED", "INDX_SUBTITLE_TASK_FINISHED",
                              "INDX_SUBTITLE_TASK_QUEUE"},
            "SUBTITLE_PROBE_CACHE": {"INDX_SUBTITLE_PROBE_CACHE_PAIR"},
            "SUBTITLE_AUDIT_STATE": {"INDX_SUBTITLE_AUDIT_STATE_PATH"},
        }
        with engine.connect() as connection:
            for table, wanted in expected.items():
                names = {row[1] for row in connection.execute(
                    text("PRAGMA index_list('%s')" % table)).fetchall()}
                self.assertTrue(wanted <= names, "%s 缺少索引 %s" % (table, wanted - names))
