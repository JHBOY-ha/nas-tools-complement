"""Local extra publication: no overwrite, no episode history, recoverable retries."""
import errno
import json
import hashlib
from contextlib import contextmanager
from threading import Lock

from app.utils.types import RmtMode
from app.utils.exclusive_publish import rename_exclusive
from app.utils.workload import get_transfer_gate
from app.utils.isolated_io import IsolatedIOTimeout, get_io_pool
from app.utils.isolated_fs import fs_os as os, isolated_open as open
from config import Config

_publish_locks = {}
_publish_locks_guard = Lock()


@contextmanager
def _local_destination_lock(destination):
    """Count holders and waiters so unrelated publications never share a lock."""
    key = os.path.normcase(os.path.abspath(destination))
    with _publish_locks_guard:
        entry = _publish_locks.setdefault(key, [Lock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _publish_locks_guard:
            entry[1] -= 1
            if not entry[1]:
                _publish_locks.pop(key, None)


@contextmanager
def _lock_destination(destination):
    """Serialize publication through cleanup, including other app processes."""
    # Keep the lock file: unlinking it lets another process lock a different inode.
    # OS locks are released on process exit, so a crash cannot leave a stale lock.
    # Waiting on the same target consumes neither an I/O worker nor a transfer
    # slot. Long-lived flock handles use their own bounded pool, leaving stat
    # and upload admission available while multiple copies are in progress.
    with _local_destination_lock(destination), get_transfer_gate().slot(), \
            get_io_pool('publication').session() as worker:
        # Keep the OS lock in a bounded child through persistence and source
        # cleanup; open/flock/NAS waits do not pin the application indefinitely.
        # 将永久锁放在共享配置目录，库中不再产生逐文件 .extra-lock。
        # 使用真实父目录归一化符号链接别名；所有应用进程共用同一个锁 inode。
        lock_root = os.path.join(Config().get_config_path(), 'publication-locks')
        os.makedirs(lock_root, exist_ok=True)
        key = os.path.normcase(os.path.join(os.path.realpath(os.path.dirname(destination)),
                                           os.path.basename(destination)))
        token = worker.execute('lock', path=os.path.join(
            lock_root, hashlib.sha256(key.encode()).hexdigest() + '.lock'))
        try:
            yield
        finally:
            worker.execute('unlock', token=token)


def _compare(source, target):
    return get_io_pool('bulk').execute('compare', source=source, target=target, timeout=300)


EXTRA_FOLDERS = {
    "jellyfin": {"other": "other", "trailers": "trailers", "interviews": "interviews",
                 "behind the scenes": "behind the scenes"},
    "plex": {"other": "Other", "trailers": "Trailers", "interviews": "Interviews",
             "behind the scenes": "Behind The Scenes"},
    "emby": {"other": "extras", "trailers": "trailers", "interviews": "interviews",
             "behind the scenes": "behind the scenes"},
}


def publish_extra(source, destination, mode, transfer, record):
    """Keep MOVE sources until both publication and persistence succeed."""
    return publish_exclusive(source, destination, mode, transfer, record)


def publish_exclusive(source, destination, mode, transfer, record, finalize=None):
    """Shared protected publication for extras and evidence-bound episodes."""
    if mode not in (RmtMode.LINK, RmtMode.COPY, RmtMode.MOVE, RmtMode.SOFTLINK):
        raise ValueError("受保护内容仅支持本地硬链接、软链接、复制或移动，远程模式无法保证排他发布")
    destination = os.path.abspath(destination)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    # Protected publication may hash existing copies as well as transfer them.
    # Nested legacy transfer commands reuse this same process-wide I/O slot.
    from app.db.main_db import MainDb
    # Reserve a future record slot before publication, without retaining the
    # SQLite writer lock while copying/hashing NAS files.
    with MainDb().reserve_write(), _lock_destination(destination):
        return _publish_extra(source, destination, mode, transfer, record, finalize)


def _publication_pending(source, destination):
    """Derive the source-specific staging path shared by admission and recovery."""
    stat = os.stat(source)
    fingerprint = "%s:%s:%s" % (os.path.abspath(source), stat.st_size, stat.st_mtime_ns)
    token = hashlib.sha256(fingerprint.encode()).hexdigest()[:20]
    return stat, os.path.abspath(destination) + "." + token + ".extra-pending"


def has_pending_publication(source, destination):
    """Route receipt candidates to the publisher; this does not authorize reuse.

    Inode identities and bytes are still checked under the destination lock.
    Even an invalid receipt must fail recovery instead of reporting a skip as success.
    """
    _, pending = _publication_pending(source, destination)
    return os.path.lexists(pending) or os.path.lexists(pending + ".receipt")


def _publish_extra(source, destination, mode, transfer, record, finalize=None):
    """The caller holds the destination lock until persistence and source cleanup."""
    stat, pending = _publication_pending(source, destination)
    receipt = pending + ".receipt"
    source_identity = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    if os.path.lexists(destination):
        same = os.path.samefile(source, destination)
        # A retained staging inode proves this exact source was published before a
        # database failure. Compare bytes for COPY/MOVE, never trust just the size.
        retry = (os.path.exists(pending) and os.path.samefile(pending, destination)
                 and (os.path.samefile(source, pending) or _compare(source, pending)))
        if not same and not retry and os.path.isfile(receipt):
            # A no-replace rename consumes pending; its prewritten inode receipt survives crashes.
            with open(receipt, encoding="utf-8") as handle:
                saved = json.load(handle)
            published = os.lstat(destination)
            retry = (saved.get("source") == source_identity
                     and saved.get("target") == [published.st_dev, published.st_ino, published.st_size, published.st_mtime_ns]
                     and _compare(source, destination))
        if not same and not retry:
            raise ValueError("Extras 目标已存在不同文件，保留源文件")
    else:
        # Only an unpublished staging file may be discarded. A published inode is
        # an immutable retry receipt and is handled exclusively by the branch above.
        if os.path.lexists(pending) and not (os.path.samefile(source, pending) or _compare(source, pending)):
            os.unlink(pending)
        if mode == RmtMode.MOVE and not os.path.islink(source) and not os.path.lexists(pending):
            # 同卷移动仅增加暂存硬链接，提交成功后移除源目录项，保留 inode
            # 且不复制媒体字节。跨卷/不支持硬链接时才退回复制，失败仍保留源。
            try:
                os.link(source, pending)
            except OSError as error:
                if error.errno not in (errno.EXDEV, errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL):
                    raise
        if not os.path.lexists(pending):
            staging_mode = RmtMode.COPY if mode == RmtMode.MOVE else mode
            if staging_mode == RmtMode.COPY:
                # Reserve the staging inode exclusively before the legacy copier
                # opens it for writing. LINK/SOFTLINK already create exclusively.
                fd = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            transfer_result = transfer(source, pending, staging_mode)
            if transfer_result == -2:
                # A timed-out child may not have been reaped. Preserve its
                # owned inode instead of erasing evidence of an uncertain copy.
                raise IsolatedIOTimeout(errno.ETIMEDOUT, '文件转移结果未确认，保留暂存')
            if transfer_result != 0:
                # A failed copy is not a valid retry receipt.
                if os.path.lexists(pending):
                    os.unlink(pending)
                raise OSError("Extras 暂存失败")
        # link is an atomic, exclusive publish, including for copies on this volume.
        try:
            os.link(pending, destination, follow_symlinks=False)
        except OSError as error:
            if error.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV, errno.EINVAL):
                raise
            staged = os.lstat(pending)
            # Persist before rename so a crash after publication can be safely retried.
            with open(receipt, "w", encoding="utf-8") as handle:
                json.dump({"source": source_identity,
                           "target": [staged.st_dev, staged.st_ino, staged.st_size, staged.st_mtime_ns]}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            rename_exclusive(pending, destination)
    current = os.stat(source)
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
            stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns):
        raise OSError("Extras 源文件在转移期间变化，保留源文件")
    from app.db.main_db import MainDb
    # History and blacklist updates share one root transaction. Only its
    # durable acknowledgement permits irreversible MOVE-source cleanup.
    with MainDb().write_transaction():
        if record() is not True:
            raise OSError("Extras 已落盘但记录失败，保留源文件等待重试")
    # 字幕 I/O 在 SQL 事务外执行。失败保留发布凭据（和 MOVE 源文件），
    # 使重启后的同 inode 重试与已完整入库的文件有明确区别。
    if finalize is not None:
        result = finalize()
        if result != 0:
            return result
    if mode == RmtMode.MOVE and os.path.abspath(source) != os.path.abspath(destination):
        os.unlink(source)
    if os.path.lexists(pending):
        os.unlink(pending)
    if os.path.exists(receipt):
        os.unlink(receipt)
    return 0
