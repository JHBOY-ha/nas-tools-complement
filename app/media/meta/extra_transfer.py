"""Local extra publication: no overwrite, no episode history, recoverable retries."""
import errno
import json
import filecmp
import hashlib
import os
from contextlib import contextmanager
from threading import Lock

from app.utils.types import RmtMode
from app.utils.exclusive_publish import rename_exclusive

_publish_lock = Lock()


@contextmanager
def _lock_destination(destination):
    """Serialize publication through cleanup, including other app processes."""
    # Keep the lock file: unlinking it lets another process lock a different inode.
    # OS locks are released on process exit, so a crash cannot leave a stale lock.
    with _publish_lock, open(destination + ".extra-lock", "a+b") as handle:
        if os.name == "nt":
            import msvcrt
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            # A competing process retries later instead of holding a worker forever.
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def publish_exclusive(source, destination, mode, transfer, record):
    """Shared protected publication for extras and evidence-bound episodes."""
    if mode not in (RmtMode.LINK, RmtMode.COPY, RmtMode.MOVE, RmtMode.SOFTLINK):
        raise ValueError("受保护内容仅支持本地硬链接、软链接、复制或移动，远程模式无法保证排他发布")
    destination = os.path.abspath(destination)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with _lock_destination(destination):
        _publish_extra(source, destination, mode, transfer, record)


def _publish_extra(source, destination, mode, transfer, record):
    """The caller holds the destination lock until persistence and source cleanup."""
    stat = os.stat(source)
    fingerprint = "%s:%s:%s" % (os.path.abspath(source), stat.st_size, stat.st_mtime_ns)
    token = hashlib.sha256(fingerprint.encode()).hexdigest()[:20]
    pending = destination + "." + token + ".extra-pending"
    receipt = pending + ".receipt"
    source_identity = [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    if os.path.lexists(destination):
        same = os.path.samefile(source, destination)
        # A retained staging inode proves this exact source was published before a
        # database failure. Compare bytes for COPY/MOVE, never trust just the size.
        retry = (os.path.exists(pending) and os.path.samefile(pending, destination)
                 and filecmp.cmp(source, pending, shallow=False))
        if not same and not retry and os.path.isfile(receipt):
            # A no-replace rename consumes pending; its prewritten inode receipt survives crashes.
            with open(receipt, encoding="utf-8") as handle:
                saved = json.load(handle)
            published = os.lstat(destination)
            retry = (saved.get("source") == source_identity
                     and saved.get("target") == [published.st_dev, published.st_ino, published.st_size, published.st_mtime_ns]
                     and filecmp.cmp(source, destination, shallow=False))
        if not same and not retry:
            raise ValueError("Extras 目标已存在不同文件，保留源文件")
    else:
        # Only an unpublished staging file may be discarded. A published inode is
        # an immutable retry receipt and is handled exclusively by the branch above.
        if os.path.lexists(pending) and not filecmp.cmp(source, pending, shallow=False):
            os.unlink(pending)
        if not os.path.lexists(pending):
            staging_mode = RmtMode.COPY if mode == RmtMode.MOVE else mode
            if staging_mode == RmtMode.COPY:
                # Reserve the staging inode exclusively before the legacy copier
                # opens it for writing. LINK/SOFTLINK already create exclusively.
                fd = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            if transfer(source, pending, staging_mode) != 0:
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
    if record() is not True:
        raise OSError("Extras 已落盘但记录失败，保留源文件等待重试")
    if mode == RmtMode.MOVE and os.path.abspath(source) != os.path.abspath(destination):
        os.unlink(source)
    if os.path.lexists(pending):
        os.unlink(pending)
    if os.path.exists(receipt):
        os.unlink(receipt)
