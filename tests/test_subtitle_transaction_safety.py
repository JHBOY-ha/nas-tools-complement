import hashlib
import os
import tempfile
from unittest import TestCase
from unittest.mock import patch

from tests.test_subtitle_upload import Subtitle
from app.helper.subtitle_health import SubtitleHealth


SRT = b"1\n00:00:01,000 --> 00:00:02,000\nA long English subtitle line.\n"
VALID = {"valid": True, "probe_available": True, "message": "ok", "encoding": "utf-8"}


class SubtitleTransactionSafetyTest(TestCase):
    @staticmethod
    def _inspection(source):
        def inspect(path, *_args, **_kwargs):
            return {
                "path": path,
                "status": "warning" if os.path.normcase(path) == os.path.normcase(source) else "ok",
                "reason": "needs repair" if os.path.normcase(path) == os.path.normcase(source) else "ok"
            }
        return inspect

    def test_upload_does_not_reread_target_after_irreversible_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = os.path.join(tmpdir, "Movie.mkv")
            staged = os.path.join(tmpdir, "upload.srt")
            target = os.path.join(tmpdir, "Movie.eng.srt")
            open(media, "wb").close()
            with open(staged, "wb") as file_obj:
                file_obj.write(SRT)

            service = Subtitle()
            real_hash = service._Subtitle__file_sha256

            def fail_target_read(path, cancel_check=None):
                if os.path.normcase(path) == os.path.normcase(target) and os.path.exists(target):
                    raise OSError("simulated NAS target reread failure")
                return real_hash(path, cancel_check=cancel_check)

            with patch.object(
                    service.__class__, "_Subtitle__file_sha256",
                    side_effect=fail_target_read), \
                    patch.object(SubtitleHealth, "normalize_uploaded_subtitle", return_value=VALID):
                result = service.process_staged_upload(
                    staged, "upload.eng.srt", media,
                    work_dir=os.path.join(tmpdir, "work")
                )

            self.assertTrue(os.path.isfile(target))
            self.assertEqual(result["output_hash"], hashlib.sha256(SRT).hexdigest())

    def test_worker_reuses_intake_hash_without_rereading_staged_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = os.path.join(tmpdir, "Movie.mkv")
            staged = os.path.join(tmpdir, "upload.srt")
            open(media, "wb").close()
            with open(staged, "wb") as file_obj:
                file_obj.write(SRT)

            service = Subtitle()
            real_hash = service._Subtitle__file_sha256
            hashed_paths = []

            def record_hash(path, cancel_check=None):
                hashed_paths.append(os.path.normcase(os.path.abspath(path)))
                return real_hash(path, cancel_check=cancel_check)

            with patch.object(
                    service.__class__, "_Subtitle__file_sha256",
                    side_effect=record_hash), \
                    patch.object(SubtitleHealth, "normalize_uploaded_subtitle", return_value=VALID):
                service.process_staged_upload(
                    staged, "upload.eng.srt", media,
                    work_dir=os.path.join(tmpdir, "work"),
                    trusted_source_hash=hashlib.sha256(SRT).hexdigest()
                )

            self.assertNotIn(
                os.path.normcase(os.path.abspath(staged)),
                hashed_paths
            )

    def test_worker_rejects_staged_content_changed_after_identity_check(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = os.path.join(tmpdir, "Movie.mkv")
            staged = os.path.join(tmpdir, "upload.srt")
            open(media, "wb").close()
            with open(staged, "wb") as file_obj:
                file_obj.write(SRT)

            with self.assertRaisesRegex(PermissionError, "暂存文件.*发生变化"):
                Subtitle().process_staged_upload(
                    staged, "upload.eng.srt", media,
                    work_dir=os.path.join(tmpdir, "work"),
                    trusted_source_hash="0" * 64
                )

            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.eng.srt")))

    def test_repair_restores_concurrent_source_replacement_instead_of_deleting_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = os.path.join(tmpdir, "Movie.mkv")
            source = os.path.join(tmpdir, "Movie.legacy.eng.srt")
            open(media, "wb").close()
            with open(source, "wb") as file_obj:
                file_obj.write(SRT)

            real_replace = os.replace
            swapped = [False]

            def race_replace(old_path, new_path):
                if not swapped[0] \
                        and os.path.normcase(old_path) == os.path.normcase(source) \
                        and ".subtitle-repair-old-" in os.path.basename(new_path):
                    foreign = os.path.join(tmpdir, "foreign.tmp")
                    with open(foreign, "wb") as file_obj:
                        file_obj.write(b"NEW-FROM-EXTERNAL-PROCESS")
                    real_replace(foreign, source)
                    swapped[0] = True
                return real_replace(old_path, new_path)

            with patch.object(SubtitleHealth, "inspect_external_subtitle",
                              side_effect=self._inspection(source)), \
                    patch.object(SubtitleHealth, "normalize_uploaded_subtitle", return_value=VALID), \
                    patch.object(SubtitleHealth, "language_defined", return_value=False), \
                    patch("app.subtitle.os.replace", side_effect=race_replace):
                success, _message, data = Subtitle().repair_external_subtitles(
                    media, "jellyfin", transaction_id="cas-race"
                )

            self.assertFalse(success)
            self.assertTrue(data["failures"])
            with open(source, "rb") as file_obj:
                self.assertEqual(file_obj.read(), b"NEW-FROM-EXTERNAL-PROCESS")
            self.assertFalse(any(
                name.endswith(".srt") and name != os.path.basename(source)
                for name in os.listdir(tmpdir)
            ))

    def test_repair_rollback_never_deletes_foreign_replacement_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = os.path.join(tmpdir, "Movie.mkv")
            source = os.path.join(tmpdir, "Movie.legacy.eng.srt")
            open(media, "wb").close()
            with open(source, "wb") as file_obj:
                file_obj.write(SRT)
            replaced_target = []

            def inspect(path, *_args, **_kwargs):
                if os.path.normcase(path) == os.path.normcase(source):
                    return {"path": path, "status": "warning", "reason": "needs repair"}
                if not replaced_target:
                    foreign = os.path.join(tmpdir, "foreign.tmp")
                    with open(foreign, "wb") as file_obj:
                        file_obj.write(b"FOREIGN-TARGET")
                    os.replace(foreign, path)
                    replaced_target.append(path)
                return {"path": path, "status": "warning", "reason": "forced validation failure"}

            with patch.object(SubtitleHealth, "inspect_external_subtitle", side_effect=inspect), \
                    patch.object(SubtitleHealth, "normalize_uploaded_subtitle", return_value=VALID), \
                    patch.object(SubtitleHealth, "language_defined", return_value=False):
                success, _message, _data = Subtitle().repair_external_subtitles(
                    media, "jellyfin", transaction_id="target-race"
                )

            self.assertFalse(success)
            self.assertTrue(replaced_target)
            with open(replaced_target[0], "rb") as file_obj:
                self.assertEqual(file_obj.read(), b"FOREIGN-TARGET")
            self.assertTrue(os.path.isfile(source))

    def test_repair_manifest_recovers_source_and_removes_exact_created_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = Subtitle()
            media = os.path.join(tmpdir, "Movie.mkv")
            original = os.path.join(tmpdir, "Movie.legacy.srt")
            backup = os.path.join(tmpdir, ".Movie.legacy.srt.subtitle-repair-old-token")
            created = os.path.join(tmpdir, "Movie.eng.srt")
            open(media, "wb").close()
            with open(backup, "wb") as file_obj:
                file_obj.write(b"ORIGINAL")
            with open(created, "wb") as file_obj:
                file_obj.write(b"CREATED")

            snapshot = service._Subtitle__file_snapshot(
                backup, hashlib.sha256(b"ORIGINAL").hexdigest()
            )
            created_record = {
                "path": created,
                "hash": hashlib.sha256(b"CREATED").hexdigest(),
                "identity": service._Subtitle__file_identity(created)
            }
            marker = os.path.join(
                tmpdir, ".Movie.mkv.subtitle-repair-crash-task.json"
            )
            service._Subtitle__write_artifact_marker(marker, {
                "version": 1,
                "media_file": media,
                "state": "retiring",
                "temporary": [],
                "created": [created_record],
                "retired": [{
                    "original": original,
                    "backup": backup,
                    "snapshot": snapshot,
                    "moved_snapshot": snapshot
                }]
            })

            result = service.recover_repair_transaction(
                "crash-task", media
            )

            self.assertTrue(result["recovered"])
            self.assertTrue(os.path.isfile(original))
            self.assertFalse(os.path.exists(backup))
            self.assertFalse(os.path.exists(created))
            self.assertFalse(os.path.exists(marker))

    def test_repair_enforces_item_and_byte_limits(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = os.path.join(tmpdir, "Movie.mkv")
            open(media, "wb").close()
            for index in range(2):
                with open(os.path.join(tmpdir, f"Movie.legacy{index}.eng.srt"), "wb") as file_obj:
                    file_obj.write(SRT)

            with patch.object(SubtitleHealth, "inspect_external_subtitle",
                              side_effect=lambda path, *_a, **_k: {
                                  "path": path, "status": "warning", "reason": "repair"
                              }), \
                    patch.object(SubtitleHealth, "normalize_uploaded_subtitle", return_value=VALID), \
                    patch.object(SubtitleHealth, "language_defined", return_value=True):
                success, _message, data = Subtitle().repair_external_subtitles(
                    media, "jellyfin", transaction_id="item-limit",
                    policy={"max_batch_items": 1, "batch_limit_mb": 250}
                )

            self.assertTrue(success)
            self.assertEqual(data["stop_reason"], "item_limit")
            self.assertTrue(data["partial"])
            self.assertEqual(len(data["processed"]), 1)

        with tempfile.TemporaryDirectory() as tmpdir:
            media = os.path.join(tmpdir, "Movie.mkv")
            source = os.path.join(tmpdir, "Movie.large.eng.srt")
            open(media, "wb").close()
            with open(source, "wb") as file_obj:
                file_obj.write(b"x" * (1024 * 1024 + 1))
            with patch.object(SubtitleHealth, "inspect_external_subtitle",
                              return_value={"status": "warning", "reason": "repair"}):
                success, _message, data = Subtitle().repair_external_subtitles(
                    media, "jellyfin", transaction_id="byte-limit",
                    policy={"max_batch_items": 20, "batch_limit_mb": 1,
                            "text_file_limit_mb": 2}
                )

            self.assertFalse(success)
            self.assertEqual(data["stop_reason"], "byte_limit")
            self.assertTrue(data["partial"])
            self.assertTrue(os.path.isfile(source))

    def test_vobsub_reuses_hashes_and_hashing_is_cooperatively_cancelable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            media = os.path.join(tmpdir, "Movie.mkv")
            source_sub = os.path.join(tmpdir, "upload.sub")
            source_idx = os.path.join(tmpdir, "upload.idx")
            open(media, "wb").close()
            with open(source_sub, "wb") as file_obj:
                file_obj.write(b"binary-vobsub")
            with open(source_idx, "wb") as file_obj:
                file_obj.write(b"VobSub index file, v7")

            service = Subtitle()
            real_hash = service._Subtitle__file_sha256
            sub_hash_calls = [0]

            def count_hash(path, cancel_check=None):
                if str(path).lower().endswith(".sub"):
                    sub_hash_calls[0] += 1
                return real_hash(path, cancel_check=cancel_check)

            with patch.object(service.__class__, "_Subtitle__file_sha256",
                              side_effect=count_hash), \
                    patch.object(SubtitleHealth, "normalize_uploaded_subtitle", return_value=VALID):
                service.process_staged_upload(
                    source_sub, "Movie.sub", media,
                    companion_path=source_idx,
                    work_dir=os.path.join(tmpdir, "work")
                )

            self.assertLessEqual(sub_hash_calls[0], 2)

            large = os.path.join(tmpdir, "large.bin")
            with open(large, "wb") as file_obj:
                file_obj.write(b"x" * (2 * 1024 * 1024))
            checks = [0]

            def cancel_after_first_chunk():
                checks[0] += 1
                return checks[0] > 1

            with self.assertRaises(InterruptedError):
                service._Subtitle__file_sha256(
                    large, cancel_check=cancel_after_first_chunk
                )
