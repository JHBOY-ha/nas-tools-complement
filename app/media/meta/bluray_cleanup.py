"""Resume BDMV/DVD cleanup; keep the historical receipt path for existing retries."""
import hashlib
import json
import os

from app.utils import PathUtils, SystemUtils
from app.utils.isolated_io import get_io_pool, isolated_stat
from config import Config


def _fingerprint(path):
    value = isolated_stat(path)
    return [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]


def save_cleanup(source, originals, targets):
    """Called after the complete disc and its history commit, before any unlink."""
    source = os.path.abspath(source)
    folder = os.path.join(Config().get_config_path(), 'bluray-cleanup')
    path = os.path.join(folder, hashlib.sha256(source.encode()).hexdigest() + '.json')
    record = dict(source=source, source_identity=_fingerprint(source)[:2],
                  sources={os.path.relpath(file, source): fingerprint for file, fingerprint in originals.items()},
                  targets={})
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
        if os.path.lexists(source):
            _clean_recorded_files(record)
            if not remove_empty_tree(source):
                raise ValueError('原盘源目录仍有未转移文件，已保留')
        # The enclosing collection must also finish before discarding its receipts.
        cleaned[source] = receipt
    return cleaned


def _clean_recorded_files(record):
    """Resume from the original disc root, even after individual indexes disappeared."""
    source = record['source']
    sources = record.get('sources')
    remaining = []

    def scan_error(error):
        raise error
    for root, dirs, names in os.walk(source, onerror=scan_error):
        if any(os.path.islink(os.path.join(root, name)) for name in dirs):
            raise ValueError('原盘源目录包含软链接，暂停清理')
        remaining.extend(os.path.relpath(os.path.join(root, name), source) for name in names)
    if not remaining:
        return
    target_root = None if sources is not None else _legacy_target_root(remaining, record['targets'])
    # Keep any remaining indexes until last, including during a second interruption.
    remaining.sort(key=lambda relative: os.path.basename(relative).lower() in ('index.bdmv', 'video_ts.ifo'))
    for relative in remaining:
        path = os.path.join(source, relative)
        if sources is not None:
            verified = sources.get(relative) == _fingerprint(path)
        else:
            target = os.path.join(target_root, relative)
            verified = target in record['targets'] and (os.path.samefile(path, target)
                or get_io_pool('bulk').execute('compare', source=path, target=target,
                                               timeout=SystemUtils.get_external_transfer_timeout()))
        if not verified:
            raise ValueError('原盘源文件未记录或已变化，暂停清理：%s' % path)
        os.unlink(path)


def _legacy_target_root(remaining, targets):
    # Old receipts have no source fingerprints. Indexes were unlinked last:
    # match their full relative paths, including DiscN, to the verified targets.
    roots = []
    for relative in remaining:
        marker = relative.lower().split(os.sep)[-2:]
        if marker in (['bdmv', 'index.bdmv'], ['video_ts', 'video_ts.ifo']):
            suffix = os.sep + relative
            roots.append({path[:-len(relative)] for path in targets if path.endswith(suffix)})
    candidates = set.intersection(*roots) if roots else set()
    if len(candidates) != 1:
        raise ValueError('无法从旧清理回执确认原盘目标目录，保留源文件')
    return candidates.pop()
