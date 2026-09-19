import os
import tempfile
from unittest import TestCase
from unittest.mock import patch

# Reuse the lightweight optional-dependency stubs used by the existing upload
# regression module before importing the application service.
from tests.test_subtitle_upload import Subtitle
from app.helper.subtitle_health import SubtitleHealth


SRT = (
    b"1\n00:00:01,000 --> 00:00:02,000\n"
    b"This is a sufficiently long English subtitle line.\n"
)


class SubtitleTaskPipelineTest(TestCase):
    def test_owned_marker_failure_rolls_back_only_the_published_text_inode(self):
        for replace_with_foreign in [False, True]:
            with self.subTest(replace_with_foreign=replace_with_foreign), \
                    tempfile.TemporaryDirectory() as tmpdir:
                target_media = os.path.join(tmpdir, "Movie.mkv")
                staged = os.path.join(tmpdir, "upload.srt")
                target_subtitle = os.path.join(tmpdir, "Movie.eng.srt")
                open(target_media, "wb").close()
                with open(staged, "wb") as file_obj:
                    file_obj.write(SRT)

                service = Subtitle()
                marker_writer = service._Subtitle__write_artifact_marker

                def fail_owned_marker(path, value):
                    if path.endswith(".publish.json") and (value.get("owned") or {}):
                        if replace_with_foreign:
                            foreign = os.path.join(tmpdir, "foreign.srt")
                            with open(foreign, "wb") as file_obj:
                                file_obj.write(SRT)
                            os.replace(foreign, target_subtitle)
                        raise OSError("simulated owned marker fsync failure")
                    return marker_writer(path, value)

                with patch.object(
                        service.__class__, "_Subtitle__write_artifact_marker",
                        side_effect=fail_owned_marker):
                    with self.assertRaisesRegex(OSError, "owned marker"):
                        service.process_staged_upload(
                            staged,
                            "subtitle.srt",
                            target_media,
                            work_dir=os.path.join(tmpdir, "work")
                        )

                self.assertEqual(os.path.exists(target_subtitle), replace_with_foreign)
                if replace_with_foreign:
                    with open(target_subtitle, "rb") as file_obj:
                        self.assertEqual(file_obj.read(), SRT)

    def test_linked_target_is_the_only_published_copy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = os.path.join(tmpdir, "source")
            target_dir = os.path.join(tmpdir, "target")
            stage_dir = os.path.join(tmpdir, "stage")
            os.makedirs(source_dir)
            os.makedirs(target_dir)
            os.makedirs(stage_dir)
            source_media = os.path.join(source_dir, "Original.mkv")
            target_media = os.path.join(target_dir, "Movie.mkv")
            staged = os.path.join(stage_dir, "upload.srt")
            open(source_media, "wb").close()
            open(target_media, "wb").close()
            with open(staged, "wb") as file_obj:
                file_obj.write(SRT)

            result = Subtitle().process_staged_upload(
                staged,
                "subtitle.srt",
                target_media,
                server_type="emby",
                work_dir=os.path.join(stage_dir, "work")
            )

            self.assertEqual(result["canonical_subtitle"], os.path.join(target_dir, "Movie.eng.srt"))
            self.assertTrue(os.path.isfile(result["canonical_subtitle"]))
            self.assertEqual(
                [name for name in os.listdir(source_dir) if name.lower().endswith(".srt")],
                []
            )

    def test_recovery_reuses_persisted_output_without_new_number(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target_media = os.path.join(tmpdir, "Movie.mkv")
            staged = os.path.join(tmpdir, "upload.srt")
            open(target_media, "wb").close()
            with open(staged, "wb") as file_obj:
                file_obj.write(SRT)
            planned = {}

            def checkpoint(_phase, _message, extra):
                if extra.get("planned_outputs"):
                    planned.update(extra["planned_outputs"])

            first = Subtitle().process_staged_upload(
                staged,
                "subtitle.srt",
                target_media,
                work_dir=os.path.join(tmpdir, "work"),
                phase_callback=checkpoint
            )
            second = Subtitle().process_staged_upload(
                staged,
                "subtitle.srt",
                target_media,
                work_dir=os.path.join(tmpdir, "work"),
                planned_outputs=planned
            )

            self.assertEqual(first["canonical_subtitle"], second["canonical_subtitle"])
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.eng(1).srt")))

    def test_vobsub_alignment_is_rejected_before_publish(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target_media = os.path.join(tmpdir, "Movie.mkv")
            staged_sub = os.path.join(tmpdir, "upload.sub")
            staged_idx = os.path.join(tmpdir, "upload.idx")
            open(target_media, "wb").close()
            with open(staged_sub, "wb") as file_obj:
                file_obj.write(b"\x00binary-vobsub")
            with open(staged_idx, "wb") as file_obj:
                file_obj.write(b"VobSub index file, v7")

            with self.assertRaisesRegex(ValueError, "VobSub"):
                Subtitle().process_staged_upload(
                    staged_sub,
                    "Movie.sub",
                    target_media,
                    companion_path=staged_idx,
                    align_mode="auto",
                    work_dir=os.path.join(tmpdir, "work")
                )
            self.assertFalse(os.path.exists(os.path.join(tmpdir, "Movie.zh-CN.sub")))

    def test_vobsub_collision_never_deletes_preexisting_matching_idx(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target_media = os.path.join(tmpdir, "Movie.mkv")
            staged_sub = os.path.join(tmpdir, "upload.sub")
            staged_idx = os.path.join(tmpdir, "upload.idx")
            target_sub = os.path.join(tmpdir, "Movie.zh-CN.sub")
            target_idx = os.path.join(tmpdir, "Movie.zh-CN.idx")
            open(target_media, "wb").close()
            with open(staged_sub, "wb") as file_obj:
                file_obj.write(b"new-vobsub")
            with open(staged_idx, "wb") as file_obj:
                file_obj.write(b"shared-index")
            with open(target_sub, "wb") as file_obj:
                file_obj.write(b"preexisting-vobsub")
            with open(target_idx, "wb") as file_obj:
                file_obj.write(b"shared-index")

            with patch.object(
                    SubtitleHealth, "normalize_uploaded_subtitle",
                    return_value={"valid": True, "message": "ok"}):
                with self.assertRaises(FileExistsError):
                    Subtitle().process_staged_upload(
                        staged_sub,
                        "Movie.sub",
                        target_media,
                        companion_path=staged_idx,
                        work_dir=os.path.join(tmpdir, "work"),
                        planned_outputs={"primary": target_sub, "companion": target_idx}
                    )

            with open(target_sub, "rb") as file_obj:
                self.assertEqual(file_obj.read(), b"preexisting-vobsub")
            with open(target_idx, "rb") as file_obj:
                self.assertEqual(file_obj.read(), b"shared-index")
