import ast
import io
import os
import tempfile
import types
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from flask.wrappers import Request as FlaskRequest
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.http import parse_options_header
from werkzeug.test import EnvironBuilder

import tests.test_subtitle_upload  # optional dependency stubs
import tests.test_media_library  # media/server dependency stubs
from app.helper import subtitle_task_processors as processors


def _load_web_request_guards():
    """Load the request guard declarations without starting the whole web app."""
    source_path = Path(__file__).resolve().parents[1] / "web" / "main.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    wanted_constants = {
        "_SUBTITLE_UPLOAD_HTTP_LIMIT",
        "_SUBTITLE_UPLOAD_MAX_FORM_MEMORY",
        "_SUBTITLE_UPLOAD_MAX_PARTS",
        "_SUBTITLE_UPLOAD_MAX_FILE_PARTS",
    }
    body = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in wanted_constants
                for target in node.targets):
            body.append(node)
        elif isinstance(node, ast.ClassDef) and node.name in {
                "_MultipartPartLimitStream", "_NasToolsRequest"}:
            body.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in {
                "_is_within_roots", "_path_authorization_snapshot"}:
            body.append(node)

    module = types.ModuleType("_subtitle_task_security_web_guards")
    module.__dict__.update({
        "os": os,
        "tempfile": tempfile,
        "FlaskRequest": FlaskRequest,
        "RequestEntityTooLarge": RequestEntityTooLarge,
        "parse_options_header": parse_options_header,
        "Config": lambda: None,
    })
    selected = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(selected)
    exec(compile(selected, str(source_path), "exec"), module.__dict__)
    return module


WEB_GUARDS = _load_web_request_guards()


def _path_snapshot(path, root=None):
    path = os.path.abspath(path)
    parent_real = os.path.normcase(os.path.realpath(os.path.dirname(path)))
    referent_real = os.path.normcase(os.path.realpath(path))

    def identity(stat_result):
        return {
            "device": int(getattr(stat_result, "st_dev", 0) or 0),
            "inode": int(getattr(stat_result, "st_ino", 0) or 0),
            "size": int(getattr(stat_result, "st_size", 0) or 0),
            "mtime_ns": int(getattr(stat_result, "st_mtime_ns", 0) or 0),
        }

    link_stat = os.lstat(path)
    referent_stat = os.stat(path)
    return {
        "path": path,
        "real_path": referent_real,
        "directory": parent_real,
        "parent_real": parent_real,
        "referent_real": referent_real,
        "is_link": os.path.islink(path),
        "trusted_roots": [os.path.normcase(os.path.realpath(root or os.path.dirname(path)))],
        "identity": identity(referent_stat),
        "link_identity": identity(link_stat),
        "referent_identity": identity(referent_stat),
    }


