"""File/path primitives for task staging, with no NAS syscalls in request threads."""
import base64
import io
import os
import stat

from app.utils.isolated_io import get_io_pool, isolated_stat


class IsolatedFile(io.IOBase):
    """Seekable inode-bound stream; workers perform every file read/write."""
    def __init__(self, path, mode='rb', encoding=None, on_write=None):
        self._buffer = bytearray()
        self.name = os.fspath(path)
        self.mode = mode
        self.encoding = encoding or 'utf-8'
        value = get_io_pool().execute('file_open', path=self.name, mode=mode)
        self.identity = value['identity']
        self._size = value['size']
        self._offset = self._size if mode.startswith('a') else 0
        self._binary = 'b' in mode
        self._on_write = on_write
        self._buffer_start = self._offset

    def readable(self):
        return self.mode.startswith('r') or '+' in self.mode

    def writable(self):
        return self.mode[0] in 'wax' or '+' in self.mode

    def seekable(self):
        return True

    def tell(self):
        return self._offset

    def seek(self, offset, whence=os.SEEK_SET):
        self.flush()
        target = offset if whence == os.SEEK_SET else (
            self._offset + offset if whence == os.SEEK_CUR else self._size + offset
        )
        if target < 0:
            raise ValueError('Negative seek position')
        self._offset = target
        return target

    def read(self, size=-1):
        self._checkClosed()
        if not self.readable():
            raise io.UnsupportedOperation('not readable')
        self.flush()
        remaining = max(self._size - self._offset, 0) if size < 0 else size
        chunks = []
        while remaining:
            value = get_io_pool().execute('file_read', path=self.name, identity=self.identity,
                                          offset=self._offset, size=min(remaining, 1024 * 1024))
            chunk = base64.b64decode(value)
            if not chunk:
                break
            chunks.append(chunk)
            self._offset += len(chunk)
            remaining -= len(chunk)
        result = b''.join(chunks)
        return result if self._binary else result.decode(self.encoding)

    def write(self, data):
        self._checkClosed()
        if not self.writable():
            raise io.UnsupportedOperation('not writable')
        raw = data if isinstance(data, bytes) else data.encode(self.encoding)
        if self._on_write:
            self._on_write(len(raw))
        if not self._buffer:
            self._buffer_start = self._offset
        self._buffer.extend(raw)
        self._offset += len(raw)
        self._size = max(self._size, self._offset)
        if len(self._buffer) >= 65536:
            self.flush()
        return len(data)

    def flush(self):
        if not self._buffer:
            return
        sent = 0
        while sent < len(self._buffer):
            count = get_io_pool().execute(
                'file_write', path=self.name, identity=self.identity, offset=self._buffer_start + sent,
                data=base64.b64encode(self._buffer[sent:sent + 1024 * 1024]).decode('ascii'),
                append=self.mode.startswith('a')
            )
            if self.mode.startswith('a'):
                # Use the child's actual append position after competing writes.
                self._offset = count['offset']
                self._size = max(self._size, self._offset)
                count = count['count']
            sent += count
        self._buffer.clear()

    def fileno(self):
        # This is an opaque remote descriptor; fs_os.fsync accepts it without
        # passing a meaningless child-process FD to the application's kernel.
        return self


def isolated_open(path, mode='r', encoding=None, **_kwargs):
    return IsolatedFile(path, mode, encoding)


class _PathProxy:
    def __getattr__(self, name):
        if name in ('exists', 'lexists', 'isfile', 'isdir', 'islink', 'realpath'):
            return lambda path: get_io_pool().execute('path_query', query=name, path=os.fspath(path))
        if name in ('getsize', 'getmtime'):
            return lambda path: getattr(isolated_stat(path), 'st_size' if name == 'getsize' else 'st_mtime')
        if name == 'samefile':
            return lambda source, target: get_io_pool().execute('samefile', source=os.fspath(source), target=os.fspath(target))
        return getattr(os.path, name)


class _Entry:
    def __init__(self, value):
        self.name, self.path = value['name'], value['path']
        self._stat = os.stat_result(value['stat']['values'], value['stat']['extra'])

    def is_symlink(self):
        return stat.S_ISLNK(self._stat.st_mode)

    def stat(self, *, follow_symlinks=True):
        return isolated_stat(self.path) if follow_symlinks and self.is_symlink() else self._stat

    def is_file(self, *, follow_symlinks=True):
        return stat.S_ISREG(self.stat(follow_symlinks=follow_symlinks).st_mode)

    def is_dir(self, *, follow_symlinks=True):
        return stat.S_ISDIR(self.stat(follow_symlinks=follow_symlinks).st_mode)


class _Entries:
    def __init__(self, values):
        self._entries = iter(_Entry(value) for value in values)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self._entries = iter(())


class _OSProxy:
    path = _PathProxy()

    @staticmethod
    def stat(path, *, follow_symlinks=True):
        return isolated_stat(path, follow_symlinks=follow_symlinks)

    @staticmethod
    def lstat(path):
        return isolated_stat(path, follow_symlinks=False)

    @staticmethod
    def scandir(path):
        return _Entries(get_io_pool().execute('scandir', path=os.fspath(path)))

    @staticmethod
    def makedirs(path, mode=0o777, exist_ok=False):
        return get_io_pool().execute('path_mutate', query='makedirs', path=os.fspath(path),
                                     mode=mode, exist_ok=exist_ok)

    @staticmethod
    def mkdir(path, mode=0o777):
        return get_io_pool().execute('path_mutate', query='mkdir', path=os.fspath(path), mode=mode)

    @staticmethod
    def replace(source, destination):
        return get_io_pool().execute('path_mutate', query='replace', path=os.fspath(source),
                                     destination=os.fspath(destination))

    @staticmethod
    def unlink(path):
        return get_io_pool().execute('path_mutate', query='unlink', path=os.fspath(path))

    remove = unlink

    @staticmethod
    def chmod(path, mode):
        return get_io_pool().execute('path_mutate', query='chmod', path=os.fspath(path), mode=mode)

    @staticmethod
    def fsync(stream):
        if isinstance(stream, IsolatedFile):
            stream.flush()
            return get_io_pool().execute('file_sync', path=stream.name, identity=stream.identity)
        return os.fsync(stream)

    @staticmethod
    def open(path, flags, mode=0o600):
        if flags & os.O_EXCL and flags & os.O_CREAT:
            return IsolatedFile(path, 'xb')
        raise ValueError('Unsupported remote descriptor flags')

    @staticmethod
    def close(stream):
        stream.close()

    @staticmethod
    def link(source, destination, *, follow_symlinks=True):
        return get_io_pool().execute('path_mutate', query='link', path=os.fspath(source),
                                     destination=os.fspath(destination), follow_symlinks=follow_symlinks)

    def __getattr__(self, name):
        return getattr(os, name)


fs_os = _OSProxy()
