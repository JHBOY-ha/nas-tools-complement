"""Standalone bounded-I/O worker: fixed operations, never application state."""
import base64
import errno
import hashlib
import ipaddress
import json
import os
import platform
import shutil
import socket
import stat
import sys
import tempfile
import filecmp
import uuid
import time
import subprocess

_locks = {}


def _metadata(path, follow=True):
    value = os.stat(path, follow_symlinks=follow)
    return {'values': list(value), 'extra': {
        name: getattr(value, name) for name in ('st_atime_ns', 'st_mtime_ns', 'st_ctime_ns')
    }}


def _validate_sqlite(connection):
    """Structural/FK checks also gate the application's publication invariants."""
    # Several checks must observe the same generation when a live source is
    # validated. This short read transaction never covers archive/file I/O.
    connection.execute('BEGIN')
    try:
        _validate_sqlite_snapshot(connection)
    finally:
        connection.rollback()


def _validate_sqlite_snapshot(connection):
    if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)] or \
            connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('Database integrity or foreign-key error')
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'SUBTITLE_PUBLICATION' not in tables:
        return  # Recognized legacy schemas are upgraded by the parent, offline.
    if 'SUBTITLE_STATE_CLOCK' not in tables:
        raise ValueError('Missing publication clock')
    clocks = connection.execute('SELECT ID,SEQUENCE,typeof(SEQUENCE) FROM SUBTITLE_STATE_CLOCK').fetchall()
    if len(clocks) != 1 or clocks[0][0] != 1 or clocks[0][2] != 'integer' or clocks[0][1] < 0:
        raise ValueError('Invalid publication clock')
    maximum = connection.execute("SELECT coalesce(max(SEQUENCE),0) FROM SUBTITLE_PUBLICATION "
                                 "WHERE STATUS='published'").fetchone()[0]
    if not isinstance(maximum, int) or maximum > clocks[0][1]:
        raise ValueError('Publication clock moved backwards')
    # GC changes physical row counts, but never a publication's committed
    # receipts. Validate receipts/visibility without demanding deleted history.
    if connection.execute("SELECT 1 FROM SUBTITLE_PUBLICATION WHERE STATUS NOT IN ('building','aborted','published') "
            "OR EXPECTED_AUDIT<0 OR EXPECTED_MEDIA<0 OR WRITTEN_AUDIT<0 OR WRITTEN_MEDIA<0 "
            "OR WRITTEN_AUDIT>EXPECTED_AUDIT OR WRITTEN_MEDIA>EXPECTED_MEDIA "
            "OR (STATUS='published' AND (SEQUENCE IS NULL OR typeof(SEQUENCE)!='integer' OR SEQUENCE<0 "
            "OR WRITTEN_AUDIT!=EXPECTED_AUDIT OR WRITTEN_MEDIA!=EXPECTED_MEDIA)) LIMIT 1").fetchone():
        raise ValueError('Invalid publication receipts')
    if 'SUBTITLE_AUDIT_SCOPE_HEAD' in tables and connection.execute(
            'SELECT 1 FROM SUBTITLE_AUDIT_SCOPE_HEAD WHERE typeof(REPLACE_SEQUENCE)!=\'integer\' '
            'OR REPLACE_SEQUENCE<0 OR REPLACE_SEQUENCE>? LIMIT 1', (clocks[0][1],)).fetchone():
        raise ValueError('Invalid replacement fence')
    if 'SUBTITLE_TASK' in tables and connection.execute(
            "SELECT 1 FROM SUBTITLE_PUBLICATION p JOIN SUBTITLE_TASK t ON t.ID=p.TASK_ID "
            "WHERE p.STATUS='published' AND t.STATUS NOT IN ('succeeded','partial') LIMIT 1").fetchone():
        raise ValueError('Published task does not have its matching terminal state')


