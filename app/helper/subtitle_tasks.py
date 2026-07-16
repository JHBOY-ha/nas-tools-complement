import datetime
import errno
import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from contextlib import contextmanager

from sqlalchemy import func, or_, text as sql_text

import log
from app.db.main_db import MainDb
from app.db.models import (
    SUBTITLEAUDITSTATE,
    SUBTITLEPROBECACHE,
    SUBTITLETASK,
    SUBTITLETASKITEM,
    SUBTITLETASKSETTING,
)
from app.utils import ExceptionUtils
from config import Config


TERMINAL_STATES = {"succeeded", "partial", "failed", "canceled", "interrupted"}
ACTIVE_STATES = {"queued", "recovering", "running", "canceling"}
TASK_STATES = TERMINAL_STATES | ACTIVE_STATES

DEFAULT_POLICY = {
    "max_upload_queue": 10,
    "text_file_limit_mb": 20,
    "vobsub_limit_mb": 200,
    "batch_limit_mb": 250,
    "staging_quota_mb": 2048,
    "reserve_free_mb": 1024,
    "max_batch_items": 20,
    "llm_max_batch_items": 5,
    "heavy_process_concurrency": 1,
    "ffprobe_timeout_seconds": 10,
    "ffmpeg_timeout_seconds": 60,
    "llm_timeout_seconds": 180,
    "llm_max_batches": 8,
    "upload_budget_minutes": 60,
    "audit_max_changed": 10000,
    "audit_max_minutes": 60,
    "audit_max_directories": 50000,
    "audit_max_issues": 200,
    "task_retention_days": 7,
    "task_retention_count": 100,
}

_HEARTBEAT_SECONDS = 10
_PROBE_CACHE_RETENTION_DAYS = 90
_PROBE_CACHE_MAX_ROWS = 50000
_AUDIT_STATE_RETENTION_DAYS = 90
_AUDIT_STATE_MAX_ROWS = 50000
_AUDIT_SNAPSHOT_CACHE_SECONDS = 30
_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
_STANDARD_STAGING_FACTOR = 3
_ALIGN_STAGING_FACTOR = 6

_POLICY_RANGES = {
    "max_upload_queue": (1, 32),
    "text_file_limit_mb": (1, 100),
    "vobsub_limit_mb": (10, 250),
    # The subtitle endpoint hard cap is 260 MiB.  Keep the configurable
    # payload ceiling below it so multipart headers still fit and an
    # administrator cannot save a policy that HTTP will always reject.
    "batch_limit_mb": (20, 250),
    "staging_quota_mb": (256, 4096),
    "reserve_free_mb": (256, 51200),
    "max_batch_items": (1, 20),
    "llm_max_batch_items": (1, 5),
    "heavy_process_concurrency": (1, 2),
    "ffprobe_timeout_seconds": (3, 60),
    "ffmpeg_timeout_seconds": (10, 600),
    "llm_timeout_seconds": (30, 900),
    "llm_max_batches": (1, 20),
    "upload_budget_minutes": (1, 360),
    "audit_max_changed": (100, 50000),
    "audit_max_minutes": (5, 240),
    "audit_max_directories": (1000, 200000),
    "audit_max_issues": (20, 1000),
    "task_retention_days": (1, 90),
    "task_retention_count": (20, 2000),
}


class SubtitleTaskError(Exception):
    status_code = 400

    def __init__(self, message, status_code=None):
        super().__init__(message)
        if status_code is not None:
            self.status_code = int(status_code)


class TaskNotFound(SubtitleTaskError):
    status_code = 404


class TaskBusy(SubtitleTaskError):
    status_code = 409


class TaskUploadTooLarge(SubtitleTaskError):
    status_code = 413


class TaskQueueFull(SubtitleTaskError):
    status_code = 429


class TaskStorageInsufficient(SubtitleTaskError):
    status_code = 507


