import datetime
import json
import os
import time
import weakref

from app.library import MediaLibrary
from app.mediaserver import MediaServer
from app.subtitle import Subtitle
from config import RMT_MEDIAEXT


_REGISTERED_MANAGERS = weakref.WeakSet()
_LOCAL_REFRESH_MIN_BUDGET_SECONDS = 300
_LOCAL_REFRESH_POST_VALIDATION_SECONDS = 180


class _TaskPathGuard:
    """Revalidate persisted canonical media paths at every destructive boundary."""

    def __init__(self, payload, bindings):
        snapshots = payload.get("path_authorization")
        if not isinstance(snapshots, dict):
            raise PermissionError("字幕任务缺少持久化路径授权，请重新提交")
        self._entries = []
        for name, path in bindings.items():
            lexical = os.path.abspath(os.path.normpath(str(path or "")))
            snapshot = snapshots.get(name)
            if not isinstance(snapshot, dict):
                raise PermissionError(f"字幕任务缺少 {name} 路径授权，请重新提交")
            snapshot_path = os.path.abspath(os.path.normpath(str(snapshot.get("path") or "")))
            if not snapshot_path or os.path.normcase(snapshot_path) != os.path.normcase(lexical):
                raise PermissionError(f"字幕任务 {name} 路径与授权快照不一致")
            parent_real = os.path.normcase(str(
                snapshot.get("parent_real") or snapshot.get("directory") or ""
            ))
            referent_real = os.path.normcase(str(
                snapshot.get("referent_real") or snapshot.get("real_path") or ""
            ))
            roots = [
                os.path.normcase(os.path.realpath(os.path.abspath(root)))
                for root in (snapshot.get("trusted_roots") or []) if root
            ]
            if not parent_real or not referent_real or not roots \
                    or not self._inside(parent_real, roots):
                raise PermissionError(f"字幕任务 {name} 路径授权无效，请重新提交")
            legacy_identity = snapshot.get("identity") or {}
            expected_is_link = snapshot.get("is_link")
            if expected_is_link is None:
                expected_is_link = os.path.islink(lexical)
            link_identity = snapshot.get("link_identity") or (
                {} if expected_is_link else legacy_identity
            )
            self._entries.append({
                "name": name,
                "path": lexical,
                "referent_real": referent_real,
                "parent_real": parent_real,
                "roots": roots,
                "is_link": bool(expected_is_link),
                "link_identity": dict(link_identity),
                "referent_identity": dict(
                    snapshot.get("referent_identity") or legacy_identity
                )
            })

    @staticmethod
    def _inside(path, roots):
        for root in roots:
            try:
                if os.path.commonpath([root, path]) == root:
                    return True
            except (OSError, ValueError, TypeError):
                continue
        return False

    @staticmethod
    def _identity_matches(expected, stat_result):
        expected = expected or {}
        expected_device = int(expected.get("device") or 0)
        expected_inode = int(expected.get("inode") or 0)
        current_device = int(getattr(stat_result, "st_dev", 0) or 0)
        current_inode = int(getattr(stat_result, "st_ino", 0) or 0)
        if expected_device > 0 and expected_inode > 0:
            if (current_device, current_inode) != (expected_device, expected_inode):
                return False
        elif "size" in expected and "mtime_ns" in expected:
            if int(getattr(stat_result, "st_size", 0) or 0) != int(expected.get("size") or 0) \
                    or int(getattr(stat_result, "st_mtime_ns", 0) or 0) \
                    != int(expected.get("mtime_ns") or 0):
                return False
        return True

    def validate_media_paths(self):
        for entry in self._entries:
            path = entry["path"]
            if not os.path.isfile(path) \
                    or os.path.splitext(path)[-1].lower() not in RMT_MEDIAEXT:
                raise PermissionError(f"任务媒体路径已失效：{path}")
            current_parent = os.path.normcase(os.path.realpath(os.path.dirname(path)))
            current_referent = os.path.normcase(os.path.realpath(path))
            if current_parent != entry["parent_real"] \
                    or current_referent != entry["referent_real"] \
                    or not self._inside(current_parent, entry["roots"]):
                raise PermissionError(f"任务媒体路径授权已变化：{path}")
            if os.path.islink(path) != entry["is_link"]:
                raise PermissionError(f"任务媒体链接类型已变化：{path}")
            if not self._identity_matches(entry["link_identity"], os.lstat(path)) \
                    or not self._identity_matches(entry["referent_identity"], os.stat(path)):
                raise PermissionError(f"任务媒体文件身份已变化：{path}")
        return True

    def __call__(self, path):
        self.validate_media_paths()
        candidate = os.path.abspath(os.path.normpath(str(path or "")))
        candidate_real = os.path.normcase(os.path.realpath(candidate))
        candidate_dir = os.path.normcase(os.path.realpath(os.path.dirname(candidate)))
        for entry in self._entries:
            if os.path.normcase(candidate) == os.path.normcase(entry["path"]):
                return True
            if candidate_dir == entry["parent_real"] \
                    and self._inside(candidate_dir, entry["roots"]) \
                    and self._inside(candidate_real, entry["roots"]):
                if os.path.lexists(candidate) and os.path.islink(candidate):
                    raise PermissionError(f"字幕任务拒绝操作软链接字幕：{candidate}")
                return True
        raise PermissionError(f"字幕任务目标超出已授权媒体目录：{candidate}")


