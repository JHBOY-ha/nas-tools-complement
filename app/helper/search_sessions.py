"""Private, bounded search snapshots shared by Telegram and web search.

Snapshots contain data, never pickled objects. Publication is atomic; callbacks
refer to stable IDs and cannot be redirected by a later search.
"""
import copy
import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from enum import Enum

from app.media.meta._base import MetaBase
from app.media.tmdbv3api.as_obj import AsObj
from app.utils.commons import singleton
from app.utils.types import MediaType
from app.utils.workload import TaskQueueFull
from config import Config


# These are runtime services, not part of a resource's identity or download data.
_RUNTIME_FIELDS = {'category_handler', 'fanart', 'proxies', 'tokens'}
ACTIVE_SEARCH_STATES = frozenset({'identifying', 'queued', 'searching'})
PENDING_DOWNLOAD_STATES = frozenset({'accepted', 'unknown', 'downloading', 'complete'})


def pack_media(media):
    # Recognition may reuse nested TMDB dictionaries and notes. A snapshot
    # must own those values before another worker continues modifying media.
    return _snapshot_value({key: value for key, value in vars(media).items()
                            if key not in _RUNTIME_FIELDS and not key.startswith('_')})


def _snapshot_value(value):
    # TMDB AsObj is dictionary-like but not JSON serializable, including inside
    # lists. Normalize at the snapshot boundary so memory and restored data have
    # the same containers; enums retain the existing typed persistence format.
    if isinstance(value, (dict, AsObj)):
        return {key: _snapshot_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(item) for item in value]
    return copy.deepcopy(value)


def unpack_media(data):
    media = MetaBase(data.get('org_string') or data.get('title'))
    media.__dict__.update(copy.deepcopy(data))
    return media


def _encode(value):
    if isinstance(value, Enum):
        return {'__media_type__': value.name} if isinstance(value, MediaType) else value.value
    raise TypeError('Unsupported search snapshot value: ' + type(value).__name__)


def _decode(value):
    if set(value) == {'__media_type__'}:
        return MediaType[value['__media_type__']]
    return value


