"""A process-wide bounded FIFO writer; Sessions stay on their owning thread."""
from collections import deque
from contextlib import contextmanager
import logging
import re
import threading
import time

from sqlalchemy import event
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import scoped_session, sessionmaker

from app.utils.workload import TaskQueueFull
from .settings import DatabaseSettings


class DatabaseBusy(TaskQueueFull):
    """No SQL was started; callers must not acknowledge a rejected write."""


class DatabaseWriteError(TaskQueueFull):
    """A sanitized failure, without SQL parameters or credentials."""


class WriteCoordinator:
    def __init__(self, settings=None):
        self.settings = settings or DatabaseSettings.from_config()
        self._condition = threading.Condition()
        self._pending = deque()
        self._reservations = set()
        self._reserved_pending = {}
        self._active = False
        self._local = threading.local()
        self._samples = deque(maxlen=256)
        self._rejected = 0

    def _usage(self):
        # A reserved file operation entering FIFO is one admitted operation,
        # not both a reservation and an additional waiter. Count it once.
        return len(self._pending) + len(self._reservations) - len(self._reserved_pending)

    @contextmanager
    def reserve(self):
        """Reserve admission, not a database lock, before irreversible file I/O."""
        prior = getattr(self._local, 'reservation', None)
        if prior is not None:
            yield prior
            return
        token = object()
        with self._condition:
            if self._usage() >= self.settings.writer_queue_size:
                self._rejected += 1
                raise DatabaseBusy('数据库写入队列已满，操作未准入')
            self._reservations.add(token)
        self._local.reservation = token
        try:
            yield token
        finally:
            self._local.reservation = None
            with self._condition:
                self._reservations.discard(token)
                self._condition.notify_all()

    @contextmanager
    def slot(self, database):
        active = getattr(self._local, 'database', None)
        if active is not None:
            if active is not database:
                raise DatabaseWriteError('不能把两个数据库嵌套成一个原子事务')
            yield 0.0
            return
        started = time.monotonic()
        deadline = started + self.settings.writer_wait_seconds
        ticket = object()
        with self._condition:
            reservation = getattr(self._local, 'reservation', None)
            if reservation in self._reservations:
                # A file publication can require more than one short metadata
                # transaction. Keep its permit until the enclosing file scope
                # exits, while every SQL unit still queues fairly at the tail.
                pass
            elif self._usage() >= self.settings.writer_queue_size:
                self._rejected += 1
                raise DatabaseBusy('数据库写入队列已满，操作未准入')
            self._pending.append(ticket)
            if reservation in self._reservations:
                self._reserved_pending[ticket] = reservation
            try:
                while self._active or self._pending[0] is not ticket:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._rejected += 1
                        raise DatabaseBusy('数据库写入等待超时，操作未准入')
                    self._condition.wait(remaining)
            except BaseException:
                self._pending.remove(ticket)
                self._reserved_pending.pop(ticket, None)
                self._condition.notify_all()
                raise
            self._pending.popleft()
            self._reserved_pending.pop(ticket, None)
            self._active = True
        self._local.database = database
        acquired = time.monotonic()
        try:
            yield acquired - started
        finally:
            # This runs only after SQL/commit has returned. A caller timeout or
            # canceled Future can never release a still-running writer slot.
            self._local.database = None
            with self._condition:
                self._samples.append((acquired - started, time.monotonic() - acquired))
                self._active = False
                self._condition.notify_all()

    def snapshot(self):
        with self._condition:
            samples = list(self._samples)
            return {'active': int(self._active), 'waiting': len(self._pending),
                    'reserved': len(self._reservations), 'capacity': self.settings.writer_queue_size,
                    'queue_used': self._usage(),
                    'rejected': self._rejected, 'samples': len(samples),
                    'wait_max_seconds': max((value[0] for value in samples), default=0),
                    'transaction_max_seconds': max((value[1] for value in samples), default=0)}


_coordinator = None
_coordinator_lock = threading.Lock()
_injected_local = threading.local()


def get_coordinator():
    global _coordinator
    with _coordinator_lock:
        if _coordinator is None:
            _coordinator = WriteCoordinator()
        return _coordinator


