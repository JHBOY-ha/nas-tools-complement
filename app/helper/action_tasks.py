"""Bounded action lifecycle records, persisted as private files without new SQL tables."""
import copy
import hashlib
import json
import re
import stat
import threading
import time
import uuid

from app.helper.thread_helper import ThreadHelper
from app.utils.commons import singleton
from app.utils.isolated_io import get_io_pool, IsolatedIOTimeout
from app.utils.isolated_fs import fs_os, isolated_open
from app.utils.workload import TaskQueueFull
from config import Config


TERMINAL = {'succeeded', 'failed', 'canceled', 'interrupted'}
COMMAND_TITLES = {
    'rename': '手动识别转移', 'rename_udf': '自定义识别转移', 're_identification': '重新识别',
    'run_directory_sync': '目录同步', 'run_userrss': '运行订阅', 'run_brushtask': '运行刷流',
    'auto_remove_torrents': '执行删种', 'start_mediasync': '媒体库同步', 'sch': '运行服务',
    'download_subtitle': '下载字幕',
    'special_confirmation': '确认特殊集转移',
}


@singleton
class ActionTasks:
    def __init__(self, root=None, helper=None):
        self._root = root or fs_os.path.join(Config().get_temp_path(), 'action-tasks')
        self._helper = helper or ThreadHelper()
        self._lock = threading.RLock()
        self._records = {}
        self._futures = {}
        self._requests = {}
        self._fingerprints = {}
        self._disk_locks = {}
        self._acceptance = {}
        fs_os.makedirs(self._root, mode=0o700, exist_ok=True)
        self._load()

    def _load(self):
        # Completed records survive navigation/restart; incomplete mutations are
        # interrupted, never automatically repeated after uncertain completion.
        with fs_os.scandir(self._root) as entries:
            files = sorted([entry for entry in entries if entry.name.endswith('.json')
                            and not entry.is_symlink() and entry.is_file()],
                           key=lambda entry: entry.stat().st_mtime_ns, reverse=True)
        deadline = time.monotonic() + 15
        for entry in files[:100]:
            if time.monotonic() >= deadline:
                raise TaskQueueFull('后台任务记录读取超时，请检查暂存卷后重试')
            try:
                if entry.stat().st_size > 256 * 1024:
                    continue
                with isolated_open(entry.path, 'r') as stream:
                    record = json.load(stream)
                task_id = str(uuid.UUID(record['task_id']))
                if entry.name != task_id + '.json' or record['command'] not in COMMAND_TITLES:
                    continue
                if record.get('updated_at', 0) < time.time() - 7 * 86400:
                    fs_os.remove(entry.path)
                    continue
                if record.get('status') not in TERMINAL:
                    record.update(status='interrupted', updated_at=time.time(),
                                  message='应用已重启，操作结果未确认；请检查后再决定是否重试',
                                  result={'code': -1, 'retcode': -1,
                                          'msg': '应用已重启，操作结果未确认', 'retmsg': '应用已重启，操作结果未确认'})
                    self._save(record)
                self._records[task_id] = record
                self._requests[(record['owner'], record['request_id'])] = task_id
                for alias in record.get('request_aliases', [])[:16]:
                    self._requests[(record['owner'], alias)] = task_id
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def _save(self, record):
        task_id = record['task_id']
        with self._lock:
            lock = self._disk_locks.setdefault(task_id, threading.Lock())
        # Disk work does not hold the status lock. Concurrent alias/cancel/result
        # writes serialize per record and always serialize the latest state.
        with lock:
            with self._lock:
                snapshot = copy.deepcopy(self._records.get(task_id) or record)
            get_io_pool().execute('write_json_atomic', path=fs_os.path.join(self._root, task_id + '.json'),
                                  value=snapshot)

    def _prune(self):
        with self._lock:
            completed = sorted((r for r in self._records.values() if r['status'] in TERMINAL),
                               key=lambda r: r['updated_at'])
            remove = [r for r in completed if r['updated_at'] < time.time() - 7 * 86400]
            for record in completed:
                if len(self._records) - len(remove) < 100:
                    break
                if record not in remove:
                    remove.append(record)
            for record in remove:
                self._records.pop(record['task_id'], None)
                self._requests.pop((record['owner'], record['request_id']), None)
                for alias in record.get('request_aliases', []):
                    self._requests.pop((record['owner'], alias), None)
                self._disk_locks.pop(record['task_id'], None)
                self._acceptance.pop(record['task_id'], None)
        for record in remove:
            try:
                fs_os.remove(fs_os.path.join(self._root, record['task_id'] + '.json'))
            except OSError:
                pass

    def submit(self, command, data, owner, function, request_id=None, service_key=None):
        request_id = str(request_id or uuid.uuid4())
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', request_id):
            raise ValueError('请求编号格式无效')
        payload = json.dumps(data or {}, sort_keys=True, ensure_ascii=True)
        if len(payload.encode()) > 128 * 1024:
            raise ValueError('后台操作参数超过大小限制，请缩小批次')
        fingerprint = hashlib.sha256((command + '\n' + payload).encode()).hexdigest()
        owner = str(owner)
        self._prune()
        ready = threading.Event()
        with self._lock:
            prior_id = self._requests.get((owner, request_id))
            prior_id = prior_id or self._fingerprints.get((owner, fingerprint))
            prior = self._records.get(prior_id)
            if prior:
                if prior['fingerprint'] != fingerprint:
                    raise ValueError('同一请求编号不能用于不同操作')
                if request_id != prior['request_id']:
                    aliases = prior.setdefault('request_aliases', [])
                    if request_id not in aliases:
                        if len(aliases) >= 16:
                            raise TaskQueueFull('重复触发过多，请查看现有任务')
                        aliases.append(request_id)
                        self._requests[(owner, request_id)] = prior['task_id']
                reused = copy.deepcopy(prior)
            else:
                reused = None
                # Lookup and publication share one critical section: two
                # simultaneous retries must never both dispatch a mutation.
                if len(self._records) >= 100:
                    raise TaskQueueFull('后台操作记录已满，请等待现有任务结束')
                now = time.time()
                task_id = str(uuid.uuid4())
                record = {'task_id': task_id, 'owner': owner, 'command': command,
                          'title': COMMAND_TITLES[command], 'request_id': request_id,
                          'fingerprint': fingerprint, 'status': 'accepting', 'created_at': now,
                          'updated_at': now, 'message': '正在接收任务', 'result': None}
                self._records[task_id] = record
                self._requests[(owner, request_id)] = task_id
                self._fingerprints[(owner, fingerprint)] = task_id
                self._acceptance[task_id] = ready
        if reused is not None:
            with self._lock:
                accepting = self._acceptance.get(reused['task_id'])
            if accepting is not None and not accepting.is_set():
                # A retry must not receive 202 before the first request's
                # durable admission actually succeeds or definitively fails.
                if not accepting.wait(15):
                    raise TaskQueueFull('同一任务仍在接收，请稍后核对状态')
                reused = self.get(reused['task_id'])
                if reused is None:
                    raise TaskQueueFull('同一任务未准入，操作未执行，请稍后重试')
            self._save(reused)
            return reused, True
        def run():
            ready.wait()
            with self._lock:
                if record['status'] != 'queued':
                    return {'code': -1, 'msg': '任务未准入，操作未执行'}
                record.update(status='running', message='正在执行', updated_at=time.time())
                snapshot = copy.deepcopy(record)
            self._save(snapshot)
            return function()

        try:
            future = self._helper.start_thread(run, (), task_key=service_key)
            with self._lock:
                self._futures[task_id] = future
                record.update(status='queued', message='已排队，等待执行', updated_at=time.time())
                snapshot = copy.deepcopy(record)
            self._save(snapshot)
        except BaseException:
            with self._lock:
                self._records.pop(task_id, None)
                self._requests.pop((owner, request_id), None)
                self._fingerprints.pop((owner, fingerprint), None)
                self._futures.pop(task_id, None)
                self._acceptance.pop(task_id, None)
                record['status'] = 'rejected'
            ready.set()
            raise
        ready.set()
        future.add_done_callback(lambda done: self._finished(task_id, done))
        return self.get(task_id), False

    def _finished(self, task_id, future):
        if future.cancelled():
            status, message = 'canceled', '排队任务已取消，操作未执行'
            result = {'code': -1, 'retcode': -1, 'msg': message, 'retmsg': message}
        else:
            error = future.exception()
            if error is not None:
                uncertain = isinstance(error, IsolatedIOTimeout)
                status = 'interrupted' if uncertain else 'failed'
                message = '操作结果未确认，请检查后再决定是否重试' if uncertain else '执行失败，请查看日志后重试'
                result = {'code': -1, 'retcode': -1, 'msg': message, 'retmsg': message}
            else:
                returned = future.result()
                result = returned if isinstance(returned, dict) else {'code': 0, 'msg': '执行结束'}
                code = result.get('code', result.get('retcode', 0))
                status = 'succeeded' if str(code) == '0' else 'failed'
                message = result.get('msg') or result.get('retmsg') or '执行结束'
                encoded = json.dumps(result, ensure_ascii=True)
                if len(encoded.encode()) > 128 * 1024:
                    # Keep honest outcome/status without persisting an unbounded
                    # result body. Detailed logs remain the recovery source.
                    message = '操作已结束，明细超过展示上限，请查看日志'
                    result = {'code': code, 'retcode': code, 'msg': message, 'retmsg': message,
                              'details_truncated': True}
        with self._lock:
            record = self._records.get(task_id)
            if not record:
                return
            record.update(status=status, message=str(message), result=result, updated_at=time.time())
            self._fingerprints.pop((record['owner'], record['fingerprint']), None)
            self._futures.pop(task_id, None)
            snapshot = copy.deepcopy(record)
        try:
            self._save(snapshot)
        except OSError:
            with self._lock:
                record['persistence_error'] = True

    def get(self, task_id):
        with self._lock:
            return copy.deepcopy(self._records.get(str(task_id)))

    def find(self, owner, request_id):
        with self._lock:
            task_id = self._requests.get((str(owner), str(request_id)))
            return copy.deepcopy(self._records.get(task_id))

    def list(self, owner=None, limit=20):
        with self._lock:
            records = [r for r in self._records.values() if owner is None or r['owner'] == str(owner)]
            return copy.deepcopy(sorted(records, key=lambda r: r['created_at'], reverse=True)[:limit])

    def cancel(self, task_id):
        with self._lock:
            future = self._futures.get(str(task_id))
        # Future callbacks persist their outcome; never invoke them while the
        # registry lock is held (disk-lock/status-lock ordering must stay acyclic).
        return bool(future is not None and future.cancel())