def _task(manager, task_id):
    task = manager.get_task(task_id, owner=None, admin=True)
    if not task:
        raise RuntimeError("字幕任务不存在")
    return task


def _policy(task):
    return task.get("policy_snapshot") or task.get("policy") or {}


def _payload(task):
    return task.get("payload") or task.get("scope") or {}


def _item_id(item):
    return item.get("item_id") or item.get("id") or item.get("item_key")


def _update_item(manager, task_id, item, **values):
    return manager.update_item(task_id, _item_id(item), **values)


def _finish(manager, task_id, status, result=None, error=None, message=None):
    return manager.finish_task(
        task_id,
        status=status,
        result=result or {},
        error=error,
        message=message
    )


def _refresh_context(payload, media_path, server_type):
    claimed = payload.get("refresh_context") or {}
    return MediaLibrary().validate_subtitle_refresh_context(
        media_path,
        server_type,
        server_item_id=claimed.get("server_item_id") or payload.get("server_item_id"),
        parent_server_item_id=(
            claimed.get("parent_server_item_id") or payload.get("parent_server_item_id")
        ),
        library_id=claimed.get("library_id") or payload.get("library_id")
    )


def _localized_refresh(payload, media_path, server_type, remaining_budget=None):
    try:
        if callable(remaining_budget) \
                and remaining_budget() < _LOCAL_REFRESH_MIN_BUDGET_SECONDS:
            return {
                "status": "skipped", "scope": "none", "server": server_type,
                "budget_limited": True,
                "message": "剩余任务预算不足以完成有界的项目校验与局部刷新"
            }
        context = _refresh_context(payload, media_path, server_type)
        if not context.get("valid"):
            return {
                "status": "skipped",
                "scope": "none",
                "server": server_type,
                "message": context.get("reason") or "无法可靠定位媒体服务器项目，已跳过刷新"
            }
        if callable(remaining_budget) \
                and remaining_budget() < _LOCAL_REFRESH_POST_VALIDATION_SECONDS:
            return {
                "status": "skipped", "scope": "none", "server": server_type,
                "budget_limited": True,
                "message": "项目校验后剩余预算不足，已跳过局部刷新"
            }
        return MediaServer().refresh_subtitle_target_by_type(
            server_type,
            server_item_id=context.get("server_item_id"),
            parent_server_item_id=context.get("parent_server_item_id"),
            library_id=context.get("library_id"),
            media_path=media_path
        )
    except Exception as error:
        return {
            "status": "failed",
            "scope": "none",
            "server": server_type,
            "message": f"局部刷新准备失败：{str(error)}"
        }


