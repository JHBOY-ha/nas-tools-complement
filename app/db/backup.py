"""Private, validated snapshots and restart-only restores; never copy a hot DB."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import uuid
import zipfile

from .settings import DatabaseSettings, wal_runtime_supported
from .transactions import DatabaseBusy, DatabaseWriteError, configure_connection

_DATABASES = ('user.db', 'media.db')
_CONFIG_FILES = ('config.yaml', 'default-category.yaml')
_ALLOWED = set(_DATABASES + _CONFIG_FILES + ('manifest.json',))
_restore_lock = threading.Lock()


def _digest(path):
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def _private_directory(path):
    if Path(path).is_symlink():
        raise DatabaseWriteError('备份或恢复目录不能是软链接')
    Path(path).mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _wal_header(path):
    with open(path, 'rb') as stream:
        header = stream.read(20)
    return len(header) >= 20 and header[18:20] == b'\x02\x02'


def online_backup(source, destination, timeout=120, settings=None):
    """Copy through SQLite's snapshot API, including committed WAL contents."""
    source, destination = Path(source).resolve(), Path(destination)
    settings = settings or DatabaseSettings.from_config()
    if _wal_header(source) and not wal_runtime_supported(sqlite3.sqlite_version_info):
        raise DatabaseWriteError('备份 WAL 库需要已修复的 SQLite 运行时')
    destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        from app.utils.isolated_io import get_io_pool, IsolatedIOError
        get_io_pool('bulk').execute('sqlite_backup', source=str(source), destination=str(destination),
                                   busy_timeout_ms=settings.busy_timeout_seconds * 1000,
                                   seconds=timeout, timeout=timeout)
    except IsolatedIOError:
        # Preserve failed copies privately for diagnosis, never offer them as
        # successful downloads or delete the live source's recovery evidence.
        raise DatabaseWriteError('数据库一致性备份失败或超时，原库保持不变') from None


def _space(directory, required, settings):
    from .runtime import check_free_space
    check_free_space(directory, settings, required, '备份或迁移')


def _snapshot_bytes(path):
    # Committed pages can still reside only in WAL. Counting just the main
    # file would underestimate backup/restore space while a reader pins WAL.
    path = Path(path)
    return path.stat().st_size + (Path(str(path) + '-wal').stat().st_size
                                 if Path(str(path) + '-wal').exists() else 0)


def _migration_sources(directory):
    """Fingerprint offline inputs, including committed pages still in WAL."""
    paths = [directory / name for name in _DATABASES + _CONFIG_FILES
             if (directory / name).is_file()]
    signatures = {}
    for path in paths:
        # Database aliases are unsafe for WAL. Config symlinks retain their
        # existing read/copy behavior; the copied bytes still enter the hash.
        if path.name in _DATABASES and path.is_symlink():
            raise DatabaseWriteError('迁移备份数据库源不能是软链接')
        signatures[path.name] = _digest(path)
        wal = Path(str(path) + '-wal')
        if path.name in _DATABASES and wal.exists():
            if wal.is_symlink():
                raise DatabaseWriteError('迁移备份 WAL 不能是软链接')
            signatures[wal.name] = _digest(wal)
    return paths, signatures


def migration_backup(directory, settings=None):
    """Reuse a validated snapshot of identical offline inputs across restarts."""
    settings = settings or DatabaseSettings.from_config()
    directory = Path(directory)
    sources, signatures = _migration_sources(directory)
    root = directory / '.db-upgrades'
    _private_directory(root)
    key = hashlib.sha256(json.dumps(signatures, sort_keys=True).encode('utf-8')).hexdigest()
    target = root / ('snapshot-' + key)
    if target.is_symlink():
        raise DatabaseWriteError('迁移备份目录不能是软链接')
    if target.exists():
        # A failed copy retains one diagnostic directory, not a new full copy
        # on every restart. Never overwrite an incomplete recovery artifact.
        manifest_path = target / 'manifest.json'
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise DatabaseWriteError('迁移备份未完成；停服后使用 scripts.database_maintenance '
                                     'delete-incomplete-upgrade 核对清理：' + target.name)
        if any(path.is_symlink() or not path.is_file() for path in target.iterdir()):
            raise DatabaseWriteError('迁移备份文件无效，保留副本并停止升级')
        _validate_restore(target)
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('source_files') != signatures:
            raise DatabaseWriteError('迁移备份源指纹不匹配，停止升级')
        return str(target)
    required = sum(_snapshot_bytes(path) for path in sources) * 3
    _space(directory, required, settings)
    _private_directory(target)
    manifest = {'format': 1, 'sqlite': sqlite3.sqlite_version, 'files': {}, 'source_files': signatures}
    for source in sources:
        copied = target / source.name
        if source.name in _DATABASES:
            online_backup(source, copied, settings=settings)
        else:
            shutil.copyfile(source, copied)
            os.chmod(copied, 0o600)
        manifest['files'][source.name] = _digest(copied)
    if _migration_sources(directory)[1] != signatures:
        raise DatabaseWriteError('迁移备份期间源发生变化，停止升级并保留副本')
    _write_json(target / 'manifest.json', manifest)
    return str(target)


