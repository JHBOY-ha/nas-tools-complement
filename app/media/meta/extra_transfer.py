"""Local extra publication: no overwrite, no episode history, recoverable retries."""
import filecmp
import hashlib
import os
from contextlib import contextmanager
from threading import Lock

from app.utils.types import RmtMode

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
    if mode not in (RmtMode.LINK, RmtMode.COPY, RmtMode.MOVE, RmtMode.SOFTLINK):
        raise ValueError("Extras 仅支持本地硬链接、软链接、复制或移动")
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
    if os.path.lexists(destination):
        same = os.path.samefile(source, destination)
        # A retained staging inode proves this exact source was published before a
        # database failure. Compare bytes for COPY/MOVE, never trust just the size.
        retry = (os.path.exists(pending) and os.path.samefile(pending, destination)
                 and filecmp.cmp(source, pending, shallow=False))
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
        os.link(pending, destination, follow_symlinks=False)
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
