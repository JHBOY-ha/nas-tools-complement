import io
import datetime
import hashlib
import json
import os
import tempfile
import threading
import time
from unittest import TestCase
from unittest.mock import patch

import tests.test_subtitle_upload  # optional dependency stubs
import tests.test_media_library  # media/server dependency stubs
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, SUBTITLEAUDITSTATE, SUBTITLETASK
from app.subtitle import Subtitle
from app.helper.subtitle_task_processors import register_subtitle_task_processors
from app.helper.subtitle_tasks import (
    SubtitleTaskError,
    SubtitleTaskManager,
    TaskBusy,
    TaskQueueFull,
    TaskStorageInsufficient,
    TaskUploadTooLarge,
)


class _MemoryDb:
    def __init__(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool
        )
        self._session = scoped_session(sessionmaker(bind=self.engine, expire_on_commit=False))

    @property
    def session(self):
        return self._session()

    def init_db(self):
        Base.metadata.create_all(self.engine)

    def query(self, *objects):
        return self.session.query(*objects)

    def insert(self, value):
        self.session.add(value)

    def flush(self):
        self.session.flush()

    def commit(self):
        self.session.commit()

    def rollback(self):
        self.session.rollback()


class _File:
    def __init__(self, name, content):
        self.filename = name
        self.stream = io.BytesIO(content)


SRT = b"1\n00:00:01,000 --> 00:00:02,000\nThis is an English subtitle line.\n"


class SubtitleTaskManagerTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.media = os.path.join(self.temp.name, "Movie.mkv")
        open(self.media, "wb").close()
        self.manager = SubtitleTaskManager(
            db=_MemoryDb(),
            staging_root=os.path.join(self.temp.name, "staging")
        )

    def tearDown(self):
        self.manager.shutdown(wait=True)
        self.temp.cleanup()

    def payload(self):
        link_stat = os.lstat(self.media)
        referent_stat = os.stat(self.media)

        def identity(stat_result):
            return {
                "device": int(getattr(stat_result, "st_dev", 0) or 0),
                "inode": int(getattr(stat_result, "st_ino", 0) or 0),
                "size": int(getattr(stat_result, "st_size", 0) or 0),
                "mtime_ns": int(getattr(stat_result, "st_mtime_ns", 0) or 0),
            }

        parent_real = os.path.normcase(os.path.realpath(os.path.dirname(self.media)))
        referent_real = os.path.normcase(os.path.realpath(self.media))
        snapshot = {
            "path": os.path.abspath(self.media),
            "real_path": referent_real,
            "directory": parent_real,
            "parent_real": parent_real,
            "referent_real": referent_real,
            "is_link": os.path.islink(self.media),
            "trusted_roots": [parent_real],
            "identity": identity(referent_stat),
            "link_identity": identity(link_stat),
            "referent_identity": identity(referent_stat),
        }
        return {
            "media_file": self.media,
            "canonical_media_file": self.media,
            "align_mode": "none",
            "server": "emby",
            "path_authorization": {
                "source": dict(snapshot),
                "target": dict(snapshot),
            },
        }

    @staticmethod
    def wait_terminal(manager, task_id, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = manager.get_task(task_id, owner=None, admin=True)
            if task and task["status"] in {"succeeded", "partial", "failed", "canceled", "interrupted"}:
                return task
            time.sleep(0.02)
        raise AssertionError("task did not become terminal")

    def test_request_id_is_permanently_idempotent(self):
        self.manager.register_processor(
            "upload",
            lambda manager, task_id: manager.finish_task(task_id, "succeeded", {"ok": True})
        )
        self.manager.start()
        task, reused = self.manager.submit_upload(
            "user", [_File("Movie.eng.srt", SRT)], self.payload(), request_id="same"
        )
        self.assertFalse(reused)
        self.wait_terminal(self.manager, task["task_id"])
        duplicate, reused = self.manager.submit_upload(
            "user", [_File("bad.idx", b"invalid")], self.payload(), request_id="same"
        )
        self.assertTrue(reused)
        self.assertEqual(duplicate["task_id"], task["task_id"])

    def test_queue_is_bounded_and_queued_cancel_cleans_staging(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(3)
            manager.finish_task(task_id, "succeeded", {})

        self.manager.register_processor("upload", blocked)
        self.manager.start()
        self.manager.update_settings({"max_upload_queue": 1})
        first, _ = self.manager.submit_upload("user", [_File("a.srt", SRT)], self.payload())
        self.assertTrue(running.wait(2))
        second, _ = self.manager.submit_upload("user", [_File("b.srt", SRT)], self.payload())
        second_stage = second["payload"]["staging_dir"]
        with self.assertRaises(TaskQueueFull):
            self.manager.submit_upload("user", [_File("c.srt", SRT)], self.payload())
        canceled = self.manager.cancel_task(second["task_id"], owner="user")
        self.assertEqual(canceled["status"], "canceled")
        self.assertFalse(os.path.exists(second_stage))
        release.set()
        self.wait_terminal(self.manager, first["task_id"])

    def test_upload_limits_map_to_413_and_507_errors(self):
        self.manager.start()
        self.manager.update_settings({"text_file_limit_mb": 1})
        with self.assertRaises(TaskUploadTooLarge) as too_large:
            self.manager.submit_upload(
                "user", [_File("large.srt", b"x" * (1024 * 1024 + 1))], self.payload()
            )
        self.assertEqual(too_large.exception.status_code, 413)

        usage = type("Usage", (), {"total": 10 ** 9, "used": 10 ** 9, "free": 0})()
        with patch("app.helper.subtitle_tasks.shutil.disk_usage", return_value=usage):
            with self.assertRaises(TaskStorageInsufficient) as insufficient:
                self.manager.submit_upload(
                    "user", [_File("small.srt", SRT)], self.payload()
                )
        self.assertEqual(insufficient.exception.status_code, 507)

    def test_target_volume_reservations_accumulate_across_queued_uploads(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(2)
            manager.finish_task(task_id, "succeeded", {})

        reserve = 1024 * 1024 * 1024
        usage = type("Usage", (), {
            "total": reserve * 4, "used": 0, "free": reserve + len(SRT) * 3
        })()
        self.manager.register_processor("upload", blocked)
        self.manager.start()
        with patch("app.helper.subtitle_tasks.shutil.disk_usage", return_value=usage):
            first, _ = self.manager.submit_upload(
                "user", [_File("first.srt", SRT)], self.payload()
            )
            self.assertTrue(running.wait(1))
            with self.assertRaises(TaskStorageInsufficient):
                self.manager.submit_upload(
                    "user", [_File("second.srt", SRT)], self.payload()
                )
        release.set()
        self.wait_terminal(self.manager, first["task_id"])

    def test_request_spooled_file_is_atomically_adopted_without_second_copy(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(2)
            manager.finish_task(task_id, "succeeded", {})

        os.makedirs(self.manager._incoming_root, exist_ok=True)
        incoming = os.path.join(self.manager._incoming_root, "request.upload")
        with open(incoming, "wb") as file_obj:
            file_obj.write(SRT)
        upload = _File("subtitle.srt", b"")
        upload.stream = open(incoming, "r+b")
        self.manager.register_processor("upload", blocked)
        self.manager.start()

        task, _ = self.manager.submit_upload("user", [upload], self.payload())

        self.assertTrue(running.wait(1))
        item = self.manager.list_items(task["task_id"])[0]
        self.assertFalse(os.path.exists(incoming))
        self.assertTrue(os.path.isfile(item["staged_path"]))
        with open(item["staged_path"], "rb") as file_obj:
            self.assertEqual(file_obj.read(), SRT)
        release.set()
        self.wait_terminal(self.manager, task["task_id"])

    def test_cold_start_removes_flat_incoming_crash_leftovers(self):
        os.makedirs(self.manager._incoming_root, exist_ok=True)
        orphan = os.path.join(self.manager._incoming_root, "crashed.upload")
        with open(orphan, "wb") as file_obj:
            file_obj.write(b"leftover")

        self.manager.start()

        self.assertFalse(os.path.exists(orphan))

    def test_incoming_crash_leftovers_count_against_admission_quota(self):
        self.manager.start()
        self.manager.update_settings({
            "staging_quota_mb": 512,
            "batch_limit_mb": 80,
            "vobsub_limit_mb": 80,
        })
        orphan = os.path.join(self.manager._incoming_root, "crashed.upload")
        with open(orphan, "wb") as file_obj:
            file_obj.truncate(40 * 1024 * 1024)
        usage = type("Usage", (), {
            "total": 10 * 1024 * 1024 * 1024,
            "used": 0,
            "free": 10 * 1024 * 1024 * 1024
        })()

        with patch("app.helper.subtitle_tasks.shutil.disk_usage", return_value=usage):
            with self.assertRaises(TaskStorageInsufficient):
                self.manager.ensure_upload_admission(80 * 1024 * 1024)

    def test_heavy_gate_wait_observes_task_cancel_without_waiting_for_holder(self):
        holder_acquired = threading.Event()
        release_holder = threading.Event()
        waiter_interrupted = threading.Event()
        cancel_waiter = threading.Event()

        def holder():
            with self.manager.heavy_operation("interactive", limit=1):
                holder_acquired.set()
                release_holder.wait(2)

        def waiter():
            try:
                with self.manager.heavy_operation(
                        "interactive", limit=1, cancel_check=cancel_waiter.is_set):
                    raise AssertionError("canceled waiter must not acquire the heavy gate")
            except InterruptedError:
                waiter_interrupted.set()

        holder_thread = threading.Thread(target=holder)
        waiter_thread = threading.Thread(target=waiter)
        holder_thread.start()
        self.assertTrue(holder_acquired.wait(1))
        waiter_thread.start()
        time.sleep(0.05)
        cancel_waiter.set()

        self.assertTrue(waiter_interrupted.wait(0.75))
        release_holder.set()
        holder_thread.join(1)
        waiter_thread.join(1)

    def test_start_refuses_while_previous_worker_is_still_stopping(self):
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocked(manager, task_id):
            calls.append((task_id, threading.get_ident()))
            entered.set()
            release.wait(2)
            manager.finish_task(task_id, "succeeded", {})

        self.manager.register_processor("upload", blocked)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], self.payload()
        )
        self.assertTrue(entered.wait(1))

        self.manager.shutdown(wait=False)
        with self.assertRaises(SubtitleTaskError):
            self.manager.start()
        time.sleep(0.1)
        self.assertEqual(len(calls), 1)

        release.set()
        terminal = self.wait_terminal(self.manager, task["task_id"])
        self.assertEqual(terminal["status"], "succeeded")

    def test_transient_first_task_read_requeues_committed_claim(self):
        processor_calls = []

        def processor(manager, task_id):
            processor_calls.append(task_id)
            manager.finish_task(task_id, "succeeded", {"ok": True})

        self.manager.register_processor("upload", processor)
        original_get_task = self.manager.get_task
        faulted = threading.Event()

        def flaky_get_task(*args, **kwargs):
            if threading.current_thread().name == "subtitle-task-worker" \
                    and not faulted.is_set():
                faulted.set()
                raise RuntimeError("transient task read failure")
            return original_get_task(*args, **kwargs)

        with patch.object(self.manager, "get_task", side_effect=flaky_get_task):
            task, _ = self.manager.submit_upload(
                "user", [_File("subtitle.srt", SRT)], self.payload()
            )
            terminal = self.wait_terminal(self.manager, task["task_id"])

        self.assertTrue(faulted.is_set())
        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(processor_calls, [task["task_id"]])

    def test_worker_recovers_when_exception_state_read_also_fails_once(self):
        processor_calls = []

        def processor(manager, task_id):
            processor_calls.append(task_id)
            if len(processor_calls) == 1:
                raise RuntimeError("processor failed before checkpoint")
            manager.finish_task(task_id, "succeeded", {"ok": True})

        self.manager.register_processor("upload", processor)
        original_get_task = self.manager.get_task
        worker_reads = [0]

        def flaky_second_worker_read(*args, **kwargs):
            if threading.current_thread().name == "subtitle-task-worker":
                worker_reads[0] += 1
                if worker_reads[0] == 2:
                    raise RuntimeError("transient exception-state read failure")
            return original_get_task(*args, **kwargs)

        with patch.object(self.manager, "get_task", side_effect=flaky_second_worker_read):
            task, _ = self.manager.submit_upload(
                "user", [_File("subtitle.srt", SRT)], self.payload()
            )
            terminal = self.wait_terminal(self.manager, task["task_id"], timeout=8)

        self.assertEqual(terminal["status"], "succeeded", terminal)
        self.assertEqual(processor_calls, [task["task_id"], task["task_id"]])

    def test_audit_claim_restarts_after_transient_first_task_read(self):
        processor_calls = []

        def processor(manager, task_id):
            processor_calls.append(task_id)
            manager.finish_task(task_id, "succeeded", {"ok": True})

        self.manager.register_processor("audit", processor)
        self.manager.start()
        original_get_task = self.manager.get_task
        faulted = threading.Event()

        def flaky_audit_read(*args, **kwargs):
            if threading.current_thread().name.startswith("subtitle-audit-") \
                    and not faulted.is_set():
                faulted.set()
                raise RuntimeError("transient audit task read failure")
            return original_get_task(*args, **kwargs)

        with patch.object(self.manager, "get_task", side_effect=flaky_audit_read):
            task, _ = self.manager.submit_task(
                "audit", "user",
                payload={"server": "emby", "category": "movie", "mode": "linked"},
                server="emby", scope_key="emby:movie:linked"
            )
            terminal = self.wait_terminal(self.manager, task["task_id"], timeout=5)

        self.assertTrue(faulted.is_set())
        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(processor_calls, [task["task_id"]])

    def test_task_detail_is_owner_isolated(self):
        self.manager.register_processor(
            "upload", lambda manager, task_id: manager.finish_task(task_id, "succeeded", {})
        )
        self.manager.start()
        task, _ = self.manager.submit_upload("owner-a", [_File("a.srt", SRT)], self.payload())
        self.assertIsNone(self.manager.get_task(task["task_id"], owner="owner-b"))
        self.assertIsNotNone(self.manager.get_task(task["task_id"], owner="owner-a"))

    def test_audit_same_scope_reuses_and_other_scope_is_busy(self):
        running = threading.Event()
        release = threading.Event()

        def audit(manager, task_id):
            running.set()
            release.wait(3)
            manager.finish_task(task_id, "succeeded", {"summary": {}})

        self.manager.register_processor("audit", audit)
        self.manager.start()
        first, _ = self.manager.submit_task(
            "audit", "user", {"category": "movie"}, scope_key="movie", dedupe_key="movie"
        )
        self.assertTrue(running.wait(2))
        same, reused = self.manager.submit_task(
            "audit", "user", {"category": "movie"}, scope_key="movie", dedupe_key="movie"
        )
        self.assertTrue(reused)
        self.assertEqual(same["task_id"], first["task_id"])
        with self.assertRaises(TaskBusy):
            self.manager.submit_task(
                "audit", "user", {"category": "tv"}, scope_key="tv", dedupe_key="tv"
            )
        release.set()
        self.wait_terminal(self.manager, first["task_id"])

    def test_vobsub_pairing_and_orphan_rejection(self):
        self.manager.register_processor(
            "upload",
            lambda manager, task_id: manager.finish_task(task_id, "succeeded", {})
        )
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user",
            [_File("Movie.sub", b"\x00binary"), _File("Movie.idx", b"VobSub index file, v7")],
            self.payload()
        )
        self.assertEqual(task["items"][0]["kind"], "vobsub")
        with self.assertRaises(SubtitleTaskError):
            self.manager.submit_upload("other", [_File("Movie.idx", b"orphan")], self.payload())
        with self.assertRaises(SubtitleTaskError):
            self.manager.submit_upload("third", [_File("Movie.sub", b"\x00binary")], self.payload())

    def test_probe_cache_uses_full_fingerprint(self):
        self.manager.start()
        fingerprint = {
            "path": os.path.join(self.temp.name, "Movie.eng.srt"),
            "size": 10,
            "mtime_ns": 1,
            "validator_version": "v1",
            "ffprobe_version": "x"
        }
        self.manager.probe_cache_put("emby", fingerprint, {"status": "ok"})
        self.assertEqual(self.manager.probe_cache_get("emby", fingerprint)["status"], "ok")
        changed = dict(fingerprint, mtime_ns=2)
        self.assertIsNone(self.manager.probe_cache_get("emby", changed))

    def test_upload_admission_does_not_walk_staging_tree(self):
        self.manager.register_processor(
            "upload", lambda manager, task_id: manager.finish_task(task_id, "succeeded", {})
        )
        self.manager.start()
        with patch("app.helper.subtitle_tasks.os.walk", side_effect=AssertionError("unexpected walk")):
            task, _ = self.manager.submit_upload(
                "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
            )
        self.assertTrue(task["payload"]["staging_reserved_bytes"] >= len(SRT) * 2)

    def test_alignment_reserves_reference_extraction_space(self):
        self.manager.register_processor(
            "upload", lambda manager, task_id: manager.finish_task(task_id, "succeeded", {})
        )
        self.manager.start()
        payload = self.payload()
        payload["align_mode"] = "auto"
        task, _ = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], payload, server="emby"
        )
        self.assertEqual(
            task["payload"]["staging_reserved_bytes"],
            task["payload"]["total_bytes"] * 6
        )

    def test_upload_dedupe_uses_lexical_target_not_shared_referent(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(3)
            manager.finish_task(task_id, "succeeded", {})

        alias_dir = os.path.join(self.temp.name, "second-library")
        os.makedirs(alias_dir)
        alias_media = os.path.join(alias_dir, "Movie.mkv")
        open(alias_media, "wb").close()
        first_payload = self.payload()
        second_payload = self.payload()
        second_payload["media_file"] = alias_media
        second_payload["canonical_media_file"] = alias_media
        real_realpath = os.path.realpath

        def shared_referent(path):
            absolute = os.path.normcase(os.path.abspath(os.path.normpath(str(path))))
            if absolute in {
                    os.path.normcase(os.path.abspath(self.media)),
                    os.path.normcase(os.path.abspath(alias_media))}:
                return os.path.join(self.temp.name, "shared-download", "Movie.mkv")
            return real_realpath(path)

        self.manager.register_processor("upload", blocked)
        self.manager.start()
        try:
            with patch(
                    "app.helper.subtitle_tasks.os.path.realpath",
                    side_effect=shared_referent):
                first, _ = self.manager.submit_upload(
                    "user", [_File("subtitle.srt", SRT)], first_payload
                )
                self.assertTrue(running.wait(2))
                second, reused = self.manager.submit_upload(
                    "user", [_File("subtitle.srt", SRT)], second_payload
                )
            self.assertFalse(reused)
            self.assertNotEqual(first["task_id"], second["task_id"])
        finally:
            release.set()
        self.wait_terminal(self.manager, first["task_id"])
        self.wait_terminal(self.manager, second["task_id"])

    def test_interactive_worker_survives_transient_claim_failure(self):
        self.manager.register_processor(
            "upload", lambda manager, task_id: manager.finish_task(task_id, "succeeded", {})
        )
        self.manager.start()
        original = self.manager._claim_next_interactive
        failed = {"value": False}

        def flaky_claim():
            queued = self.manager._db.query(SUBTITLETASK).filter(
                SUBTITLETASK.TYPE == "upload", SUBTITLETASK.STATUS == "queued"
            ).count()
            if queued and not failed["value"]:
                failed["value"] = True
                raise RuntimeError("simulated sqlite lock")
            return original()

        with patch.object(self.manager, "_claim_next_interactive", side_effect=flaky_claim):
            task, _ = self.manager.submit_upload(
                "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
            )
            detail = self.wait_terminal(self.manager, task["task_id"], timeout=10)
        self.assertTrue(failed["value"])
        self.assertTrue(self.manager._worker.is_alive())
        self.assertEqual(detail["status"], "succeeded")

    def test_cancel_wins_atomic_audit_commit_without_visible_state(self):
        running = threading.Event()
        release = threading.Event()

        def audit(_manager, _task_id):
            running.set()
            release.wait(3)

        self.manager.register_processor("audit", audit)
        self.manager.start()
        task, _ = self.manager.submit_task(
            "audit", "user", {"category": "movie"}, server="emby",
            scope_key='{"category":"movie"}', dedupe_key="movie"
        )
        self.assertTrue(running.wait(2))
        self.manager.cancel_task(task["task_id"], owner="user")
        detail = self.manager.commit_audit_result(
            task["task_id"], '{"category":"movie"}', "emby",
            {"movie": {"media_path": "movie", "status": "ok"}},
            result={"summary": {"total": 1}}, status="succeeded", message="done",
            replace=True
        )
        release.set()
        self.assertEqual(detail["status"], "canceled")
        self.assertEqual(self.manager._db.query(SUBTITLEAUDITSTATE).count(), 0)

    def test_real_upload_processor_publishes_canonical_copy(self):
        register_subtitle_task_processors(self.manager)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
        )
        detail = self.wait_terminal(self.manager, task["task_id"], timeout=10)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertTrue(os.path.isfile(os.path.join(self.temp.name, "Movie.eng.srt")))
        self.assertEqual(detail["result"]["refresh"]["status"], "skipped")

    def test_settings_cross_field_validation(self):
        self.manager.start()
        with self.assertRaises(SubtitleTaskError):
            self.manager.update_settings({"batch_limit_mb": 50, "vobsub_limit_mb": 200})
        with self.assertRaises(SubtitleTaskError):
            self.manager.update_settings({"max_batch_items": 3, "llm_max_batch_items": 5})
        with self.assertRaises(SubtitleTaskError):
            self.manager.update_settings({"staging_quota_mb": 1499})

    def test_request_id_does_not_cross_task_types(self):
        self.manager.register_processor(
            "audit", lambda manager, task_id: manager.finish_task(task_id, "succeeded", {})
        )
        self.manager.register_processor(
            "upload", lambda manager, task_id: manager.finish_task(task_id, "succeeded", {})
        )
        self.manager.start()
        audit, _ = self.manager.submit_task(
            "audit", "user", {"category": "movie"}, request_id="shared-request",
            scope_key="movie", dedupe_key="movie"
        )
        upload, reused = self.manager.submit_upload(
            "user", [_File("Movie.eng.srt", SRT)], self.payload(),
            request_id="shared-request"
        )
        self.assertFalse(reused)
        self.assertEqual(audit["type"], "audit")
        self.assertEqual(upload["type"], "upload")

    def test_post_publish_cache_failure_never_creates_numbered_retry(self):
        register_subtitle_task_processors(self.manager)
        self.manager.start()
        with patch.object(
                self.manager, "invalidate_probe_cache",
                side_effect=RuntimeError("sqlite locked")):
            task, _ = self.manager.submit_upload(
                "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
            )
            detail = self.wait_terminal(self.manager, task["task_id"], timeout=10)
        self.assertEqual(detail["status"], "succeeded", detail)
        duplicate, reused = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
        )
        self.assertTrue(reused)
        self.assertEqual(duplicate["task_id"], task["task_id"])
        subtitles = [name for name in os.listdir(self.temp.name) if name.endswith(".srt")]
        self.assertEqual(subtitles, ["Movie.eng.srt"])

    def test_post_publish_checkpoint_failure_recovers_without_numbered_copy(self):
        register_subtitle_task_processors(self.manager)
        self.manager.start()
        original_update = self.manager.update_item
        failed = {"value": False}

        def flaky_update(task_id, item_id, **values):
            if values.get("status") == "succeeded" and values.get("stage") == "published" \
                    and not failed["value"]:
                failed["value"] = True
                raise RuntimeError("simulated item checkpoint failure")
            return original_update(task_id, item_id, **values)

        with patch.object(self.manager, "update_item", side_effect=flaky_update):
            task, _ = self.manager.submit_upload(
                "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
            )
            detail = self.wait_terminal(self.manager, task["task_id"], timeout=10)
        self.assertTrue(failed["value"])
        self.assertTrue(self.manager._worker.is_alive())
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertTrue(os.path.isfile(os.path.join(self.temp.name, "Movie.eng.srt")))
        self.assertFalse(os.path.exists(os.path.join(self.temp.name, "Movie.eng(1).srt")))

    def test_cancel_during_refresh_finishes_partial(self):
        register_subtitle_task_processors(self.manager)
        self.manager.start()

        def cancel_while_refreshing(*_args, **_kwargs):
            running = self.manager.list_tasks(owner="user", task_types=["upload"])["items"]
            self.manager.cancel_task(running[0]["task_id"], owner="user")
            return {"status": "skipped", "scope": "none", "message": "test cancellation"}

        with patch(
                "app.helper.subtitle_task_processors._localized_refresh",
                side_effect=cancel_while_refreshing):
            task, _ = self.manager.submit_upload(
                "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
            )
            detail = self.wait_terminal(self.manager, task["task_id"], timeout=10)
        self.assertEqual(detail["status"], "partial", detail)
        self.assertEqual(detail["result"]["success_count"], 1)

    def test_refresh_exception_after_publish_is_terminal_without_retry_loop(self):
        register_subtitle_task_processors(self.manager)
        self.manager.start()
        with patch(
                "app.helper.subtitle_task_processors._localized_refresh",
                side_effect=RuntimeError("refresh unavailable")) as refresh:
            task, _ = self.manager.submit_upload(
                "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
            )
            detail = self.wait_terminal(self.manager, task["task_id"], timeout=10)
        self.assertEqual(detail["status"], "succeeded", detail)
        self.assertEqual(detail["result"]["refresh"]["status"], "failed")
        self.assertEqual(refresh.call_count, 1)

    def test_non_user_interruption_is_not_reported_as_success(self):
        register_subtitle_task_processors(self.manager)
        self.manager.start()
        with patch.object(
                Subtitle(), "process_staged_upload",
                side_effect=InterruptedError("manager stopping")):
            task, _ = self.manager.submit_upload(
                "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
            )
            detail = self.wait_terminal(self.manager, task["task_id"], timeout=10)
        self.assertEqual(detail["status"], "failed", detail)
        self.assertNotEqual(detail["status"], "succeeded")
        self.assertEqual(detail["items"][0]["status"], "failed")

    def test_publish_fallback_never_deletes_exclusive_create_race_winner(self):
        source = os.path.join(self.temp.name, "source.srt")
        target = os.path.join(self.temp.name, "target.srt")
        with open(source, "wb") as file_obj:
            file_obj.write(SRT)
        def racing_rename(_source, _target):
            with open(target, "wb") as file_obj:
                file_obj.write(b"foreign process")
            raise FileExistsError(target)

        with patch("app.subtitle.os.link", side_effect=OSError("hardlink unavailable")), \
                patch.object(Subtitle().__class__, "_Subtitle__rename_no_replace",
                             side_effect=racing_rename):
            with self.assertRaises(FileExistsError):
                Subtitle()._Subtitle__publish_file_no_replace(source, target)
        self.assertTrue(os.path.isfile(target))
        with open(target, "rb") as file_obj:
            self.assertEqual(file_obj.read(), b"foreign process")

    def test_legacy_audit_migration_keeps_latest_state_without_unique_collision(self):
        history_file = os.path.join(self.temp.name, "subtitle-audit-history.json")
        media_key = os.path.normcase(self.media)
        history = {
            "history": [
                {
                    "checked_at": datetime.datetime.now().astimezone().isoformat(), "server": "emby",
                    "category": "movie", "mode": "linked",
                    "summary": {"total": 1},
                    "media_statuses": {media_key: {"media_path": self.media, "status": "ok"}}
                },
                {
                    "checked_at": datetime.datetime.now().astimezone().isoformat(), "server": "emby",
                    "category": "movie", "mode": "linked",
                    "summary": {"total": 1},
                    "media_statuses": {media_key: {"media_path": self.media, "status": "warning"}}
                }
            ],
            "latest": {}
        }
        import json
        with open(history_file, "w", encoding="utf-8") as file_obj:
            json.dump(history, file_obj)
        from config import Config
        with patch.object(Config(), "get_config_path", return_value=self.temp.name):
            self.manager.start()
        self.assertEqual(len(self.manager.recent_audit_results("emby", 3)), 2)
        states = self.manager._db.query(SUBTITLEAUDITSTATE).all()
        self.assertEqual(len(states), 1)
        self.assertEqual(states[0].STATUS, "ok")
        self.assertTrue(os.path.isfile(history_file + ".migrated.bak"))

    def test_settings_reject_impossible_staging_volume(self):
        self.manager.start()
        usage = type("Usage", (), {
            "total": 2 * 1024 * 1024 * 1024,
            "used": 0,
            "free": 2 * 1024 * 1024 * 1024
        })()
        with patch("app.helper.subtitle_tasks.shutil.disk_usage", return_value=usage):
            with self.assertRaises(SubtitleTaskError):
                self.manager.update_settings({"staging_quota_mb": 2048, "reserve_free_mb": 1024})

    def test_publish_before_checkpoint_is_reconciled_without_renumbering(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(3)
            manager.finish_task(task_id, "succeeded", {})

        self.manager.register_processor("upload", blocked)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
        )
        self.assertTrue(running.wait(2))
        item = self.manager.list_items(task["task_id"])[0]
        output = os.path.join(self.temp.name, "Movie.eng.srt")
        with open(output, "wb") as file_obj:
            file_obj.write(SRT)
        output_hash = hashlib.sha256(SRT).hexdigest()
        self.manager.update_item(
            task["task_id"], item["item_id"], stage="planned",
            output_path=output, output_hash=output_hash,
            result={"planned_output_hash": output_hash, "planned_companion_hash": ""}
        )
        reconciled = self.manager._reconcile_upload_outputs(task["task_id"])
        self.assertEqual(reconciled["recovered"], 1)
        self.assertEqual(self.manager.list_items(task["task_id"])[0]["status"], "succeeded")
        release.set()
        self.wait_terminal(self.manager, task["task_id"])

    def test_incomplete_vobsub_publish_rolls_back_matching_component(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(3)
            manager.finish_task(task_id, "succeeded", {})

        primary = b"\x00binary-vobsub"
        companion = b"VobSub index file, v7"
        self.manager.register_processor("upload", blocked)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("Movie.sub", primary), _File("Movie.idx", companion)],
            self.payload(), server="emby"
        )
        self.assertTrue(running.wait(2))
        item = self.manager.list_items(task["task_id"])[0]
        output_sub = os.path.join(self.temp.name, "Movie.zh-CN.sub")
        output_idx = os.path.join(self.temp.name, "Movie.zh-CN.idx")
        with open(output_sub, "wb") as file_obj:
            file_obj.write(primary)
        primary_hash = hashlib.sha256(primary).hexdigest()
        companion_hash = hashlib.sha256(companion).hexdigest()
        self.manager.update_item(
            task["task_id"], item["item_id"], stage="planned",
            output_path=output_sub, output_companion_path=output_idx,
            output_hash=primary_hash,
            result={
                "planned_output_hash": primary_hash,
                "planned_companion_hash": companion_hash,
                "ownership_marker": os.path.join(task["payload"]["staging_dir"], "owned.json")
            }
        )
        with open(os.path.join(task["payload"]["staging_dir"], "owned.json"), "w", encoding="utf-8") as file_obj:
            json.dump({"owned": {
                os.path.normcase(os.path.abspath(output_sub)): {
                    "path": output_sub, "hash": primary_hash
                }
            }}, file_obj)
        reconciled = self.manager._reconcile_upload_outputs(
            task["task_id"], rollback_incomplete=True
        )
        self.assertEqual(reconciled["rolled_back"], 1)
        self.assertFalse(os.path.exists(output_sub))
        release.set()
        self.wait_terminal(self.manager, task["task_id"])

    def test_vobsub_pre_publish_identity_intent_recovers_orphan_idx(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(3)
            manager.finish_task(task_id, "succeeded", {})

        primary = b"\x00binary-vobsub"
        companion = b"VobSub index file, v7"
        self.manager.register_processor("upload", blocked)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("Movie.sub", primary), _File("Movie.idx", companion)],
            self.payload(), server="emby"
        )
        self.assertTrue(running.wait(2))
        item = self.manager.list_items(task["task_id"])[0]
        output_sub = os.path.join(self.temp.name, "Movie.zh-CN.sub")
        orphan_idx = os.path.join(self.temp.name, "Movie.zh-CN.idx")
        with open(orphan_idx, "wb") as file_obj:
            file_obj.write(companion)
        identity = os.stat(orphan_idx, follow_symlinks=False)
        marker = os.path.join(task["payload"]["staging_dir"], "publish-owned.json")
        companion_hash = hashlib.sha256(companion).hexdigest()
        with open(marker, "w", encoding="utf-8") as file_obj:
            json.dump({"intents": {
                os.path.normcase(os.path.abspath(orphan_idx)): {
                    "path": orphan_idx,
                    "hash": companion_hash,
                    "identity": {"device": identity.st_dev, "inode": identity.st_ino}
                }
            }}, file_obj)
        self.manager.update_item(
            task["task_id"], item["item_id"], stage="planned",
            output_path=output_sub, output_companion_path=orphan_idx,
            output_hash=hashlib.sha256(primary).hexdigest(),
            result={
                "planned_output_hash": hashlib.sha256(primary).hexdigest(),
                "planned_companion_hash": companion_hash,
                "ownership_marker": marker
            }
        )

        reconciled = self.manager._reconcile_upload_outputs(
            task["task_id"], rollback_incomplete=True
        )

        self.assertEqual(reconciled["rolled_back"], 1)
        self.assertFalse(os.path.exists(orphan_idx))
        release.set()
        self.wait_terminal(self.manager, task["task_id"])

    def test_cancel_recovering_vobsub_rolls_back_orphan_before_staging_cleanup(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(3)
            manager.finish_task(task_id, "succeeded", {})

        primary = b"\x00binary-vobsub"
        companion = b"VobSub index file, v7"
        self.manager.register_processor("upload", blocked)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("Movie.sub", primary), _File("Movie.idx", companion)],
            self.payload(), server="emby"
        )
        self.assertTrue(running.wait(2))
        item = self.manager.list_items(task["task_id"])[0]
        output_sub = os.path.join(self.temp.name, "Movie.zh-CN.sub")
        output_idx = os.path.join(self.temp.name, "Movie.zh-CN.idx")
        with open(output_sub, "wb") as file_obj:
            file_obj.write(primary)
        primary_hash = hashlib.sha256(primary).hexdigest()
        companion_hash = hashlib.sha256(companion).hexdigest()
        self.manager.update_item(
            task["task_id"], item["item_id"], stage="planned",
            output_path=output_sub, output_companion_path=output_idx,
            output_hash=primary_hash,
            result={
                "planned_companion_hash": companion_hash,
                "ownership_marker": os.path.join(task["payload"]["staging_dir"], "owned.json")
            }
        )
        with open(os.path.join(task["payload"]["staging_dir"], "owned.json"), "w", encoding="utf-8") as file_obj:
            json.dump({"owned": {
                os.path.normcase(os.path.abspath(output_sub)): {
                    "path": output_sub, "hash": primary_hash
                }
            }}, file_obj)
        row = self.manager._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.ID == task["task_id"]
        ).first()
        row.STATUS = "recovering"
        self.manager._db.commit()
        canceled = self.manager.cancel_task(task["task_id"], owner="user")
        self.assertEqual(canceled["status"], "canceled")
        self.assertFalse(os.path.exists(output_sub))
        self.assertFalse(os.path.exists(task["payload"]["staging_dir"]))
        release.set()

    def test_recovery_does_not_charge_long_service_downtime_as_active(self):
        now = time.time()
        row = type("TaskRow", (), {
            "RUN_STARTED_AT": now - 7200,
            "UPDATED_AT": now - 7200,
            "ACTIVE_SECONDS": 10.0
        })()
        self.manager._checkpoint_recovered_active(row, now)
        self.assertLessEqual(row.ACTIVE_SECONDS, 20.1)
        self.assertIsNone(row.RUN_STARTED_AT)

    def test_succeeded_checkpoint_with_missing_output_is_requeued(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(3)
            manager.finish_task(task_id, "succeeded", {})

        self.manager.register_processor("upload", blocked)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], self.payload(), server="emby"
        )
        self.assertTrue(running.wait(2))
        item = self.manager.list_items(task["task_id"])[0]
        missing = os.path.join(self.temp.name, "missing.srt")
        self.manager.update_item(
            task["task_id"], item["item_id"], status="succeeded", stage="published",
            output_path=missing, output_hash=hashlib.sha256(SRT).hexdigest(),
            result={"planned_output_hash": hashlib.sha256(SRT).hexdigest()}
        )
        reconciled = self.manager._reconcile_upload_outputs(task["task_id"])
        self.assertEqual(reconciled["invalidated"], 1)
        self.assertEqual(reconciled["succeeded"], 0)
        self.assertEqual(self.manager.list_items(task["task_id"])[0]["status"], "queued")
        release.set()

    def test_preparse_admission_rejects_policy_overflow_and_counts_active_reservations(self):
        self.manager.start()
        mib = 1024 * 1024
        batch_limit = self.manager.get_settings()["batch_limit_mb"] * mib
        with self.assertRaises(TaskUploadTooLarge):
            self.manager.ensure_upload_admission(batch_limit + mib + 1)
        with self.assertRaises(SubtitleTaskError):
            self.manager.update_settings({"batch_limit_mb": 251})

        existing = 10 * mib
        reserve = self.manager.get_settings()["reserve_free_mb"] * mib
        usage = type("Usage", (), {
            "total": 10 * 1024 * mib,
            "used": 0,
            "free": reserve + existing + 6 * mib - 1
        })()
        with patch.object(
                self.manager, "_active_staging_reservation_totals",
                return_value=(existing, existing)
        ), \
                patch("app.helper.subtitle_tasks.shutil.disk_usage", return_value=usage):
            with self.assertRaises(TaskStorageInsufficient):
                self.manager.ensure_upload_admission(mib)

    def test_streaming_space_check_counts_existing_active_reservations(self):
        self.manager.start()
        policy = self.manager.get_settings()
        mib = 1024 * 1024
        existing = 10 * mib
        reserve = policy["reserve_free_mb"] * mib
        usage = type("Usage", (), {
            "total": 10 * 1024 * mib,
            "used": 0,
            "free": reserve + existing + len(SRT) * 3 - 1
        })()
        raw_dir = os.path.join(self.temp.name, "spool-space")
        os.makedirs(raw_dir)
        with patch("app.helper.subtitle_tasks.shutil.disk_usage", return_value=usage):
            with self.assertRaises(TaskStorageInsufficient):
                self.manager._spool_files(
                    [_File("subtitle.srt", SRT)], raw_dir, policy,
                    existing_reserved=existing,
                    existing_future_reserved=existing,
                    reserve_factor=3
                )

    def test_physical_free_guard_does_not_double_charge_staged_raw_bytes(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(3)
            manager.finish_task(task_id, "succeeded", {})

        self.manager.register_processor("upload", blocked)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], self.payload()
        )
        self.assertTrue(running.wait(2))
        try:
            reserved, future = self.manager._active_staging_reservation_totals()
            raw = task["payload"]["total_bytes"]
            self.assertEqual(reserved, task["payload"]["staging_reserved_bytes"])
            self.assertEqual(future, reserved - raw)
        finally:
            release.set()

    def test_intake_hash_is_reused_only_while_staged_identity_matches(self):
        task, _ = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], self.payload()
        )
        item = self.manager.list_items(task["task_id"])[0]
        expected = hashlib.sha256(SRT).hexdigest()

        self.assertTrue((item.get("result") or {}).get("staged_identity"))
        self.assertEqual(
            self.manager.trusted_upload_item_hashes(
                task["task_id"], item["item_id"]
            )["source"],
            expected
        )

        current = os.stat(item["staged_path"])
        os.utime(
            item["staged_path"],
            ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000)
        )
        self.assertEqual(
            self.manager.trusted_upload_item_hashes(
                task["task_id"], item["item_id"]
            )["source"],
            ""
        )

    def test_terminal_staging_delete_failure_is_retried_and_kept_in_quota(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(5)
            manager.finish_task(task_id, "succeeded", {})

        self.manager.register_processor("upload", blocked)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], self.payload()
        )
        self.assertTrue(running.wait(2))
        task_id = task["task_id"]
        staging_dir = task["payload"]["staging_dir"]
        row = self.manager._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.ID == task_id
        ).first()
        row.STATUS = "succeeded"
        row.FINISHED_AT = time.time()
        row.UPDATED_AT = row.FINISHED_AT
        self.manager._db.commit()

        try:
            with patch.object(self.manager, "_remove_tree", return_value=False) as remove_tree:
                self.assertFalse(self.manager._cleanup_task_staging(task_id))
                reserved, future = self.manager._active_staging_reservation_totals()
                self.assertEqual(
                    reserved,
                    task["payload"]["staging_reserved_bytes"]
                )
                self.assertEqual(future, 0)
                self.manager.cleanup()
                self.assertGreaterEqual(remove_tree.call_count, 2)
                self.assertTrue(os.path.isdir(staging_dir))

            self.manager.cleanup()
            self.assertFalse(os.path.exists(staging_dir))
            self.assertEqual(self.manager._active_staging_reservation_totals(), (0, 0))
        finally:
            release.set()

    def test_remove_tree_reports_failure_when_directory_survives(self):
        path = os.path.join(self.temp.name, "undeleted")
        os.makedirs(path)
        with patch("app.helper.subtitle_tasks.shutil.rmtree", return_value=None):
            self.assertFalse(self.manager._remove_tree(path))
        self.assertTrue(os.path.isdir(path))

    def test_terminal_scope_dedupe_only_reuses_explicit_request_id(self):
        self.manager.register_processor(
            "audit", lambda manager, task_id: manager.finish_task(task_id, "succeeded", {})
        )
        self.manager.register_processor(
            "repair", lambda manager, task_id: manager.finish_task(task_id, "succeeded", {})
        )
        self.manager.start()

        first, reused = self.manager.submit_task(
            "audit", "user", {"category": "movie"},
            scope_key="movie", dedupe_key="movie"
        )
        self.assertFalse(reused)
        self.wait_terminal(self.manager, first["task_id"])
        second, reused = self.manager.submit_task(
            "audit", "user", {"category": "movie"},
            scope_key="movie", dedupe_key="movie"
        )
        self.assertFalse(reused)
        self.assertNotEqual(second["task_id"], first["task_id"])
        self.wait_terminal(self.manager, second["task_id"])

        explicit, reused = self.manager.submit_task(
            "repair", "user", {"media_file": self.media},
            request_id="repair-idempotency", dedupe_key="repair"
        )
        self.assertFalse(reused)
        self.wait_terminal(self.manager, explicit["task_id"])
        duplicate, reused = self.manager.submit_task(
            "repair", "user", {"media_file": self.media},
            request_id="repair-idempotency", dedupe_key="repair"
        )
        self.assertTrue(reused)
        self.assertEqual(duplicate["task_id"], explicit["task_id"])

    def test_audit_worker_start_failure_cannot_commit_an_orphan_task(self):
        original_start = threading.Thread.start

        def fail_audit_worker(thread, *args, **kwargs):
            if thread.name == "subtitle-audit-worker":
                raise RuntimeError("thread quota exhausted")
            return original_start(thread, *args, **kwargs)

        with patch.object(threading.Thread, "start", new=fail_audit_worker):
            with self.assertRaises(SubtitleTaskError):
                self.manager.submit_task(
                    "audit", "user", {"category": "movie"},
                    scope_key="movie", dedupe_key="movie"
                )
        self.assertEqual(self.manager._db.query(SUBTITLETASK).count(), 0)

        # Also cover a runtime dispatcher restart failure after SQLite has
        # already accepted the task row.
        self.manager.start()
        with patch.object(self.manager, "_claim_next_audit", return_value=None), \
                patch.object(
                    self.manager, "_ensure_audit_worker_alive",
                    side_effect=RuntimeError("runtime thread quota exhausted")
                ):
            task, reused = self.manager.submit_task(
                "audit", "user", {"category": "movie"},
                scope_key="movie", dedupe_key="movie"
            )
        self.assertFalse(reused)
        self.assertEqual(task["status"], "interrupted")
        active = self.manager._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.TYPE == "audit",
            SUBTITLETASK.STATUS.in_(["queued", "recovering", "running", "canceling"])
        ).count()
        self.assertEqual(active, 0)

    def test_audit_terminal_write_failure_is_recovered_without_active_ghost(self):
        self.manager.register_processor(
            "audit", lambda manager, task_id: manager.finish_task(task_id, "succeeded", {})
        )
        original_commit = self.manager._db.commit
        audit_commits = {"count": 0}
        failures = {"count": 0}

        def flaky_commit():
            if threading.current_thread().name == "subtitle-audit-worker":
                audit_commits["count"] += 1
                # Claim succeeds; both finish attempts and the first watchdog
                # checkpoint fail.  The persistent worker must retry recovery.
                if audit_commits["count"] in [2, 3, 4]:
                    failures["count"] += 1
                    raise RuntimeError("simulated terminal sqlite failure")
            return original_commit()

        with patch.object(self.manager._db, "commit", side_effect=flaky_commit):
            task, _ = self.manager.submit_task(
                "audit", "user", {"category": "movie"},
                scope_key="movie", dedupe_key="movie"
            )
            terminal = self.wait_terminal(self.manager, task["task_id"], timeout=5)

        self.assertEqual(failures["count"], 3)
        self.assertEqual(terminal["status"], "interrupted")
        self.assertNotIn(task["task_id"], self.manager._executing_task_ids)
        replacement, reused = self.manager.submit_task(
            "audit", "user", {"category": "movie"},
            scope_key="movie", dedupe_key="movie"
        )
        self.assertFalse(reused)
        self.assertNotEqual(replacement["task_id"], task["task_id"])

    def test_startup_recovery_does_not_hash_verified_output_twice(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(3)
            manager.finish_task(task_id, "succeeded", {})

        self.manager.register_processor("upload", blocked)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], self.payload()
        )
        self.assertTrue(running.wait(2))
        item = self.manager.list_items(task["task_id"])[0]
        output = os.path.join(self.temp.name, "Movie.eng.srt")
        with open(output, "wb") as file_obj:
            file_obj.write(SRT)
        output_hash = hashlib.sha256(SRT).hexdigest()
        self.manager.update_item(
            task["task_id"], item["item_id"], status="succeeded", stage="published",
            output_path=output, output_hash=output_hash,
            result={"planned_output_hash": output_hash}
        )
        row = self.manager._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.ID == task["task_id"]
        ).first()
        row.STATUS = "recovering"
        self.manager._db.commit()
        original_matches = self.manager._file_matches
        matched_paths = []

        def count_matches(path, expected_hash):
            matched_paths.append(path)
            return original_matches(path, expected_hash)

        try:
            with patch.object(self.manager, "_file_matches", side_effect=count_matches):
                self.manager._recover_tasks()
            self.assertEqual(matched_paths.count(output), 1)
        finally:
            release.set()

    def test_startup_repair_recovery_uses_exact_transaction_manifest(self):
        self.manager.start()
        now = time.time()
        task_id = "repair-recovery-test"
        snapshot = dict(self.payload()["path_authorization"]["source"])
        # Simulate a library media symlink: the referent differs, while repair
        # artifacts and subtitles must remain beside the lexical media path.
        snapshot["real_path"] = os.path.join(self.temp.name, "downloads", "Movie.mkv")
        snapshot["referent_real"] = snapshot["real_path"]
        self.manager._db.insert(SUBTITLETASK(
            ID=task_id,
            TYPE="repair",
            OWNER="user",
            STATUS="running",
            PRIORITY=100,
            SERVER="emby",
            PAYLOAD=json.dumps({
                "media_path": self.media,
                "path_authorization": {
                    "repair": snapshot
                }
            }),
            POLICY=json.dumps(self.manager.get_settings()),
            PHASE="repairing",
            COMPLETED=0,
            METRICS="{}",
            CANCEL_REQUESTED=0,
            CREATED_AT=now,
            QUEUED_AT=now,
            STARTED_AT=now,
            RUN_STARTED_AT=now,
            UPDATED_AT=now
        ))
        self.manager._db.commit()

        with patch.object(
                Subtitle().__class__, "recover_repair_transaction",
                return_value={"recovered": True, "reason": "restored", "manifest_path": "exact"}
        ) as recover, patch(
                "app.helper.subtitle_task_processors._TaskPathGuard"
        ) as path_guard_cls:
            self.manager._recover_tasks()

        self.assertEqual(recover.call_count, 1)
        recovery_kwargs = recover.call_args.kwargs
        self.assertEqual(recovery_kwargs["transaction_id"], task_id)
        self.assertEqual(
            os.path.normcase(recovery_kwargs["media_file"]),
            os.path.normcase(self.media)
        )
        self.assertEqual(
            os.path.normcase(path_guard_cls.call_args.args[1]["repair"]),
            os.path.normcase(self.media)
        )
        self.assertIsNotNone(recovery_kwargs.get("path_guard_check"))
        recovered = self.manager.get_task(task_id, owner=None, admin=True)
        self.assertEqual(recovered["status"], "interrupted")
        self.assertIn("已恢复", recovered["progress"]["message"])

    def test_orphan_cleanup_requires_exact_marker_path_and_inode_without_scanning(self):
        target = os.path.join(self.temp.name, "Movie.eng.srt")
        orphan = os.path.join(
            self.temp.name, f".Movie.eng.srt.subtitle-task-{'a' * 32}.tmp"
        )
        wrong_identity = os.path.join(
            self.temp.name, f".Movie.zh-CN.srt.subtitle-task-{'b' * 32}.tmp"
        )
        unrecorded = os.path.join(
            self.temp.name, f".Movie.forced.srt.subtitle-task-{'c' * 32}.tmp"
        )
        for path in [orphan, wrong_identity, unrecorded]:
            with open(path, "wb") as file_obj:
                file_obj.write(b"temp")
        identity = os.lstat(orphan)
        marker_dir = os.path.join(self.manager._staging_root, "marker-test")
        os.makedirs(marker_dir, exist_ok=True)
        marker_path = os.path.join(marker_dir, "item.publish.json")
        marker = {
            "intents": {
                os.path.normcase(os.path.abspath(target)): {
                    "path": target,
                    "hash": hashlib.sha256(b"temp").hexdigest(),
                    "identity": {"device": identity.st_dev, "inode": identity.st_ino},
                    "temporary_path": orphan,
                    "temporary_hash": hashlib.sha256(b"temp").hexdigest(),
                    "temporary_complete": True
                },
                os.path.normcase(os.path.abspath(os.path.join(self.temp.name, "Movie.zh-CN.srt"))): {
                    "path": os.path.join(self.temp.name, "Movie.zh-CN.srt"),
                    "identity": {"device": identity.st_dev, "inode": identity.st_ino},
                    "temporary_path": wrong_identity,
                    "temporary_complete": False
                }
            }
        }
        with open(marker_path, "w", encoding="utf-8") as file_obj:
            json.dump(marker, file_obj)

        with patch("app.helper.subtitle_tasks.os.scandir", side_effect=AssertionError("must not scan")):
            self.manager._cleanup_upload_temp_artifacts(marker_paths=[marker_path])

        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.isfile(wrong_identity))
        self.assertTrue(os.path.isfile(unrecorded))

    def test_temp_delete_failure_persists_minimal_marker_and_retries(self):
        self.manager.start()
        task_id = "cleanup-retry-task"
        staging_dir = os.path.join(self.manager._staging_root, task_id)
        os.makedirs(staging_dir, exist_ok=True)
        target = os.path.join(self.temp.name, "Movie.eng.srt")
        orphan = os.path.join(
            self.temp.name, f".Movie.eng.srt.subtitle-task-{'e' * 32}.tmp"
        )
        with open(orphan, "wb") as file_obj:
            file_obj.write(b"temp")
        identity = os.lstat(orphan)
        marker_path = os.path.join(staging_dir, "item.publish.json")
        with open(marker_path, "w", encoding="utf-8") as file_obj:
            json.dump({"intents": {
                os.path.normcase(os.path.abspath(target)): {
                    "path": target,
                    "identity": {"device": identity.st_dev, "inode": identity.st_ino},
                    "temporary_path": orphan,
                    "temporary_hash": hashlib.sha256(b"temp").hexdigest(),
                    "temporary_complete": True
                }
            }}, file_obj)
        original_remove = os.remove
        failed = {"value": False}

        def fail_once(path):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(orphan) \
                    and not failed["value"]:
                failed["value"] = True
                raise PermissionError("temporary NAS delete failure")
            return original_remove(path)

        with patch("app.helper.subtitle_tasks.os.remove", side_effect=fail_once):
            cleaned = self.manager._cleanup_task_staging(
                task_id, marker_paths=[marker_path]
            )

        self.assertTrue(cleaned)
        self.assertTrue(os.path.isfile(orphan))
        self.assertFalse(os.path.exists(staging_dir))
        retained = [
            entry.path for entry in os.scandir(self.manager._cleanup_marker_root)
            if entry.name.endswith(".json")
        ]
        self.assertEqual(len(retained), 1)

        self.manager._retry_persistent_temp_cleanup()

        self.assertFalse(os.path.exists(orphan))
        self.assertFalse(os.path.exists(retained[0]))

    def test_retention_keeps_row_and_staging_when_cleanup_evidence_cannot_persist(self):
        running = threading.Event()
        release = threading.Event()

        def blocked(manager, task_id):
            running.set()
            release.wait(5)
            manager.finish_task(task_id, "succeeded", {})

        self.manager.register_processor("upload", blocked)
        self.manager.start()
        task, _ = self.manager.submit_upload(
            "user", [_File("subtitle.srt", SRT)], self.payload()
        )
        self.assertTrue(running.wait(2))
        task_id = task["task_id"]
        staging_dir = task["payload"]["staging_dir"]
        target = os.path.join(self.temp.name, "Movie.eng.srt")
        orphan = os.path.join(
            self.temp.name, f".Movie.eng.srt.subtitle-task-{'f' * 32}.tmp"
        )
        with open(orphan, "wb") as file_obj:
            file_obj.write(b"temp")
        identity = os.lstat(orphan)
        marker_path = os.path.join(staging_dir, "item.publish.json")
        with open(marker_path, "w", encoding="utf-8") as file_obj:
            json.dump({"intents": {
                os.path.normcase(os.path.abspath(target)): {
                    "path": target,
                    "identity": {"device": identity.st_dev, "inode": identity.st_ino},
                    "temporary_path": orphan,
                    "temporary_hash": hashlib.sha256(b"temp").hexdigest(),
                    "temporary_complete": True,
                }
            }}, file_obj)
        item = self.manager.list_items(task_id)[0]
        self.manager.update_item(
            task_id, item["item_id"],
            result={"ownership_marker": marker_path}
        )
        old = time.time() - 8 * 86400
        row = self.manager._db.query(SUBTITLETASK).filter(
            SUBTITLETASK.ID == task_id
        ).first()
        row.STATUS = "succeeded"
        row.FINISHED_AT = old
        row.UPDATED_AT = old
        self.manager._db.commit()
        os.utime(staging_dir, (old, old))
        original_remove = os.remove

        def fail_orphan_remove(path):
            if os.path.normcase(os.path.abspath(path)) == os.path.normcase(orphan):
                raise PermissionError("temporary NAS delete failure")
            return original_remove(path)

        try:
            with patch(
                    "app.helper.subtitle_tasks.os.remove",
                    side_effect=fail_orphan_remove), patch.object(
                        self.manager, "_persist_cleanup_markers",
                        return_value=set()):
                self.manager.cleanup()

            self.assertIsNotNone(
                self.manager.get_task(task_id, owner=None, admin=True)
            )
            self.assertTrue(os.path.isfile(marker_path))
            self.assertTrue(os.path.isdir(staging_dir))
            self.assertTrue(os.path.isfile(orphan))
        finally:
            release.set()
