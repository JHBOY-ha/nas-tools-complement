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
    return copy.deepcopy({key: value for key, value in vars(media).items()
                          if key not in _RUNTIME_FIELDS and not key.startswith('_')})


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
        self.ttl = max(60, int((Config().get_config('pt') or {}).get('search_session_ttl_minutes', 30)) * 60)
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        # Interrupted searches can be restarted; uncertain downloads must never
        # be submitted again automatically after process restart.
        for name in os.listdir(self.root):
            if not re.fullmatch(r'[0-9a-f]{16}\.json', name):
                continue
            with open(os.path.join(self.root, name), encoding='utf-8') as stream:
                record = json.load(stream, object_hook=_decode)
            if record['state'] in ACTIVE_SEARCH_STATES:
                record.update(state='interrupted', message='服务已重启，请重新搜索')
            for operation in record['downloads'].values():
                if operation['state'] == 'submitting':
                    operation.update(state='unknown', message='提交状态待确认，请查询下载器')
            self._records[record['id']] = record
        self._prune()

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
        cutoff = time.time() - 7 * 86400
        for sid, record in list(self._records.items()):
            if record['created_at'] < cutoff:
                os.unlink(os.path.join(self.root, sid + '.json'))
                del self._records[sid]

    def create(self, owner, chat, query, channel='telegram', request_id=None):
        with self._lock:
            self._prune()
            # Telegram may redeliver the same update; only one session is created.
            if request_id is not None:
                for record in self._records.values():
                    if (record['channel'], record['owner'], record['chat'], record['request_id']) == (
                            channel, str(owner), str(chat), str(request_id)):
                        return copy.deepcopy(record), False
            if len(self._records) >= 100:
                expired = sorted((item for item in self._records.values() if item['expires_at'] < time.time()
                                  and not item['downloads'] and item['state'] not in ACTIVE_SEARCH_STATES),
                                 key=lambda item: item['updated_at'])
                for old in expired[:len(self._records) - 99]:
                    os.unlink(os.path.join(self.root, old['id'] + '.json'))
                    del self._records[old['id']]
            if len(self._records) >= 100:
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
            return copy.deepcopy(self._records.get(sid))

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
        """Return a detached receipt; call inside edit() for an atomic claim."""
        with self._lock:
            return next((copy.deepcopy(operation) for record in self._records.values()
                         for operation in record['downloads'].values()
                         if operation.get('tag') == tag and operation['state'] != 'not_submitted'), None)

    def pending_download_sessions(self, channel, limit):
        """Select least-recently checked sessions without exposing live records."""
        with self._lock:
            self._prune()
            pending = []
            for record in self._records.values():
                if record['channel'] != channel:
                    continue
                checked = [operation.get('checked_at', 0) for operation in record['downloads'].values()
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