class MultipartRequestGuardTest(TestCase):
    def test_boundary_counter_handles_markers_split_across_read_chunks(self):
        marker = b"\r\n--subtitle-boundary\r\n"
        accepted = WEB_GUARDS._MultipartPartLimitStream(
            io.BytesIO(b"initial" + marker * 64), b"subtitle-boundary", 64
        )
        while accepted.read(3):
            pass
        self.assertEqual(accepted._boundaries, 64)

        rejected = WEB_GUARDS._MultipartPartLimitStream(
            io.BytesIO(b"initial" + marker * 65), b"subtitle-boundary", 64
        )
        with self.assertRaises(RequestEntityTooLarge):
            while rejected.read(3):
                pass
        self.assertEqual(rejected._boundaries, 65)

        for line_break in [b"\n", b"\r"]:
            alternate = WEB_GUARDS._MultipartPartLimitStream(
                io.BytesIO(
                    b"initial"
                    + (line_break + b"--subtitle-boundary" + line_break) * 65
                ),
                b"subtitle-boundary", 64
            )
            with self.subTest(line_break=line_break), \
                    self.assertRaises(RequestEntityTooLarge):
                while alternate.read(2):
                    pass

    def test_boundary_prefix_inside_file_content_does_not_count_as_a_part(self):
        false_prefixes = (
            b"subtitle line\n--subtitle-boundary-not-a-delimiter\n"
            b"subtitle line\n--subtitle-boundary--not-a-real-close\n"
        )
        false_prefixes *= 80
        body = (
            b"--subtitle-boundary\r\n"
            b"Content-Disposition: form-data; name=\"file\"; filename=\"a.srt\"\r\n\r\n"
            + false_prefixes
            + b"\r\n--subtitle-boundary--\r\n"
        )
        stream = WEB_GUARDS._MultipartPartLimitStream(
            io.BytesIO(body), b"subtitle-boundary", 1
        )
        while stream.read(7):
            pass
        self.assertEqual(stream._boundaries, 1)

    def test_closing_boundary_split_at_eof_is_counted_once(self):
        stream = WEB_GUARDS._MultipartPartLimitStream(
            io.BytesIO(b"content\r\n--subtitle-boundary--"),
            b"subtitle-boundary", 1
        )
        while stream.read(2):
            pass
        self.assertEqual(stream._boundaries, 1)

    def test_boundary_transport_padding_is_counted_and_bounded(self):
        padded = WEB_GUARDS._MultipartPartLimitStream(
            io.BytesIO(
                b"initial"
                + (b"\r\n--subtitle-boundary \t\r\n" * 65)
            ),
            b"subtitle-boundary", 64
        )
        with self.assertRaises(RequestEntityTooLarge):
            while padded.read(5):
                pass

        closing = WEB_GUARDS._MultipartPartLimitStream(
            io.BytesIO(b"content\r\n--subtitle-boundary-- \t"),
            b"subtitle-boundary", 1
        )
        while closing.read(3):
            pass
        self.assertEqual(closing._boundaries, 1)

        excessive = WEB_GUARDS._MultipartPartLimitStream(
            io.BytesIO(
                b"content\r\n--subtitle-boundary"
                + b" " * 1025
                + b"\r\n"
            ),
            b"subtitle-boundary", 1
        )
        with self.assertRaises(RequestEntityTooLarge):
            while excessive.read(17):
                pass

        ordinary_content = WEB_GUARDS._MultipartPartLimitStream(
            io.BytesIO(
                b"abc--subtitle-boundary"
                + b" " * 1025
                + b"still subtitle data\n--subtitle-boundary"
                + b" " * 1025
                + b"still not a delimiter"
            ),
            b"subtitle-boundary", 1
        )
        while ordinary_content.read(19):
            pass
        self.assertEqual(ordinary_content._boundaries, 0)

    def test_streaming_byte_limit_applies_without_content_length(self):
        stream = WEB_GUARDS._MultipartPartLimitStream(
            io.BytesIO(b"123456789"), b"subtitle-boundary", 64, max_bytes=8
        )
        with self.assertRaises(RequestEntityTooLarge):
            while stream.read(3):
                pass

    def test_chunked_urlencoded_body_is_bounded_before_form_allocation(self):
        builder = EnvironBuilder(
            path="/subtitle/upload", method="POST",
            content_type="application/x-www-form-urlencoded"
        )
        environ = builder.get_environ()
        environ.pop("CONTENT_LENGTH", None)
        environ["wsgi.input"] = io.BytesIO(b"value=123456789")
        environ["wsgi.input_terminated"] = True
        request = WEB_GUARDS._NasToolsRequest(environ)
        request._subtitle_upload_body_limit = 8
        try:
            with self.assertRaises(RequestEntityTooLarge):
                _ = request.form
        finally:
            request.close()
            builder.close()

    def test_request_rejects_41st_file_part_without_creating_temp_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(get_temp_path=lambda: temp_dir)
            WEB_GUARDS.Config = lambda: config
            builder = EnvironBuilder(path="/subtitle/upload", method="POST")
            request = WEB_GUARDS._NasToolsRequest(builder.get_environ())
            streams = []
            try:
                for index in range(40):
                    streams.append(request._get_file_stream(
                        total_content_length=40,
                        content_type="text/plain",
                        filename=f"part-{index}.srt",
                        content_length=1,
                    ))
                incoming = Path(temp_dir) / "subtitle-upload-incoming"
                files_before = sorted(incoming.iterdir())
                self.assertEqual(len(files_before), 40)

                with self.assertRaises(RequestEntityTooLarge):
                    request._get_file_stream(
                        total_content_length=41,
                        content_type="text/plain",
                        filename="part-40.srt",
                        content_length=1,
                    )

                self.assertEqual(sorted(incoming.iterdir()), files_before)
            finally:
                request.close()
                self.assertTrue(all(stream.closed for stream in streams))
                self.assertFalse(any(path.exists() for path in files_before))
                builder.close()

    def test_request_close_cleans_unregistered_parser_stream(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = SimpleNamespace(get_temp_path=lambda: temp_dir)
            WEB_GUARDS.Config = lambda: config
            builder = EnvironBuilder(path="/subtitle/upload", method="POST")
            request = WEB_GUARDS._NasToolsRequest(builder.get_environ())
            stream = request._get_file_stream(
                total_content_length=None,
                content_type="text/plain",
                filename="aborted.srt",
                content_length=None,
            )
            path = Path(stream.name)
            self.assertTrue(path.exists())
            request.close()
            builder.close()
            self.assertTrue(stream.closed)
            self.assertFalse(path.exists())


class TaskPathGuardTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.media = os.path.join(self.temp.name, "Movie.mkv")
        with open(self.media, "wb") as media_file:
            media_file.write(b"media")
        self.directory = os.path.normcase(os.path.realpath(self.temp.name))
        self.real_media = os.path.normcase(os.path.realpath(self.media))

    def tearDown(self):
        self.temp.cleanup()

    def guard(self, identity=None):
        snapshot = _path_snapshot(self.media, self.temp.name)
        if identity is not None:
            snapshot["identity"] = dict(identity)
            snapshot["referent_identity"] = dict(identity)
        return processors._TaskPathGuard({
            "path_authorization": {
                "source": snapshot
            }
        }, {"source": self.media})

    def test_allows_subtitle_target_in_authorized_media_directory(self):
        subtitle = os.path.join(self.temp.name, "Movie.zh-CN.srt")
        self.assertTrue(self.guard()(subtitle))

    def test_rejects_existing_subtitle_symlink(self):
        subtitle = os.path.join(self.temp.name, "Movie.zh-CN.srt")
        guard = self.guard()

        def is_link(path):
            return os.path.normcase(os.path.abspath(str(path))) \
                == os.path.normcase(os.path.abspath(subtitle))

        with patch(
                "app.helper.subtitle_task_processors.os.path.lexists",
                side_effect=lambda path: is_link(path)), patch(
                    "app.helper.subtitle_task_processors.os.path.islink",
                    side_effect=is_link):
            with self.assertRaises(PermissionError):
                guard(subtitle)

    def test_rejects_media_or_target_parent_realpath_change(self):
        real_realpath = os.path.realpath
        moved_media = os.path.join(self.temp.name, "outside", "Movie.mkv")

        def media_retargeted(path):
            if os.path.abspath(os.path.normpath(str(path))) == self.media:
                return moved_media
            return real_realpath(path)

        guard = self.guard()
        with patch(
                "app.helper.subtitle_task_processors.os.path.realpath",
                side_effect=media_retargeted):
            with self.assertRaises(PermissionError):
                guard.validate_media_paths()

        guard = self.guard()
        subtitle = os.path.join(self.temp.name, "Movie.zh-CN.srt")
        outside = os.path.join(self.temp.name, "outside")

        def parent_retargeted(path):
            normalized = os.path.abspath(os.path.normpath(str(path)))
            if normalized == os.path.dirname(subtitle):
                return outside
            if normalized == subtitle:
                return os.path.join(outside, os.path.basename(subtitle))
            return real_realpath(path)

        with patch(
                "app.helper.subtitle_task_processors.os.path.realpath",
                side_effect=parent_retargeted):
            with self.assertRaises(PermissionError):
                guard(subtitle)

    def test_rejects_media_file_identity_change(self):
        guard = self.guard(identity={"device": 11, "inode": 22})
        changed = SimpleNamespace(st_dev=11, st_ino=23)
        with patch(
                "app.helper.subtitle_task_processors.os.path.isfile",
                return_value=True), patch(
                    "app.helper.subtitle_task_processors.os.stat",
                    return_value=changed):
            with self.assertRaises(PermissionError):
                guard.validate_media_paths()

    def test_missing_snapshot_fails_closed(self):
        with self.assertRaises(PermissionError):
            processors._TaskPathGuard({}, {"source": self.media})

    def test_snapshot_skips_incompatible_root_and_keeps_checking(self):
        real_commonpath = os.path.commonpath
        calls = [0]

        def cross_volume_once(paths):
            calls[0] += 1
            if calls[0] == 1:
                raise ValueError("Paths don't have the same drive")
            return real_commonpath(paths)

        with patch.object(
                WEB_GUARDS.os.path, "commonpath", side_effect=cross_volume_once):
            snapshot = WEB_GUARDS._path_authorization_snapshot(
                self.media, [os.path.join(self.temp.name, "other"), self.temp.name]
            )
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["path"], os.path.abspath(self.media))

    def test_media_file_symlink_keeps_lexical_subtitle_directory(self):
        with tempfile.TemporaryDirectory() as root:
            library = os.path.join(root, "library")
            downloads = os.path.join(root, "downloads")
            os.makedirs(library)
            os.makedirs(downloads)
            referent = os.path.join(downloads, "Movie.mkv")
            link = os.path.join(library, "Movie.mkv")
            Path(referent).write_bytes(b"media")
            try:
                os.symlink(referent, link)
            except (OSError, NotImplementedError) as error:
                self.skipTest(f"当前环境无法创建文件软链接：{error}")
            snapshot = _path_snapshot(link, library)
            guard = processors._TaskPathGuard({
                "path_authorization": {"target": snapshot}
            }, {"target": link})
            subtitle = os.path.join(library, "Movie.zh-CN.srt")
            self.assertTrue(guard(subtitle))
            with self.assertRaises(PermissionError):
                guard(os.path.join(downloads, "Movie.zh-CN.srt"))