def configure_connection(connection, settings, readonly=False):
    """Apply and verify safety PRAGMAs before a connection begins a transaction."""
    cursor = connection.cursor()
    try:
        settings_values = {'synchronous': 2, 'foreign_keys': 1,
                           'busy_timeout': settings.busy_timeout_seconds * 1000,
                           'wal_autocheckpoint': settings.wal_autocheckpoint_pages,
                           'query_only': int(readonly)}
        for name, value in settings_values.items():
            cursor.execute('PRAGMA %s=%d' % (name, value))
            cursor.execute('PRAGMA %s' % name)
            if cursor.fetchone()[0] != value:
                raise DatabaseWriteError('数据库连接安全参数未生效：%s' % name)
    finally:
        cursor.close()


class ManagedDatabase:
    """Separate read/write factories behind the existing MainDb-style interface."""
    def __init__(self, read_engine, write_engine, settings=None, coordinator=None, path=None):
        self.read_engine = read_engine
        self.write_engine = write_engine
        self.settings = settings or DatabaseSettings.from_config()
        self.coordinator = coordinator or get_coordinator()
        self.path = path
        self._local = threading.local()
        self._snapshot_mutex = threading.Lock()
        self._snapshots = {}
        self._reads = scoped_session(sessionmaker(bind=read_engine, autoflush=False,
                                                  expire_on_commit=False))
        event.listen(read_engine, 'connect', self._read_connection)
        event.listen(write_engine, 'connect', self._write_connection)
        event.listen(read_engine, 'before_cursor_execute', self._guard_read_maintenance)

    def _read_connection(self, connection, _record):
        configure_connection(connection, self.settings, readonly=True)

    def _write_connection(self, connection, _record):
        configure_connection(connection, self.settings)

    @staticmethod
    def _guard_read_maintenance(_conn, _cursor, statement, _parameters, _context, _many):
        # SQLite permits leading comments and both PRAGMA x=value / x(value).
        # Neither spelling may turn a pooled reader into an unadmitted writer.
        value = re.sub(r'^(?:\s+|--[^\r\n]*(?:\r?\n|$)|/\*.*?\*/)*', '',
                       statement, flags=re.DOTALL).lower()
        if value.startswith('pragma') and any(name in value for name in (
                'wal_checkpoint', 'optimize', 'analysis_limit', 'writable_schema')):
            raise DatabaseWriteError('数据库维护必须经过写入准入')
        if value.startswith('pragma') and '=' in value:
            raise DatabaseWriteError('只读连接不能修改数据库参数')
        if value.startswith('pragma') and '(' in value and any(name in value for name in (
                'query_only', 'journal_mode', 'synchronous', 'foreign_keys', 'busy_timeout',
                'wal_autocheckpoint', 'locking_mode', 'ignore_check_constraints')):
            raise DatabaseWriteError('只读连接不能修改数据库参数')
        if value.startswith('begin') and ('immediate' in value or 'exclusive' in value):
            raise DatabaseWriteError('只读连接不能取得数据库写锁')

    @property
    def session(self):
        current = getattr(self._local, 'write', None)
        return current['session'] if current is not None else self._reads()

    def remove_session(self):
        self._reads.remove()

    @contextmanager
    def write_transaction(self, required_bytes=0):
        current = getattr(self._local, 'write', None)
        if current is not None:
            try:
                yield current['session']
            except BaseException:
                # Nested failures, including DbPersist's explicit False
                # sentinel, poison the shared outer unit.  Returning from the
                # inner decorator cannot make that unit committable again.
                current['rollback_only'] = True
                raise
            return
        read = self._reads.registry()
        if read.new or read.dirty or read.deleted:
            raise DatabaseWriteError('写事务外存在未提交的对象修改')
        self._reads.remove()
        with self.coordinator.slot(self):
            self._check_capacity(required_bytes)
            session = sessionmaker(bind=self.write_engine, expire_on_commit=False)()
            state = {'session': session, 'rollback_only': False}
            self._local.write = state
            try:
                session.execute('BEGIN IMMEDIATE')
                yield session
                if state['rollback_only']:
                    raise DatabaseWriteError('嵌套数据库操作失败，整个事务已回滚')
                session.commit()
            except SQLAlchemyError as error:
                session.rollback()
                # SQLAlchemy exception strings include bound values. Do not
                # chain or echo them into UI responses/application logs.
                if isinstance(error, DBAPIError) and ('locked' in str(error.orig).lower()
                                                      or 'busy' in str(error.orig).lower()):
                    raise DatabaseBusy('数据库锁竞争，操作未完成，请核对后重试') from None
                raise DatabaseWriteError('数据库事务失败，结果未确认，请先核对，勿重复执行') from None
            except BaseException:
                session.rollback()
                raise
            finally:
                self._local.write = None
                session.close()

    def _check_capacity(self, required_bytes=0):
        if not self.path:
            return
        from .runtime import check_write_capacity
        check_write_capacity(self.path, self.settings, required_bytes=required_bytes)

    @contextmanager
    def reserve_write(self):
        """Check before file mutation, retain admission but not the writer lock."""
        with self.coordinator.reserve() as token:
            with self.coordinator.slot(self):
                self._check_capacity()
            yield token

    def commit(self):
        current = getattr(self._local, 'write', None)
        if current is None:
            # A clean read commit is harmless; a write without admission is not.
            session = self._reads()
            if session.new or session.dirty or session.deleted:
                raise DatabaseWriteError('数据库写入缺少事务准入')
            session.commit()
        else:
            session = current['session']
            session.flush()

    def rollback(self):
        current = getattr(self._local, 'write', None)
        if current is None:
            self._reads().rollback()
        else:
            current['rollback_only'] = True

    @contextmanager
    def maintenance(self, foreign_keys=True):
        """Schema/checkpoint work owns the same writer budget, without ORM state."""
        if getattr(self._local, 'write', None) is not None:
            # The sole write connection is already checked out. Opening a
            # second maintenance connection here would wait for itself.
            raise DatabaseWriteError('数据库维护不能嵌套在业务写事务中')
        self._reads.remove()
        with self.coordinator.slot(self):
            with self.write_engine.connect() as connection:
                if not foreign_keys:
                    connection.exec_driver_sql('PRAGMA foreign_keys=OFF')
                try:
                    yield connection
                finally:
                    if not foreign_keys:
                        connection.exec_driver_sql('PRAGMA foreign_keys=ON')

    @contextmanager
    def read_snapshot(self):
        """Pin only grouped SQL reads, never an entire job containing NAS I/O."""
        if getattr(self._local, 'write', None) is not None:
            yield self.session
            return
        prior = getattr(self._local, 'snapshot_depth', 0)
        self._local.snapshot_depth = prior + 1
        try:
            if not prior:
                self._reads.remove()
                with self._snapshot_mutex:
                    self._snapshots[threading.get_ident()] = time.monotonic()
                self._reads().execute('BEGIN')
            yield self._reads()
        finally:
            self._local.snapshot_depth -= 1
            if not prior:
                self._reads.remove()
                with self._snapshot_mutex:
                    self._snapshots.pop(threading.get_ident(), None)

    def read_diagnostics(self):
        # Only bounded counts/ages are logged, never SQL text or bound values.
        with self._snapshot_mutex:
            now = time.monotonic()
            return {'active': len(self._snapshots),
                    'oldest_seconds': max((now - started for started in self._snapshots.values()), default=0)}