def create_backup(directory, settings=None):
    """Keep the existing ZIP response, but create consistent copies of both DBs."""
    settings = settings or DatabaseSettings.from_config()
    directory = Path(directory)
    root = directory / 'backup_file'
    _private_directory(root)
    target = root / ('bk_' + time.strftime('%Y%m%d%H%M%S') + '-' + uuid.uuid4().hex)
    _private_directory(target)
    _space(directory, sum(_snapshot_bytes(directory / name) for name in _DATABASES + _CONFIG_FILES
                          if (directory / name).exists()) * 2, settings)
    manifest = {'format': 1, 'sqlite': sqlite3.sqlite_version, 'files': {}}
    for name in _DATABASES + _CONFIG_FILES:
        source = directory / name
        if not source.is_file():
            continue
        copied = target / name
        if name in _DATABASES:
            online_backup(source, copied, settings=settings)
        else:
            shutil.copyfile(source, copied)
            os.chmod(copied, 0o600)
        manifest['files'][name] = _digest(copied)
    _write_json(target / 'manifest.json', manifest)
    archive = Path(str(target) + '.zip')
    descriptor = os.open(archive, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'wb') as stream, zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for source in target.iterdir():
            bundle.write(source, source.name)
        stream.flush()
        os.fsync(stream.fileno())
    shutil.rmtree(target)
    return str(archive)


