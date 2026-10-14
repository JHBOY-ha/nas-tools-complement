"""Offline bootstrap, WAL compatibility checks and bounded SQLite maintenance."""
import atexit
from contextlib import closing
import json
import logging
import os
from pathlib import Path
import re
import select
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time

from .settings import DatabaseSettings, wal_runtime_supported
from .transactions import DatabaseBusy, DatabaseWriteError, configure_connection

_lock = threading.RLock()
_leases = {}
_prepared = {}
_capacity_checkers = {}
_last_maintenance = {}
_last_diagnostic = 0.0
_logger = logging.getLogger(__name__)


def acquire_instance(directory):
    """Keep an OS lease for this process; children cannot inherit its descriptor."""
    directory = str(Path(directory).resolve())
    with _lock:
        if directory in _leases:
            return
        Path(directory).mkdir(parents=True, exist_ok=True)
        descriptor = os.open(os.path.join(directory, '.sqlite-runtime.lock'),
                             os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise DatabaseWriteError('数据库实例锁不是普通文件')
        os.set_inheritable(descriptor, False)
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b'0')
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(descriptor)
            raise DatabaseWriteError('同一配置目录已有数据库服务运行') from None
        _leases[directory] = descriptor


@atexit.register
def _close_leases():
    for descriptor in list(_leases.values()):
        try:
            os.close(descriptor)
        except OSError:
            pass
    _leases.clear()


def filesystem_type(directory):
    """Determine the actual mounted filesystem; a successful mmap is insufficient."""
    target = str(Path(directory).resolve())
    candidates = []
    if sys.platform.startswith('linux'):
        def unescape(value):
            return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)
        try:
            for line in Path('/proc/self/mountinfo').read_text().splitlines():
                left, right = line.split(' - ', 1)
                mountpoint = unescape(left.split()[4])
                if target == mountpoint or target.startswith(mountpoint.rstrip('/') + '/'):
                    candidates.append((len(mountpoint), right.split()[0]))
        except (OSError, ValueError, IndexError):
            return 'unknown'
    elif sys.platform == 'darwin':
        try:
            output = subprocess.run(['/sbin/mount'], capture_output=True, text=True,
                                    timeout=5, check=True).stdout
            for match in re.finditer(r' on (.*?) \(([^,]+)', output):
                mountpoint, kind = match.groups()
                if target == mountpoint or target.startswith(mountpoint.rstrip('/') + '/'):
                    candidates.append((len(mountpoint), kind))
        except (OSError, subprocess.SubprocessError):
            return 'unknown'
    return max(candidates)[1] if candidates else 'unknown'