def process_upload_task(manager, task_id):
    task = _task(manager, task_id)
    payload = _payload(task)
    policy = _policy(task)
    items = manager.list_items(task_id)
    requested_canonical_media = os.path.normpath(str(
        payload.get("canonical_media_file") or payload.get("target_media_file")
        or payload.get("media_file") or ""
    ))
    requested_original_media = os.path.normpath(str(
        payload.get("media_file") or requested_canonical_media
    ))
    # Keep lexical paths: a library media file may itself be a symlink to the
    # download volume, while its subtitle must be published beside the link.
    canonical_media = requested_canonical_media
    original_media = requested_original_media
    server_type = str(task.get("server") or payload.get("server") or "emby").lower()
    align_mode = str(payload.get("align_mode") or payload.get("align") or "none").lower()
    path_guard = _TaskPathGuard(payload, {
        "source": original_media,
        "target": canonical_media
    })
    path_guard.validate_media_paths()
    started = time.monotonic()
    total_budget_seconds = max(float(policy.get("upload_budget_minutes") or 60) * 60, 60)
    already_active = float((task.get("progress") or {}).get("elapsed_seconds") or 0)
    budget_seconds = max(total_budget_seconds - already_active, 0)
    heavy_limit = int(policy.get("heavy_process_concurrency") or 1)
    results = []
    successes = 0
    failures = 0

    def canceled():
        return manager.is_cancel_requested(task_id)

    def budget_exhausted():
        return budget_seconds <= 0 or time.monotonic() - started >= budget_seconds

    def remaining_budget():
        return max(budget_seconds - (time.monotonic() - started), 0)

    def should_abort():
        return canceled() or budget_exhausted() or manager.is_stopping()

    def heavy_operation(kind="interactive"):
        return manager.heavy_operation(
            kind, limit=heavy_limit, cancel_check=should_abort
        )

    for item_index, item in enumerate(items):
        if str(item.get("status") or "").lower() == "succeeded":
            if manager.verify_upload_item_output(task_id, _item_id(item)):
                previous = item.get("result") or {}
                results.append({
                    "filename": item.get("source_name") or "",
                    "success": True,
                    "message": "已从检查点恢复",
                    "data": previous
                })
                successes += 1
                continue
            _update_item(
                manager, task_id, item, status="queued", stage="planned",
                error="发布文件缺失或哈希不匹配，正在从暂存区恢复"
            )
        if should_abort():
            break
        source_name = item.get("source_name") or os.path.basename(item.get("staged_path") or "")
        manager.update_progress(
            task_id,
            phase="validating",
            completed=item_index,
            total=len(items),
            percent=round(item_index * 100 / len(items), 2) if items else 0,
            current_item=source_name,
            message="正在校验字幕"
        )
        _update_item(manager, task_id, item, status="running", stage="validating", error=None)
        planned = {
            "primary": item.get("output_path") or "",
            "companion": item.get("output_companion_path") or ""
        }
        if not planned["primary"]:
            planned = None

        def checkpoint(phase, message, extra, current=item, current_name=source_name):
            if extra.get("planned_outputs"):
                paths = extra["planned_outputs"]
                hashes = extra.get("planned_hashes") or {}
                planned_result = dict(current.get("result") or {})
                planned_result.update({
                    "planned_output_hash": hashes.get("primary") or "",
                    "planned_companion_hash": hashes.get("companion") or "",
                    "ownership_marker": extra.get("ownership_marker") or ""
                })
                _update_item(
                    manager,
                    task_id,
                    current,
                    stage="planned",
                    output_path=paths.get("primary") or "",
                    output_companion_path=paths.get("companion") or "",
                    output_hash=hashes.get("primary") or "",
                    result=planned_result
                )
            manager.update_progress(
                task_id,
                phase=phase,
                completed=item_index,
                total=len(items),
                percent=round(item_index * 100 / len(items), 2) if items else 0,
                current_item=current_name,
                message=message
            )

        publication_returned = False
        try:
            manager.ensure_task_target_space(task_id, canonical_media)
            trusted_hashes = {}
            trusted_hash_lookup = getattr(manager, "trusted_upload_item_hashes", None)
            if callable(trusted_hash_lookup):
                trusted_hashes = trusted_hash_lookup(task_id, _item_id(item)) or {}
            work_dir = os.path.join(
                os.path.dirname(item.get("staged_path") or ""),
                "derived",
                str(item.get("item_key") or _item_id(item))
            )
            data = Subtitle().process_staged_upload(
                staged_path=item.get("staged_path"),
                original_name=source_name,
                canonical_media_file=canonical_media,
                server_type=server_type,
                align_mode=align_mode,
                companion_path=item.get("companion_path") or None,
                work_dir=work_dir,
                planned_outputs=planned,
                cancel_check=should_abort,
                heavy_operation=heavy_operation,
                phase_callback=checkpoint,
                policy=policy,
                remaining_budget=remaining_budget,
                path_guard_check=path_guard,
                trusted_source_hash=trusted_hashes.get("source") or "",
                trusted_companion_hash=trusted_hashes.get("companion") or ""
            )
            publication_returned = True
            data["source_subtitle"] = data.get("canonical_subtitle") if canonical_media == original_media else ""
            data["linked_target"] = canonical_media != original_media
            _update_item(
                manager,
                task_id,
                item,
                status="succeeded",
                stage="published",
                output_path=data.get("canonical_subtitle") or "",
                output_companion_path=data.get("companion_subtitle") or "",
                output_hash=data.get("output_hash") or "",
                language=data.get("language") or "",
                result=data,
                error=None
            )
        except InterruptedError:
            if canceled():
                _update_item(
                    manager, task_id, item, status="canceled", stage="canceled", error="任务已取消"
                )
            elif budget_exhausted():
                _update_item(
                    manager, task_id, item, status="failed", stage="time_limit",
                    error="上传任务累计处理预算已耗尽"
                )
                results.append({
                    "filename": source_name,
                    "success": False,
                    "message": "上传任务累计处理预算已耗尽",
                    "data": {}
                })
                failures += 1
            else:
                if not manager.is_stopping():
                    _update_item(
                        manager, task_id, item, status="failed", stage="interrupted",
                        error="字幕处理被意外中断"
                    )
                raise
            break
        except Exception as error:
            # The file publication boundary has already been crossed.  A DB
            # checkpoint failure must bubble to the manager so it can rollback
            # the Session and reconcile the persisted path/hash marker; never
            # rewrite the item as failed after an irreversible publish.
            if publication_returned:
                raise
            _update_item(
                manager,
                task_id,
                item,
                status="failed",
                stage="failed",
                error=str(error)
            )
            results.append({
                "filename": source_name,
                "success": False,
                "message": str(error),
                "data": {}
            })
            failures += 1
        else:
            # Publication + item checkpoint is the irreversible boundary.  Any
            # cache or UI housekeeping failure after this point is only a
            # warning and must never turn the item back into failed.
            results.append({
                "filename": source_name,
                "success": True,
                "message": "字幕已发布",
                "data": data
            })
            successes += 1
            try:
                manager.invalidate_probe_cache([
                    data.get("canonical_subtitle"),
                    data.get("companion_subtitle")
                ])
            except Exception as warning:
                data.setdefault("warnings", []).append(f"探测缓存失效失败：{str(warning)}")
            try:
                MediaLibrary.invalidate_subtitle_directory_cache(data.get("canonical_subtitle"))
            except Exception as warning:
                data.setdefault("warnings", []).append(f"目录缓存失效失败：{str(warning)}")

    if manager.is_stopping() and not canceled() and not budget_exhausted():
        raise InterruptedError("manager stopping")
    processing_timed_out = budget_exhausted()
    was_canceled = canceled()
    refresh = {
        "status": "skipped", "scope": "none",
        "message": "没有已发布字幕，无需刷新"
    }
    if successes:
        try:
            manager.invalidate_audit_states([requested_canonical_media])
        except Exception as warning:
            result_warning = f"检测状态失效失败：{str(warning)}"
            for item_result in results:
                if item_result.get("success"):
                    item_result.setdefault("data", {}).setdefault("warnings", []).append(result_warning)
    if successes and not was_canceled and not processing_timed_out:
        try:
            manager.update_progress(
                task_id,
                phase="refreshing",
                completed=successes + failures,
                total=len(items),
                percent=99,
                current_item="",
                message="正在局部刷新媒体项目"
            )
        except Exception as warning:
            for item_result in results:
                if item_result.get("success"):
                    item_result.setdefault("data", {}).setdefault("warnings", []).append(
                        f"刷新阶段进度保存失败：{str(warning)}"
                    )
        try:
            refresh = _localized_refresh(
                payload, requested_canonical_media, server_type,
                remaining_budget=remaining_budget
            )
        except Exception as warning:
            refresh = {
                "status": "failed", "scope": "none", "server": server_type,
                "message": f"局部刷新异常：{str(warning)}"
            }
        was_canceled = canceled()
        if budget_exhausted():
            refresh.setdefault("budget_limited", True)
            refresh.setdefault(
                "budget_warning",
                "局部刷新跨过任务预算边界；字幕发布结果不受影响"
            )
    elif successes and was_canceled:
        refresh = {
            "status": "skipped", "scope": "none",
            "message": "任务已取消；已发布字幕交由媒体服务器实时监控发现"
        }
    elif successes and processing_timed_out:
        refresh = {
            "status": "skipped", "scope": "none",
            "message": "剩余任务预算不足，已跳过局部刷新；字幕交由实时监控发现"
        }

    result = {
        "media_file": requested_original_media,
        "canonical_media_file": requested_canonical_media,
        "linked_target": bool(payload.get("linked_target")),
        "server": server_type,
        "results": results,
        "success_count": successes,
        "failure_count": failures,
        "refresh": refresh
    }
    if was_canceled:
        status = "partial" if successes else "canceled"
        message = "任务已取消，已发布的字幕予以保留" if successes else "任务已取消"
    elif processing_timed_out:
        status = "partial" if successes else "failed"
        message = f"上传任务达到 {int(policy.get('upload_budget_minutes') or 60)} 分钟累计处理预算"
        result["stop_reason"] = "time_limit"
    elif failures and successes:
        status = "partial"
        message = f"已发布 {successes} 个字幕，{failures} 个失败"
    elif failures:
        status = "failed"
        message = "字幕均处理失败"
    else:
        status = "succeeded"
        message = f"已发布 {successes} 个字幕"
    return _finish(
        manager,
        task_id,
        status,
        result=result,
        error=None if status in ["succeeded", "partial", "canceled"] else message,
        message=message
    )