def _write_json(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix='.db-manifest-', dir=str(Path(path).parent))
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(Path(path).parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sync_directory(directory):
    if os.name != 'nt':
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def stage_restore(archive, directory, settings=None):
    # The service instance lease excludes another process; this lock excludes
    # concurrent admin requests from replacing each other's pending journal.
    if not _restore_lock.acquire(blocking=False):
        raise DatabaseBusy('另一个恢复请求正在校验，操作未准入')
    try:
        return _stage_restore(archive, directory, settings)
    finally:
        _restore_lock.release()


def _stage_restore(archive, directory, settings=None):
    """Validate a strict flat archive; do not unpack over active database files."""
    settings = settings or DatabaseSettings.from_config()
    directory = Path(directory).resolve()
    marker = directory / '.db-restore-pending.json'
    if marker.exists():
        raise DatabaseBusy('已有待重启恢复的备份，请先完成或核对')
    root = directory / '.db-restores'
    _private_directory(root)
    target = root / uuid.uuid4().hex
    _private_directory(target)
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            names = [item.filename for item in entries]
            if len(names) != len(set(names)) or 'user.db' not in names:
                raise DatabaseWriteError('备份缺少 user.db 或存在重复文件')
            total = sum(item.file_size for item in entries)
            _space(directory, total * 2, settings)
            available = shutil.disk_usage(directory).free - settings.reserve_free_mb * 1024 * 1024
            used = 0
            for entry in entries:
                if entry.filename not in _ALLOWED or entry.is_dir() or \
                        stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                    raise DatabaseWriteError('备份包含非预期路径、软链接或加密文件')
                destination = target / entry.filename
                descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with bundle.open(entry) as src, os.fdopen(descriptor, 'wb') as dst:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        used += len(chunk)
                        if used > available:
                            raise DatabaseBusy('备份解压超过可用空间预算')
                        dst.write(chunk)
                    dst.flush()
                    os.fsync(dst.fileno())
        _validate_restore(target)
        _write_json(marker, {'format': 1, 'directory': target.name,
                             'files': {path.name: _digest(path) for path in target.iterdir()}})
        return {'code': 0, 'msg': '备份已校验，重启后离线恢复生效', 'restart_required': True}
    except BaseException:
        shutil.rmtree(target)
        raise


def _validate_restore(target):
    from .runtime import validate_database
    for name in _DATABASES:
        if (target / name).is_file():
            validate_database(target / name)
    config = target / 'config.yaml'
    if config.exists():
        from ruamel.yaml import YAML
        value = YAML(typ='safe').load(config.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or not isinstance(value.get('app', {}), dict):
            raise DatabaseWriteError('备份配置格式无效')
        DatabaseSettings.from_config(value.get('app'))
    manifest = target / 'manifest.json'
    if manifest.exists():
        if manifest.stat().st_size > 65536:
            raise DatabaseWriteError('备份校验清单过大')
        value = json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('format') != 1 or not isinstance(value.get('files'), dict):
            raise DatabaseWriteError('备份校验清单无效')
        actual = {path.name for path in target.iterdir()} - {'manifest.json'}
        if set(value['files']) != actual:
            raise DatabaseWriteError('备份文件与校验清单不一致')
        for name, digest in value['files'].items():
            if name not in _ALLOWED or digest != _digest(target / name):
                raise DatabaseWriteError('备份文件校验失败')


def _read_pending(directory):
    """Read an offline restore journal without following user-supplied paths."""
    marker = Path(directory) / '.db-restore-pending.json'
    if marker.is_symlink() or marker.stat().st_size > 65536:
        raise DatabaseWriteError('恢复标记无效')
    value = json.loads(marker.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('format') != 1 or not re_uuid(value.get('directory')):
        raise DatabaseWriteError('恢复标记格式无效')
    return marker, value


def _discard_canceled_restore(directory, marker, value):
    """A durable cancellation never becomes a restore after a process crash."""
    if 'rollback' in value or 'applied' in value:
        raise DatabaseWriteError('恢复已开始，不能取消；请完成恢复或按旧库副本回退')
    root = Path(directory) / '.db-restores'
    target = root / value['directory']
    if root.is_symlink() or target.is_symlink():
        raise DatabaseWriteError('恢复目录不能是软链接')
    if target.exists():
        if not target.is_dir() or any(path.name not in _ALLOWED or path.is_symlink()
                                     or not path.is_file() for path in target.iterdir()):
            raise DatabaseWriteError('恢复暂存目录含非预期文件，取消未清理')
        shutil.rmtree(target)
        _sync_directory(root)
    marker.unlink()
    _sync_directory(Path(directory))


def cancel_pending_restore(directory):
    """Cancel only an unstarted restore; caller must hold the offline instance lease."""
    directory = Path(directory)
    with _restore_lock:
        marker = directory / '.db-restore-pending.json'
        if not marker.exists() and not marker.is_symlink():
            return {'code': 0, 'msg': '没有待恢复任务', 'restart_required': False}
        marker, value = _read_pending(directory)
        if 'rollback' in value or 'applied' in value:
            raise DatabaseWriteError('恢复已开始，不能取消；请完成恢复或按旧库副本回退')
        # Record the decision before deleting any staged bytes. Bootstrap
        # finishes cleanup if the command dies after this durable write.
        value['canceled'] = True
        _write_json(marker, value)
        _discard_canceled_restore(directory, marker, value)
        return {'code': 0, 'msg': '待恢复任务已取消，当前数据库未改变', 'restart_required': False}


def delete_backup_archive(directory, filename):
    """Explicit offline deletion of one generated ZIP, never automatic retention."""
    import re
    if not isinstance(filename, str) or not re.fullmatch(r'bk_\d{14}-[0-9a-f]{32}\.zip', filename):
        raise DatabaseWriteError('只能指定 backup_file 中生成的备份 ZIP 文件名')
    root = Path(directory) / 'backup_file'
    path = root / filename
    if root.is_symlink() or path.is_symlink() or not path.is_file():
        raise DatabaseWriteError('备份不存在或路径无效')
    # Staged restores own independent extracted copies; deleting this archive
    # cannot remove their recovery journal or a migration rollback snapshot.
    path.unlink()
    _sync_directory(root)


def delete_incomplete_upgrade(directory, name):
    """Remove only an explicitly selected, unfinished content-addressed copy."""
    import re
    if not isinstance(name, str) or not re.fullmatch(r'snapshot-[0-9a-f]{64}', name):
        raise DatabaseWriteError('未完成升级副本名称无效')
    directory = Path(directory)
    marker = directory / '.db-restore-pending.json'
    if marker.exists() or marker.is_symlink():
        raise DatabaseWriteError('请先核对待恢复任务，不能删除其可能需要的升级副本')
    root = directory / '.db-upgrades'
    target = root / name
    if root.is_symlink() or target.is_symlink() or not target.is_dir():
        raise DatabaseWriteError('升级副本路径无效')
    if (target / 'manifest.json').exists() or (target / 'manifest.json').is_symlink():
        raise DatabaseWriteError('该副本已有完成清单，禁止按未完成副本删除')
    if any(path.is_symlink() or not path.is_file() or path.name not in _ALLOWED
           for path in target.iterdir()):
        raise DatabaseWriteError('升级副本含非预期文件，保留副本')
    shutil.rmtree(target)
    _sync_directory(root)


def apply_pending_restore(directory, settings):
    """An offline, restartable copy journal; originals have a validated backup."""
    directory = Path(directory)
    marker = directory / '.db-restore-pending.json'
    if not marker.exists():
        return False
    if marker.is_symlink() or marker.stat().st_size > 65536:
        raise DatabaseWriteError('恢复标记无效')
    value = json.loads(marker.read_text(encoding='utf-8'))
    if not isinstance(value, dict) or value.get('format') != 1:
        raise DatabaseWriteError('恢复标记格式无效')
    token = value.get('directory', '')
    if not re_uuid(token):
        raise DatabaseWriteError('恢复目录无效')
    if value.get('canceled') is True:
        # Retry cancellation cleanup, including a missing staging directory,
        # without ever applying the canceled backup to active database files.
        _discard_canceled_restore(directory, marker, value)
        return False
    target = directory / '.db-restores' / token
    if target.is_symlink() or not target.is_dir():
        raise DatabaseWriteError('恢复目录不存在或已改变')
    files = value.get('files', {})
    if not isinstance(files, dict) or 'user.db' not in files or set(files) != {path.name for path in target.iterdir()}:
        raise DatabaseWriteError('恢复文件清单无效')
    for name, digest in files.items():
        path = target / name
        if name not in _ALLOWED or path.is_symlink() or not path.is_file() or _digest(path) != digest:
            raise DatabaseWriteError('恢复文件已改变')
    _validate_restore(target)
    rollback = value.get('rollback')
    applied = value.get('applied', [])
    if not isinstance(applied, list) or any(name not in files for name in applied) or len(set(applied)) != len(applied):
        raise DatabaseWriteError('恢复进度无效')
    if rollback:
        # Interrupted restore can trust its receipt only while the old, private
        # snapshot still exists with exactly its original validated contents.
        root = directory / '.db-upgrades'
        old = Path(rollback) if isinstance(rollback, str) else root
        if root.is_symlink() or old.is_symlink() or old.parent.resolve() != root.resolve() or not old.is_dir():
            raise DatabaseWriteError('恢复回退目录无效，停止替换数据库')
        if not (old / 'manifest.json').is_file():
            raise DatabaseWriteError('恢复回退清单缺失，停止替换数据库')
        for path in old.iterdir():
            if path.name not in _ALLOWED or path.is_symlink() or not path.is_file():
                raise DatabaseWriteError('恢复回退文件无效，停止替换数据库')
        _validate_restore(old)
    else:
        if applied:
            raise DatabaseWriteError('恢复缺少旧库备份，停止替换数据库')
        value['rollback'] = migration_backup(directory, settings)
        value['applied'] = []
        _write_json(marker, value)
    _space(directory, max(_snapshot_bytes(target / name) for name in files), settings)
    for name in _DATABASES + _CONFIG_FILES:
        if name not in files or name in value['applied']:
            continue
        source, destination = target / name, directory / name
        if name in _DATABASES and destination.exists():
            if _wal_header(destination) and not wal_runtime_supported(sqlite3.sqlite_version_info):
                raise DatabaseWriteError('恢复现有 WAL 库需要已修复的 SQLite')
            with closing(sqlite3.connect(destination, timeout=settings.busy_timeout_seconds)) as connection:
                configure_connection(connection, settings)
                if connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal':
                    if connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                        raise DatabaseBusy('数据库仍有活动读者，恢复未执行')
                    connection.execute('PRAGMA journal_mode=DELETE')
        # Keep staged originals so interrupted application can repeat an atomic
        # replace. Never rename/delete hot -wal/-shm files ourselves.
        temporary = directory / ('.restore-' + uuid.uuid4().hex)
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o600)
        with open(temporary, 'rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        _sync_directory(directory)
        value['applied'].append(name)
        _write_json(marker, value)
    marker.unlink()
    _sync_directory(directory)
    shutil.rmtree(target)
    return any(name in files for name in _CONFIG_FILES)


def re_uuid(value):
    try:
        return isinstance(value, str) and uuid.UUID(value).hex == value
    except (ValueError, AttributeError):
        return False