def _loads(value, default=None):
    if value is None or value == "":
        return {} if default is None else default
    if isinstance(value, (dict, list, int, float, bool)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value if default is None else default


def _dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _iso(timestamp):
    if timestamp is None:
        return None
    return datetime.datetime.fromtimestamp(float(timestamp), datetime.timezone.utc).astimezone().isoformat(
        timespec="seconds"
    )


def _fingerprint_hash(value):
    return hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()


class SubtitleTaskManager:
    """SQLite-backed subtitle task queue with bounded NAS-friendly workers."""

    def __init__(self, db=None, staging_root=None):
        self._db = db or MainDb()
        self._staging_root = staging_root or os.path.join(Config().get_temp_path(), "subtitle-upload")
        self._incoming_root = os.path.join(
            os.path.dirname(os.path.abspath(self._staging_root)),
            "subtitle-upload-incoming"
        )
        self._cleanup_marker_root = os.path.join(
            os.path.abspath(self._staging_root), ".cleanup-markers"
        )
        self._lock = threading.RLock()
        # Re-entrant because the HTTP admission lease spans multipart parsing
        # and the definitive submit_upload transaction on the same thread.
        self._submit_lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._started = False
        self._worker = None
        self._heartbeat_worker = None
        self._audit_worker = None
        self._audit_wake = threading.Event()
        # Heartbeats are emitted only for claims that still have a live worker.
        # A row left active after a failed DB write therefore cannot be kept
        # alive forever merely by the manager heartbeat.
        self._executing_task_ids = set()
        self._processors = {}
        self._progress_last = {}
        self._spooling_ids = set()
        self._audit_snapshot_cache = {}
        self._last_persistent_cache_cleanup = 0
        self._heavy_condition = threading.Condition(threading.Lock())
        self._heavy_active = 0
        self._interactive_waiters = 0
        # Each task supplies the concurrency value captured in its policy
        # snapshot.  Keeping the requests here prevents a settings change from
        # retroactively increasing the load generated by an already-running
        # task.
        self._heavy_requests = {}

    def register_processor(self, task_type, processor):
        task_type = str(task_type or "").lower()
        if task_type not in ["upload", "repair", "audit"] or not callable(processor):
            raise ValueError("字幕任务处理器无效")
        with self._lock:
            self._processors[task_type] = processor
        self._wake.set()

    register_handler = register_processor

    def start(self):
        with self._lock:
            if self._started:
                if not self._stop.is_set():
                    return self
                if self._background_threads_alive():
                    raise SubtitleTaskError("字幕任务中心正在停止，请稍后重试")
                self._started = False
            self._db.init_db()
            self._ensure_schema_columns()
            os.makedirs(self._staging_root, exist_ok=True)
            os.makedirs(self._cleanup_marker_root, exist_ok=True)
            os.makedirs(self._incoming_root, exist_ok=True)
            # No parser can be active while the cold-start manager owns the
            # ingress lease.  Remove every crash-left flat spool before quota
            # admission opens; periodic cleanup remains age-gated.
            with self._submit_lock:
                self._cleanup_incoming_files(remove_all=True)
            self._recover_tasks()
            self._migrate_legacy_audit_history()
            self.cleanup()
            self._stop.clear()
            self._started = True
            self._worker = threading.Thread(
                target=self._interactive_worker,
                name="subtitle-task-worker",
                daemon=True
            )
            self._heartbeat_worker = threading.Thread(
                target=self._heartbeat_loop,
                name="subtitle-task-heartbeat",
                daemon=True
            )
            self._audit_worker = self._new_audit_worker()
            try:
                # Start the persistent audit worker first.  If the runtime
                # cannot create it, no request can subsequently commit an
                # audit row that has no dispatcher.
                self._audit_worker.start()
                self._worker.start()
                self._heartbeat_worker.start()
            except Exception as error:
                self._stop.set()
                self._wake.set()
                self._audit_wake.set()
                self._started = self._background_threads_alive()
                if not self._started:
                    self._worker = None
                    self._heartbeat_worker = None
                    self._audit_worker = None
                raise SubtitleTaskError(f"字幕任务后台线程启动失败：{str(error)}")
            self._wake.set()
            self._audit_wake.set()
        return self

    def _ensure_schema_columns(self):
        """Allow upgrading an early task-table build without a full Alembic cycle."""
        try:
            columns = {
                row[1] for row in self._db.session.execute(
                    sql_text("PRAGMA table_info('SUBTITLE_TASK')")
                ).fetchall()
            }
            if "ACTIVE_SECONDS" not in columns:
                self._db.session.execute(sql_text(
                    "ALTER TABLE SUBTITLE_TASK ADD COLUMN ACTIVE_SECONDS FLOAT NOT NULL DEFAULT 0"
                ))
            if "RUN_STARTED_AT" not in columns:
                self._db.session.execute(sql_text(
                    "ALTER TABLE SUBTITLE_TASK ADD COLUMN RUN_STARTED_AT FLOAT"
                ))
            self._db.session.execute(sql_text(
                "CREATE INDEX IF NOT EXISTS INDX_SUBTITLE_AUDIT_STATE_UPDATED "
                "ON SUBTITLE_AUDIT_STATE (UPDATED_AT)"
            ))
            self._db.session.execute(sql_text(
                "CREATE INDEX IF NOT EXISTS INDX_SUBTITLE_AUDIT_STATE_SERVER_UPDATED "
                "ON SUBTITLE_AUDIT_STATE (SERVER, UPDATED_AT)"
            ))
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

    def shutdown(self, wait=True):
        # Persist the last known active interval before signaling workers.  A
        # hard crash cannot provide an exact stop timestamp, but graceful NAS
        # shutdowns must not lose the cumulative task budget checkpoint.
        if self._started:
            with self._lock:
                now = time.time()
                rows = self._db.query(SUBTITLETASK).filter(
                    SUBTITLETASK.STATUS.in_(["running", "canceling"])
                ).all()
                for row in rows:
                    self._checkpoint_active(row, now, keep_running=False)
                    row.UPDATED_AT = now
                if rows:
                    self._db.commit()
        self._stop.set()
        self._wake.set()
        self._audit_wake.set()
        with self._heavy_condition:
            self._heavy_condition.notify_all()
        worker = self._worker
        if wait and worker and worker.is_alive():
            worker.join(timeout=5)
        heartbeat = self._heartbeat_worker
        if wait and heartbeat and heartbeat.is_alive():
            heartbeat.join(timeout=2)
        audit_worker = self._audit_worker
        if wait and audit_worker and audit_worker.is_alive():
            audit_worker.join(timeout=2)
        with self._lock:
            still_alive = self._background_threads_alive()
            self._started = still_alive
            if not still_alive:
                self._worker = None
                self._heartbeat_worker = None
                self._audit_worker = None

    def _background_threads_alive(self):
        threads = [self._worker, self._heartbeat_worker, self._audit_worker]
        return any(thread and thread.is_alive() for thread in threads)

    def _new_audit_worker(self):
        return threading.Thread(
            target=self._audit_worker_loop,
            name="subtitle-audit-worker",
            daemon=True
        )

    def _ensure_audit_worker_alive(self):
        """Restart an unexpectedly exited audit dispatcher under supervision."""
        with self._lock:
            if self._stop.is_set():
                return False
            current = self._audit_worker
            if current and current.is_alive():
                return True
            replacement = self._new_audit_worker()
            self._audit_worker = replacement
        try:
            replacement.start()
            self._audit_wake.set()
            return True
        except Exception:
            with self._lock:
                if self._audit_worker is replacement:
                    self._audit_worker = None
            raise

    def get_settings(self):
        with self._lock:
            row = self._db.query(SUBTITLETASKSETTING).filter(SUBTITLETASKSETTING.ID == 1).first()
            overrides = _loads(row.POLICY, {}) if row else {}
            policy = dict(DEFAULT_POLICY)
            if isinstance(overrides, dict):
                policy.update({key: value for key, value in overrides.items() if key in DEFAULT_POLICY})
            return self.validate_settings(policy, complete=True)

    @classmethod
    def validate_settings(cls, values, complete=False):
        if not isinstance(values, dict):
            raise SubtitleTaskError("字幕任务设置必须是对象")
        source = dict(DEFAULT_POLICY) if complete else {}
        unknown = set(values) - set(DEFAULT_POLICY)
        if unknown:
            raise SubtitleTaskError("未知字幕任务设置：%s" % "、".join(sorted(unknown)))
        source.update(values)
        validated = {}
        for key, value in source.items():
            if key not in _POLICY_RANGES:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise SubtitleTaskError(f"{key} 必须是整数")
            minimum, maximum = _POLICY_RANGES[key]
            if number < minimum or number > maximum:
                raise SubtitleTaskError(f"{key} 必须在 {minimum} 到 {maximum} 之间")
            validated[key] = number
        if complete:
            missing = set(DEFAULT_POLICY) - set(validated)
            if missing:
                raise SubtitleTaskError("字幕任务设置不完整")
        if "batch_limit_mb" in validated and "text_file_limit_mb" in validated \
                and validated["batch_limit_mb"] < validated["text_file_limit_mb"]:
            raise SubtitleTaskError("单批总量不能小于文本单文件上限")
        if "batch_limit_mb" in validated and "vobsub_limit_mb" in validated \
                and validated["batch_limit_mb"] < validated["vobsub_limit_mb"]:
            raise SubtitleTaskError("单批总量不能小于 VobSub 组合上限")
        if "staging_quota_mb" in validated and "batch_limit_mb" in validated \
                and validated["staging_quota_mb"] \
                < validated["batch_limit_mb"] * _ALIGN_STAGING_FACTOR:
            raise SubtitleTaskError(
                f"暂存总额度不能小于单批总量的 {_ALIGN_STAGING_FACTOR} 倍"
            )
        if "llm_max_batch_items" in validated and "max_batch_items" in validated \
                and validated["llm_max_batch_items"] > validated["max_batch_items"]:
            raise SubtitleTaskError("LLM 单批字幕数不能大于普通单批字幕数")
        return validated

    def update_settings(self, values, updated_by=""):
        current = self.get_settings()
        current.update(values or {})
        policy = self.validate_settings(current, complete=True)
        self._validate_disk_policy(policy)
        now = time.time()
        with self._lock:
            row = self._db.query(SUBTITLETASKSETTING).filter(SUBTITLETASKSETTING.ID == 1).first()
            if row:
                row.POLICY = _dumps(policy)
                row.UPDATED_BY = str(updated_by or "")
                row.UPDATED_AT = now
            else:
                self._db.insert(SUBTITLETASKSETTING(
                    ID=1,
                    POLICY=_dumps(policy),
                    UPDATED_BY=str(updated_by or ""),
                    UPDATED_AT=now
                ))
            self._db.commit()
        return policy

    def _validate_disk_policy(self, policy):
        """Reject a staging quota that cannot fit on the configured volume."""
        try:
            os.makedirs(self._staging_root, exist_ok=True)
            usage = shutil.disk_usage(self._staging_root)
        except OSError as error:
            raise TaskStorageInsufficient(f"无法读取字幕暂存卷容量：{str(error)}")
        quota = int(policy["staging_quota_mb"]) * 1024 * 1024
        reserve = int(policy["reserve_free_mb"]) * 1024 * 1024
        if quota + reserve > usage.total:
            raise SubtitleTaskError("暂存总额度与保留空间之和不能超过暂存卷总容量")

    def submit_task(self, task_type, owner, payload=None, request_id=None, server=None,
                    scope_key=None, dedupe_key=None):
        task_type = str(task_type or "").strip().lower()
        if task_type not in ["repair", "audit"]:
            raise SubtitleTaskError("任务类型无效")
        self.start()
        owner = str(owner or "")
        payload = dict(payload or {})
        request_id = str(request_id or "").strip() or None
        server = str(server or payload.get("server") or "").strip().lower()
        scope_key = str(scope_key or "") or None
        if not dedupe_key:
            dedupe_key = _fingerprint_hash({
                "type": task_type,
                "owner": owner,
                "server": server,
                "payload": payload
            })
        now = time.time()
        with self._lock:
            if task_type == "audit":
                self._interrupt_orphaned_audits_locked(
                    "检测执行线程已退出，任务已安全中断"
                )
            reusable = self._find_reusable(owner, task_type, request_id, dedupe_key)
            if reusable:
                return self._task_dict(reusable, include_result=True, include_items=True), True
            if task_type == "audit":
                active = self._db.query(SUBTITLETASK).filter(
                    SUBTITLETASK.TYPE == "audit",
                    SUBTITLETASK.STATUS.in_(list(ACTIVE_STATES))
                ).order_by(SUBTITLETASK.CREATED_AT.asc()).first()
                if active:
                    if active.SCOPE_KEY == scope_key and active.OWNER == owner:
                        return self._task_dict(active, include_result=True, include_items=True), True
                    raise TaskBusy("已有字幕检测正在运行，请等待其完成或取消")
            else:
                self._ensure_queue_capacity()
            task_id = str(uuid.uuid4())
            row = SUBTITLETASK(
                ID=task_id,
                TYPE=task_type,
                OWNER=owner,
                STATUS="queued",
                PRIORITY=100,
                SERVER=server,
                REQUEST_ID=request_id,
                DEDUPE_KEY=str(dedupe_key),
                SCOPE_KEY=scope_key,
                PAYLOAD=_dumps(payload),
                POLICY=_dumps(self.get_settings()),
                PHASE="queued",
                COMPLETED=0,
                TOTAL=None,
                PERCENT=0 if task_type != "audit" else None,
                CURRENT_ITEM="",
                MESSAGE="等待后台处理",
                METRICS="{}",
                CANCEL_REQUESTED=0,
                CREATED_AT=now,
                QUEUED_AT=now,
                UPDATED_AT=now
            )
            self._db.insert(row)
            self._db.commit()
            result = self._task_dict(row, include_result=True, include_items=True)
        if task_type == "audit":
            # The SQLite row is the queue.  A single supervised worker claims
            # it; request threads never create one-off audit threads.
            try:
                self._start_audit_thread(task_id)
            except Exception as error:
                # A dispatcher creation failure after the row commit must not
                # reserve the global audit slot forever.  Persist interruption;
                # the heartbeat retries this write if SQLite is also transient.
                self._db.rollback()
                with self._lock:
                    self._interrupt_orphaned_audits_locked(
                        f"检测后台线程启动失败：{str(error)}",
                        include_unclaimed=True
                    )
                    failed = self._db.query(SUBTITLETASK).filter(
                        SUBTITLETASK.ID == task_id
                    ).first()
                    result = self._task_dict(
                        failed, include_result=True, include_items=True
                    )
        else:
            self._wake.set()
        return result, False

    def submit_upload(self, owner, files, payload, request_id=None, server=None):
        self.start()
        owner = str(owner or "")
        files = list(files or [])
        payload = dict(payload or {})
        request_id = str(request_id or "").strip() or None
        server = str(server or payload.get("server") or "").strip().lower()
        if not files:
            raise SubtitleTaskError("请选择字幕文件")
        with self._submit_lock:
            with self._lock:
                if request_id:
                    prior = self._db.query(SUBTITLETASK).filter(
                        SUBTITLETASK.OWNER == owner,
                        SUBTITLETASK.TYPE == "upload",
                        SUBTITLETASK.REQUEST_ID == request_id
                    ).order_by(SUBTITLETASK.CREATED_AT.desc()).first()
                    if prior:
                        return self._task_dict(prior, include_result=True, include_items=True), True
                self._ensure_queue_capacity()
                policy = self.get_settings()
            task_id = str(uuid.uuid4())
            staging_dir = os.path.join(self._staging_root, task_id)
            raw_dir = os.path.join(staging_dir, "raw")
            self._spooling_ids.add(task_id)
            try:
                os.makedirs(raw_dir, exist_ok=False)
                reserve_factor = _ALIGN_STAGING_FACTOR \
                    if str(payload.get("align_mode") or payload.get("align") or "none").lower() \
                    in ["auto", "offset", "segmented", "llm"] \
                    else _STANDARD_STAGING_FACTOR
                existing_reserved, existing_future_reserved = \
                    self._active_staging_reservation_totals()
                incoming_total = self._incoming_spool_bytes()
                current_incoming = 0
                for upload_file in files:
                    stream = getattr(upload_file, "stream", upload_file)
                    incoming_path = self._trusted_incoming_path(stream)
                    if incoming_path:
                        try:
                            current_incoming += os.path.getsize(incoming_path)
                        except OSError:
                            pass
                # The current request is accounted by total_bytes * factor as
                # it is adopted.  Only unrelated crash leftovers augment the
                # existing quota reservation here.
                existing_reserved += max(incoming_total - current_incoming, 0)
                components, total_bytes = self._spool_files(
                    files, raw_dir, policy,
                    existing_reserved=existing_reserved,
                    existing_future_reserved=existing_future_reserved,
                    reserve_factor=reserve_factor
                )
                logical_items = self._logical_items(components, policy, payload)
                payload.update({
                    "staging_dir": staging_dir,
                    "total_bytes": total_bytes,
                    "staging_reserved_bytes": total_bytes * reserve_factor,
                    "logical_items": len(logical_items)
                })
                canonical_media = str(payload.get("canonical_media_file") or "")
                if canonical_media:
                    target_dir = os.path.dirname(os.path.abspath(canonical_media))
                    payload.update({
                        "target_volume_key": self._target_volume_key(target_dir),
                        # UTF conversion/normalization may expand text.  Keep
                        # a conservative 2x persistent output reservation.
                        "target_reserved_bytes": total_bytes * 2
                    })
                dedupe_key = _fingerprint_hash({
                    "owner": owner,
                    # Publication is keyed by the lexical library entry.  Two
                    # library symlinks may share one media referent while
                    # requiring independent subtitle files beside each link.
                    "target": os.path.normcase(os.path.abspath(os.path.normpath(
                        canonical_media
                    ))) if canonical_media else "",
                    "server": server,
                    "align": payload.get("align_mode") or "none",
                    # Sort logical signatures so multipart ordering does not
                    # defeat idempotency.  The filename remains part of the
                    # signature because it carries language/default/forced and
                    # source semantics used by the final naming policy.
                    "items": sorted([
                        [item["source_name"].casefold(), item["content_hash"],
                         item.get("companion_hash") or ""]
                        for item in logical_items
                    ])
                })
                now = time.time()
                with self._lock:
                    reusable = self._find_reusable(owner, "upload", request_id, dedupe_key)
                    if reusable:
                        self._remove_tree(staging_dir)
                        return self._task_dict(reusable, include_result=True, include_items=True), True
                    self._ensure_queue_capacity()
                    if canonical_media:
                        self._check_target_capacity(
                            os.path.dirname(os.path.abspath(canonical_media)),
                            payload.get("target_volume_key"),
                            additional_bytes=payload.get("target_reserved_bytes") or 0,
                            reserve_bytes=policy["reserve_free_mb"] * 1024 * 1024
                        )
                    row = SUBTITLETASK(
                        ID=task_id,
                        TYPE="upload",
                        OWNER=owner,
                        STATUS="queued",
                        PRIORITY=100,
                        SERVER=server,
                        REQUEST_ID=request_id,
                        DEDUPE_KEY=dedupe_key,
                        SCOPE_KEY=None,
                        PAYLOAD=_dumps(payload),
                        POLICY=_dumps(policy),
                        PHASE="queued",
                        COMPLETED=0,
                        TOTAL=len(logical_items),
                        PERCENT=0,
                        CURRENT_ITEM="",
                        MESSAGE="已完成暂存，等待后台处理",
                        METRICS=_dumps({"uploaded_bytes": total_bytes}),
                        CANCEL_REQUESTED=0,
                        CREATED_AT=now,
                        QUEUED_AT=now,
                        UPDATED_AT=now
                    )
                    self._db.insert(row)
                    self._db.flush()
                    for index, item in enumerate(logical_items):
                        self._db.insert(SUBTITLETASKITEM(
                            TASK_ID=task_id,
                            ITEM_KEY=item["item_key"],
                            LOGICAL_INDEX=index,
                            KIND=item["kind"],
                            SOURCE_NAME=item["source_name"],
                            COMPANION_NAME=item.get("companion_name"),
                            STAGED_PATH=item["staged_path"],
                            COMPANION_PATH=item.get("companion_path"),
                            CONTENT_HASH=item["content_hash"],
                            COMPANION_HASH=item.get("companion_hash"),
                            SIZE=item["size"],
                            COMPANION_SIZE=item.get("companion_size") or 0,
                            STATUS="queued",
                            STAGE="staged",
                            RESULT=_dumps({
                                "staged_identity": item.get("staged_identity") or {},
                                "companion_staged_identity": (
                                    item.get("companion_staged_identity") or {}
                                )
                            }),
                            CREATED_AT=now,
                            UPDATED_AT=now
                        ))
                    self._db.commit()
                    result = self._task_dict(row, include_result=True, include_items=True)
                self._wake.set()
                return result, False
            except SubtitleTaskError:
                self._remove_tree(staging_dir)
                raise
            except OSError as error:
                self._db.rollback()
                self._remove_tree(staging_dir)
                if error.errno in [errno.ENOSPC, errno.EDQUOT, errno.EROFS]:
                    raise TaskStorageInsufficient("字幕暂存空间不足或文件系统只读")
                raise SubtitleTaskError(f"字幕暂存失败：{str(error)}")
            except Exception:
                self._db.rollback()
                self._remove_tree(staging_dir)
                raise
            finally:
                self._spooling_ids.discard(task_id)

    def ensure_upload_admission(self, content_length=None):
        """Fail fast before Flask parses a multipart upload when resources are full.

        The definitive checks still run while the task is submitted, because
        another request can win the race after this preflight.  This early
        guard prevents the common queue-full or exhausted-volume case from
        first consuming the complete HTTP body and a temporary file.
        """
        self.start()
        with self._submit_lock:
            self._ensure_upload_admission_locked(content_length)

    def acquire_upload_admission(self, content_length=None):
        """Hold the single multipart ingress slot through task submission."""
        self.start()
        self._submit_lock.acquire()
        try:
            self._ensure_upload_admission_locked(content_length)
        except Exception:
            self._submit_lock.release()
            raise

    def release_upload_admission(self):
        self._submit_lock.release()

    def _ensure_upload_admission_locked(self, content_length=None):
        with self._lock:
            self._ensure_queue_capacity()
            policy = self.get_settings()
            reserved, future_reserved = self._active_staging_reservation_totals()
            reserved += self._incoming_spool_bytes()
        quota = int(policy["staging_quota_mb"]) * 1024 * 1024
        reserve = int(policy["reserve_free_mb"]) * 1024 * 1024
        batch_limit = int(policy["batch_limit_mb"]) * 1024 * 1024
        try:
            announced = int(content_length) if content_length is not None else batch_limit
        except (TypeError, ValueError):
            announced = batch_limit
        if announced < 0:
            announced = 0
        if announced > batch_limit + _MULTIPART_OVERHEAD_BYTES:
            raise TaskUploadTooLarge("字幕批次总量超出限制")
        # Multipart metadata is included in Content-Length.  Factor six
        # covers worst-case encoding expansion, atomic rewrite temps, aligned
        # copies, and the bounded extracted reference before the body reveals
        # whether alignment was selected.
        worst_new_reservation = announced * _ALIGN_STAGING_FACTOR
        try:
            os.makedirs(self._staging_root, exist_ok=True)
            free = shutil.disk_usage(self._staging_root).free
        except OSError as error:
            raise TaskStorageInsufficient(f"无法读取字幕暂存卷容量：{str(error)}")
        if reserved + worst_new_reservation > quota:
            raise TaskStorageInsufficient("字幕暂存总额度不足")
        if free - future_reserved - worst_new_reservation < reserve:
            raise TaskStorageInsufficient("字幕暂存卷可用空间不足，已保留安全余量")

    def _trusted_incoming_path(self, stream):
        """Return a request-spooled path only when it belongs to our flat inbox."""
        candidate = getattr(stream, "name", None)
        if not isinstance(candidate, (str, os.PathLike)):
            return ""
        try:
            candidate = os.path.realpath(os.path.abspath(os.fspath(candidate)))
            incoming_root = os.path.realpath(os.path.abspath(self._incoming_root))
            if os.path.commonpath([incoming_root, candidate]) != incoming_root:
                return ""
            if os.path.dirname(candidate) != incoming_root or os.path.islink(candidate):
                return ""
            return candidate if os.path.isfile(candidate) else ""
        except (OSError, ValueError, TypeError):
            return ""

    def _incoming_spool_bytes(self):
        """Count the dedicated flat HTTP inbox without walking any directory."""
        total = 0
        try:
            entries = os.scandir(self._incoming_root)
        except OSError:
            return 0
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    total += max(int(entry.stat(follow_symlinks=False).st_size), 0)
                except OSError:
                    continue
        return total

    def _cleanup_incoming_files(self, remove_all=False, now=None):
        """Clean only ordinary files from the dedicated flat HTTP inbox."""
        now = float(now or time.time())
        try:
            entries = os.scandir(self._incoming_root)
        except OSError:
            return
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    if not remove_all and now - entry.stat(follow_symlinks=False).st_mtime < 86400:
                        continue
                    os.remove(entry.path)
                except OSError:
                    continue

    def _find_reusable(self, owner, task_type, request_id, dedupe_key):
        query = self._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.OWNER == owner,
            SUBTITLETASK.TYPE == task_type
        )
        if request_id:
            row = query.filter(SUBTITLETASK.REQUEST_ID == request_id).order_by(
                SUBTITLETASK.CREATED_AT.desc()
            ).first()
            if row:
                return row
        if not dedupe_key:
            return None
        status_filter = SUBTITLETASK.STATUS.in_(list(ACTIVE_STATES))
        if task_type == "upload":
            cutoff = time.time() - 600
            status_filter = or_(
                status_filter,
                (SUBTITLETASK.STATUS.in_(["succeeded", "partial"]))
                & (SUBTITLETASK.FINISHED_AT >= cutoff)
            )
        return query.filter(
            SUBTITLETASK.DEDUPE_KEY == str(dedupe_key),
            status_filter
        ).order_by(SUBTITLETASK.CREATED_AT.desc()).first()

    def _ensure_queue_capacity(self):
        policy = self.get_settings()
        queued = self._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.TYPE.in_(["upload", "repair"]),
            SUBTITLETASK.STATUS.in_(["queued", "recovering"])
        ).count()
        if queued >= policy["max_upload_queue"]:
            raise TaskQueueFull("字幕上传/修复等待队列已满")

    def get_task(self, task_id, owner=None, admin=False):
        with self._lock:
            row = self._db.query(SUBTITLETASK).filter(SUBTITLETASK.ID == str(task_id)).first()
            if not row:
                return None
            if not admin and owner is not None and row.OWNER != str(owner):
                return None
            return self._task_dict(row, include_result=True, include_items=True)

    def list_tasks(self, owner=None, task_types=None, statuses=None, limit=20, offset=0, admin=False):
        limit = max(1, min(int(limit or 20), 100))
        offset = max(int(offset or 0), 0)
        with self._lock:
            query = self._db.query(SUBTITLETASK)
            if not admin and owner is not None:
                query = query.filter(SUBTITLETASK.OWNER == str(owner))
            if task_types:
                query = query.filter(SUBTITLETASK.TYPE.in_([str(value).lower() for value in task_types]))
            if statuses:
                query = query.filter(SUBTITLETASK.STATUS.in_([str(value).lower() for value in statuses]))
            total = query.count()
            rows = query.order_by(SUBTITLETASK.CREATED_AT.desc()).limit(limit).offset(offset).all()
            return {
                "items": [self._task_dict(row, include_result=False, include_items=False) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset
            }

    def _task_dict(self, row, include_result=True, include_items=False):
        now = time.time()
        active_seconds = float(row.ACTIVE_SECONDS or 0)
        if row.RUN_STARTED_AT and row.STATUS in ["running", "canceling"]:
            active_seconds += max(now - row.RUN_STARTED_AT, 0)
        progress = {
            "phase": row.PHASE or "queued",
            "completed": int(row.COMPLETED or 0),
            "total": row.TOTAL,
            "percent": row.PERCENT,
            "current_item": row.CURRENT_ITEM or "",
            "message": row.MESSAGE or "",
            "elapsed_seconds": round(active_seconds, 3),
            "metrics": _loads(row.METRICS, {})
        }
        result = {
            "task_id": row.ID,
            "id": row.ID,
            "type": row.TYPE,
            "owner": row.OWNER,
            "status": row.STATUS,
            "server": row.SERVER or "",
            "scope_key": row.SCOPE_KEY or "",
            "queue_position": self._queue_position(row),
            "cancellable": row.STATUS in ACTIVE_STATES,
            "progress": progress,
            "payload": _loads(row.PAYLOAD, {}),
            "scope": _loads(row.PAYLOAD, {}),
            "policy_snapshot": _loads(row.POLICY, {}),
            "created_at": _iso(row.CREATED_AT),
            "queued_at": _iso(row.QUEUED_AT),
            "started_at": _iso(row.STARTED_AT),
            "updated_at": _iso(row.UPDATED_AT),
            "finished_at": _iso(row.FINISHED_AT),
            "error": _loads(row.ERROR, row.ERROR) if row.ERROR else None
        }
        if include_result:
            result["result"] = _loads(row.RESULT, {}) if row.RESULT else None
        if include_items:
            result["items"] = self.list_items(row.ID)
        return result

    def _queue_position(self, row):
        if row.STATUS not in ["queued", "recovering"] or row.TYPE not in ["upload", "repair"]:
            return None
        return self._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.TYPE.in_(["upload", "repair"]),
            SUBTITLETASK.STATUS.in_(["queued", "recovering"]),
            SUBTITLETASK.CREATED_AT <= row.CREATED_AT
        ).count()

    @staticmethod
    def _staging_identity(stat_result):
        return {
            "device": int(getattr(stat_result, "st_dev", 0) or 0),
            "inode": int(getattr(stat_result, "st_ino", 0) or 0),
            "size": int(getattr(stat_result, "st_size", 0) or 0),
            "mtime_ns": int(getattr(stat_result, "st_mtime_ns", 0) or 0)
        }

    def _spool_files(self, files, raw_dir, policy, existing_reserved=0,
                     existing_future_reserved=0,
                     reserve_factor=_STANDARD_STAGING_FACTOR):
        allowed = {".srt", ".ass", ".ssa", ".smi", ".vtt", ".sub", ".idx"}
        text_limit = policy["text_file_limit_mb"] * 1024 * 1024
        vob_limit = policy["vobsub_limit_mb"] * 1024 * 1024
        batch_limit = policy["batch_limit_mb"] * 1024 * 1024
        staging_quota = policy["staging_quota_mb"] * 1024 * 1024
        reserve = policy["reserve_free_mb"] * 1024 * 1024
        existing_reserved = max(int(existing_reserved or 0), 0)
        existing_future_reserved = max(int(existing_future_reserved or 0), 0)
        reserve_factor = max(
            int(reserve_factor or _STANDARD_STAGING_FACTOR),
            _STANDARD_STAGING_FACTOR
        )
        seen_names = set()
        components = []
        total_bytes = 0
        for index, upload_file in enumerate(files):
            original_name = os.path.basename(str(getattr(upload_file, "filename", "") or "")).replace("\x00", "")
            if not original_name or original_name in [".", ".."]:
                raise SubtitleTaskError("字幕文件名无效")
            name_key = original_name.casefold()
            if name_key in seen_names:
                raise SubtitleTaskError(f"存在同名字幕组件：{original_name}")
            seen_names.add(name_key)
            extension = os.path.splitext(original_name)[-1].lower()
            if extension not in allowed:
                raise SubtitleTaskError(f"不支持的字幕格式：{original_name}")
            staged_path = os.path.join(raw_dir, f"{index:03d}-{uuid.uuid4().hex}{extension}")
            digest = hashlib.sha256()
            size = 0
            stream = getattr(upload_file, "stream", upload_file)
            incoming_path = self._trusted_incoming_path(stream)

            def consume(reader, writer=None):
                nonlocal size, total_bytes
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        chunk = bytes(chunk)
                    size += len(chunk)
                    total_bytes += len(chunk)
                    component_limit = vob_limit if extension in [".sub", ".idx"] else text_limit
                    if size > component_limit:
                        raise TaskUploadTooLarge(f"字幕文件超出大小限制：{original_name}")
                    if total_bytes > batch_limit:
                        raise TaskUploadTooLarge("字幕批次总量超出限制")
                    if existing_reserved + total_bytes * reserve_factor > staging_quota:
                        raise TaskStorageInsufficient("字幕暂存总额度不足")
                    free = shutil.disk_usage(self._staging_root).free
                    future_derived_bytes = total_bytes * (reserve_factor - 1)
                    pending_write = len(chunk) if writer is not None else 0
                    if free - existing_future_reserved - pending_write \
                            - future_derived_bytes < reserve:
                        raise TaskStorageInsufficient("字幕暂存卷可用空间不足，已保留安全余量")
                    if writer is not None:
                        writer.write(chunk)
                    digest.update(chunk)

            try:
                if incoming_path:
                    # The request stream already lives beside the task staging
                    # root.  Flush it once, hash/validate by reading, then use
                    # a same-volume atomic move instead of writing a second
                    # complete copy of the upload.
                    stream.flush()
                    os.fsync(stream.fileno())
                    stream.close()
                    with open(incoming_path, "rb") as source_obj:
                        consume(source_obj)
                    if size <= 0:
                        raise SubtitleTaskError(f"字幕文件为空：{original_name}")
                    os.replace(incoming_path, staged_path)
                else:
                    try:
                        if hasattr(stream, "seek"):
                            stream.seek(0)
                    except (OSError, ValueError):
                        pass
                    with open(staged_path, "xb") as target_obj:
                        consume(stream, target_obj)
                        target_obj.flush()
                        os.fsync(target_obj.fileno())
            except Exception:
                if incoming_path and os.path.exists(incoming_path):
                    try:
                        os.remove(incoming_path)
                    except OSError:
                        pass
                raise
            if size <= 0:
                raise SubtitleTaskError(f"字幕文件为空：{original_name}")
            try:
                os.chmod(staged_path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            except OSError:
                pass
            staged_stat = os.stat(staged_path, follow_symlinks=False)
            components.append({
                "source_name": original_name,
                "extension": extension,
                "stem": os.path.splitext(original_name)[0].casefold(),
                "staged_path": staged_path,
                "content_hash": digest.hexdigest(),
                "size": size,
                "staged_identity": self._staging_identity(staged_stat)
            })
        return components, total_bytes

    def _active_staging_reservations(self):
        """Return DB-backed reservations without recursively walking NAS staging."""
        return self._active_staging_reservation_totals()[0]

    def _active_staging_reservation_totals(self):
        """Return (quota reservation, future bytes not yet charged to disk free).

        ``disk_usage().free`` already reflects every staged raw byte.  Only the
        reserved normalized/aligned expansion must be subtracted again for the
        physical free-space guard; quota accounting still uses the full value.
        """
        total = 0
        future_total = 0
        with self._lock:
            rows = self._db.query(
                SUBTITLETASK.ID,
                SUBTITLETASK.PAYLOAD,
                SUBTITLETASK.STATUS
            ).filter(
                SUBTITLETASK.TYPE == "upload",
                SUBTITLETASK.STATUS.in_(list(TASK_STATES))
            ).all()
        for row in rows:
            task_id, raw_payload, status = row
            payload = _loads(raw_payload, {}) if raw_payload else {}
            active = str(status or "") in ACTIVE_STATES
            if not active and not self._terminal_staging_present(task_id, payload):
                continue
            reserved = int((payload or {}).get("staging_reserved_bytes") or 0)
            if not reserved:
                align_mode = str((payload or {}).get("align_mode") or "none").lower()
                factor = _ALIGN_STAGING_FACTOR \
                    if align_mode in ["auto", "offset", "segmented", "llm"] \
                    else _STANDARD_STAGING_FACTOR
                reserved = int((payload or {}).get("total_bytes") or 0) * factor
            raw_bytes = max(int((payload or {}).get("total_bytes") or 0), 0)
            total += max(reserved, 0)
            if active:
                future_total += max(reserved - raw_bytes, 0)
        return total, future_total

    def _terminal_staging_present(self, task_id, payload):
        staging_dir = str(
            (payload or {}).get("staging_dir")
            or os.path.join(self._staging_root, str(task_id))
        )
        try:
            real_root = os.path.normcase(os.path.realpath(self._staging_root))
            real_target = os.path.normcase(os.path.realpath(staging_dir))
            if real_target == real_root \
                    or os.path.commonpath([real_root, real_target]) != real_root:
                return False
            current = os.lstat(real_target)
            return stat.S_ISDIR(current.st_mode) and not stat.S_ISLNK(current.st_mode)
        except FileNotFoundError:
            return False
        except (OSError, ValueError, TypeError):
            # Fail closed for quota accounting when the staging volume is
            # temporarily unreadable.  Its bytes must not disappear from the
            # logical quota merely because cleanup cannot inspect them.
            return True

    def _logical_items(self, components, policy, payload):
        by_stem = {}
        for component in components:
            by_stem.setdefault(component["stem"], {})[component["extension"]] = component
        logical = []
        consumed = set()
        for index, component in enumerate(components):
            path = component["staged_path"]
            if path in consumed:
                continue
            extension = component["extension"]
            pair = by_stem.get(component["stem"], {})
            if extension == ".idx":
                if ".sub" not in pair:
                    raise SubtitleTaskError(f"VobSub 缺少同名 .sub 文件：{component['source_name']}")
                continue
            item = {
                "source_name": component["source_name"],
                "staged_path": path,
                "content_hash": component["content_hash"],
                "size": component["size"],
                "staged_identity": component.get("staged_identity") or {},
                "kind": "text"
            }
            if extension == ".sub" and ".idx" in pair:
                companion = pair[".idx"]
                if component["size"] + companion["size"] > policy["vobsub_limit_mb"] * 1024 * 1024:
                    raise TaskUploadTooLarge(f"VobSub 组合超出大小限制：{component['source_name']}")
                item.update({
                    "kind": "vobsub",
                    "companion_name": companion["source_name"],
                    "companion_path": companion["staged_path"],
                    "companion_hash": companion["content_hash"],
                    "companion_size": companion["size"],
                    "companion_staged_identity": companion.get("staged_identity") or {}
                })
                consumed.add(companion["staged_path"])
            elif extension == ".sub" and not self._is_microdvd_text(path):
                raise SubtitleTaskError(f"二进制 .sub 缺少同名 .idx 文件：{component['source_name']}")
            elif component["size"] > policy["text_file_limit_mb"] * 1024 * 1024:
                raise TaskUploadTooLarge(f"文本字幕超出大小限制：{component['source_name']}")
            consumed.add(path)
            item["item_key"] = hashlib.sha256(
                (item["content_hash"] + (item.get("companion_hash") or "") + str(index)).encode("utf-8")
            ).hexdigest()[:32]
            logical.append(item)
        if not logical:
            raise SubtitleTaskError("没有可处理的逻辑字幕")
        if len(logical) > policy["max_batch_items"]:
            raise TaskUploadTooLarge(f"单批最多上传 {policy['max_batch_items']} 个逻辑字幕")
        align_mode = str(payload.get("align_mode") or payload.get("align") or "none").lower()
        if align_mode == "llm" and len(logical) > policy["llm_max_batch_items"]:
            raise TaskUploadTooLarge(f"LLM 对齐单批最多 {policy['llm_max_batch_items']} 个字幕")
        if align_mode in ["auto", "offset", "segmented", "llm"] \
                and any(item["kind"] == "vobsub" for item in logical):
            raise SubtitleTaskError("VobSub 图形字幕不支持时间轴对齐")
        return logical

    @staticmethod
    def _is_microdvd_text(path):
        try:
            with open(path, "rb") as file_obj:
                sample = file_obj.read(64 * 1024)
            if not sample or b"\x00" in sample:
                return False
            text = sample.decode("utf-8", errors="ignore")
            return bool(re.search(r"(?m)^\{\d+\}\{\d+\}", text))
        except OSError:
            return False

    @staticmethod
    def _target_volume_key(path):
        real_path = os.path.realpath(os.path.abspath(path))
        drive = os.path.splitdrive(real_path)[0]
        if drive:
            return "drive:%s" % os.path.normcase(drive)
        try:
            return "device:%s" % os.stat(real_path).st_dev
        except OSError as error:
            raise TaskStorageInsufficient(f"无法识别字幕目标卷：{str(error)}")

    def _active_target_reservations(self, volume_key):
        total = 0
        required_reserve = 0
        rows = self._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.TYPE == "upload",
            SUBTITLETASK.STATUS.in_(list(ACTIVE_STATES))
        ).all()
        for row in rows:
            payload = _loads(row.PAYLOAD, {}) or {}
            row_volume = payload.get("target_volume_key")
            if not row_volume:
                canonical = str(payload.get("canonical_media_file") or "")
                if not canonical:
                    continue
                try:
                    row_volume = self._target_volume_key(
                        os.path.dirname(os.path.abspath(canonical))
                    )
                except TaskStorageInsufficient:
                    continue
            if row_volume != volume_key:
                continue
            policy = _loads(row.POLICY, {}) or {}
            required_reserve = max(
                required_reserve,
                int(policy.get("reserve_free_mb") or DEFAULT_POLICY["reserve_free_mb"])
                * 1024 * 1024
            )
            items = self._db.query(
                SUBTITLETASKITEM.SIZE,
                SUBTITLETASKITEM.COMPANION_SIZE,
                SUBTITLETASKITEM.STATUS
            ).filter(SUBTITLETASKITEM.TASK_ID == row.ID).all()
            pending = [
                item for item in items
                if str(item[2] or "").lower() not in ["succeeded", "failed", "canceled"]
            ]
            if pending:
                total += sum(
                    (max(int(item[0] or 0), 0) + max(int(item[1] or 0), 0)) * 2
                    for item in pending
                )
        return total, required_reserve

    def _check_target_capacity(self, path, volume_key, additional_bytes=0, reserve_bytes=0):
        try:
            free = shutil.disk_usage(path).free
        except OSError as error:
            raise TaskStorageInsufficient(f"无法读取目标卷空间：{str(error)}")
        active_reserved, active_reserve = self._active_target_reservations(volume_key)
        required = active_reserved + max(int(additional_bytes or 0), 0)
        reserve = max(int(reserve_bytes or 0), active_reserve)
        if free - required < reserve:
            raise TaskStorageInsufficient("字幕目标卷可用空间不足，已保留安全余量")

    def ensure_task_target_space(self, task_id, media_path):
        """Recheck all active same-volume reservations immediately before publish."""
        target_dir = os.path.dirname(os.path.abspath(str(media_path or "")))
        volume_key = self._target_volume_key(target_dir)
        with self._lock:
            row = self._db.query(SUBTITLETASK).filter(
                SUBTITLETASK.ID == str(task_id)
            ).first()
            policy = _loads(row.POLICY, {}) if row else {}
            self._check_target_capacity(
                target_dir, volume_key, additional_bytes=0,
                reserve_bytes=int(
                    (policy or {}).get("reserve_free_mb") or DEFAULT_POLICY["reserve_free_mb"]
                ) * 1024 * 1024
            )

    def _recover_tasks(self):
        now = time.time()
        with self._lock:
            rows = self._db.query(SUBTITLETASK).filter(
                SUBTITLETASK.STATUS.in_(list(ACTIVE_STATES))
            ).all()
            cleanup_ids = []
            for row in rows:
                self._checkpoint_recovered_active(row, now)
                reconciliation = {"succeeded": 0}
                if row.TYPE == "upload":
                    reconciliation = self._reconcile_upload_outputs(
                        row.ID, rollback_incomplete=bool(row.CANCEL_REQUESTED)
                    )
                    # The publisher records an exact hidden path and inode in
                    # its durable marker before copying.  Recovery can remove
                    # that one proven artifact without enumerating the media
                    # directory or guessing from a filename pattern.
                    self._cleanup_upload_temp_artifacts(row.ID)
                if row.CANCEL_REQUESTED:
                    row.STATUS = "partial" if reconciliation["succeeded"] else "canceled"
                    row.PHASE = "complete"
                    row.MESSAGE = (
                        "取消请求已生效，崩溃前完成发布的字幕予以保留"
                        if reconciliation["succeeded"] else "取消请求已在重启恢复时生效"
                    )
                    if reconciliation["succeeded"]:
                        row.RESULT = _dumps({
                            "success_count": reconciliation["succeeded"],
                            "failure_count": 0,
                            "stop_reason": "canceled_during_recovery",
                            "refresh": {
                                "status": "skipped", "scope": "none",
                                "message": "任务已取消，未执行局部刷新"
                            }
                        })
                    row.FINISHED_AT = now
                    row.UPDATED_AT = now
                    if row.TYPE == "upload":
                        cleanup_ids.append(row.ID)
                    continue
                if row.TYPE != "upload":
                    restart_message = "服务重启后未自动继续，请重新发起任务"
                    if row.TYPE == "repair":
                        payload = _loads(row.PAYLOAD, {}) or {}
                        media_file = payload.get("media_path") \
                            or payload.get("media_file") \
                            or payload.get("canonical_media_file")
                        if media_file:
                            try:
                                from app.subtitle import Subtitle
                                from app.helper.subtitle_task_processors import _TaskPathGuard
                                # Keep the lexical library path.  For media
                                # file symlinks the transaction manifest and
                                # subtitles live beside the link; the
                                # referent stored in the authorization
                                # snapshot is validation-only.
                                authorized_media = str(media_file)
                                path_guard = _TaskPathGuard(
                                    payload, {"repair": authorized_media}
                                )
                                path_guard.validate_media_paths()
                                recovery = Subtitle().recover_repair_transaction(
                                    transaction_id=row.ID,
                                    media_file=authorized_media,
                                    path_guard_check=path_guard
                                )
                                if recovery.get("recovered"):
                                    restart_message = "已恢复中断的字幕修复事务，请确认后重新发起"
                            except Exception as error:
                                # Do not guess or scan the media directory.  A
                                # failed exact-manifest recovery is retained for
                                # a later explicit repair attempt.
                                ExceptionUtils.exception_traceback(error)
                    row.STATUS = "interrupted"
                    row.PHASE = "complete"
                    row.MESSAGE = restart_message
                    row.ERROR = _dumps("服务重启导致任务中断")
                    row.FINISHED_AT = now
                    row.UPDATED_AT = now
                    continue
                if self._upload_staging_valid(
                        row.ID,
                        verified_output_item_ids=reconciliation.get("verified_item_ids")
                ):
                    row.STATUS = "recovering"
                    row.PHASE = "recovering"
                    row.MESSAGE = "正在从上传检查点恢复"
                    row.UPDATED_AT = now
                else:
                    # A broken staging manifest cannot complete a half-published
                    # VobSub pair; remove only components matching this task's
                    # persisted planned hashes.
                    self._reconcile_upload_outputs(row.ID, rollback_incomplete=True)
                    row.STATUS = "partial" if reconciliation["succeeded"] else "interrupted"
                    row.PHASE = "complete"
                    row.MESSAGE = (
                        "部分字幕已在崩溃前发布，其余暂存清单损坏"
                        if reconciliation["succeeded"] else "上传暂存清单缺失或损坏，无法恢复"
                    )
                    row.ERROR = _dumps("上传暂存文件缺失或哈希不匹配")
                    if reconciliation["succeeded"]:
                        row.RESULT = _dumps({
                            "success_count": reconciliation["succeeded"],
                            "failure_count": 0,
                            "stop_reason": "staging_invalid_after_publish",
                            "refresh": {
                                "status": "skipped", "scope": "none",
                                "message": "恢复不完整，未执行局部刷新"
                            }
                        })
                    row.FINISHED_AT = now
                    row.UPDATED_AT = now
                    cleanup_ids.append(row.ID)
            self._db.commit()
        for task_id in cleanup_ids:
            self._cleanup_task_staging(task_id)

    def _upload_staging_valid(self, task_id, verified_output_item_ids=None):
        verified_output_item_ids = {
            int(item_id) for item_id in (verified_output_item_ids or [])
        }
        items = self._db.query(SUBTITLETASKITEM).filter(
            SUBTITLETASKITEM.TASK_ID == task_id
        ).all()
        if not items:
            return False
        for item in items:
            if item.STATUS == "succeeded":
                # _recover_tasks has just reconciled and hashed these outputs.
                # Reuse that verified checkpoint instead of immediately
                # rereading the same potentially large NAS file.
                if int(item.ID) in verified_output_item_ids:
                    continue
                if self._upload_output_state(item)["complete"]:
                    continue
            if not self._file_matches(item.STAGED_PATH, item.CONTENT_HASH):
                return False
            if item.COMPANION_PATH and not self._file_matches(item.COMPANION_PATH, item.COMPANION_HASH):
                return False
        return True

    @staticmethod
    def _file_matches(path, expected_hash):
        if not path or not os.path.isfile(path) or not expected_hash:
            return False
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as file_obj:
                for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest() == expected_hash
        except OSError:
            return False

    def _upload_output_state(self, item):
        planned = _loads(item.RESULT, {}) if item.RESULT else {}
        planned = planned if isinstance(planned, dict) else {}
        primary_path = str(item.OUTPUT_PATH or "")
        primary_hash = str(
            item.OUTPUT_HASH or planned.get("planned_output_hash")
            or planned.get("output_hash") or ""
        )
        companion_path = str(item.OUTPUT_COMPANION_PATH or "")
        companion_hash = str(
            planned.get("planned_companion_hash")
            or planned.get("companion_output_hash")
            or (item.COMPANION_HASH if companion_path else "")
            or ""
        )
        primary_ok = self._file_matches(primary_path, primary_hash)
        companion_ok = not companion_path or self._file_matches(companion_path, companion_hash)
        return {
            "planned": planned,
            "primary_path": primary_path,
            "primary_hash": primary_hash,
            "primary_ok": primary_ok,
            "companion_path": companion_path,
            "companion_hash": companion_hash,
            "companion_ok": companion_ok,
            "complete": bool(primary_ok and companion_ok)
        }

    def verify_upload_item_output(self, task_id, item_id):
        """Recheck a succeeded checkpoint before the worker trusts it."""
        with self._lock:
            item = self._db.query(SUBTITLETASKITEM).filter(
                SUBTITLETASKITEM.TASK_ID == str(task_id),
                SUBTITLETASKITEM.ID == int(item_id)
            ).first()
            return bool(item and self._upload_output_state(item)["complete"])

    def trusted_upload_item_hashes(self, task_id, item_id):
        """Reuse intake hashes only while the immutable staged files keep their identity.

        Intake already hashes every byte while enforcing limits.  Rechecking the
        recorded stat identity avoids another full read on the normal worker path;
        old tasks or any changed staging file deliberately fall back to hashing in
        ``process_staged_upload``.
        """
        with self._lock:
            item = self._db.query(SUBTITLETASKITEM).filter(
                SUBTITLETASKITEM.TASK_ID == str(task_id),
                SUBTITLETASKITEM.ID == int(item_id)
            ).first()
            if not item:
                return {"source": "", "companion": ""}
            result = _loads(item.RESULT, {}) if item.RESULT else {}
            result = result if isinstance(result, dict) else {}
            source_ok = self._staged_identity_matches(
                item.STAGED_PATH,
                item.SIZE,
                result.get("staged_identity")
            )
            companion_ok = not item.COMPANION_PATH or self._staged_identity_matches(
                item.COMPANION_PATH,
                item.COMPANION_SIZE,
                result.get("companion_staged_identity")
            )
            return {
                "source": str(item.CONTENT_HASH or "") if source_ok else "",
                "companion": str(item.COMPANION_HASH or "")
                if source_ok and companion_ok and item.COMPANION_PATH else ""
            }

    def _staged_identity_matches(self, path, expected_size, expected_identity):
        identity = expected_identity if isinstance(expected_identity, dict) else {}
        if not path or not identity:
            return False
        try:
            real_root = os.path.normcase(os.path.realpath(self._staging_root))
            real_path = os.path.normcase(os.path.realpath(path))
            if os.path.commonpath([real_root, real_path]) != real_root:
                return False
            current = os.lstat(path)
            if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
                return False
            current_size = int(getattr(current, "st_size", 0) or 0)
            if current_size != int(expected_size or 0) \
                    or current_size != int(identity.get("size") or 0) \
                    or int(getattr(current, "st_mtime_ns", 0) or 0) \
                    != int(identity.get("mtime_ns") or 0):
                return False
            expected_device = int(identity.get("device") or 0)
            expected_inode = int(identity.get("inode") or 0)
            if expected_device > 0 and expected_inode > 0:
                return (
                    int(getattr(current, "st_dev", 0) or 0),
                    int(getattr(current, "st_ino", 0) or 0)
                ) == (expected_device, expected_inode)
            return True
        except (OSError, ValueError, TypeError):
            return False

    def _upload_output_owned(self, item, path, expected_hash):
        """Return true only when a durable staging marker proves ownership."""
        state = self._upload_output_state(item)
        marker_path = str((state["planned"] or {}).get("ownership_marker") or "")
        if not marker_path or not path or not expected_hash:
            return False
        try:
            real_root = os.path.realpath(self._staging_root)
            real_marker = os.path.realpath(marker_path)
            if os.path.commonpath([real_root, real_marker]) != real_root:
                return False
            with open(real_marker, "r", encoding="utf-8") as marker_obj:
                marker = json.load(marker_obj)
            key = os.path.normcase(os.path.abspath(path))
            record = (marker.get("owned") or {}).get(key) or {}
            if os.path.normcase(os.path.abspath(str(record.get("path") or ""))) == key \
                    and str(record.get("hash") or "") == str(expected_hash):
                identity = record.get("identity") or {}
                if not identity:
                    # Backward compatibility for ownership markers written by
                    # the first task implementation.
                    return True
                current = os.stat(path, follow_symlinks=False)
                return int(identity.get("inode") or 0) > 0 \
                    and int(current.st_dev) == int(identity.get("device")) \
                    and int(current.st_ino) == int(identity.get("inode"))

            # A durable pre-publish intent records the inode of the verified
            # hidden sibling before link/rename.  If the process dies after
            # atomic publication but before the normal owned checkpoint, the
            # target retains that inode and can be safely rolled back.  A race
            # winner with identical bytes has a different identity and is
            # never deleted.
            intent = (marker.get("intents") or {}).get(key) or {}
            identity = intent.get("identity") or {}
            if os.path.normcase(os.path.abspath(str(intent.get("path") or ""))) != key \
                    or str(intent.get("hash") or "") != str(expected_hash) \
                    or int(identity.get("inode") or 0) <= 0:
                return False
            current = os.stat(path, follow_symlinks=False)
            return int(current.st_dev) == int(identity.get("device")) \
                and int(current.st_ino) == int(identity.get("inode"))
        except (OSError, ValueError, TypeError):
            return False

    def _reconcile_upload_outputs(self, task_id, rollback_incomplete=False):
        """Recover the publish-before-checkpoint crash window from planned hashes."""
        recovered = 0
        rolled_back = 0
        invalidated = 0
        verified = 0
        verified_item_ids = []
        changed = False
        with self._lock:
            items = self._db.query(SUBTITLETASKITEM).filter(
                SUBTITLETASKITEM.TASK_ID == str(task_id)
            ).all()
            for item in items:
                state = self._upload_output_state(item)
                if state["complete"]:
                    verified += 1
                    verified_item_ids.append(int(item.ID))
                    if item.STATUS == "succeeded":
                        continue
                    result = dict(state["planned"] or {})
                    result.update({
                        "canonical_subtitle": item.OUTPUT_PATH,
                        "target_subtitle": item.OUTPUT_PATH,
                        "companion_subtitle": item.OUTPUT_COMPANION_PATH or "",
                        "output_hash": item.OUTPUT_HASH,
                        "recovered_after_publish": True
                    })
                    item.STATUS = "succeeded"
                    item.STAGE = "published"
                    item.RESULT = _dumps(result)
                    item.ERROR = None
                    item.UPDATED_AT = time.time()
                    recovered += 1
                    changed = True
                    continue

                if item.STATUS == "succeeded":
                    item.STATUS = "queued"
                    item.STAGE = "planned"
                    item.ERROR = "已完成检查点的发布文件缺失或哈希不匹配，等待安全恢复"
                    item.UPDATED_AT = time.time()
                    invalidated += 1
                    changed = True

                if rollback_incomplete and (state["primary_ok"] or state["companion_ok"]):
                    candidates = [
                        (state["primary_path"], state["primary_hash"], state["primary_ok"]),
                        (state["companion_path"], state["companion_hash"], state["companion_ok"])
                    ]
                    for path, expected_hash, matches in candidates:
                        if matches and self._upload_output_owned(item, path, expected_hash):
                            try:
                                os.remove(path)
                                rolled_back += 1
                                changed = True
                            except OSError:
                                pass
                    item.STATUS = "canceled"
                    item.STAGE = "canceled"
                    item.UPDATED_AT = time.time()
                    changed = True
            if changed:
                self._db.commit()
        return {
            "recovered": recovered,
            "rolled_back": rolled_back,
            "invalidated": invalidated,
            "succeeded": verified,
            "verified_item_ids": verified_item_ids
        }

    def _interactive_worker(self):
        pending_recovery = None
        while not self._stop.is_set():
            self._wake.wait(timeout=1)
            self._wake.clear()
            while not self._stop.is_set():
                task_id = None
                try:
                    if pending_recovery:
                        recovery_id, recovery_error = pending_recovery
                        self._recover_escaped_claim(recovery_id, recovery_error)
                        pending_recovery = None
                    task_id = self._claim_next_interactive()
                    if not task_id:
                        break
                    try:
                        self._execute_task(task_id)
                    finally:
                        with self._lock:
                            self._executing_task_ids.discard(str(task_id))
                    task_id = None
                except Exception as error:
                    # SQLite lock/commit failures must not permanently kill the
                    # only interactive worker or orphan its already committed
                    # running claim.  Persist recovery before claiming another
                    # task; if recovery itself fails, keep retrying that same ID.
                    self._db.rollback()
                    ExceptionUtils.exception_traceback(error)
                    if task_id:
                        pending_recovery = (task_id, error)
                    if self._stop.wait(1):
                        break

    def _heartbeat_loop(self):
        """Checkpoint active budgets independently of long ffmpeg/LLM/NFS steps."""
        while not self._stop.wait(_HEARTBEAT_SECONDS):
            try:
                audit_worker_available = True
                try:
                    self._ensure_audit_worker_alive()
                except Exception as worker_error:
                    audit_worker_available = False
                    ExceptionUtils.exception_traceback(worker_error)
                with self._lock:
                    self._interrupt_orphaned_audits_locked(
                        "检测后台线程不可用，任务已安全中断",
                        include_unclaimed=not audit_worker_available
                    )
                    live_task_ids = list(self._executing_task_ids)
                    if not live_task_ids:
                        continue
                    now = time.time()
                    rows = self._db.query(SUBTITLETASK).filter(
                        SUBTITLETASK.STATUS.in_(["running", "canceling"]),
                        SUBTITLETASK.ID.in_(live_task_ids)
                    ).all()
                    for row in rows:
                        self._checkpoint_active(row, now, keep_running=True)
                        row.UPDATED_AT = now
                    if rows:
                        self._db.commit()
            except Exception as error:
                self._db.rollback()
                ExceptionUtils.exception_traceback(error)

    def _interrupt_orphaned_audits_locked(self, reason, include_unclaimed=False):
        """Finalize audit rows that have no corresponding live execution claim."""
        statuses = ["running", "canceling"]
        if include_unclaimed:
            statuses.extend(["queued", "recovering"])
        rows = self._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.TYPE == "audit",
            SUBTITLETASK.STATUS.in_(statuses)
        ).all()
        changed = False
        now = time.time()
        for row in rows:
            if str(row.ID) in self._executing_task_ids:
                continue
            self._checkpoint_active(row, now, keep_running=False)
            row.STATUS = "canceled" if row.CANCEL_REQUESTED else "interrupted"
            row.PHASE = "complete"
            row.MESSAGE = "任务已取消" if row.CANCEL_REQUESTED else str(reason)
            row.ERROR = None if row.CANCEL_REQUESTED else _dumps(str(reason))
            row.FINISHED_AT = now
            row.UPDATED_AT = now
            changed = True
        if changed:
            self._db.commit()
        return changed

    def _claim_next_interactive(self):
        with self._lock:
            row = self._db.query(SUBTITLETASK).filter(
                SUBTITLETASK.TYPE.in_(["upload", "repair"]),
                SUBTITLETASK.STATUS.in_(["queued", "recovering"])
            ).order_by(
                SUBTITLETASK.PRIORITY.desc(),
                SUBTITLETASK.CREATED_AT.asc()
            ).first()
            if not row:
                return None
            if row.CANCEL_REQUESTED:
                row.STATUS = "canceled"
                row.FINISHED_AT = time.time()
                row.UPDATED_AT = row.FINISHED_AT
                self._db.commit()
                self._cleanup_task_staging(row.ID)
                return None
            now = time.time()
            row.STATUS = "running"
            row.PHASE = "validating"
            row.MESSAGE = "任务开始处理"
            row.STARTED_AT = row.STARTED_AT or now
            row.RUN_STARTED_AT = now
            row.UPDATED_AT = now
            self._db.commit()
            self._executing_task_ids.add(str(row.ID))
            return row.ID

    def _start_audit_thread(self, task_id):
        # Kept as a compatibility shim for recovery call sites.  The task ID is
        # intentionally not bound to a transient thread: SQLite remains the
        # queue and the single dispatcher claims the oldest row.
        self._ensure_audit_worker_alive()
        self._audit_wake.set()

    def _claim_next_audit(self):
        with self._lock:
            row = self._db.query(SUBTITLETASK).filter(
                SUBTITLETASK.TYPE == "audit",
                SUBTITLETASK.STATUS.in_(["queued", "recovering"])
            ).order_by(SUBTITLETASK.CREATED_AT.asc()).first()
            if not row:
                return None
            now = time.time()
            if row.CANCEL_REQUESTED:
                row.STATUS = "canceled"
                row.PHASE = "complete"
                row.MESSAGE = "任务已在队列中取消"
                row.FINISHED_AT = now
                row.UPDATED_AT = now
                self._db.commit()
                return None
            row.STATUS = "running"
            row.PHASE = "enumerating"
            row.MESSAGE = "正在枚举字幕范围"
            row.STARTED_AT = row.STARTED_AT or now
            row.RUN_STARTED_AT = now
            row.UPDATED_AT = now
            self._db.commit()
            self._executing_task_ids.add(str(row.ID))
            return str(row.ID)

    def _audit_worker_loop(self):
        """Supervised single-slot audit dispatcher backed by the SQLite queue."""
        pending_recovery = None
        while not self._stop.is_set():
            self._audit_wake.wait(timeout=1)
            self._audit_wake.clear()
            while not self._stop.is_set():
                if pending_recovery:
                    recovery_id, recovery_error = pending_recovery
                    try:
                        self._recover_escaped_claim(recovery_id, recovery_error)
                        pending_recovery = None
                    except Exception as error:
                        self._db.rollback()
                        ExceptionUtils.exception_traceback(error)
                        if self._stop.wait(1):
                            return
                        continue
                task_id = None
                try:
                    task_id = self._claim_next_audit()
                    if not task_id:
                        break
                    try:
                        self._execute_task(task_id)
                    finally:
                        with self._lock:
                            self._executing_task_ids.discard(str(task_id))
                    task_id = None
                except Exception as error:
                    self._db.rollback()
                    ExceptionUtils.exception_traceback(error)
                    if task_id:
                        pending_recovery = (task_id, error)
                    if self._stop.wait(1):
                        return

    def _execute_task(self, task_id):
        try:
            task = self.get_task(task_id, owner=None, admin=True)
        except Exception as error:
            # The claim has already been committed.  If the first task read
            # fails transiently, no processor side effect has happened yet;
            # put the task back into the persistent recovery queue instead of
            # leaving an unreachable ``running`` row forever.
            self._db.rollback()
            self._recover_unstarted_claim(task_id, error)
            return
        if not task:
            return
        processor = self._processors.get(task.get("type"))
        if not processor:
            self.finish_task(task_id, "failed", error="字幕任务处理器未注册", message="任务无法执行")
            return
        try:
            processor(self, task_id)
            current = self.get_task(task_id, owner=None, admin=True)
            if current and current.get("status") not in TERMINAL_STATES:
                if self.is_cancel_requested(task_id):
                    self.finish_task(task_id, "canceled", message="任务已取消")
                else:
                    self.finish_task(task_id, "succeeded", result=current.get("result") or {}, message="任务完成")
        except Exception as error:
            # Any SQLAlchemy flush/commit failure leaves the scoped Session in
            # a failed transaction.  Clear it before reading task state or
            # attempting publish reconciliation so the worker cannot die with
            # PendingRollbackError.
            self._db.rollback()
            ExceptionUtils.exception_traceback(error)
            current = self.get_task(task_id, owner=None, admin=True)
            if current and current.get("status") not in TERMINAL_STATES:
                if self._stop.is_set():
                    # Leave uploads active for restart recovery; audit/repair
                    # will be marked interrupted by the next startup pass.
                    return
                if current.get("type") == "upload":
                    try:
                        reconciliation = self._reconcile_upload_outputs(task_id)
                        if reconciliation.get("succeeded"):
                            with self._lock:
                                row = self._db.query(SUBTITLETASK).filter(
                                    SUBTITLETASK.ID == str(task_id)
                                ).first()
                                if row and row.STATUS not in TERMINAL_STATES:
                                    now = time.time()
                                    self._checkpoint_active(row, now, keep_running=False)
                                    row.STATUS = "recovering"
                                    row.PHASE = "recovering"
                                    row.MESSAGE = "已确认发布检查点，继续恢复剩余字幕"
                                    row.UPDATED_AT = now
                                    self._db.commit()
                                    self._wake.set()
                                    return
                    except Exception as reconcile_error:
                        self._db.rollback()
                        ExceptionUtils.exception_traceback(reconcile_error)
                status = "canceled" if self.is_cancel_requested(task_id) else "failed"
                self.finish_task(task_id, status, error=str(error), message=str(error))
        finally:
            try:
                self.cleanup()
            except Exception as error:
                self._db.rollback()
                ExceptionUtils.exception_traceback(error)

    def _recover_unstarted_claim(self, task_id, error):
        cleanup_staging = False
        restart_audit = False
        with self._lock:
            row = self._db.query(SUBTITLETASK).filter(
                SUBTITLETASK.ID == str(task_id)
            ).first()
            if not row or row.STATUS in TERMINAL_STATES:
                return
            now = time.time()
            self._checkpoint_active(row, now, keep_running=False)
            if row.CANCEL_REQUESTED:
                row.STATUS = "canceled"
                row.PHASE = "canceled"
                row.MESSAGE = "任务已取消"
                row.FINISHED_AT = now
                cleanup_staging = row.TYPE == "upload"
            elif self._stop.is_set():
                # Leave the active row for the normal restart recovery rules.
                row.MESSAGE = "服务停止时任务尚未开始处理"
            else:
                row.STATUS = "recovering"
                row.PHASE = "recovering"
                row.MESSAGE = "任务启动读取失败，正在自动重试"
                restart_audit = row.TYPE == "audit"
            row.ERROR = _dumps(str(error))
            row.UPDATED_AT = now
            self._db.commit()
        if cleanup_staging:
            self._cleanup_task_staging(task_id)
        elif restart_audit and not self._stop.is_set():
            self._start_audit_thread(str(task_id))
        elif not self._stop.is_set():
            self._wake.set()

    def _recover_escaped_claim(self, task_id, error):
        """Resolve any exception escaping execution after a committed claim."""
        self._db.rollback()
        row = self._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.ID == str(task_id)
        ).first()
        if not row or row.STATUS in TERMINAL_STATES:
            return
        task_type = row.TYPE
        cancel_requested = bool(row.CANCEL_REQUESTED)
        recovered_outputs = 0
        if task_type == "upload":
            try:
                recovered_outputs = int(
                    self._reconcile_upload_outputs(task_id).get("succeeded") or 0
                )
            except Exception as reconcile_error:
                self._db.rollback()
                ExceptionUtils.exception_traceback(reconcile_error)
                # Ownership/hash state is unknown.  Keep this claim as the
                # worker's pending recovery and retry; never finalize cancel
                # or delete staging evidence on an unverified publish boundary.
                raise
        with self._lock:
            row = self._db.query(SUBTITLETASK).filter(
                SUBTITLETASK.ID == str(task_id)
            ).first()
            if not row or row.STATUS in TERMINAL_STATES:
                return
            now = time.time()
            self._checkpoint_active(row, now, keep_running=False)
            if task_type == "upload" and not cancel_requested and not self._stop.is_set():
                row.STATUS = "recovering"
                row.PHASE = "recovering"
                row.MESSAGE = "任务执行状态读取失败，正在从检查点恢复"
            elif task_type == "upload" and cancel_requested:
                row.STATUS = "partial" if recovered_outputs else "canceled"
                row.PHASE = row.STATUS
                row.MESSAGE = "任务已取消，已发布字幕予以保留" if recovered_outputs else "任务已取消"
                row.RESULT = _dumps({"recovered_publish_count": recovered_outputs})
                row.FINISHED_AT = now
            else:
                # Audit and repair are not replayed after an unknown
                # processor/DB boundary.  ``interrupted`` is honest and avoids
                # a duplicate scan or destructive rename; the user can
                # explicitly start either task again.
                row.STATUS = "interrupted"
                row.PHASE = "interrupted"
                row.MESSAGE = (
                    "字幕检测执行状态中断，请重新发起"
                    if task_type == "audit"
                    else "字幕修复执行状态中断，请确认结果后重新发起"
                )
                row.FINISHED_AT = now
            row.ERROR = _dumps(str(error))
            row.UPDATED_AT = now
            self._db.commit()
        if task_type == "upload" and not cancel_requested and not self._stop.is_set():
            self._wake.set()
        elif task_type == "upload" and cancel_requested and not recovered_outputs:
            self._cleanup_task_staging(task_id)

    def update_progress(self, task_id, phase=None, completed=None, total=None, percent=None,
                        current_item=None, message=None, metrics=None, force=False):
        now = time.time()
        with self._lock:
            row = self._db.query(SUBTITLETASK).filter(SUBTITLETASK.ID == str(task_id)).first()
            if not row or row.STATUS in TERMINAL_STATES:
                return False
            last = self._progress_last.get(task_id) or (0, "", "")
            phase_changed = phase is not None and phase != row.PHASE
            item_changed = current_item is not None and current_item != row.CURRENT_ITEM
            # Audit current paths can change several times per second.  They do
            # not bypass the five-second transaction throttle; phase changes do.
            changed = phase_changed or (row.TYPE != "audit" and item_changed)
            minimum_interval = 5 if row.TYPE == "audit" else 2
            if not force and not changed and now - last[0] < minimum_interval:
                return True
            if phase is not None:
                row.PHASE = str(phase)
            if completed is not None:
                row.COMPLETED = max(int(completed), 0)
            if total is not None:
                row.TOTAL = max(int(total), 0)
            row.PERCENT = float(percent) if percent is not None else None
            if current_item is not None:
                row.CURRENT_ITEM = str(current_item)
            if message is not None:
                row.MESSAGE = str(message)
            if metrics is not None:
                row.METRICS = _dumps(metrics)
            self._checkpoint_active(row, now, keep_running=True)
            row.UPDATED_AT = now
            self._db.commit()
            self._progress_last[task_id] = (now, row.PHASE or "", row.CURRENT_ITEM or "")
            return True

    def finish_task(self, task_id, status, result=None, error=None, message=None):
        status = str(status or "").lower()
        if status not in TERMINAL_STATES:
            raise SubtitleTaskError("任务终态无效")
        now = time.time()
        task_type = ""
        with self._lock:
            row = self._db.query(SUBTITLETASK).filter(SUBTITLETASK.ID == str(task_id)).first()
            if not row:
                raise TaskNotFound("字幕任务不存在")
            if row.STATUS in TERMINAL_STATES:
                return self._task_dict(row, include_result=True, include_items=True)
            task_type = row.TYPE
            if row.CANCEL_REQUESTED and status != "canceled":
                effect_persisted = False
                result_value = result if isinstance(result, dict) else {}
                if task_type == "upload":
                    effect_persisted = int(result_value.get("success_count") or 0) > 0
                elif task_type == "repair":
                    repair_data = result_value.get("data") or {}
                    effect_persisted = bool(repair_data.get("processed"))
                status = "partial" if effect_persisted else "canceled"
                message = (
                    "任务已取消，已完成的文件修改予以保留"
                    if effect_persisted else "任务已取消"
                )
                error = None
            self._checkpoint_active(row, now, keep_running=False)
            row.STATUS = status
            row.PHASE = "complete"
            if status == "succeeded":
                row.PERCENT = 100
            row.RESULT = _dumps(result or {})
            row.ERROR = _dumps(error) if error else None
            row.MESSAGE = str(message or row.MESSAGE or "")
            row.FINISHED_AT = now
            row.UPDATED_AT = now
            self._db.commit()
            value = self._task_dict(row, include_result=True, include_items=True)
        if task_type == "upload":
            self._cleanup_task_staging(task_id)
        self._wake.set()
        return value

    def cancel_task(self, task_id, owner=None, admin=False):
        cleanup = False
        with self._lock:
            row = self._db.query(SUBTITLETASK).filter(SUBTITLETASK.ID == str(task_id)).first()
            if not row or (not admin and owner is not None and row.OWNER != str(owner)):
                return None
            if row.STATUS in TERMINAL_STATES:
                return self._task_dict(row, include_result=True, include_items=True)
            now = time.time()
            row.CANCEL_REQUESTED = 1
            if row.STATUS in ["queued", "recovering"]:
                reconciliation = {"succeeded": 0}
                if row.TYPE == "upload":
                    reconciliation = self._reconcile_upload_outputs(
                        row.ID, rollback_incomplete=True
                    )
                row.STATUS = "partial" if reconciliation["succeeded"] else "canceled"
                row.PHASE = "complete"
                row.MESSAGE = (
                    "任务已取消，恢复时确认已发布的字幕予以保留"
                    if reconciliation["succeeded"] else "任务已在队列中取消"
                )
                row.RESULT = _dumps({
                    "success_count": reconciliation["succeeded"],
                    "failure_count": 0,
                    "stop_reason": "canceled",
                    "refresh": {
                        "status": "skipped", "scope": "none",
                        "message": "任务已取消，未执行局部刷新"
                    }
                } if reconciliation["succeeded"] else {})
                row.FINISHED_AT = now
                cleanup = row.TYPE == "upload"
            else:
                row.STATUS = "canceling"
                row.MESSAGE = "正在等待当前安全步骤停止"
            row.UPDATED_AT = now
            self._db.commit()
            value = self._task_dict(row, include_result=True, include_items=True)
        if cleanup:
            self._cleanup_task_staging(task_id)
        self._wake.set()
        return value

    def is_cancel_requested(self, task_id):
        with self._lock:
            row = self._db.query(SUBTITLETASK.CANCEL_REQUESTED, SUBTITLETASK.STATUS).filter(
                SUBTITLETASK.ID == str(task_id)
            ).first()
            return bool(row and (row[0] or row[1] in ["canceling", "canceled", "interrupted"]))

    def is_stopping(self):
        return self._stop.is_set()

    @staticmethod
    def _checkpoint_active(row, now, keep_running):
        if row.RUN_STARTED_AT:
            row.ACTIVE_SECONDS = float(row.ACTIVE_SECONDS or 0) + max(now - row.RUN_STARTED_AT, 0)
        row.RUN_STARTED_AT = now if keep_running and row.STATUS in ["running", "canceling"] else None

    @staticmethod
    def _checkpoint_recovered_active(row, now):
        """Settle only the bounded interval after the last durable heartbeat."""
        if row.RUN_STARTED_AT:
            last_durable = max(float(row.UPDATED_AT or 0), float(row.RUN_STARTED_AT))
            confirmed_end = min(float(now), last_durable + _HEARTBEAT_SECONDS)
            row.ACTIVE_SECONDS = float(row.ACTIVE_SECONDS or 0) + max(
                confirmed_end - float(row.RUN_STARTED_AT), 0
            )
        row.RUN_STARTED_AT = None

    def list_items(self, task_id):
        rows = self._db.query(SUBTITLETASKITEM).filter(
            SUBTITLETASKITEM.TASK_ID == str(task_id)
        ).order_by(SUBTITLETASKITEM.LOGICAL_INDEX.asc()).all()
        return [self._item_dict(row) for row in rows]

    @staticmethod
    def _item_dict(row):
        return {
            "item_id": row.ID,
            "id": row.ID,
            "item_key": row.ITEM_KEY,
            "index": row.LOGICAL_INDEX,
            "kind": row.KIND,
            "source_name": row.SOURCE_NAME,
            "companion_name": row.COMPANION_NAME or "",
            "staged_path": row.STAGED_PATH,
            "companion_path": row.COMPANION_PATH or "",
            "content_hash": row.CONTENT_HASH,
            "companion_hash": row.COMPANION_HASH or "",
            "size": row.SIZE or 0,
            "companion_size": row.COMPANION_SIZE or 0,
            "language": row.LANGUAGE or "",
            "status": row.STATUS,
            "stage": row.STAGE,
            "output_path": row.OUTPUT_PATH or "",
            "output_companion_path": row.OUTPUT_COMPANION_PATH or "",
            "output_hash": row.OUTPUT_HASH or "",
            "result": _loads(row.RESULT, {}) if row.RESULT else None,
            "error": _loads(row.ERROR, row.ERROR) if row.ERROR else None
        }

    def update_item(self, task_id, item_id, **values):
        with self._lock:
            try:
                query = self._db.query(SUBTITLETASKITEM).filter(
                    SUBTITLETASKITEM.TASK_ID == str(task_id)
                )
                try:
                    numeric_id = int(item_id)
                    row = query.filter(SUBTITLETASKITEM.ID == numeric_id).first()
                except (TypeError, ValueError):
                    row = query.filter(SUBTITLETASKITEM.ITEM_KEY == str(item_id)).first()
                if not row:
                    raise TaskNotFound("字幕任务项不存在")
                mapping = {
                    "status": "STATUS", "stage": "STAGE", "output_path": "OUTPUT_PATH",
                    "output_companion_path": "OUTPUT_COMPANION_PATH", "output_hash": "OUTPUT_HASH",
                    "language": "LANGUAGE"
                }
                for key, column in mapping.items():
                    if key in values:
                        setattr(row, column, values[key])
                if "result" in values:
                    row.RESULT = _dumps(values.get("result")) if values.get("result") is not None else None
                if "error" in values:
                    row.ERROR = _dumps(values.get("error")) if values.get("error") else None
                row.UPDATED_AT = time.time()
                self._db.commit()
                return self._item_dict(row)
            except Exception:
                self._db.rollback()
                raise

    @contextmanager
    def heavy_operation(self, kind="interactive", limit=None, cancel_check=None):
        """Global fair gate: audit releases after one subtitle and yields to UI work."""
        interactive = str(kind or "interactive").lower() != "audit"
        try:
            requested_limit = int(limit or DEFAULT_POLICY["heavy_process_concurrency"])
        except (TypeError, ValueError):
            requested_limit = DEFAULT_POLICY["heavy_process_concurrency"]
        requested_limit = max(
            _POLICY_RANGES["heavy_process_concurrency"][0],
            min(requested_limit, _POLICY_RANGES["heavy_process_concurrency"][1])
        )
        request_token = uuid.uuid4().hex
        acquired = False
        with self._heavy_condition:
            self._heavy_requests[request_token] = requested_limit
            if interactive:
                self._interactive_waiters += 1
            try:
                while True:
                    if self._stop.is_set():
                        raise InterruptedError("字幕任务管理器正在停止")
                    if cancel_check and cancel_check():
                        raise InterruptedError("字幕任务已取消或处理预算已耗尽")
                    audit_must_yield = not interactive and self._interactive_waiters > 0
                    effective_limit = min(self._heavy_requests.values())
                    if self._heavy_active < effective_limit and not audit_must_yield:
                        self._heavy_active += 1
                        acquired = True
                        break
                    self._heavy_condition.wait(timeout=0.25)
            finally:
                if interactive:
                    self._interactive_waiters = max(self._interactive_waiters - 1, 0)
                if not acquired:
                    self._heavy_requests.pop(request_token, None)
                    self._heavy_condition.notify_all()
        try:
            yield
        finally:
            with self._heavy_condition:
                if acquired:
                    self._heavy_active = max(self._heavy_active - 1, 0)
                self._heavy_requests.pop(request_token, None)
                self._heavy_condition.notify_all()

    def recent_audit_results(self, server=None, limit=3):
        """Return shared audit history without exposing task ownership metadata."""
        limit = max(1, min(int(limit or 3), 20))
        with self._lock:
            query = self._db.query(SUBTITLETASK).filter(
                SUBTITLETASK.TYPE == "audit",
                SUBTITLETASK.STATUS.in_(["succeeded", "partial"])
            )
            server = str(server or "").strip().lower()
            if server:
                query = query.filter(SUBTITLETASK.SERVER.in_({
                    server, server.capitalize(), server.upper()
                }))
            rows = query.order_by(
                SUBTITLETASK.FINISHED_AT.desc(), SUBTITLETASK.CREATED_AT.desc()
            ).limit(limit).all()
            history = []
            for row in rows:
                record = _loads(row.RESULT, {}) if row.RESULT else {}
                if not isinstance(record, dict) or not record:
                    continue
                record = dict(record)
                record.setdefault("checked_at", _iso(row.FINISHED_AT or row.UPDATED_AT))
                history.append(record)
            return history

    def probe_cache_get(self, server, fingerprint):
        if not isinstance(fingerprint, dict) or not fingerprint.get("path"):
            return None
        server = str(server or "").lower()
        path = os.path.normcase(os.path.abspath(str(fingerprint.get("path"))))
        row = self._db.query(SUBTITLEPROBECACHE).filter(
            SUBTITLEPROBECACHE.SERVER == server,
            SUBTITLEPROBECACHE.PATH == path
        ).first()
        if not row or row.FINGERPRINT != _fingerprint_hash(fingerprint):
            return None
        return _loads(row.RESULT, {})

    def probe_cache_put(self, server, fingerprint, result):
        return bool(self.probe_cache_put_many(server, [(fingerprint, result)]))

    def probe_cache_put_many(self, server, entries):
        """Persist a bounded cache batch in one SQLite transaction."""
        entries = list(entries or [])
        if not entries:
            return 0
        server = str(server or "").lower()
        written = 0
        with self._lock:
            for fingerprint, result in entries:
                if not isinstance(fingerprint, dict) or not fingerprint.get("path"):
                    continue
                now = time.time()
                path = os.path.normcase(os.path.abspath(str(fingerprint.get("path"))))
                row = self._db.query(SUBTITLEPROBECACHE).filter(
                    SUBTITLEPROBECACHE.SERVER == server,
                    SUBTITLEPROBECACHE.PATH == path
                ).first()
                values = {
                    "FINGERPRINT": _fingerprint_hash(fingerprint),
                    "SIZE": int(fingerprint.get("size") or 0),
                    "MTIME_NS": str(fingerprint.get("mtime_ns") or ""),
                    "PAIR_PATH": (
                        os.path.normcase(os.path.abspath(str(fingerprint.get("pair_path"))))
                        if fingerprint.get("pair_path") else None
                    ),
                    "PAIR_SIZE": fingerprint.get("pair_size"),
                    "PAIR_MTIME_NS": str(fingerprint.get("pair_mtime_ns") or ""),
                    "VALIDATOR_VERSION": fingerprint.get("validator_version"),
                    "PROBE_VERSION": fingerprint.get("ffprobe_version"),
                    "RESULT": _dumps(result or {}),
                    "UPDATED_AT": now
                }
                if row:
                    for key, value in values.items():
                        setattr(row, key, value)
                else:
                    self._db.insert(SUBTITLEPROBECACHE(
                        SERVER=server,
                        PATH=path,
                        CREATED_AT=now,
                        **values
                    ))
                written += 1
            if written:
                try:
                    self._db.commit()
                except Exception:
                    self._db.rollback()
                    raise
        return written

    def invalidate_probe_cache(self, paths):
        normalized = {
            os.path.normcase(os.path.abspath(str(path)))
            for path in (paths or []) if path
        }
        if not normalized:
            return 0
        with self._lock:
            query = self._db.query(SUBTITLEPROBECACHE).filter(or_(
                SUBTITLEPROBECACHE.PATH.in_(list(normalized)),
                SUBTITLEPROBECACHE.PAIR_PATH.in_(list(normalized))
            ))
            count = query.count()
            if count:
                query.delete(synchronize_session=False)
                self._db.commit()
            return count

    def replace_audit_states(self, scope_key, server, media_statuses, task_id=None):
        with self._lock:
            self._db.query(SUBTITLEAUDITSTATE).filter(
                SUBTITLEAUDITSTATE.SCOPE_KEY == str(scope_key),
                SUBTITLEAUDITSTATE.SERVER == str(server).lower()
            ).delete(synchronize_session=False)
            self._insert_audit_states(scope_key, server, media_statuses, task_id)
            self._db.commit()
            self._invalidate_audit_snapshot_cache(server)

    def commit_audit_result(self, task_id, scope_key, server, media_statuses,
                            result, status, message, replace=False):
        """Atomically publish visible audit state and the task terminal status.

        ``cancel_task`` uses the same manager lock, so a cancellation either
        wins before this transaction (no visible state is written) or arrives
        after the task is already terminal.
        """
        status = str(status or "").lower()
        if status not in ["succeeded", "partial"]:
            raise SubtitleTaskError("检测任务终态无效")
        # Latest-state storage is intentionally bounded.  A scope exceeding
        # the retention ceiling becomes partial and only upserts confirmed
        # entries; it must not delete older state as if coverage were complete.
        limited_statuses = {}
        state_overflow = False
        for index, (key, value) in enumerate((media_statuses or {}).items()):
            if index >= 50000:
                state_overflow = True
                break
            limited_statuses[key] = value
        media_statuses = limited_statuses
        if state_overflow:
            result = dict(result or {})
            result["partial"] = True
            result["coverage_complete"] = False
            result["stop_reason"] = result.get("stop_reason") or "state_limit"
            result["persisted_state_limit"] = 50000
            status = "partial"
            replace = False
            message = "外挂字幕检测部分完成：最新状态达到 50000 条持久化上限"
        now = time.time()
        with self._lock:
            row = self._db.query(SUBTITLETASK).filter(
                SUBTITLETASK.ID == str(task_id), SUBTITLETASK.TYPE == "audit"
            ).first()
            if not row:
                raise TaskNotFound("字幕检测任务不存在")
            if row.STATUS in TERMINAL_STATES:
                return self._task_dict(row, include_result=True, include_items=True)
            if row.CANCEL_REQUESTED:
                self._checkpoint_active(row, now, keep_running=False)
                row.STATUS = "canceled"
                row.PHASE = "complete"
                row.RESULT = _dumps(result or {})
                row.ERROR = None
                row.MESSAGE = "字幕检测已取消"
                row.FINISHED_AT = now
                row.UPDATED_AT = now
                self._db.commit()
                return self._task_dict(row, include_result=True, include_items=True)
            try:
                if replace:
                    self._db.query(SUBTITLEAUDITSTATE).filter(
                        SUBTITLEAUDITSTATE.SCOPE_KEY == str(scope_key),
                        SUBTITLEAUDITSTATE.SERVER == str(server).lower()
                    ).delete(synchronize_session=False)
                    self._insert_audit_states(scope_key, server, media_statuses, task_id)
                else:
                    self._upsert_audit_states_uncommitted(
                        scope_key, server, media_statuses, task_id, now
                    )
                self._checkpoint_active(row, now, keep_running=False)
                row.STATUS = status
                row.PHASE = "complete"
                row.PERCENT = 100 if status == "succeeded" else row.PERCENT
                row.RESULT = _dumps(result or {})
                row.ERROR = None
                row.MESSAGE = str(message or "")
                row.FINISHED_AT = now
                row.UPDATED_AT = now
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            self._invalidate_audit_snapshot_cache(server)
            return self._task_dict(row, include_result=True, include_items=True)

    def upsert_audit_states(self, scope_key, server, media_statuses, task_id=None):
        now = time.time()
        with self._lock:
            self._upsert_audit_states_uncommitted(
                scope_key, server, media_statuses, task_id, now
            )
            self._db.commit()
            self._invalidate_audit_snapshot_cache(server)

    def _upsert_audit_states_uncommitted(self, scope_key, server, media_statuses, task_id, now):
        scope_key = str(scope_key)
        server = str(server).lower()
        entries = []
        for key, value in (media_statuses or {}).items():
            media_path = str((value or {}).get("media_path") or key)
            entries.append((
                os.path.normcase(os.path.normpath(media_path)),
                media_path,
                value or {}
            ))
        for offset in range(0, len(entries), 500):
            chunk = entries[offset:offset + 500]
            paths = [entry[0] for entry in chunk]
            existing = self._db.query(SUBTITLEAUDITSTATE).filter(
                SUBTITLEAUDITSTATE.SCOPE_KEY == scope_key,
                SUBTITLEAUDITSTATE.SERVER == server,
                SUBTITLEAUDITSTATE.SUBTITLE_PATH.in_(paths)
            ).all()
            existing_by_path = {row.SUBTITLE_PATH: row for row in existing}
            for normalized, media_path, value in chunk:
                row = existing_by_path.get(normalized)
                if row:
                    row.MEDIA_PATH = media_path
                    row.STATUS = str(value.get("status") or "error")
                    row.REASON = str(value.get("reason") or "")
                    row.RESULT = _dumps(value)
                    row.TASK_ID = task_id
                    row.CONFIRMED_AT = now
                    row.UPDATED_AT = now
                else:
                    self._db.insert(self._audit_state_row(
                        scope_key, server, normalized, media_path, value, task_id, now
                    ))

    def _insert_audit_states(self, scope_key, server, media_statuses, task_id):
        now = time.time()
        for key, value in (media_statuses or {}).items():
            media_path = str((value or {}).get("media_path") or key)
            normalized = os.path.normcase(os.path.normpath(media_path))
            self._db.insert(self._audit_state_row(
                scope_key, server, normalized, media_path, value, task_id, now
            ))

    @staticmethod
    def _audit_state_row(scope_key, server, normalized, media_path, value, task_id, now):
        return SUBTITLEAUDITSTATE(
            SCOPE_KEY=str(scope_key),
            SERVER=str(server).lower(),
            SUBTITLE_PATH=normalized,
            MEDIA_PATH=media_path,
            STATUS=str((value or {}).get("status") or "error"),
            REASON=str((value or {}).get("reason") or ""),
            RESULT=_dumps(value or {}),
            TASK_ID=task_id,
            CONFIRMED_AT=now,
            UPDATED_AT=now
        )

    def get_audit_states(self, scope_key, server):
        rows = self._db.query(SUBTITLEAUDITSTATE).filter(
            SUBTITLEAUDITSTATE.SCOPE_KEY == str(scope_key),
            SUBTITLEAUDITSTATE.SERVER == str(server).lower()
        ).all()
        return {
            row.SUBTITLE_PATH: _loads(row.RESULT, {})
            for row in rows
        }

    def latest_audit_snapshots(self, server):
        """Aggregate the newest confirmed state per media path for library cards."""
        server = str(server or "").lower()
        with self._lock:
            cached = self._audit_snapshot_cache.get(server)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            rows = self._db.query(SUBTITLEAUDITSTATE).filter(
                SUBTITLEAUDITSTATE.SERVER == server
            ).order_by(SUBTITLEAUDITSTATE.UPDATED_AT.desc()).limit(
                _AUDIT_STATE_MAX_ROWS
            ).all()
            snapshots = {}
            seen = set()
            for row in rows:
                scope = _loads(row.SCOPE_KEY, {})
                category = str(scope.get("category") or "") if isinstance(scope, dict) else ""
                if category not in ["movie", "tv", "anime"]:
                    continue
                unique = (category, row.SUBTITLE_PATH)
                if unique in seen:
                    continue
                seen.add(unique)
                snapshot = snapshots.setdefault(category, {
                    "checked_at": _iso(row.CONFIRMED_AT),
                    "server": server,
                    "media_statuses": {}
                })
                snapshot["media_statuses"][row.SUBTITLE_PATH] = _loads(row.RESULT, {})
            self._audit_snapshot_cache[server] = (
                time.monotonic() + _AUDIT_SNAPSHOT_CACHE_SECONDS,
                snapshots
            )
            return snapshots

    def _invalidate_audit_snapshot_cache(self, server=None):
        if server is None:
            self._audit_snapshot_cache.clear()
        else:
            self._audit_snapshot_cache.pop(str(server or "").lower(), None)

    def invalidate_audit_states(self, paths):
        normalized = {
            os.path.normcase(os.path.normpath(str(path)))
            for path in (paths or []) if path
        }
        if not normalized:
            return 0
        with self._lock:
            query = self._db.query(SUBTITLEAUDITSTATE).filter(
                SUBTITLEAUDITSTATE.SUBTITLE_PATH.in_(list(normalized))
            )
            count = query.count()
            if count:
                query.delete(synchronize_session=False)
                self._db.commit()
                self._invalidate_audit_snapshot_cache()
            return count

    def cleanup(self):
        now = time.time()
        policy = self.get_settings()
        cutoff = now - policy["task_retention_days"] * 86400
        cleanup_markers = {}
        delete_ids = set()
        terminal_ids = set()
        terminal_upload_ids = set()
        with self._lock:
            terminal_rows = self._db.query(SUBTITLETASK).filter(
                SUBTITLETASK.STATUS.in_(list(TERMINAL_STATES))
            ).order_by(SUBTITLETASK.FINISHED_AT.desc(), SUBTITLETASK.CREATED_AT.desc()).all()
            terminal_ids = {str(row.ID) for row in terminal_rows}
            terminal_upload_ids = {
                str(row.ID) for row in terminal_rows if row.TYPE == "upload"
            }
            delete_ids = {
                row.ID for index, row in enumerate(terminal_rows)
                if (row.FINISHED_AT or row.UPDATED_AT or row.CREATED_AT) < cutoff
                or index >= policy["task_retention_count"]
            }
            if delete_ids:
                cleanup_markers = {
                    task_id: self._upload_marker_paths(task_id)
                    for task_id in delete_ids
                }
            if now - self._last_persistent_cache_cleanup >= 3600:
                cache_cutoff = now - _PROBE_CACHE_RETENTION_DAYS * 86400
                state_cutoff = now - _AUDIT_STATE_RETENTION_DAYS * 86400
                cache_changed = self._db.query(SUBTITLEPROBECACHE).filter(
                    SUBTITLEPROBECACHE.UPDATED_AT < cache_cutoff
                ).delete(synchronize_session=False)
                cache_overflow = [
                    row[0] for row in self._db.query(SUBTITLEPROBECACHE.ID).order_by(
                        SUBTITLEPROBECACHE.UPDATED_AT.desc()
                    ).offset(_PROBE_CACHE_MAX_ROWS).all()
                ]
                if cache_overflow:
                    cache_changed += self._db.query(SUBTITLEPROBECACHE).filter(
                        SUBTITLEPROBECACHE.ID.in_(cache_overflow)
                    ).delete(synchronize_session=False)
                state_changed = self._db.query(SUBTITLEAUDITSTATE).filter(
                    SUBTITLEAUDITSTATE.UPDATED_AT < state_cutoff
                ).delete(synchronize_session=False)
                state_overflow = [
                    row[0] for row in self._db.query(SUBTITLEAUDITSTATE.ID).order_by(
                        SUBTITLEAUDITSTATE.UPDATED_AT.desc()
                    ).offset(_AUDIT_STATE_MAX_ROWS).all()
                ]
                if state_overflow:
                    state_changed += self._db.query(SUBTITLEAUDITSTATE).filter(
                        SUBTITLEAUDITSTATE.ID.in_(state_overflow)
                    ).delete(synchronize_session=False)
                if cache_changed or state_changed:
                    self._db.commit()
                if state_changed:
                    self._invalidate_audit_snapshot_cache()
                self._last_persistent_cache_cleanup = now
            active_ids = {
                str(row[0]) for row in self._db.query(SUBTITLETASK.ID).filter(
                    SUBTITLETASK.TYPE == "upload",
                    SUBTITLETASK.STATUS.in_(list(ACTIVE_STATES))
                ).all()
            }
        # Migrate or consume exact ownership evidence before deleting the DB
        # rows that point to it.  Failed evidence persistence keeps both the
        # row and staging directory for a later bounded retry.
        deletable_ids = set()
        protected_ids = set()
        for task_id in delete_ids:
            try:
                if self._cleanup_task_staging(
                        task_id, marker_paths=cleanup_markers.get(task_id)):
                    deletable_ids.add(task_id)
                else:
                    protected_ids.add(str(task_id))
            except Exception:
                protected_ids.add(str(task_id))
        if deletable_ids:
            with self._lock:
                self._db.query(SUBTITLETASKITEM).filter(
                    SUBTITLETASKITEM.TASK_ID.in_(list(deletable_ids))
                ).delete(synchronize_session=False)
                self._db.query(SUBTITLETASK).filter(
                    SUBTITLETASK.ID.in_(list(deletable_ids))
                ).delete(synchronize_session=False)
                self._db.commit()
        # A recent terminal upload is retained in SQLite for the task center,
        # but its raw/derived bytes must still be retried after a transient NAS
        # delete failure.  Keeping terminal IDs out of the generic orphan pass
        # remains important because only this path has exact task ownership.
        for task_id in terminal_upload_ids - {str(value) for value in delete_ids}:
            try:
                if not self._cleanup_task_staging(task_id):
                    protected_ids.add(task_id)
            except Exception:
                protected_ids.add(task_id)
        self._retry_persistent_temp_cleanup()
        try:
            for entry in os.scandir(self._staging_root):
                if not entry.is_dir(follow_symlinks=False):
                    continue
                if os.path.normcase(os.path.realpath(entry.path)) \
                        == os.path.normcase(os.path.realpath(self._cleanup_marker_root)):
                    continue
                if entry.name in active_ids or entry.name in terminal_ids \
                        or entry.name in protected_ids \
                        or entry.name in self._spooling_ids:
                    continue
                try:
                    if now - entry.stat(follow_symlinks=False).st_mtime >= 300:
                        self._remove_tree(entry.path)
                except OSError:
                    continue
        except OSError:
            pass
        # Multipart request streams are flat files in a sibling inbox.  They
        # normally disappear when adopted by a task or when Flask closes the
        # request; this bounded pass removes crash leftovers without walking
        # the filesystem or touching an upload that could still be active.
        self._cleanup_incoming_files(remove_all=False, now=now)
        backup = os.path.join(Config().get_config_path(), "subtitle-audit-history.json.migrated.bak")
        try:
            if os.path.isfile(backup) and now - os.path.getmtime(backup) > 7 * 86400:
                os.remove(backup)
        except OSError:
            pass

    def _upload_marker_paths(self, task_id):
        rows = self._db.query(SUBTITLETASKITEM.RESULT).filter(
            SUBTITLETASKITEM.TASK_ID == str(task_id)
        ).all()
        paths = []
        for row in rows:
            result = _loads(row[0], {}) if row else {}
            marker = str((result or {}).get("ownership_marker") or "")
            if marker:
                paths.append(marker)
        return list(dict.fromkeys(paths))

    def _cleanup_upload_temp_artifacts(self, task_id=None, marker_paths=None):
        """Delete exact marker-owned hidden files without scanning media dirs."""
        if marker_paths is None:
            marker_paths = self._upload_marker_paths(task_id)
        staging_root = os.path.realpath(self._staging_root)
        token = r"[0-9a-fA-F]{32}"
        unresolved = set()
        for marker_path in marker_paths or []:
            real_marker = ""
            try:
                real_marker = os.path.realpath(os.path.abspath(str(marker_path)))
                if os.path.commonpath([staging_root, real_marker]) != staging_root:
                    continue
                with open(real_marker, "r", encoding="utf-8") as marker_obj:
                    marker = json.load(marker_obj)
                intents = marker.get("intents") or {}
                if not isinstance(intents, dict):
                    continue
                for target_key, record in intents.items():
                    if not isinstance(record, dict):
                        continue
                    target_value = str(record.get("path") or target_key or "")
                    temporary_value = str(record.get("temporary_path") or "")
                    if not target_value or not temporary_value:
                        continue
                    target = os.path.abspath(target_value)
                    temporary = os.path.abspath(temporary_value)
                    identity = record.get("identity") or {}
                    if os.path.normcase(os.path.dirname(target)) \
                            != os.path.normcase(os.path.dirname(temporary)):
                        continue
                    temp_pattern = re.compile(
                        rf"^\.{re.escape(os.path.basename(target))}\.subtitle-task-{token}\.tmp"
                        rf"(?:\.tmp-{token})?$"
                    )
                    if not temp_pattern.fullmatch(os.path.basename(temporary)) \
                            or os.path.islink(temporary):
                        continue
                    try:
                        current = os.lstat(temporary)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        unresolved.add(real_marker)
                        continue
                    if not stat.S_ISREG(current.st_mode) \
                            or int(identity.get("inode") or 0) <= 0 \
                            or int(current.st_dev) != int(identity.get("device") or -1) \
                            or int(current.st_ino) != int(identity.get("inode") or -1):
                        continue
                    if record.get("temporary_complete"):
                        expected_hash = str(record.get("temporary_hash") or "")
                        if not expected_hash or not self._file_matches(temporary, expected_hash):
                            unresolved.add(real_marker)
                            continue
                        # Recheck identity after hashing to close a replacement
                        # race before the destructive operation.
                        after_hash = os.lstat(temporary)
                        if int(after_hash.st_dev) != int(current.st_dev) \
                                or int(after_hash.st_ino) != int(current.st_ino):
                            continue
                    before_remove = os.lstat(temporary)
                    if int(before_remove.st_dev) != int(current.st_dev) \
                            or int(before_remove.st_ino) != int(current.st_ino):
                        continue
                    try:
                        os.remove(temporary)
                    except OSError:
                        try:
                            remaining = os.lstat(temporary)
                            if int(remaining.st_dev) == int(current.st_dev) \
                                    and int(remaining.st_ino) == int(current.st_ino):
                                unresolved.add(real_marker)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            unresolved.add(real_marker)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                if real_marker and os.path.lexists(real_marker):
                    unresolved.add(real_marker)
        return unresolved

    def _persist_cleanup_markers(self, task_id, marker_paths):
        """Atomically retain small cleanup evidence outside the bulky task dir."""
        preserved = set()
        os.makedirs(self._cleanup_marker_root, exist_ok=True)
        cleanup_root = os.path.realpath(self._cleanup_marker_root)
        staging_root = os.path.realpath(self._staging_root)
        for marker_path in marker_paths or []:
            try:
                source = os.path.realpath(os.path.abspath(str(marker_path)))
                if os.path.commonpath([staging_root, source]) != staging_root:
                    continue
                if os.path.commonpath([cleanup_root, source]) == cleanup_root:
                    preserved.add(source)
                    continue
                with open(source, "rb") as source_obj:
                    content = source_obj.read(1024 * 1024 + 1)
                if len(content) > 1024 * 1024:
                    continue
                key = hashlib.sha256(
                    (str(task_id or "") + "\0" + source).encode("utf-8")
                ).hexdigest()
                destination = os.path.join(cleanup_root, key + ".json")
                temporary = destination + ".tmp-" + uuid.uuid4().hex
                try:
                    with open(temporary, "xb") as target_obj:
                        target_obj.write(content)
                        target_obj.flush()
                        os.fsync(target_obj.fileno())
                    os.replace(temporary, destination)
                    temporary = ""
                    preserved.add(destination)
                finally:
                    if temporary and os.path.exists(temporary):
                        try:
                            os.remove(temporary)
                        except OSError:
                            pass
            except (OSError, ValueError, TypeError):
                continue
        return preserved

    def _retry_persistent_temp_cleanup(self):
        try:
            marker_paths = [
                entry.path for entry in os.scandir(self._cleanup_marker_root)
                if entry.is_file(follow_symlinks=False)
                and not entry.is_symlink()
                and re.fullmatch(r"[0-9a-f]{64}\.json", entry.name)
            ]
        except OSError:
            return
        unresolved = self._cleanup_upload_temp_artifacts(marker_paths=marker_paths)
        for marker_path in marker_paths:
            if os.path.realpath(marker_path) in unresolved:
                continue
            try:
                os.remove(marker_path)
            except OSError:
                continue

    def _cleanup_task_staging(self, task_id, marker_paths=None):
        task = self._db.query(SUBTITLETASK).filter(SUBTITLETASK.ID == str(task_id)).first()
        payload = _loads(task.PAYLOAD, {}) if task else {}
        staging_dir = payload.get("staging_dir") or os.path.join(self._staging_root, str(task_id))
        if marker_paths is None:
            marker_paths = self._upload_marker_paths(task_id)
        unresolved = self._cleanup_upload_temp_artifacts(
            task_id, marker_paths=marker_paths
        )
        if unresolved:
            preserved = self._persist_cleanup_markers(task_id, unresolved)
            if len(preserved) != len(unresolved):
                # Never delete the only durable ownership evidence.  A later
                # bounded cleanup pass can retry after the volume recovers.
                return False
        real_root = os.path.realpath(self._staging_root)
        real_target = os.path.realpath(staging_dir)
        try:
            if os.path.commonpath([real_root, real_target]) == real_root and real_target != real_root:
                return self._remove_tree(real_target)
        except ValueError:
            return False
        return False

    @staticmethod
    def _remove_tree(path):
        if not path or not os.path.lexists(path):
            return True

        def onerror(function, target, _exc):
            try:
                os.chmod(target, stat.S_IWRITE | stat.S_IREAD)
                function(target)
            except OSError:
                pass

        try:
            shutil.rmtree(path, onerror=onerror)
        except OSError:
            return False
        return not os.path.lexists(path)

    def _migrate_legacy_audit_history(self):
        history_file = os.path.join(Config().get_config_path(), "subtitle-audit-history.json")
        if not os.path.isfile(history_file):
            return
        if self._db.query(SUBTITLETASK).filter(SUBTITLETASK.TYPE == "audit").count() > 0:
            return
        try:
            with open(history_file, "r", encoding="utf-8") as file_obj:
                store = json.load(file_obj)
            history = (store.get("history") or [])[:3]
            now = time.time()
            latest_states_by_scope = {}
            for offset, record in enumerate(reversed(history)):
                checked_at = self._parse_time(record.get("checked_at")) or now - offset
                result = dict(record or {})
                media_statuses = result.pop("media_statuses", {})
                scope_payload = {
                    "server": result.get("server") or "emby",
                    "category": result.get("category") or "",
                    "subcategory": result.get("subcategory") or "",
                    "mode": result.get("mode") or "deep"
                }
                scope_key = _dumps(scope_payload)
                task_id = str(uuid.uuid4())
                complete = result.get("coverage_complete") is not False
                self._db.insert(SUBTITLETASK(
                    ID=task_id,
                    TYPE="audit",
                    OWNER="0",
                    STATUS="succeeded" if complete else "partial",
                    PRIORITY=0,
                    SERVER=scope_payload["server"],
                    DEDUPE_KEY=_fingerprint_hash(scope_payload),
                    SCOPE_KEY=scope_key,
                    PAYLOAD=_dumps(scope_payload),
                    POLICY=_dumps(DEFAULT_POLICY),
                    PHASE="complete",
                    COMPLETED=int((result.get("summary") or {}).get("total") or 0),
                    TOTAL=int((result.get("summary") or {}).get("total") or 0),
                    PERCENT=100,
                    MESSAGE="从旧字幕检测记录迁移",
                    METRICS=_dumps(result.get("metrics") or {}),
                    RESULT=_dumps(result),
                    CANCEL_REQUESTED=0,
                    CREATED_AT=checked_at,
                    QUEUED_AT=checked_at,
                    STARTED_AT=checked_at,
                    FINISHED_AT=checked_at,
                    UPDATED_AT=checked_at
                ))
                # The state table stores only the newest snapshot for a scope.
                # Importing every historical snapshot would violate its unique
                # (scope, server, path) key when the same media appears twice.
                latest_states_by_scope[(scope_key, scope_payload["server"])] = (
                    media_statuses, task_id
                )
            for (scope_key, server), (media_statuses, task_id) in latest_states_by_scope.items():
                self._insert_audit_states(scope_key, server, media_statuses, task_id)
            latest = store.get("latest") or {}
            for category, snapshot in latest.items():
                server = str((snapshot or {}).get("server") or "emby")
                scope_key = _dumps({
                    "server": server, "category": category,
                    "subcategory": "", "mode": "legacy"
                })
                self._insert_audit_states(
                    scope_key, server, (snapshot or {}).get("media_statuses") or {}, None
                )
            self._db.commit()
            backup = history_file + ".migrated.bak"
            os.replace(history_file, backup)
            log.info("【SubtitleTask】旧字幕检测记录已迁移到 SQLite")
        except Exception as error:
            self._db.rollback()
            ExceptionUtils.exception_traceback(error)
            log.error("【SubtitleTask】迁移旧字幕检测记录失败，已保留原文件：%s" % str(error))

    @staticmethod
    def _parse_time(value):
        try:
            return datetime.datetime.fromisoformat(str(value)).timestamp()
        except (TypeError, ValueError):
            return None


_MANAGER = None
_MANAGER_LOCK = threading.Lock()


def get_subtitle_task_manager():
    global _MANAGER
    if _MANAGER is None:
        with _MANAGER_LOCK:
            if _MANAGER is None:
                _MANAGER = SubtitleTaskManager()
    return _MANAGER