class _UploadManager:
    def __init__(self, media_file, staged_path, budget_minutes=4):
        snapshot = _path_snapshot(media_file)
        self.task = {
            "task_id": "upload-task",
            "server": "emby",
            "payload": {
                "media_file": media_file,
                "canonical_media_file": media_file,
                "align_mode": "none",
                "server": "emby",
                "path_authorization": {
                    "source": dict(snapshot),
                    "target": dict(snapshot),
                },
            },
            "policy_snapshot": {
                "upload_budget_minutes": budget_minutes,
                "heavy_process_concurrency": 1,
            },
            "progress": {"elapsed_seconds": 0},
        }
        self.items = [{
            "item_id": "item-1",
            "item_key": "item-1",
            "source_name": "Movie.zh-CN.srt",
            "staged_path": staged_path,
            "status": "queued",
        }]
        self.finished = None

    def get_task(self, task_id, owner=None, admin=False):
        return self.task

    def list_items(self, task_id):
        return self.items

    def verify_upload_item_output(self, task_id, item_id):
        return False

    def update_item(self, task_id, item_id, **values):
        self.items[0].update(values)
        return self.items[0]

    def update_progress(self, task_id, **values):
        self.task.setdefault("progress", {}).update(values)

    def ensure_task_target_space(self, task_id, media_file):
        return True

    def is_cancel_requested(self, task_id):
        return False

    def is_stopping(self):
        return False

    def heavy_operation(self, kind, limit=1, cancel_check=None):
        return nullcontext()

    def invalidate_probe_cache(self, paths):
        return None

    def invalidate_audit_states(self, media_paths):
        return None

    def finish_task(self, task_id, status, result=None, error=None, message=None):
        self.finished = {
            "task_id": task_id,
            "status": status,
            "result": result or {},
            "error": error,
            "message": message,
        }
        return self.finished


