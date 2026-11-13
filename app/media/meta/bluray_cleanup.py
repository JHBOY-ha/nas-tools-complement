"""Resume directory cleanup after a verified, durably recorded Blu-ray MOVE."""
import hashlib
import json
import os

from app.utils import PathUtils
from app.utils.isolated_io import get_io_pool, isolated_stat
from config import Config


def _fingerprint(path):
    value = isolated_stat(path)
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def save_cleanup(source, targets):
    """Called after the complete disc and its history commit, before any unlink."""
    source = os.path.abspath(source)
    folder = os.path.join(Config().get_config_path(), 'bluray-cleanup')
    path = os.path.join(folder, hashlib.sha256(source.encode()).hexdigest() + '.json')
    record = dict(source=source, source_identity=_fingerprint(source)[:2], targets={})
    if os.path.exists(path):
        with open(path, encoding='utf-8') as stream:
            previous = json.load(stream)
        if previous['source_identity'] != record['source_identity']:
            raise ValueError('原盘待清理源目录已变化，保留现场')
        # A previous attempt may have deleted some sources. Keep evidence for
        # their targets too, rather than shrinking the manifest on retry.
        record['targets'].update(previous['targets'])
    record['targets'].update({os.path.abspath(target): _fingerprint(target) for target in targets})
    get_io_pool().execute('write_json_atomic', path=path, value=record)
    return path


def remove_empty_tree(path):
    """Remove directories only; new files and symlink entries always stop cleanup."""
    if not os.path.lexists(path):
        return True
    if not os.path.isdir(path) or os.path.islink(path):
        return False
    directories = []
    def scan_error(error):
        raise error
    for root, dirs, files in os.walk(path, onerror=scan_error):
        if files or any(os.path.islink(os.path.join(root, name)) for name in dirs):
            return False
        directories.append(root)
    for directory in reversed(directories):
        os.rmdir(directory)
    return True


def resume_cleanup(paths):
    """Recover only receipts within this request, without recognizing empty discs."""
    folder = os.path.join(Config().get_config_path(), 'bluray-cleanup')
    if not os.path.isdir(folder):
        return {}
    paths = [os.path.abspath(path) for path in paths]
    cleaned = {}
    for name in os.listdir(folder):
        if not name.endswith('.json'):
            continue
        receipt = os.path.join(folder, name)
        with open(receipt, encoding='utf-8') as stream:
            record = json.load(stream)
        source = record['source']
        if not any(PathUtils.is_path_in_path(path, source) or PathUtils.is_path_in_path(source, path)
                   for path in paths):
            continue
        if os.path.lexists(source) and (os.path.islink(source)
                or _fingerprint(source)[:2] != record['source_identity']):
            raise ValueError('原盘待清理源目录已变化，保留现场')
        for target, expected in record['targets'].items():
            if os.path.islink(target) or not os.path.isfile(target) or _fingerprint(target) != expected:
                raise ValueError('原盘已发布目标发生变化，暂停源目录清理：%s' % target)
        if remove_empty_tree(source):
            # The caller retains these until its enclosing collection finishes
            # too; a failed parent rmdir must not lose all recovery evidence.
            cleaned[source] = receipt
        # If source files remain, normal disc recognition/publication resumes;
        # its index is still present and the manifest above has been verified.
    return cleaned