def execute(operation, arguments):
    """Fixed operations only; payloads cannot select Python code or a shell."""
    if operation in ('sqlite_backup', 'sqlite_validate'):
        # Only these explicit operations open SQLite. Source connections are
        # read-only and use this process's SAME standard-library runtime; no
        # application ORM, credentials or writer Session enters the child.
        import sqlite3
        from contextlib import closing
        from pathlib import Path
        source = Path(arguments['source']).resolve()
        with open(source, 'rb') as stream:
            header = stream.read(20)
        version = sqlite3.sqlite_version_info
        fixed = (version >= (3, 51, 3) or version[:2] == (3, 50) and version >= (3, 50, 7)
                 or version[:2] == (3, 44) and version >= (3, 44, 6))
        if header[18:20] == b'\x02\x02' and not fixed:
            raise ValueError('Unsupported WAL runtime')
        deadline = time.monotonic() + arguments.get('seconds', 120)
        def progress(_status, _remaining, _total):
            if time.monotonic() > deadline:
                raise TimeoutError('Snapshot deadline exceeded')
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)) as src:
            # Do not depend on a distributor's compiled synchronous default.
            # Source reads and the destination's FIRST backup commit use FULL.
            for name, value in {'synchronous': 2, 'foreign_keys': 1, 'query_only': 1,
                                'busy_timeout': arguments.get('busy_timeout_ms', 30000)}.items():
                src.execute('PRAGMA %s=%d' % (name, value))
                if src.execute('PRAGMA %s' % name).fetchone()[0] != value:
                    raise ValueError('Unsafe source connection')
            if operation == 'sqlite_backup':
                destination = arguments['destination']
                descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                with closing(sqlite3.connect(destination, timeout=5)) as dst:
                    for name, value in {'synchronous': 2, 'foreign_keys': 1,
                                        'busy_timeout': arguments.get('busy_timeout_ms', 30000)}.items():
                        dst.execute('PRAGMA %s=%d' % (name, value))
                        if dst.execute('PRAGMA %s' % name).fetchone()[0] != value:
                            raise ValueError('Unsafe snapshot connection')
                    src.backup(dst, pages=256, progress=progress, sleep=0.02)
                    dst.execute('PRAGMA journal_mode=DELETE')
                    dst.execute('PRAGMA synchronous=FULL')
                    _validate_sqlite(dst)
            else:
                _validate_sqlite(src)
        return True
    if operation == 'stat':
        return _metadata(arguments['path'], arguments.get('follow', True))
    if operation == 'disk_usage':
        return list(shutil.disk_usage(arguments['path']))
    if operation == 'path_query':
        query = arguments['query']
        path = arguments['path']
        if query == 'realpath':
            return os.path.realpath(path)
        try:
            value = os.lstat(path) if query in ('lexists', 'islink') else os.stat(path)
        except FileNotFoundError:
            return False
        return {'exists': True, 'lexists': True, 'isfile': stat.S_ISREG(value.st_mode),
                'isdir': stat.S_ISDIR(value.st_mode), 'islink': stat.S_ISLNK(value.st_mode)}[query]
    if operation == 'path_mutate':
        query = arguments['query']
        path = arguments['path']
        if query == 'makedirs':
            os.makedirs(path, mode=arguments.get('mode', 0o777), exist_ok=arguments.get('exist_ok', False))
        elif query == 'mkdir':
            os.mkdir(path, arguments.get('mode', 0o777))
        elif query in ('unlink', 'remove'):
            os.unlink(path)
        elif query == 'replace':
            os.replace(path, arguments['destination'])
        elif query == 'chmod':
            os.chmod(path, arguments['mode'])
        elif query == 'link':
            os.link(path, arguments['destination'], follow_symlinks=arguments.get('follow_symlinks', True))
        else:
            raise ValueError('Unsupported path mutation')
        return None
    if operation == 'samefile':
        return os.path.samefile(arguments['source'], arguments['target'])
    if operation == 'write_json_atomic':
        path = arguments['path']
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.task-', dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(arguments['value'], output, ensure_ascii=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True
    if operation == 'scandir':
        result = []
        with os.scandir(arguments['path']) as entries:
            for entry in entries:
                if len(result) >= 4096:
                    raise OSError(errno.E2BIG, 'Staging directory enumeration exceeds limit')
                result.append({'name': entry.name, 'path': entry.path,
                               'stat': _metadata(entry.path, False)})
        return result
    if operation == 'orphan_usage':
        root = arguments['path']
        known = set(arguments['known'])
        total = visited = 0
        def count(path):
            nonlocal total, visited
            with os.scandir(path) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 10000:
                        raise OSError(errno.E2BIG, 'Untracked staging enumeration exceeds limit')
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        count(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name in known or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    count(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
        return total
    if operation == 'file_open':
        mode = arguments['mode']
        flags = os.O_RDWR if '+' in mode else (os.O_RDONLY if mode.startswith('r') else os.O_WRONLY)
        if mode.startswith('w'):
            flags |= os.O_CREAT | os.O_TRUNC
        elif mode.startswith('x'):
            flags |= os.O_CREAT | os.O_EXCL
        elif mode.startswith('a'):
            flags |= os.O_CREAT | os.O_APPEND
        flags |= getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(arguments['path'], flags, 0o600)
        try:
            value = os.fstat(fd)
            return {'identity': [value.st_dev, value.st_ino], 'size': value.st_size}
        finally:
            os.close(fd)
    if operation in ('file_read', 'file_write', 'file_sync'):
        flags = os.O_RDONLY if operation == 'file_read' else os.O_WRONLY
        # Append must choose EOF in the kernel on every write, not reuse the
        # size observed when a different process opened the stream.
        append = operation == 'file_write' and arguments.get('append', False)
        if append:
            flags |= os.O_APPEND
        fd = os.open(arguments['path'], flags | getattr(os, 'O_NOFOLLOW', 0))
        try:
            value = os.fstat(fd)
            if [value.st_dev, value.st_ino] != arguments['identity']:
                raise OSError(errno.ESTALE, 'File identity changed')
            if operation == 'file_sync':
                os.fsync(fd)
                return True
            os.lseek(fd, arguments['offset'], os.SEEK_SET)
            if operation == 'file_read':
                data = os.read(fd, min(arguments['size'], 1024 * 1024))
                return base64.b64encode(data).decode('ascii')
            data = base64.b64decode(arguments['data'], validate=True)
            if len(data) > 1024 * 1024:
                raise ValueError('File write exceeds chunk limit')
            sent = 0
            while sent < len(data):
                sent += os.write(fd, data[sent:])
            return {'count': sent, 'offset': os.lseek(fd, 0, os.SEEK_CUR)} if append else sent
        finally:
            os.close(fd)
    if operation == 'hash':
        digest = hashlib.sha256()
        with open(arguments['path'], 'rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()
    if operation == 'compare':
        return filecmp.cmp(arguments['source'], arguments['target'], shallow=False)
    if operation == 'lock':
        handle = open(arguments['path'], 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(handle.fileno()).st_size == 0:
                    handle.write(b'\0'); handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                # A crashed parent's worker may need a brief EOF/reap window
                # to release its descriptor. Live competitors still fail fast.
                deadline = time.monotonic() + 0.25
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise
                        time.sleep(0.01)
        except BaseException:
            handle.close()
            raise
        token = uuid.uuid4().hex
        _locks[token] = handle
        return token
    if operation == 'unlock':
        handle = _locks.pop(arguments['token'])
        handle.close()
        return True
    if operation == 'transfer':
        source, target = arguments['source'], arguments['target']
        mode = arguments['mode']
        if mode == 'copy':
            shutil.copy2(source, target)
        elif mode == 'move':
            temporary = os.path.join(os.path.dirname(source), os.path.basename(target))
            shutil.move(source, temporary)
            shutil.move(temporary, target)
        elif mode == 'link':
            if '-z4-' in platform.release():
                temporary = os.path.join(os.path.dirname(os.path.dirname(target)), os.path.basename(target))
                os.link(source, temporary)
                shutil.move(temporary, target)
            else:
                os.link(source, target)
        elif mode == 'softlink':
            os.symlink(source, target)
        else:
            raise ValueError('Unsupported transfer mode')
        return True
    if operation == 'external_command':
        # Commands inherit this worker's process group, allowing the parent to
        # terminate the whole operation without an unbounded post-kill wait.
        options = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NO_WINDOW
        return subprocess.run(arguments['command'], timeout=arguments['timeout_seconds'],
                              shell=False, **options).returncode
    if operation == 'remove_tree':
        path = arguments['path']
        if not os.path.lexists(path):
            return True
        # Recheck directory identity inside the worker, immediately before
        # removal. The caller's ownership/marker checks remain mandatory.
        current = os.lstat(path)
        if [current.st_dev, current.st_ino] != arguments['identity'] or stat.S_ISLNK(current.st_mode):
            raise OSError(errno.ESTALE, 'Staging directory identity changed')
        def onerror(function, target, _error):
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD, follow_symlinks=False)
            function(target)
        shutil.rmtree(path, onerror=onerror)
        return not os.path.lexists(path)
    if operation == 'resolve':
        if os.environ.get('NASTOOL_OFFLINE_TESTS') == '1':
            raise OSError(errno.EACCES, 'External network disabled in tests')
        return [list(value[:4]) + [list(value[4])] for value in socket.getaddrinfo(
            arguments['host'], arguments['port'], type=socket.SOCK_STREAM
        )]
    if operation == 'http':
        import requests
        if os.environ.get('NASTOOL_OFFLINE_TESTS') == '1':
            # Child processes cannot inherit unittest's socket mocks. Permit
            # only numeric loopback test servers; all external targets fail.
            from urllib.parse import urlsplit
            try:
                allowed = ipaddress.ip_address(urlsplit(arguments['url']).hostname).is_loopback
            except ValueError:
                allowed = False
            if not allowed:
                raise OSError(errno.EACCES, 'External network disabled in tests')
        with requests.request(
            arguments.get('method', 'GET'), arguments['url'],
            params=arguments.get('params'), json=arguments.get('json'),
            headers=arguments.get('headers'), proxies=arguments.get('proxies'),
            cookies=arguments.get('cookies'),
            timeout=tuple(arguments.get('request_timeout', [5, 20])),
            allow_redirects=arguments.get('allow_redirects', False), stream=True,
            verify=arguments.get('verify', True)
        ) as response:
            maximum = arguments.get('max_bytes', 20 * 1024 * 1024)
            declared = response.headers.get('Content-Length', '')
            if declared.isdigit() and int(declared) > maximum:
                raise ValueError('Response exceeds byte limit')
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > maximum:
                    raise ValueError('Response exceeds byte limit')
            return {'status': response.status_code, 'headers': {
                name: response.headers[name] for name in (
                    'Content-Type', 'Content-Length', 'Location', 'Retry-After'
                ) if name in response.headers
            }, 'body': base64.b64encode(body).decode('ascii')}
    raise ValueError('Unsupported I/O operation')


def main():
    # One process serves sequential requests. Messages are bounded and the
    # parent controls the wall-clock deadline, including blocking reads/DNS.
    while True:
        line = sys.stdin.buffer.readline(2 * 1024 * 1024 + 1)
        if not line:
            for handle in _locks.values():
                handle.close()
            _locks.clear()
            return
        try:
            if len(line) > 2 * 1024 * 1024 or not line.endswith(b'\n'):
                raise ValueError('Worker request exceeds limit')
            request = json.loads(line)
            value = execute(request['operation'], request['arguments'])
            result = {'ok': True, 'value': value}
        except BaseException as error:
            # Exception text may contain signed URLs, tokens or proxy passwords.
            result = {'ok': False, 'kind': type(error).__name__,
                      'errno': getattr(error, 'errno', None)}
        sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode() + b'\n')
        sys.stdout.buffer.flush()


if __name__ == '__main__':
    main()