def probe_wal(directory):
    """Two processes share -shm while a retained read snapshot permits a commit."""
    reader = None
    with tempfile.TemporaryDirectory(prefix='.wal-probe-', dir=directory) as scratch:
        path = os.path.join(scratch, 'probe.db')
        parent = sqlite3.connect(path, timeout=2)
        try:
            if parent.execute('PRAGMA journal_mode=WAL').fetchone()[0] != 'wal':
                raise DatabaseWriteError('卷未接受 WAL 模式')
            parent.execute('CREATE TABLE probe(value INTEGER)')
            parent.execute('INSERT INTO probe VALUES(1)')
            parent.commit()
            code = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=2); "
                    "c.execute('BEGIN'); assert c.execute('SELECT value FROM probe').fetchone()[0]==1; "
                    "print('ready',flush=True); sys.stdin.readline(); "
                    "assert c.execute('SELECT value FROM probe').fetchone()[0]==1; "
                    "c.rollback(); c.close()")
            reader = subprocess.Popen([sys.executable, '-c', code, path], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      text=True, close_fds=True)
            if not select.select([reader.stdout], [], [], 5)[0] or reader.stdout.readline().strip() != 'ready':
                raise DatabaseWriteError('WAL 读事务探测未就绪')
            writer = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=2); "
                      "c.execute('UPDATE probe SET value=2'); c.commit(); c.close()")
            subprocess.run([sys.executable, '-c', writer, path], check=True, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not Path(path + '-shm').is_file() or not Path(path + '-wal').is_file():
                raise DatabaseWriteError('卷未提供 WAL 共享内存伴生文件')
            reader.communicate('\n', timeout=5)
            if reader.returncode != 0:
                raise DatabaseWriteError('WAL 快照一致性探测失败')
            return True
        finally:
            if reader is not None and reader.poll() is None:
                reader.kill()
                reader.communicate(timeout=5)
            parent.close()


def validate_database(path):
    """Check structure and foreign keys separately, without modifying user rows."""
    if not Path(path).exists():
        return
    from app.utils.isolated_io import get_io_pool, IsolatedIOError
    try:
        get_io_pool('bulk').execute('sqlite_validate', source=str(Path(path).resolve()), timeout=120)
    except IsolatedIOError:
        raise DatabaseWriteError('数据库完整性或外键检查失败/超时，保留原库；不会自动删除记录') from None


def prepare(directory, settings=None):
    """Prepare before migrations/services, never change journal mode in a pool hook."""
    settings = settings or DatabaseSettings.from_config()
    directory = str(Path(directory).resolve())
    with _lock:
        if directory in _prepared:
            return _prepared[directory]
        acquire_instance(directory)
        check_free_space(directory, settings, 4 * 1024 * 1024, '数据库启动维护')
        from .backup import apply_pending_restore, _wal_header
        # Aliased files produce different -wal/-shm names for the same inode.
        # Probe the canonical directory only after rejecting such layouts.
        identities = set()
        for name in ('user.db', 'media.db'):
            path = Path(directory) / name
            for candidate in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
                if candidate.is_symlink():
                    raise DatabaseWriteError('数据库及伴生文件不能是软链接')
            if path.exists():
                value = path.stat()
                identity = (value.st_dev, value.st_ino)
                if value.st_nlink != 1 or identity in identities:
                    raise DatabaseWriteError('数据库文件存在硬链接或路径别名，启动已停止')
                identities.add(identity)
        # Pending restores are applied only while no application connection is
        # open. The backup module uses independent SQLite backup connections.
        from .main_db import _Database as main
        from .media_db import _Database as media
        # Bootstrap runs before workers; imported helpers may have read data.
        # Release those clean reads before any offline restore replaces files.
        for database in (main, media):
            database.remove_session()
            if database.read_engine.pool.checkedout() or database.write_engine.pool.checkedout():
                raise DatabaseWriteError('恢复或升级前仍有活动数据库连接')
            database.read_engine.dispose()
            database.write_engine.dispose()
        supported = wal_runtime_supported(sqlite3.sqlite_version_info)
        kind = filesystem_type(directory)
        eligible = supported and kind in ('btrfs', 'ext4', 'ext3', 'xfs', 'zfs', 'tmpfs', 'apfs', 'hfs')
        probed = False
        if eligible and settings.journal_mode != 'delete':
            try:
                eligible = probe_wal(directory)
                probed = True
            except (OSError, subprocess.SubprocessError, DatabaseWriteError):
                eligible = False
        for name in ('user.db', 'media.db'):
            path = Path(directory) / name
            if not path.exists():
                continue
            if _wal_header(path) and not eligible:
                raise DatabaseWriteError('现有 WAL 库的 SQLite 运行时或卷未通过安全检查')
            # A killed DELETE migration can leave a hot rollback journal. Its
            # recovery needs a writable connection under the instance lease,
            # before readonly validation/backup. SQLite owns journal recovery;
            # the application never deletes or guesses at recovery files.
            with closing(sqlite3.connect(path, timeout=settings.busy_timeout_seconds)) as connection:
                configure_connection(connection, settings)
                connection.execute('SELECT name FROM sqlite_master LIMIT 1').fetchall()
        if apply_pending_restore(directory, settings):
            from config import Config
            Config().init_config()
            settings = DatabaseSettings.from_config()
            check_free_space(directory, settings, 4 * 1024 * 1024, '恢复配置后的数据库启动')
            main.settings = media.settings = settings
            main.coordinator.settings = settings
            if eligible and not probed and settings.journal_mode != 'delete':
                # Restored configuration can enable auto/WAL where the old
                # configuration explicitly requested DELETE. Probe that change.
                try:
                    eligible = probe_wal(directory)
                except (OSError, subprocess.SubprocessError, DatabaseWriteError):
                    eligible = False
        modes = {}
        for name in ('user.db', 'media.db'):
            path = Path(directory) / name
            if path.exists():
                if _wal_header(path) and not wal_runtime_supported(sqlite3.sqlite_version_info):
                    raise DatabaseWriteError('现有 WAL 库不能在未修复 SQLite 上启动')
                with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as connection:
                    configure_connection(connection, settings, readonly=True)
                    modes[name] = connection.execute('PRAGMA journal_mode').fetchone()[0]
            else:
                modes[name] = 'delete'
        if not eligible and (settings.journal_mode == 'wal' or 'wal' in modes.values()):
            raise DatabaseWriteError('当前 SQLite 运行时或数据库卷未通过 WAL 安全检查')
        for name in ('user.db', 'media.db'):
            validate_database(Path(directory) / name)
        target = 'wal' if eligible and settings.journal_mode != 'delete' else 'delete'
        if settings.journal_mode == 'auto' and target == 'delete':
            _logger.warning('WAL 未启用：SQLite %s，文件系统 %s；保持 DELETE', sqlite3.sqlite_version, kind)
        # A persistent pre-upgrade copy is created once per startup only when
        # the schema requires migration, not every time a worker starts.
        state = {'target': target, 'settings': settings, 'modes': modes,
                 'filesystem': kind, 'complete': False, 'backup': None}
        _prepared[directory] = state
        return state


def complete(directory):
    directory = str(Path(directory).resolve())
    state = _prepared[directory]
    if state['complete']:
        return
    for name in ('user.db', 'media.db'):
        path = Path(directory) / name
        validate_database(path)
        with closing(sqlite3.connect(path, timeout=state['settings'].busy_timeout_seconds)) as connection:
            configure_connection(connection, state['settings'])
            # Explicit DELETE rollback requires a completed checkpoint; merely
            # deleting source code would leave WAL in the persistent file header.
            if state['target'] == 'delete' and state['modes'][name] == 'wal':
                if connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                    raise DatabaseBusy('WAL 尚有活动连接，不能切换日志模式')
            mode = connection.execute('PRAGMA journal_mode=' + state['target']).fetchone()[0]
            if mode != state['target']:
                raise DatabaseWriteError('数据库日志模式切换未生效')
            configure_connection(connection, state['settings'])
        # Restrict files created/owned by this service; never change another
        # owner's files or the permissions of a shared NAS parent directory.
        if hasattr(os, 'geteuid'):
            for candidate in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
                if candidate.exists() and candidate.stat().st_uid == os.geteuid():
                    candidate.chmod(0o600)
    # Populate statistics after index creation, including the first day after
    # boot. Subsequent worker maintenance is throttled and uses the same FIFO.
    from .main_db import _Database as main
    from .media_db import _Database as media
    for database in (main, media):
        maintain(database, force_stats=True)
    state['complete'] = True


def check_free_space(directory, settings, required_bytes, operation):
    """Explain capacity rejection without confusing it with database corruption."""
    free = shutil.disk_usage(directory).free
    required = settings.reserve_free_mb * 1024 * 1024 + required_bytes
    if free < required:
        raise DatabaseBusy(
            '%s空间不足：可用 %.1f MiB，需要至少 %.1f MiB；请清理或扩容配置卷，'
            '或核对 app.database.reserve_free_mb（当前 %s MiB，最低 256 MiB）；'
            '原库与恢复文件保持不变' % (operation, free / 1048576, required / 1048576,
                                         settings.reserve_free_mb))


def check_write_capacity(path, settings, required_bytes=0):
    """Reject before BEGIN; never pretend this can interrupt an active fsync."""
    try:
        check_free_space(str(Path(path).parent), settings,
                         max(4 * 1024 * 1024, required_bytes), '数据库写入')
        wal = Path(str(path) + '-wal')
        size = wal.stat().st_size if wal.exists() else 0
    except OSError:
        raise DatabaseBusy('无法确认数据库卷空间，写入未准入') from None
    # The minimum allows ordinary metadata/index pages; prepared audit batches
    # provide a larger estimate before BEGIN. This is not a physical hard cap
    # and cannot reserve space against other applications on the same volume.
    if size >= settings.wal_limit_mb * 1024 * 1024:
        # Called while owning the global writer slot. All maintenance uses the
        # same admission; no concurrent checkpoint or companion-file deletion.
        with closing(sqlite3.connect(path, timeout=0)) as connection:
            configure_connection(connection, settings)
            connection.execute('PRAGMA busy_timeout=0')
            result = connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        if result[0] or wal.exists() and wal.stat().st_size >= settings.wal_warning_mb * 1024 * 1024:
            raise DatabaseBusy('数据库 WAL 达到高水位，请检查长读事务；写入未准入')


def maintain(database, force_stats=False):
    """Throttled checkpoint/stats with bounded diagnostics and writer admission."""
    global _last_diagnostic
    now = time.monotonic()
    name = database.path
    if not name or not force_stats and now - _last_maintenance.get(name, 0) < 60:
        return
    with database.maintenance() as connection:
        wal = Path(name + '-wal')
        if wal.exists() and wal.stat().st_size >= database.settings.wal_warning_mb * 1024 * 1024:
            connection.exec_driver_sql('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
        day = _last_maintenance.get((name, 'stats'))
        if force_stats or day is None or now - day >= 86400:
            if sqlite3.sqlite_version_info >= (3, 46, 0):
                connection.exec_driver_sql('PRAGMA optimize=0x10002')
            else:
                connection.exec_driver_sql('PRAGMA analysis_limit=1000')
                tables = ('SUBTITLE_TASK', 'SUBTITLE_PROBE_CACHE', 'SUBTITLE_AUDIT_STATE',
                          'SUBTITLE_MEDIA_STATUS', 'SUBTITLE_PUBLICATION', 'TRANSFER_HISTORY',
                          'MEDIASYNC_ITEMS', 'MEDIASYNC_STATISTICS')
                existing = {row[0] for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                for table in tables:
                    if table in existing:
                        connection.exec_driver_sql('ANALYZE "%s"' % table)
            _last_maintenance[(name, 'stats')] = now
    _last_maintenance[name] = now
    if now - _last_diagnostic >= 60:
        _logger.info('数据库事务预算：%s；短读快照：%s',
                     json.dumps(database.coordinator.snapshot(), ensure_ascii=False),
                     json.dumps(database.read_diagnostics(), ensure_ascii=False))
        _last_diagnostic = now