def process_repair_task(manager, task_id):
    task = _task(manager, task_id)
    payload = _payload(task)
    policy = _policy(task)
    requested_media_file = os.path.normpath(str(payload.get("media_path") or ""))
    # Repair external subtitles beside the lexical library entry, including
    # symlink-file media layouts.
    media_file = requested_media_file
    server_type = str(task.get("server") or payload.get("server") or "emby").lower()
    path_guard = _TaskPathGuard(payload, {"repair": media_file})
    path_guard.validate_media_paths()
    started = time.monotonic()
    total_budget_seconds = max(float(policy.get("upload_budget_minutes") or 60) * 60, 60)
    already_active = float((task.get("progress") or {}).get("elapsed_seconds") or 0)
    budget_seconds = max(total_budget_seconds - already_active, 0)

    def canceled():
        return manager.is_cancel_requested(task_id)

    def budget_exhausted():
        return budget_seconds <= 0 or time.monotonic() - started >= budget_seconds

    def remaining_budget():
        return max(budget_seconds - (time.monotonic() - started), 0)

    def should_abort():
        return canceled() or budget_exhausted() or manager.is_stopping()

    def interruption_requested():
        # Repair has its own remaining_budget callback so it can distinguish a
        # time limit from a user/service interruption in its result.
        return canceled() or manager.is_stopping()

    def heavy_operation(kind="interactive"):
        return manager.heavy_operation(
            kind, limit=int(policy.get("heavy_process_concurrency") or 1),
            cancel_check=should_abort
        )

    def progress(value):
        manager.update_progress(
            task_id,
            phase=value.get("phase") or "normalizing",
            completed=value.get("completed") or 0,
            total=value.get("total"),
            percent=None if not value.get("total") else round(
                (value.get("completed") or 0) * 100 / value.get("total"), 2
            ),
            current_item=value.get("current_item") or "",
            message=value.get("message") or "正在二次处理字幕"
        )

    manager.update_progress(
        task_id, phase="validating", completed=0, total=None, percent=None,
        current_item=os.path.basename(media_file), message="正在校验媒体与外挂字幕"
    )
    success, message, data = Subtitle().repair_external_subtitles(
        media_file,
        server_type,
        cancel_check=interruption_requested,
        progress_callback=progress,
        heavy_operation=heavy_operation,
        policy=policy,
        path_guard_check=path_guard,
        remaining_budget=remaining_budget,
        transaction_id=task_id
    )
    if manager.is_stopping() and not canceled() and not budget_exhausted():
        raise InterruptedError("manager stopping")
    processed = data.get("processed") or []
    warnings = data.setdefault("warnings", [])
    for item in processed:
        try:
            manager.invalidate_probe_cache([item.get("source"), item.get("target")])
        except Exception as warning:
            warnings.append(f"探测缓存失效失败：{str(warning)}")
    try:
        MediaLibrary.invalidate_subtitle_directory_cache(requested_media_file)
    except Exception as warning:
        warnings.append(f"目录缓存失效失败：{str(warning)}")
    if processed:
        try:
            manager.invalidate_audit_states([requested_media_file])
        except Exception as warning:
            warnings.append(f"检测状态失效失败：{str(warning)}")
    if canceled():
        status = "partial" if processed else "canceled"
        refresh = {"status": "skipped", "scope": "none", "message": "任务已取消"}
    elif budget_exhausted():
        status = "partial" if processed else "failed"
        message = f"修复任务达到 {int(policy.get('upload_budget_minutes') or 60)} 分钟累计处理预算"
        data["stop_reason"] = "time_limit"
        data["partial"] = bool(processed)
        refresh = {
            "status": "skipped", "scope": "none",
            "message": "修复任务预算已耗尽，已跳过局部刷新"
        }
    elif data.get("canceled"):
        # A policy item/byte limit is reported as partial, whereas a real
        # cooperative cancellation without a limit remains canceled.
        stop_reason = str(data.get("stop_reason") or "")
        if stop_reason in ["item_limit", "byte_limit", "time_limit"]:
            status = "partial" if processed else "failed"
            refresh = {"status": "skipped", "scope": "none", "message": message}
        else:
            status = "partial" if processed else "canceled"
            refresh = {"status": "skipped", "scope": "none", "message": "任务已取消"}
    elif success:
        status = "partial" if data.get("failures") or data.get("partial") else "succeeded"
        try:
            manager.update_progress(
                task_id, phase="refreshing", completed=len(processed), total=len(processed),
                percent=99, current_item="", message="正在局部刷新媒体项目"
            )
        except Exception as warning:
            warnings.append(f"刷新阶段进度保存失败：{str(warning)}")
        try:
            refresh = _localized_refresh(
                payload, requested_media_file, server_type,
                # Use the request path for transfer-history and media-server
                # mapping; destructive filesystem operations use real_path.
                remaining_budget=remaining_budget
            )
        except Exception as warning:
            refresh = {
                "status": "failed", "scope": "none", "server": server_type,
                "message": f"局部刷新异常：{str(warning)}"
            }
    elif data.get("partial"):
        status = "partial" if processed or data.get("skipped") else "failed"
        refresh = {
            "status": "skipped", "scope": "none",
            "message": data.get("limit_reason") or "字幕修复达到资源限制"
        }
    elif data.get("skipped") and not data.get("failures"):
        status = "succeeded"
        refresh = {"status": "skipped", "scope": "none", "message": "字幕无需修改"}
    else:
        status = "failed"
        refresh = {"status": "skipped", "scope": "none", "message": "没有成功修改字幕"}
    if manager.is_stopping() and not canceled():
        raise InterruptedError("manager stopping")
    if canceled():
        status = "partial" if processed else "canceled"
        if refresh.get("status") != "refreshed":
            refresh = {"status": "skipped", "scope": "none", "message": "任务已取消"}
    result = {"data": data, "refresh": refresh, "server": server_type}
    return _finish(
        manager, task_id, status, result=result,
        error=None if status in ["succeeded", "partial", "canceled"] else message,
        message=message
    )