@singleton
class SearchSessions:
    def __init__(self, root=None):
        self.root = root or os.path.join(Config().get_config_path(), 'search-sessions')
        self._lock = threading.RLock()
        self._records = {}
        self._receipts = {}
        self.ttl = max(60, int((Config().get_config('pt') or {}).get('search_session_ttl_minutes', 30)) * 60)
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        # Interrupted searches can be restarted; uncertain downloads must never
        # be submitted again automatically after process restart.
        for name in os.listdir(self.root):
            receipt = re.fullmatch(r'download-[0-9a-f]{64}\.json', name)
            if not receipt and not re.fullmatch(r'[0-9a-f]{16}\.json', name):
                continue
            with open(os.path.join(self.root, name), encoding='utf-8') as stream:
                record = json.load(stream, object_hook=_decode)
            if receipt:
                if record['state'] == 'submitting':
                    record.update(state='unknown', message='提交状态待确认，请查询下载器')
                self._receipts[record['tag']] = record
                continue
            if record['state'] in ACTIVE_SEARCH_STATES:
                record.update(state='interrupted', message='服务已重启，请重新搜索')
            for operation in record['downloads'].values():
                if operation['state'] == 'submitting':
                    operation.update(state='unknown', message='提交状态待确认，请查询下载器')
            self._records[record['id']] = record
        self._prune()
        # Previous versions copied in-flight receipts between searches. On
        # upgrade, confirmed downloader evidence outranks a stale copy. An
        # interrupted submission stays unknown if nothing confirms its outcome.
        legacy = {}
        for record in self._records.values():
            if record['channel'] == 'telegram':
                for op in record['downloads'].values():
                    tag = op.get('tag')
                    if tag and tag not in self._receipts:
                        legacy.setdefault(tag, []).append(op)
        states = ('not_submitted', 'unknown', 'exists', 'accepted', 'downloading', 'complete', 'organized')
        for operations in legacy.values():
            if len(operations) > 1:
                self._save_download(max(operations, key=lambda op: (
                    states.index(op['state']), op.get('checked_at', 0))))

    def _save(self, name, value):
        path = os.path.join(self.root, name + '.json')
        temporary = path + '.tmp'
        # Persist before exposing a claimed operation to another request.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, default=_encode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':
            directory = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    def _prune(self):
        now = time.time()
        cutoff = now - 7 * 86400
        for sid, record in list(self._records.items()):
            if record['created_at'] < cutoff:
                os.unlink(os.path.join(self.root, sid + '.json'))
                del self._records[sid]
            elif (record['expires_at'] < now and record['state'] not in ACTIVE_SEARCH_STATES
                  and record['downloads'] and not record.get('archived')
                  and all(self._receipt(op)['state'] != 'submitting' for op in record['downloads'].values())):
                # Receipts outlive the search quota for status checks and dedupe.
                # Drop the heavy snapshot while preserving owner, callback IDs and
                # download tags; expiry already prevents new download actions.
                archived = dict(record, archived=True, results={}, candidates=[], selected=None, report={})
                self._save(sid, archived)
                self._records[sid] = archived
        referenced = {op.get('tag') for record in self._records.values() for op in record['downloads'].values()}
        for tag in self._receipts.keys() - referenced:
            os.unlink(os.path.join(self.root, self._receipt_name(tag) + '.json'))
            del self._receipts[tag]

    def create(self, owner, chat, query, channel='telegram', request_id=None):
        with self._lock:
            self._prune()
            # Telegram may redeliver the same update; only one session is created.
            if request_id is not None:
                for record in self._records.values():
                    if (record['channel'], record['owner'], record['chat'], record['request_id']) == (
                            channel, str(owner), str(chat), str(request_id)):
                        return copy.deepcopy(record), False
            count = sum(not item.get('archived') for item in self._records.values())
            if count >= 100:
                expired = sorted((item for item in self._records.values() if item['expires_at'] < time.time()
                                  and not item['downloads'] and item['state'] not in ACTIVE_SEARCH_STATES),
                                 key=lambda item: item['updated_at'])
                for old in expired[:count - 99]:
                    os.unlink(os.path.join(self.root, old['id'] + '.json'))
                    del self._records[old['id']]
                    count -= 1
            if count >= 100:
                raise TaskQueueFull('搜索记录已满，请清理旧搜索后再试')
            now = time.time()
            record = dict(id=secrets.token_hex(8), owner=str(owner), chat=str(chat),
                          channel=channel, query=query, request_id=str(request_id) if request_id is not None else None,
                          state='identifying', epoch=0, created_at=now, updated_at=now,
                          expires_at=now + self.ttl, message_id=None, message='正在识别电影…',
                          candidates=[], candidate_page=1, more_candidates=False, selected=None,
                          results={}, downloads={}, report={}, preferences=self.preferences(owner, channel),
                          view='candidates', page=0, user_name=None)
            self._save(record['id'], record)
            self._records[record['id']] = record
            return copy.deepcopy(record), True

    def get(self, sid):
        with self._lock:
            record = self._records.get(sid)
            if record is not None:
                return copy.deepcopy(dict(record, downloads={
                    rid: self._receipt(op) for rid, op in record['downloads'].items()}))

    def latest_web_search(self, owner):
        with self._lock:
            now = time.time()
            latest = max((record for record in self._records.values()
                          if record['channel'] == 'web' and record['owner'] == str(owner)
                          and record['expires_at'] >= now),
                         key=lambda record: record['created_at'], default=None)
            return latest['id'] if latest else None

    @contextmanager
    def edit(self, sid):
        # Network work belongs outside this transaction. Copy-on-write keeps a
        # failed disk write from making an operation appear durably accepted.
        with self._lock:
            record = copy.deepcopy(self._records[sid])
            yield record
            if record == self._records[sid]:
                return
            record['updated_at'] = time.time()
            self._save(sid, record)
            self._records[sid] = record

    def __contains__(self, sid):
        with self._lock:
            return sid in self._records

    def find_download(self, tag):
        """Older snapshots embedded their receipt in the search record."""
        with self._lock:
            if tag in self._receipts:
                receipt = self._receipts[tag]
                return copy.deepcopy(receipt) if receipt['state'] != 'not_submitted' else None
            return next((copy.deepcopy(operation) for record in self._records.values()
                         for operation in record['downloads'].values()
                         if operation.get('tag') == tag and operation['state'] != 'not_submitted'), None)

    def _receipt(self, operation):
        return self._receipts.get(operation.get('tag'), operation)

    def claim_download(self, sid, epoch, rid, operation):
        """Attach the session before persisting the single claim that permits submission."""
        tag = operation['tag']
        with self._lock:
            with self.edit(sid) as record:
                if record['epoch'] != epoch or record['state'] in ('canceled', 'failed', 'interrupted'):
                    return False
                existing = record['downloads'].get(rid)
                if existing and self._receipt(existing)['state'] != 'not_submitted':
                    return False
                prior = self.find_download(tag)
                # If saving the claim fails, no downloader call was made. A
                # reference left on disk can safely retry after a restart.
                record['downloads'][rid] = prior or dict(operation, state='not_submitted', message='尚未提交下载')
            if prior:
                if tag not in self._receipts:
                    self._save_download(prior)
                return False
            self._save_download(operation)
            return True

    @staticmethod
    def _receipt_name(tag):
        return 'download-' + hashlib.sha256(tag.encode()).hexdigest()

    def _save_download(self, operation):
        tag = operation['tag']
        self._save(self._receipt_name(tag), operation)
        self._receipts[tag] = operation.copy()

    def update_download(self, sid, rid, **changes):
        """A result or status poll updates the same receipt for every search sharing the tag."""
        with self._lock:
            op = self._receipt(self._records[sid]['downloads'][rid])
            updated = dict(op, **changes)
            self._save_download(updated)
            return [item['id'] for item in self._records.values()
                    if item['downloads'].get(rid, {}).get('tag') == op['tag']]

    def pending_download_sessions(self, channel, limit):
        """Select least-recently checked sessions without exposing live records."""
        with self._lock:
            self._prune()
            pending = []
            for record in self._records.values():
                if record['channel'] != channel:
                    continue
                operations = map(self._receipt, record['downloads'].values())
                checked = [operation.get('checked_at', 0) for operation in operations
                           if operation['state'] in PENDING_DOWNLOAD_STATES]
                if checked:
                    pending.append((min(checked), record['id']))
            return [sid for _, sid in sorted(pending)[:limit]]

    def owned(self, sid, owner, chat, epoch=None, channel=None):
        record = self.get(sid)
        if (not record or (record['owner'], record['chat']) != (str(owner), str(chat))
                or (channel is not None and record['channel'] != channel)):
            raise ValueError('这不是你的搜索，请发送片名开始搜索')
        if epoch is not None and record['epoch'] != epoch:
            raise ValueError('这条消息已更新，请使用最新按钮')
        return record

    def preferences(self, owner, channel='telegram', value=None):
        name = 'preferences-' + hashlib.sha256((channel + ':' + str(owner)).encode()).hexdigest()[:24]
        with self._lock:
            if value is not None:
                self._save(name, value)
                return value
            path = os.path.join(self.root, name + '.json')
            if os.path.exists(path):
                with open(path, encoding='utf-8') as stream:
                    return json.load(stream)
            return {'quality': '', 'max_gb': 0, 'sites': [], 'setting': None}

    def find_message(self, owner, chat, message_id):
        with self._lock:
            return next((copy.deepcopy(record) for record in self._records.values()
                         if (record['owner'], record['chat'], record['message_id']) ==
                         (str(owner), str(chat), message_id)), None)