@contextmanager
def write_transaction(db, required_bytes=0):
    """Use the same protocol for production stores and explicitly injected stores."""
    method = getattr(db, 'write_transaction', None)
    if callable(method):
        with (method(required_bytes=required_bytes) if required_bytes else method()) as session:
            yield session
        return
    # Injected stores retain their own connection/session management. They
    # still exercise real FIFO admission and rollback, not a no-op decorator.
    states = getattr(_injected_local, 'states', None)
    if states is None:
        states = _injected_local.states = {}
    if id(db) in states:
        try:
            yield db.session
        except BaseException:
            # Test/injected stores follow the same all-or-nothing nested
            # contract as ManagedDatabase; the outer scope checks this flag
            # even when the inner caller ignores its False return.
            states[id(db)]['failed'] = True
            raise
        return
    coordinator = get_coordinator()
    with coordinator.slot(db):
        state = states[id(db)] = {'failed': False}
        try:
            yield db.session
            if state['failed']:
                raise DatabaseWriteError('注入数据库的嵌套事务失败')
            session = db.session
            connection = session.connection().connection
            raw = getattr(connection, 'dbapi_connection', getattr(connection, 'connection', connection))
            # Legacy injected stores expose a physical commit() (production
            # exposes flush inside the scope). Do not commit twice after their
            # explicit commit: a failed empty second commit would turn an
            # already-durable claim into an unreachable active task.
            if session.new or session.dirty or session.deleted or getattr(raw, 'in_transaction', True):
                db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            states.pop(id(db), None)


@contextmanager
def read_snapshot(db):
    method = getattr(db, 'read_snapshot', None)
    if callable(method):
        with method() as session:
            yield session
    else:
        # Test/injected stores own their SQLite connection; production stores
        # always use the explicit short snapshot above, never a no-op fallback.
        yield db.session