def process_audit_task(manager, task_id):
    task = _task(manager, task_id)
    payload = _payload(task)
    policy = _policy(task)
    server_type = str(task.get("server") or payload.get("server") or "emby").lower()
    scope_key = task.get("scope_key") or json.dumps({
        "server": server_type,
        "category": payload.get("category"),
        "subcategory": payload.get("subcategory") or "",
        "mode": payload.get("mode") or "linked"
    }, ensure_ascii=False, sort_keys=True)
    cache_buffer = []
    cache_last_flush = [time.monotonic()]
    cache_warnings = []
    audit_started = time.monotonic()
    audit_max_seconds = max(float(policy.get("audit_max_minutes") or 60) * 60, 1)
    audit_timed_out = [False]

    def canceled():
        return manager.is_cancel_requested(task_id)

    def audit_abort():
        if canceled() or manager.is_stopping():
            return True
        if time.monotonic() - audit_started >= audit_max_seconds:
            audit_timed_out[0] = True
            return True
        return False

    def heavy_operation(kind="audit"):
        return manager.heavy_operation(
            kind, limit=int(policy.get("heavy_process_concurrency") or 1),
            cancel_check=audit_abort
        )

    def cache_get(fingerprint):
        for pending_fingerprint, pending_result in reversed(cache_buffer):
            if pending_fingerprint == fingerprint:
                return dict(pending_result)
        return manager.probe_cache_get(server_type, fingerprint)

    def cache_put(fingerprint, result):
        cache_buffer.append((dict(fingerprint), dict(result)))
        flush_cache()

    def flush_cache(force=False):
        if not cache_buffer:
            return
        if not force and len(cache_buffer) < 25 \
                and time.monotonic() - cache_last_flush[0] < 2:
            return
        pending = list(cache_buffer)
        del cache_buffer[:]
        try:
            manager.probe_cache_put_many(server_type, pending)
            cache_last_flush[0] = time.monotonic()
        except Exception as error:
            # Caches are an optimization; a write failure must not invalidate
            # already confirmed scan results or create a retry storm.
            cache_warnings.append(str(error))

    def progress(metrics):
        inspected = int(metrics.get("inspected") or 0)
        cache_hits = int(metrics.get("cache_hits") or 0)
        manager.update_progress(
            task_id,
            phase="probing" if inspected else "enumerating",
            completed=inspected + cache_hits,
            total=None,
            percent=None,
            current_item=metrics.get("current_item") or "",
            message="正在流式检测外挂字幕",
            metrics=metrics
        )

    limits = {
        "max_subtitles": policy.get("audit_max_changed") or 10000,
        "max_seconds": audit_max_seconds,
        "max_directories": policy.get("audit_max_directories") or 50000,
        "issue_limit": policy.get("audit_max_issues") or 200,
        "probe_timeout_seconds": policy.get("ffprobe_timeout_seconds") or 10
    }
    try:
        result = MediaLibrary().audit_external_subtitles(
            payload.get("category"),
            payload.get("subcategory"),
            server_type=server_type,
            mode=payload.get("mode") or "linked",
            deep_confirmed=bool(payload.get("deep_confirmed") or payload.get("confirmed")),
            cancel_check=audit_abort,
            limits=limits,
            progress_callback=progress,
            cache_get=cache_get,
            cache_put=cache_put,
            heavy_operation=heavy_operation,
            persist_history=False
        )
    finally:
        flush_cache(force=True)
    if manager.is_stopping() and not canceled():
        # Leave the active task for startup recovery to mark interrupted.  In
        # particular, do not commit partial visible state/history on shutdown.
        raise InterruptedError("manager stopping")
    if audit_timed_out[0]:
        # The gate reports cooperative interruption so its waiter exits
        # promptly; translate that internal signal back to the audit contract
        # (partial/time_limit, never user-canceled/failed) while retaining all
        # results accumulated before the deadline.
        result["canceled"] = False
        result["partial"] = True
        result["coverage_complete"] = False
        result["stop_reason"] = "time_limit"
    if cache_warnings:
        result["cache_warning"] = cache_warnings[-1]
    if result.get("code") != 0:
        return _finish(
            manager, task_id, "failed", result=result,
            error=result.get("msg") or "字幕检测失败",
            message=result.get("msg") or "字幕检测失败"
        )
    if canceled() or result.get("canceled"):
        # Probe cache is intentionally retained, but canceled scans must not
        # update the visible latest state or audit history.
        result.pop("media_statuses", None)
        return _finish(manager, task_id, "canceled", result=result, message="字幕检测已取消")

    media_statuses = result.pop("media_statuses", {})
    result["checked_at"] = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    result["scope_key"] = scope_key
    status = "partial" if result.get("partial") or not result.get("coverage_complete") else "succeeded"
    message = "外挂字幕检测部分完成" if status == "partial" else "外挂字幕检测完成"
    return manager.commit_audit_result(
        task_id, scope_key, server_type, media_statuses,
        result=result, status=status, message=message,
        replace=bool(result.get("coverage_complete"))
    )


def register_subtitle_task_processors(manager=None):
    if manager is None:
        from app.helper.subtitle_tasks import get_subtitle_task_manager
        manager = get_subtitle_task_manager()
    if manager in _REGISTERED_MANAGERS:
        return manager
    register = getattr(manager, "register_processor", None) or getattr(manager, "register_handler")
    register("upload", process_upload_task)
    register("repair", process_repair_task)
    register("audit", process_audit_task)
    _REGISTERED_MANAGERS.add(manager)
    return manager
