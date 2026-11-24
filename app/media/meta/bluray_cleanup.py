"""Resume BDMV/DVD cleanup; keep the historical receipt path for existing retries."""
import hashlib
import json
import os

from app.utils import PathUtils, SystemUtils
from app.utils.isolated_io import get_io_pool, isolated_stat
from config import Config


def disc_fingerprint(path, *, include_ctime=False):
    value = isolated_stat(path)
    identity = [value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns]
    # Monitor snapshots can detect in-place changes with restored mtimes. MOVE
    # receipts omit ctime because unlinking a source hardlink changes the target's ctime.
    return identity + [value.st_ctime_ns] if include_ctime else identity


def save_cleanup(source, originals, target_root, verified_targets):
    """Called after the complete disc and its history commit, before any unlink."""
    source = os.path.abspath(source)
    folder = os.path.join(Config().get_config_path(), 'bluray-cleanup')
    path = os.path.join(folder, hashlib.sha256(source.encode()).hexdigest() + '.json')
    record = dict(source=source, source_identity=disc_fingerprint(source)[:2],
                  sources={os.path.relpath(file, source): fingerprint for file, fingerprint in originals.items()},
                  target_root=os.path.abspath(target_root), targets={})
    if os.path.exists(path):
        with open(path, encoding='utf-8') as stream:
            previous = json.load(stream)
        if previous['source_identity'] != record['source_identity']:
            raise ValueError('原盘待清理源目录已变化，保留现场')
        # A previous attempt may have deleted some sources. Keep evidence for
        # their targets too, rather than shrinking the manifest on retry.
        record['targets'].update(previous['targets'])
    # These identities accompanied the successful byte/metadata verification.
    # Reading fresh ones after the history commit would bless a replacement.
    record['targets'].update({os.path.abspath(target): identity for target, identity in verified_targets.items()})
    for target, expected in record['targets'].items():
        _check_target(target, expected, source)
    get_io_pool().execute('write_json_atomic', path=path, value=record)
    return path, record


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
        finish_cleanup(record)
        # The enclosing collection must also finish before discarding its receipts.
        cleaned[source] = receipt
    return cleaned


def _check_target(target, expected, source):
    if (os.path.islink(target) or not os.path.isfile(target)
            or PathUtils.is_path_in_path(os.path.realpath(source), os.path.realpath(target))
            or disc_fingerprint(target) != expected):
        raise ValueError('原盘已发布目标发生变化，暂停源目录清理：%s' % target)


def finish_cleanup(record):
    source = record['source']
    if os.path.lexists(source) and (os.path.islink(source)
            or disc_fingerprint(source)[:2] != record['source_identity']):
        raise ValueError('原盘待清理源目录已变化，保留现场')
    for target, expected in record['targets'].items():
        _check_target(target, expected, source)
    if os.path.lexists(source):
        _clean_recorded_files(record)
        if not remove_empty_tree(source):
            raise ValueError('原盘源目录仍有未转移文件，已保留')


def _clean_recorded_files(record):
    """Resume from the original disc root, even after individual indexes disappeared."""
    source = record['source']
    real_source = os.path.realpath(source)
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
    target_root = record.get('target_root') or _legacy_target_root(sources or remaining, record['targets'])
    # Keep any remaining indexes until last, including during a second interruption.
    remaining.sort(key=lambda relative: os.path.basename(relative).lower() in ('index.bdmv', 'video_ts.ifo'))
    for relative in remaining:
        path = os.path.join(source, relative)
        expected_source = sources.get(relative) if sources is not None else disc_fingerprint(path)
        target = os.path.join(target_root, relative)
        expected_target = record['targets'].get(target)
        # Older receipts sampled targets after the commit, so their fingerprints
        # alone cannot prove that this surviving source was published correctly.
        if not record.get('target_root') or sources is None:
            _check_target(target, expected_target, source)
            verified = (os.path.samefile(path, target)
                or get_io_pool('bulk').execute('compare', source=path, target=target,
                                               timeout=SystemUtils.get_external_transfer_timeout()))
            if not verified:
                raise ValueError('原盘目标内容与源文件不符，暂停清理：%s' % path)
        # A long legacy comparison can finish against open descriptors after
        # either path was replaced. Recheck the live directory entries at unlink.
        _check_target(target, expected_target, source)
        if (os.path.islink(source)
                or os.path.realpath(os.path.dirname(path)) != os.path.dirname(os.path.join(real_source, relative))
                or disc_fingerprint(path) != expected_source):
            raise ValueError('原盘源文件未记录或已变化，暂停清理：%s' % path)
        os.unlink(path)


def _legacy_target_root(relative_paths, targets):
    # Indexes normally survive until last. If none remain in an older manifest,
    # other surviving paths may still prove one unambiguous destination root.
    indexes = [name for name in relative_paths if name.lower().split(os.sep)[-2:]
               in (['bdmv', 'index.bdmv'], ['video_ts', 'video_ts.ifo'])]
    paths = iter(indexes or relative_paths)
    first = next(paths)
    candidates = {path[:-len(first)] for path in targets if path.endswith(os.sep + first)}
    for relative in paths:
        candidates = {root for root in candidates if os.path.join(root, relative) in targets}
    if len(candidates) != 1:
        raise ValueError('无法从旧清理回执确认原盘目标目录，保留源文件')
    return candidates.pop()