class UploadRefreshBudgetTest(TestCase):
    def test_successful_publication_with_under_five_minutes_left_stays_succeeded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = os.path.join(temp_dir, "Movie.mkv")
            staged = os.path.join(temp_dir, "incoming.srt")
            output = os.path.join(temp_dir, "Movie.zh-CN.srt")
            Path(media).write_bytes(b"media")
            Path(staged).write_bytes(b"subtitle")
            manager = _UploadManager(media, staged)
            published = {
                "canonical_subtitle": output,
                "companion_subtitle": "",
                "output_hash": "published-hash",
                "language": "zh-CN",
            }

            with patch.object(
                    processors.Subtitle(),
                    "process_staged_upload",
                    return_value=published) as process_upload, patch.object(
                        processors.MediaLibrary,
                        "invalidate_subtitle_directory_cache",
                        return_value=None):
                result = processors.process_upload_task(manager, "upload-task")

            remaining_budget = process_upload.call_args.kwargs["remaining_budget"]
            self.assertGreater(remaining_budget(), 0)
            self.assertLess(remaining_budget(), 300)
            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["result"]["refresh"]["status"], "skipped")
            self.assertTrue(result["result"]["refresh"]["budget_limited"])
            self.assertNotIn("stop_reason", result["result"])

    def test_refresh_crossing_budget_does_not_downgrade_published_upload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = os.path.join(temp_dir, "Movie.mkv")
            staged = os.path.join(temp_dir, "incoming.srt")
            output = os.path.join(temp_dir, "Movie.zh-CN.srt")
            Path(media).write_bytes(b"media")
            Path(staged).write_bytes(b"subtitle")
            manager = _UploadManager(media, staged, budget_minutes=6)
            published = {
                "canonical_subtitle": output,
                "companion_subtitle": "",
                "output_hash": "published-hash",
                "language": "zh-CN",
            }
            clock = [0.0]

            def refresh(*args, **kwargs):
                clock[0] = 361.0
                return {"status": "refreshed", "scope": "item"}

            with patch("app.helper.subtitle_task_processors.time.monotonic",
                       side_effect=lambda: clock[0]), patch.object(
                        processors.Subtitle(), "process_staged_upload",
                        return_value=published), patch.object(
                            processors, "_localized_refresh", side_effect=refresh), patch.object(
                                processors.MediaLibrary,
                                "invalidate_subtitle_directory_cache",
                                return_value=None):
                result = processors.process_upload_task(manager, "upload-task")

            self.assertEqual(result["status"], "succeeded")
            self.assertEqual(result["result"]["refresh"]["status"], "refreshed")
            self.assertTrue(result["result"]["refresh"]["budget_limited"])
            self.assertNotIn("stop_reason", result["result"])


class AuditShutdownTest(TestCase):
    def test_shutdown_interrupts_audit_without_committing_history(self):
        stopping = [False]
        manager = MagicMock()
        manager.get_task.return_value = {
            "task_id": "audit-task",
            "server": "emby",
            "scope_key": "scope",
            "payload": {"category": "movie", "mode": "linked", "server": "emby"},
            "policy_snapshot": {"audit_max_minutes": 60},
        }
        manager.is_cancel_requested.return_value = False
        manager.is_stopping.side_effect = lambda: stopping[0]
        manager.heavy_operation.return_value = nullcontext()

        def audit(*args, **kwargs):
            stopping[0] = True
            self.assertTrue(kwargs["cancel_check"]())
            return {
                "code": 0,
                "canceled": True,
                "coverage_complete": False,
                "media_statuses": {},
            }

        with patch.object(
                processors.MediaLibrary, "audit_external_subtitles",
                side_effect=audit):
            with self.assertRaises(InterruptedError):
                processors.process_audit_task(manager, "audit-task")
        manager.commit_audit_result.assert_not_called()
