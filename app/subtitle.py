import datetime
import errno
import hashlib
import json
import os.path
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import nullcontext

from lxml import etree

import log
from app.conf import SiteConf
from app.helper import OpenSubtitles
from app.helper.subtitle_align import SubtitleAligner
from app.helper.subtitle_health import SubtitleHealth
from app.utils import RequestUtils, PathUtils, SystemUtils, StringUtils, ExceptionUtils
from app.utils.commons import singleton
from app.utils.types import MediaType, RmtMode
from config import Config, RMT_MEDIAEXT, RMT_SUBEXT


@singleton
class Subtitle:
    opensubtitles = None
    _save_tmp_path = None
    _server = None
    _host = None
    _api_key = None
    _remote_path = None
    _local_path = None
    _opensubtitles_enable = False
    _repair_lock = threading.RLock()
    _jellyfin_iso639_2 = {
        "ar": "ara", "bg": "bul", "zh": "chi", "cs": "cze", "da": "dan", "nl": "dut",
        "en": "eng", "fi": "fin", "fr": "fre", "de": "ger", "el": "gre", "he": "heb",
        "hi": "hin", "hu": "hun", "id": "ind", "it": "ita", "ja": "jpn", "ko": "kor",
        "ms": "may", "no": "nor", "fa": "per", "pl": "pol", "pt": "por", "ro": "rum",
        "ru": "rus", "es": "spa", "sv": "swe", "th": "tha", "tr": "tur", "uk": "ukr",
        "vi": "vie"
    }
    _traditional_chinese_chars = set(
        "體臺萬與為這個們來時會說後裡麼還點從對開關讓過發現長門間見當無於學國華"
        "電車書風區東氣應實種樣頭總經業數據網絡軟雲檔檢測識別聲愛寫讀買賣請問謝歡樂"
    )
    _simplified_chinese_chars = set(
        "体台万与为这个们来时会说后里面么还点从对开关让过发现长门间见当无于学国华"
        "电车书风区东气应实种样头总经业数据网络软云档检测识别声爱写读买卖请问谢欢乐"
    )

    def __init__(self):
        self.init_config()

    def init_config(self):
        self.opensubtitles = OpenSubtitles()
        self._save_tmp_path = Config().get_temp_path()
        if not os.path.exists(self._save_tmp_path):
            os.makedirs(self._save_tmp_path)
        subtitle = Config().get_config('subtitle')
        if subtitle:
            self._server = subtitle.get("server")
            if self._server == "chinesesubfinder":
                self._api_key = subtitle.get("chinesesubfinder", {}).get("api_key")
                self._host = subtitle.get("chinesesubfinder", {}).get('host')
                if self._host:
                    if not self._host.startswith('http'):
                        self._host = "http://" + self._host
                    if not self._host.endswith('/'):
                        self._host = self._host + "/"
                self._local_path = subtitle.get("chinesesubfinder", {}).get("local_path")
                self._remote_path = subtitle.get("chinesesubfinder", {}).get("remote_path")
            else:
                self._opensubtitles_enable = subtitle.get("opensubtitles", {}).get("enable")

    def download_subtitle(self, items, server=None):
        """
        字幕下载入口
        :param items: {"type":, "file", "file_ext":, "name":, "title", "year":, "season":, "episode":, "bluray":}
        :param server: 字幕下载服务器
        :return: 是否成功，消息内容
        """
        if not items:
            return False, "参数有误"
        _server = self._server if not server else server
        if not _server:
            return False, "未配置字幕下载器"
        if _server == "opensubtitles":
            if server or self._opensubtitles_enable:
                return self.__download_opensubtitles(items)
        elif _server == "chinesesubfinder":
            return self.__download_chinesesubfinder(items)
        return False, "未配置字幕下载器"

    def upload_subtitle(self, upload_file, media_file, target_media_file=None, rmt_mode=None, server_type=None,
                        align_mode=None):
        """
        手动上传字幕并同步到目标媒体文件目录。
        流程：
          1. 校验参数（媒体文件、字幕格式）
          2. 从文件名识别语言和标记
          3. 生成不重名字幕路径并原子写入源目录（防并发覆盖）
          4. 若目标媒体文件存在且路径不同，按 rmt_mode 同步到媒体库目录
        :param upload_file: Flask上传文件对象
        :param media_file: 原媒体文件路径（字幕首先保存在该文件同目录）
        :param target_media_file: 媒体库目标媒体文件路径（字幕同步到该文件同目录）
        :param rmt_mode: 目标同步方式（link/softlink/copy）
        :param server_type: 目标媒体服务器类型（emby/jellyfin/plex）
        :param align_mode: 字幕时间轴对齐模式（auto/offset/segmented/llm/none）
        """
        if not upload_file or not media_file:
            return False, "参数有误", {}
        if not os.path.exists(media_file) or not os.path.isfile(media_file):
            return False, "媒体文件不存在", {}
        if os.path.splitext(media_file)[-1].lower() not in RMT_MEDIAEXT:
            return False, "请选择有效的媒体文件", {}

        upload_name = os.path.basename(upload_file.filename or "")
        sub_ext = os.path.splitext(upload_name)[-1].lower()
        if sub_ext not in RMT_SUBEXT:
            return False, "仅支持上传 srt、ass、ssa、smi、vtt、sub 字幕文件", {}

        subtitle_profile = self.__guess_subtitle_profile(upload_name)
        subtitle_profile["source"] = self.__guess_subtitle_source(upload_name, media_file)

        # 通过重试机制规避并发上传导致的 TOCTOU 竞态条件
        # __build_subtitle_path 检查文件不存在后返回路径，但 save 期间可能被其他请求先创建
        _max_retry = 3
        source_sub_file = None
        for _retry in range(_max_retry):
            try:
                src_path = self.__build_subtitle_path(media_file, subtitle_profile, sub_ext, server_type)
            except FileExistsError:
                return False, "字幕文件重名过多", {}
            try:
                self.__save_upload_file_exclusive(upload_file, src_path)
                source_sub_file = src_path
                break
            except FileExistsError:
                if _retry < _max_retry - 1:
                    continue
                return False, "字幕文件保存失败，重试次数耗尽", {}
            except OSError as e:
                ExceptionUtils.exception_traceback(e)
                return False, f"保存源字幕失败：{str(e)}", {}
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                return False, f"保存源字幕失败：{str(e)}", {}
        if not source_sub_file:
            return False, "保存源字幕失败", {}

        validation = SubtitleHealth.normalize_uploaded_subtitle(source_sub_file)
        if not validation.get("valid"):
            try:
                os.remove(source_sub_file)
            except OSError:
                pass
            return False, f"字幕无法被媒体服务器解析：{validation.get('message') or '格式无效'}", {}

        normalize_messages = []
        if validation.get("normalized"):
            normalize_messages.append("已转换为 UTF-8")
        if validation.get("ass_repairs"):
            normalize_messages.extend(validation.get("ass_repairs"))
        elif validation.get("repaired"):
            normalize_messages.append(f"已修复 {validation.get('removed_blank_lines') or 0} 处 SRT 异常空行")
        normalize_msg = f"，{'，'.join(normalize_messages)}" if normalize_messages else ""

        result = {
            "source_subtitle": source_sub_file,
            "target_subtitle": "",
            "language": subtitle_profile.get("language"),
            "source": subtitle_profile.get("source") or "",
            "server": server_type or "",
            "synced": False,
            "validation": validation,
            "alignment": {"applied": False, "skipped": False, "message": "", "mode": ""}
        }
        if not target_media_file:
            return True, f"字幕已保存到源目录{normalize_msg}，未找到媒体库目标文件", result
        if not os.path.exists(target_media_file) or not os.path.isfile(target_media_file):
            return True, f"字幕已保存到源目录{normalize_msg}，媒体库目标文件不存在，未同步", result
        if os.path.splitext(target_media_file)[-1].lower() not in RMT_MEDIAEXT:
            return True, f"字幕已保存到源目录{normalize_msg}，目标文件不是有效媒体文件，未同步", result

        alignment_msg = ""
        if str(align_mode or "").lower() in ["auto", "offset", "segmented", "llm"]:
            alignment = SubtitleAligner.align_subtitle(source_sub_file, target_media_file, align_mode=align_mode)
            result["alignment"] = alignment
            if alignment.get("applied"):
                alignment_msg = "，已自动对齐字幕"
            elif alignment.get("message"):
                alignment_msg = f"，自动对齐跳过：{alignment.get('message')}"

        if os.path.normpath(media_file) == os.path.normpath(target_media_file):
            result["target_subtitle"] = source_sub_file
            result["synced"] = True
            return True, f"字幕已保存到媒体目录{normalize_msg}{alignment_msg}", result

        source_norm = os.path.normpath(source_sub_file)
        target_sub_file = ""
        retmsg = ""
        for _retry in range(_max_retry):
            try:
                target_sub_file = self.__build_subtitle_path(target_media_file, subtitle_profile, sub_ext, server_type)
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                return False, f"生成目标字幕文件名失败：{str(e)}", result
            target_norm = os.path.normpath(target_sub_file)
            if source_norm == target_norm:
                result["target_subtitle"] = target_sub_file
                result["synced"] = True
                return True, "字幕已保存到媒体目录", result

            retcode, retmsg = self.__sync_manual_subtitle(source_sub_file, target_sub_file, rmt_mode)
            result["target_subtitle"] = target_sub_file
            if retcode == 0:
                break
            if retmsg == "目标字幕文件已存在" and _retry < _max_retry - 1:
                continue
            return False, f"字幕已保存到源目录，同步到媒体库失败：{retmsg}", result
        result["synced"] = True
        return True, f"字幕已保存并同步到媒体库目录{normalize_msg}{alignment_msg}", result

    def process_staged_upload(self, staged_path, original_name, canonical_media_file, server_type=None,
                              align_mode="none", companion_path=None, work_dir=None, planned_outputs=None,
                              cancel_check=None, heavy_operation=None, phase_callback=None, policy=None,
                              remaining_budget=None, path_guard_check=None,
                              trusted_source_hash=None, trusted_companion_hash=None):
        """Normalize, optionally align and atomically publish an immutable staged upload.

        This is the task-worker entry point.  Unlike :meth:`upload_subtitle`, no
        file is exposed beside a media file until validation has completed.  A
        filesystem completion marker makes normalization/alignment idempotent
        when the process stops between an artifact write and its DB checkpoint.
        """
        policy = policy or {}
        server_type = str(server_type or "emby").lower()
        align_mode = str(align_mode or "none").lower()
        original_name = os.path.basename(original_name or staged_path or "")
        sub_ext = os.path.splitext(original_name)[-1].lower()
        is_vobsub = bool(companion_path)
        if server_type not in ["emby", "jellyfin", "plex"]:
            raise ValueError("目标影视服务器无效")
        if not staged_path or not os.path.isfile(staged_path):
            raise FileNotFoundError("上传暂存文件不存在")
        if not canonical_media_file or not os.path.isfile(canonical_media_file):
            raise FileNotFoundError("目标媒体文件不存在")
        if os.path.splitext(canonical_media_file)[-1].lower() not in RMT_MEDIAEXT:
            raise ValueError("目标文件不是有效媒体文件")
        if sub_ext not in RMT_SUBEXT:
            raise ValueError("字幕格式不受支持")
        if is_vobsub:
            if sub_ext != ".sub" or not os.path.isfile(companion_path) \
                    or os.path.splitext(companion_path)[-1].lower() != ".idx":
                raise ValueError("VobSub 必须同时提交同名 .sub 与 .idx 文件")
            if align_mode in ["auto", "offset", "segmented", "llm"]:
                raise ValueError("VobSub 图形字幕不支持时间轴对齐")

        def canceled():
            return bool(cancel_check and cancel_check())

        def guard_path(path):
            if not path_guard_check:
                return
            if path_guard_check(path) is False:
                raise PermissionError(errno.EACCES, "字幕目标路径已不在授权范围内", path)

        def checkpoint(phase, message, extra=None):
            if canceled():
                raise InterruptedError("任务已取消")
            if phase_callback:
                phase_callback(phase, message, extra or {})

        work_dir = os.path.abspath(work_dir or os.path.join(os.path.dirname(staged_path), "derived"))
        os.makedirs(work_dir, exist_ok=True)
        source_fingerprint = str(trusted_source_hash or "").lower()
        if not re.fullmatch(r"[0-9a-f]{64}", source_fingerprint):
            source_fingerprint = self.__file_sha256(staged_path, cancel_check=cancel_check)
        companion_fingerprint = str(trusted_companion_hash or "").lower()
        if companion_path and not re.fullmatch(r"[0-9a-f]{64}", companion_fingerprint):
            companion_fingerprint = self.__file_sha256(
                companion_path, cancel_check=cancel_check
            )
        elif not companion_path:
            companion_fingerprint = ""
        artifact_key = hashlib.sha256(
            (source_fingerprint + companion_fingerprint + original_name).encode("utf-8")
        ).hexdigest()[:20]
        normalized_dir = os.path.join(work_dir, "normalized")
        os.makedirs(normalized_dir, exist_ok=True)
        normalized_primary = os.path.join(normalized_dir, artifact_key + sub_ext)
        normalized_companion = os.path.join(normalized_dir, artifact_key + ".idx") if is_vobsub else ""
        normalized_marker = os.path.join(normalized_dir, artifact_key + ".json")
        publish_marker = os.path.join(work_dir, artifact_key + ".publish.json")

        checkpoint("validating", "正在校验字幕", {"original_name": original_name})
        marker = self.__read_artifact_marker(normalized_marker)
        expected_inputs = {"primary": source_fingerprint, "companion": companion_fingerprint}
        normalized_files = [normalized_primary] + ([normalized_companion] if normalized_companion else [])
        if not self.__artifact_marker_valid(
                marker, expected_inputs, normalized_files, cancel_check=cancel_check):
            copied_hashes = {
                normalized_primary: self.__atomic_copy_file(
                    staged_path, normalized_primary, cancel_check=cancel_check
                )
            }
            if copied_hashes[normalized_primary] != source_fingerprint:
                raise PermissionError("上传暂存文件在处理期间发生变化，请重新提交")
            if is_vobsub:
                copied_hashes[normalized_companion] = self.__atomic_copy_file(
                    companion_path, normalized_companion, cancel_check=cancel_check
                )
                if copied_hashes[normalized_companion] != companion_fingerprint:
                    raise PermissionError("VobSub 暂存组件在处理期间发生变化，请重新提交")
            operation = heavy_operation("interactive") if is_vobsub and heavy_operation else nullcontext()
            with operation:
                validation = SubtitleHealth.normalize_uploaded_subtitle(
                    normalized_primary,
                    timeout_seconds=policy.get("ffprobe_timeout_seconds") or 10,
                    cancel_check=cancel_check
                )
            if validation.get("canceled"):
                raise InterruptedError("任务已取消")
            if not validation.get("valid"):
                raise ValueError(validation.get("message") or "字幕内容无法解析")
            marker = {
                "inputs": expected_inputs,
                "outputs": {
                    path: (
                        copied_hashes.get(path)
                        if path != normalized_primary or not validation.get("normalized")
                        else self.__file_sha256(path, cancel_check=cancel_check)
                    )
                    for path in normalized_files
                },
                "validation": validation
            }
            self.__write_artifact_marker(normalized_marker, marker)
        validation = marker.get("validation") or {}

        checkpoint("normalizing", "字幕规范化完成")
        subtitle_profile = self.__guess_subtitle_profile(original_name, default_language="")
        if not subtitle_profile.get("language"):
            subtitle_profile["language"] = self.__infer_subtitle_language(
                normalized_primary,
                validation
            ) or "zh-CN"
        subtitle_profile["source"] = self.__guess_subtitle_source(original_name, canonical_media_file)

        prepared_primary = normalized_primary
        alignment = {"applied": False, "skipped": False, "message": "", "mode": "none"}
        if align_mode in ["auto", "offset", "segmented", "llm"]:
            checkpoint("aligning", "正在对齐字幕")
            aligned_dir = os.path.join(work_dir, "aligned")
            os.makedirs(aligned_dir, exist_ok=True)
            aligned_primary = os.path.join(aligned_dir, artifact_key + sub_ext)
            aligned_marker = os.path.join(aligned_dir, artifact_key + f".{align_mode}.json")
            aligned_inputs = {
                "primary": (marker.get("outputs") or {}).get(normalized_primary)
                or self.__file_sha256(normalized_primary, cancel_check=cancel_check),
                "mode": align_mode
            }
            align_manifest = self.__read_artifact_marker(aligned_marker)
            if not self.__artifact_marker_valid(
                    align_manifest, aligned_inputs, [aligned_primary],
                    cancel_check=cancel_check):
                self.__atomic_copy_file(normalized_primary, aligned_primary, cancel_check=cancel_check)
                operation = heavy_operation("interactive") if heavy_operation else nullcontext()
                with operation:
                    alignment = SubtitleAligner.align_subtitle(
                        aligned_primary,
                        canonical_media_file,
                        align_mode=align_mode,
                        cancel_check=cancel_check,
                        ffprobe_timeout=policy.get("ffprobe_timeout_seconds") or 10,
                        ffmpeg_timeout=policy.get("ffmpeg_timeout_seconds") or 60,
                        llm_timeout=policy.get("llm_timeout_seconds") or 180,
                        llm_max_batches=policy.get("llm_max_batches") or 8,
                        remaining_budget=remaining_budget,
                        temporary_dir=work_dir,
                        reference_max_bytes=(
                            int(policy.get("text_file_limit_mb") or 20) * 1024 * 1024
                        )
                    )
                if canceled() or alignment.get("message") == "任务已取消":
                    raise InterruptedError("任务已取消")
                align_manifest = {
                    "inputs": aligned_inputs,
                    "outputs": {
                        aligned_primary: self.__file_sha256(
                            aligned_primary, cancel_check=cancel_check
                        )
                    },
                    "alignment": alignment
                }
                self.__write_artifact_marker(aligned_marker, align_manifest)
            alignment = align_manifest.get("alignment") or alignment
            prepared_primary = aligned_primary

        outputs = planned_outputs if isinstance(planned_outputs, dict) else {}
        output_primary = str(outputs.get("primary") or "")
        output_companion = str(outputs.get("companion") or "")
        if not output_primary:
            output_primary = self.__build_subtitle_path(
                canonical_media_file,
                subtitle_profile,
                sub_ext,
                server_type,
                companion_exts=[".idx"] if is_vobsub else None
            )
            if is_vobsub:
                output_companion = os.path.splitext(output_primary)[0] + ".idx"
            outputs = {"primary": output_primary, "companion": output_companion}
            prepared_marker = align_manifest if prepared_primary != normalized_primary else marker
            planned_hashes = {
                "primary": (prepared_marker.get("outputs") or {}).get(prepared_primary)
                or self.__file_sha256(prepared_primary, cancel_check=cancel_check),
                "companion": (marker.get("outputs") or {}).get(normalized_companion, "")
                if is_vobsub else ""
            }
            checkpoint("planning", "已固定最终字幕路径", {
                "planned_outputs": outputs,
                "planned_hashes": planned_hashes,
                "ownership_marker": publish_marker
            })
        else:
            prepared_marker = align_manifest if prepared_primary != normalized_primary else marker
            planned_hashes = {
                "primary": (prepared_marker.get("outputs") or {}).get(prepared_primary)
                or self.__file_sha256(prepared_primary, cancel_check=cancel_check),
                "companion": (marker.get("outputs") or {}).get(normalized_companion, "")
                if is_vobsub else ""
            }
            checkpoint("planning", "继续使用已固定的字幕路径", {
                "planned_outputs": outputs,
                "planned_hashes": planned_hashes,
                "ownership_marker": publish_marker
            })

        checkpoint("publishing", "正在原子发布字幕", {
            "planned_outputs": outputs,
            "planned_hashes": planned_hashes,
            "ownership_marker": publish_marker
        })
        ownership = self.__read_artifact_marker(publish_marker)
        if ownership.get("inputs") != expected_inputs:
            ownership = {"inputs": expected_inputs, "owned": {}}

        def record_owned(path, expected_hash):
            key = os.path.normcase(os.path.abspath(path))
            owned = dict(ownership.get("owned") or {})
            owned[key] = {
                "path": path, "hash": expected_hash,
                "identity": self.__file_identity(path)
            }
            ownership["owned"] = owned
            intents = dict(ownership.get("intents") or {})
            intents.pop(key, None)
            ownership["intents"] = intents
            self.__write_artifact_marker(publish_marker, ownership)

        def record_intent(path, expected_hash, identity, temporary_path="",
                          temporary_hash=""):
            key = os.path.normcase(os.path.abspath(path))
            intents = dict(ownership.get("intents") or {})
            intents[key] = {
                "path": path, "hash": expected_hash,
                "identity": identity or {},
                "temporary_path": temporary_path or "",
                "temporary_hash": temporary_hash or "",
                "temporary_complete": bool(temporary_hash)
            }
            ownership["intents"] = intents
            self.__write_artifact_marker(publish_marker, ownership)

        def forget_owned(path):
            key = os.path.normcase(os.path.abspath(path))
            owned = dict(ownership.get("owned") or {})
            owned.pop(key, None)
            ownership["owned"] = owned
            intents = dict(ownership.get("intents") or {})
            intents.pop(key, None)
            ownership["intents"] = intents
            self.__write_artifact_marker(publish_marker, ownership)

        def rollback_published(path, expected_hash, published):
            """Remove only the exact inode created by this publication attempt."""
            if not published or not path or not os.path.isfile(path):
                return
            key = os.path.normcase(os.path.abspath(path))
            record = (ownership.get("owned") or {}).get(key) \
                or (ownership.get("intents") or {}).get(key) \
                or {}
            identity = record.get("identity") or {}
            expected_identity = {
                "device": int(identity.get("device") or 0),
                "inode": int(identity.get("inode") or 0)
            }
            if os.path.normcase(os.path.abspath(str(record.get("path") or ""))) != key \
                    or str(record.get("hash") or "") != str(expected_hash) \
                    or expected_identity["inode"] <= 0:
                return
            if not self.__owned_file_matches(path, record):
                return
            guard_path(path)
            os.remove(path)
            forget_owned(path)

        def cleanup_stale_intents():
            """Reconcile the single publish temp left by a hard process stop."""
            intents = dict(ownership.get("intents") or {})
            if not intents:
                return
            owned = dict(ownership.get("owned") or {})
            changed = False
            for key, record in list(intents.items()):
                target = str(record.get("path") or "")
                temporary_path = str(record.get("temporary_path") or "")
                if target and self.__owned_file_matches(target, record):
                    owned[key] = {
                        "path": target,
                        "hash": record.get("hash") or "",
                        "identity": record.get("identity") or {}
                    }
                if temporary_path and self.__same_file_identity(
                        temporary_path, record.get("identity") or {}):
                    try:
                        os.remove(temporary_path)
                    except OSError:
                        # Keep the intent so a later recovery can try again.
                        continue
                intents.pop(key, None)
                changed = True
            if changed:
                ownership["owned"] = owned
                ownership["intents"] = intents
                self.__write_artifact_marker(publish_marker, ownership)

        cleanup_stale_intents()

        # Alignment/probing can run for minutes after admission. Recheck the
        # actual target volume immediately before creating publish temps so a
        # NAS that filled meanwhile keeps its configured safety reserve.
        reserve_bytes = max(int(policy.get("reserve_free_mb") or 1024), 0) * 1024 * 1024
        volume_requirements = {}
        publish_components = [(prepared_primary, output_primary)]
        if is_vobsub:
            publish_components.append((normalized_companion, output_companion))
        for source_path, target_path in publish_components:
            if canceled():
                raise InterruptedError("任务已取消")
            guard_path(target_path)
            if os.path.exists(target_path):
                # Recovery/collision handling below verifies its hash. Existing
                # bytes do not require another allocation reservation.
                continue
            target_dir = os.path.dirname(os.path.abspath(target_path))
            if not os.path.isdir(target_dir):
                raise FileNotFoundError(f"字幕目标目录不存在：{target_dir}")
            disk = shutil.disk_usage(target_dir)
            try:
                device = int(os.stat(target_dir, follow_symlinks=False).st_dev)
            except OSError:
                device = 0
            volume_key = (
                device,
                os.path.normcase(os.path.splitdrive(os.path.realpath(target_dir))[0]),
                int(disk.total)
            )
            value = volume_requirements.setdefault(volume_key, {
                "free": int(disk.free), "required": 0, "directory": target_dir
            })
            value["free"] = min(value["free"], int(disk.free))
            value["required"] += os.path.getsize(source_path)
        for value in volume_requirements.values():
            if value["free"] - value["required"] < reserve_bytes:
                raise OSError(
                    errno.ENOSPC,
                    "字幕发布目标卷空间不足，已保留安全余量",
                    value["directory"]
                )

        published_primary = False
        published_companion = False
        try:
            if is_vobsub:
                # Expose the small index first so media watchers never observe
                # a newly published binary .sub without its required .idx.
                guard_path(output_companion)
                published_companion = self.__publish_file_no_replace(
                    normalized_companion,
                    output_companion,
                    cancel_check=cancel_check,
                    ownership_intent=record_intent,
                    expected_hash=planned_hashes["companion"]
                )
                if published_companion:
                    record_owned(output_companion, planned_hashes["companion"])
            guard_path(output_primary)
            published_primary = self.__publish_file_no_replace(
                prepared_primary,
                output_primary,
                cancel_check=cancel_check,
                ownership_intent=record_intent,
                expected_hash=planned_hashes["primary"]
            )
            if published_primary:
                record_owned(output_primary, planned_hashes["primary"])
        except Exception:
            try:
                rollback_published(
                    output_companion, planned_hashes.get("companion"),
                    is_vobsub and published_companion
                )
            except OSError:
                pass
            try:
                rollback_published(
                    output_primary, planned_hashes.get("primary"), published_primary
                )
            except OSError:
                pass
            raise

        return {
            "canonical_subtitle": output_primary,
            "target_subtitle": output_primary,
            "companion_subtitle": output_companion,
            "language": subtitle_profile.get("language") or "zh-CN",
            "source": subtitle_profile.get("source") or "",
            "server": server_type,
            "synced": True,
            "validation": validation,
            "alignment": alignment,
            "planned_outputs": outputs,
            # Publication already verified these bytes before the irreversible
            # link/rename. Re-reading a NAS target here can only turn a durable
            # success into a false task failure.
            "output_hash": planned_hashes.get("primary") or "",
            "companion_output_hash": planned_hashes.get("companion") or "",
            "planned_output_hash": planned_hashes.get("primary") or "",
            "planned_companion_hash": planned_hashes.get("companion") or "",
            "ownership_marker": publish_marker
        }

    def recover_repair_transaction(self, transaction_id, media_file,
                                   path_guard_check=None):
        """Reconcile one interrupted repair by its persisted task id."""
        token = re.sub(r"[^A-Za-z0-9_-]", "", str(transaction_id or ""))[:64]
        if not token or not media_file:
            return {"recovered": False, "reason": "invalid_transaction"}
        media_file = os.path.abspath(media_file)
        media_dir = os.path.dirname(media_file)
        marker_path = os.path.join(
            media_dir,
            f".{os.path.basename(media_file)}.subtitle-repair-{token}.json"
        )
        if not os.path.isfile(marker_path):
            return {
                "recovered": False,
                "reason": "manifest_missing",
                "manifest_path": marker_path
            }
        recovered = self.__recover_repair_manifest(
            marker_path, media_file, path_guard_check=path_guard_check
        )
        if recovered:
            active_path = os.path.join(
                media_dir,
                f".{os.path.basename(media_file)}.subtitle-repair-active.json"
            )
            pointer = self.__read_artifact_marker(active_path)
            if os.path.normcase(os.path.abspath(
                    str(pointer.get("manifest_path") or ""))) == os.path.normcase(marker_path):
                try:
                    os.remove(active_path)
                except OSError:
                    pass
        return {
            "recovered": recovered,
            "reason": "recovered" if recovered else "artifacts_remaining",
            "manifest_path": marker_path
        }

    def repair_external_subtitles(self, media_file, server_type=None, cancel_check=None,
                                  progress_callback=None, heavy_operation=None, policy=None,
                                  path_guard_check=None, remaining_budget=None,
                                  transaction_id=None):
        """二次处理已有外挂字幕，使文件名、编码和内容符合目标影视服务器规则。"""
        policy = policy or {}
        probe_timeout = float(policy.get("ffprobe_timeout_seconds") or 10)
        server_type = str(server_type or "emby").lower()
        if not media_file or not os.path.isfile(media_file):
            return False, "媒体文件不存在", {}
        if os.path.splitext(media_file)[-1].lower() not in RMT_MEDIAEXT:
            return False, "请选择有效的媒体文件", {}
        if server_type not in ["emby", "jellyfin", "plex"]:
            return False, "目标影视服务器无效", {}

        started = time.monotonic()
        max_items = max(int(policy.get("repair_max_items")
                            or policy.get("max_batch_items") or 20), 1)
        max_total_bytes = max(int(policy.get("repair_max_total_mb")
                                  or policy.get("batch_limit_mb") or 250), 1) * 1024 * 1024
        local_budget_seconds = max(float(policy.get("repair_budget_seconds")
                                         or (float(policy.get("upload_budget_minutes") or 60) * 60)), 1)
        limited = False
        limit_reason = ""
        stop_reason = ""
        consumed_bytes = 0

        def guard_path(path):
            if not path_guard_check:
                return
            if path_guard_check(path) is False:
                raise PermissionError(errno.EACCES, "字幕路径已不在授权范围内", path)

        def user_canceled():
            return bool(cancel_check and cancel_check())

        def available_seconds():
            local_remaining = max(local_budget_seconds - (time.monotonic() - started), 0)
            if remaining_budget is None:
                return local_remaining
            external = remaining_budget() if callable(remaining_budget) else remaining_budget
            try:
                return min(local_remaining, max(float(external), 0))
            except (TypeError, ValueError):
                return local_remaining

        def budget_exhausted(reason="字幕修复达到累计处理时间限制"):
            nonlocal limited, limit_reason, stop_reason
            if available_seconds() > 0:
                return False
            limited = True
            limit_reason = limit_reason or reason
            stop_reason = stop_reason or "time_limit"
            return True

        def operation_abort():
            return user_canceled() or budget_exhausted()

        with self._repair_lock:
            # Repair tasks are intentionally not resumed after restart, but a
            # later invocation must first reconcile exact transaction artifacts
            # left beside this media file.
            if not self.__recover_repair_artifacts(
                    media_file, path_guard_check=path_guard_check):
                return False, "存在尚未安全恢复的字幕修复事务", {
                    "media_file": media_file,
                    "server": server_type,
                    "processed": [],
                    "skipped": [],
                    "failures": [{
                        "path": media_file,
                        "reason": "上一次修复事务仍有身份不明或无法访问的文件，未启动新任务"
                    }],
                    "canceled": False,
                    "partial": False,
                    "stop_reason": "transaction_pending"
                }
            subtitle_files = SubtitleHealth.list_external_subtitles(
                media_file,
                max_results=max_items + 1,
                cancel_check=operation_abort
            )
            item_limit_reached = len(subtitle_files) > max_items
            if item_limit_reached:
                subtitle_files = subtitle_files[:max_items]
            if not subtitle_files:
                return False, "未找到可处理的外挂字幕", {}

            processed = []
            skipped = []
            failures = []
            canceled = False
            last_recovery_ok = True

            def inspect(subtitle_file):
                operation = nullcontext()
                if SubtitleHealth.requires_external_probe(subtitle_file) and heavy_operation:
                    operation = heavy_operation("interactive")
                with operation:
                    if cancel_check is None and remaining_budget is None \
                            and probe_timeout == 10:
                        return SubtitleHealth.inspect_external_subtitle(
                            subtitle_file, media_file, server_type
                        )
                    return SubtitleHealth.inspect_external_subtitle(
                        subtitle_file, media_file, server_type,
                        cancel_check=operation_abort,
                        probe_timeout_seconds=probe_timeout
                    )

            for item_index, subtitle_file in enumerate(subtitle_files):
                last_recovery_ok = self.__recover_repair_artifacts(
                    media_file, path_guard_check=path_guard_check
                )
                if not last_recovery_ok:
                    limited = True
                    stop_reason = "transaction_pending"
                    limit_reason = "字幕修复事务清理未完成，已停止后续项目"
                    failures.append({
                        "path": media_file,
                        "reason": limit_reason
                    })
                    break
                if user_canceled():
                    canceled = True
                    break
                if budget_exhausted():
                    break
                if progress_callback:
                    progress_callback({
                        "phase": "normalizing",
                        "completed": item_index,
                        "total": len(subtitle_files),
                        "current_item": os.path.basename(subtitle_file),
                        "message": "正在二次处理外挂字幕"
                    })
                try:
                    inspection = inspect(subtitle_file)
                except InterruptedError:
                    canceled = True
                    break
                if inspection.get("canceled"):
                    canceled = True
                    break
                if inspection.get("status") == "ok":
                    skipped.append({"path": subtitle_file, "reason": "已符合影视服务器规范"})
                    continue
                if not SubtitleHealth.is_supported_extension(subtitle_file, server_type):
                    failures.append({"path": subtitle_file, "reason": inspection.get("reason") or "字幕格式不受支持"})
                    continue

                sub_ext = os.path.splitext(subtitle_file)[-1].lower()
                work_file = ""
                work_companion = ""
                work_file_identity = {}
                work_companion_identity = {}
                repair_token = re.sub(
                    r"[^A-Za-z0-9_-]", "", str(transaction_id or "")
                )[:64] or uuid.uuid4().hex
                repair_manifest_path = os.path.join(
                    os.path.dirname(media_file),
                    f".{os.path.basename(media_file)}.subtitle-repair-{repair_token}.json"
                )
                repair_active_path = os.path.join(
                    os.path.dirname(media_file),
                    f".{os.path.basename(media_file)}.subtitle-repair-active.json"
                )
                repair_manifest = {
                    "version": 1,
                    "media_file": os.path.abspath(media_file),
                    "state": "working",
                    "temporary": [],
                    "created": [],
                    "retired": []
                }
                self.__write_artifact_marker(repair_active_path, {
                    "media_file": os.path.abspath(media_file),
                    "transaction_id": repair_token,
                    "manifest_path": repair_manifest_path
                })

                def persist_repair_manifest():
                    self.__write_artifact_marker(
                        repair_manifest_path, repair_manifest
                    )

                def mark_repair_committed():
                    if repair_manifest.get("state") == "committed":
                        return
                    repair_manifest["state"] = "committed"
                    persist_repair_manifest()

                try:
                    guard_path(subtitle_file)
                    source_companion = os.path.splitext(subtitle_file)[0] + ".idx"
                    is_vobsub = sub_ext == ".sub" and os.path.isfile(source_companion)
                    if is_vobsub:
                        guard_path(source_companion)
                    size_limit = int(policy.get(
                        "vobsub_limit_mb" if is_vobsub else "text_file_limit_mb"
                    ) or (200 if is_vobsub else 20)) * 1024 * 1024
                    source_snapshot = self.__file_snapshot(subtitle_file)
                    companion_snapshot = self.__file_snapshot(source_companion) if is_vobsub else {}
                    source_size = os.path.getsize(subtitle_file)
                    companion_size = os.path.getsize(source_companion) if is_vobsub else 0
                    if source_size + companion_size > size_limit:
                        raise OSError(errno.EFBIG, "字幕修复项超过任务大小限制")
                    if consumed_bytes + source_size + companion_size > max_total_bytes:
                        limited = True
                        limit_reason = f"字幕修复累计大小达到 {max_total_bytes // (1024 * 1024)} MiB 限制"
                        stop_reason = "byte_limit"
                        break
                    reserve_bytes = int(policy.get("reserve_free_mb") or 1024) * 1024 * 1024
                    free_bytes = shutil.disk_usage(os.path.dirname(subtitle_file)).free
                    if free_bytes - (source_size + companion_size) * 2 < reserve_bytes:
                        raise OSError(errno.ENOSPC, "字幕修复目标卷空间不足，已保留安全余量")
                    persist_repair_manifest()
                    fd, work_file = tempfile.mkstemp(
                        prefix=".subtitle-repair-",
                        suffix=sub_ext,
                        dir=os.path.dirname(subtitle_file)
                    )
                    os.close(fd)
                    work_file_identity = self.__file_identity(work_file)
                    repair_manifest["temporary"].append({
                        "path": work_file,
                        "identity": work_file_identity
                    })
                    persist_repair_manifest()
                    source_digest = []
                    self.__copy_file_bounded(
                        subtitle_file, work_file, size_limit,
                        cancel_check=operation_abort,
                        digest_callback=source_digest.append
                    )
                    source_snapshot["content_hash"] = source_digest[0]
                    if not self.__file_snapshot_matches(subtitle_file, source_snapshot):
                        raise OSError(errno.EBUSY, "原字幕在复制期间已被其他进程修改")
                    if is_vobsub:
                        work_companion = os.path.splitext(work_file)[0] + ".idx"
                        companion_fd = os.open(
                            work_companion, os.O_CREAT | os.O_EXCL | os.O_WRONLY
                        )
                        os.close(companion_fd)
                        work_companion_identity = self.__file_identity(work_companion)
                        repair_manifest["temporary"].append({
                            "path": work_companion,
                            "identity": work_companion_identity
                        })
                        persist_repair_manifest()
                        companion_digest = []
                        self.__copy_file_bounded(
                            source_companion, work_companion,
                            size_limit - source_size,
                            cancel_check=operation_abort,
                            digest_callback=companion_digest.append
                        )
                        companion_snapshot["content_hash"] = companion_digest[0]
                        if not self.__file_snapshot_matches(
                                source_companion, companion_snapshot):
                            raise OSError(errno.EBUSY, "VobSub 索引在复制期间已被其他进程修改")
                    consumed_bytes += source_size + companion_size

                    operation = heavy_operation("interactive") if is_vobsub and heavy_operation else nullcontext()
                    with operation:
                        validation = SubtitleHealth.normalize_uploaded_subtitle(
                            work_file,
                            timeout_seconds=probe_timeout,
                            cancel_check=operation_abort
                        )
                    if validation.get("canceled"):
                        canceled = user_canceled()
                        if not canceled:
                            limited = True
                            limit_reason = limit_reason or "字幕修复达到累计处理时间限制"
                            stop_reason = stop_reason or "time_limit"
                        break
                    if not validation.get("valid"):
                        failures.append({
                            "path": subtitle_file,
                            "reason": validation.get("message") or "字幕内容无法解析"
                        })
                        continue

                    subtitle_profile = self.__guess_subtitle_profile(
                        os.path.basename(subtitle_file),
                        default_language=""
                    )
                    if not subtitle_profile.get("language"):
                        subtitle_profile["language"] = self.__infer_subtitle_language(
                            work_file,
                            validation
                        ) or "zh-CN"
                    subtitle_profile["source"] = self.__guess_subtitle_source(
                        os.path.basename(subtitle_file),
                        media_file
                    )

                    target_companion = ""
                    created_primary = False
                    created_companion = False
                    created_records = {}
                    pending_publications = {}

                    def remember_publication(path, expected_hash, identity, temporary_path="",
                                             temporary_hash=""):
                        key = os.path.normcase(os.path.abspath(path))
                        record = {
                            "path": path,
                            "hash": expected_hash,
                            "identity": identity or {},
                            "temporary_path": temporary_path or "",
                            "temporary_hash": temporary_hash or "",
                            "temporary_complete": bool(temporary_hash),
                            "published": False
                        }
                        pending_publications[key] = record
                        repair_manifest["created"] = [
                            item for item in repair_manifest.get("created") or []
                            if os.path.normcase(os.path.abspath(
                                str((item or {}).get("path") or ""))) != key
                        ] + [record]
                        repair_manifest["state"] = "publishing"
                        persist_repair_manifest()

                    def mark_created(path):
                        key = os.path.normcase(os.path.abspath(path))
                        record = pending_publications.get(key) or {}
                        if record:
                            record["published"] = True
                            created_records[key] = record
                            persist_repair_manifest()

                    def remove_created_targets():
                        for record in list(created_records.values()):
                            path = str(record.get("path") or "")
                            if not self.__owned_file_matches(path, record):
                                continue
                            try:
                                guard_path(path)
                                os.remove(path)
                            except OSError:
                                # A rollback must never broaden into deleting a
                                # path whose ownership or authorization changed.
                                continue

                    def restore_retired(backup_path, original_path, snapshot):
                        if not backup_path or not os.path.isfile(backup_path):
                            return False
                        if not self.__file_snapshot_matches(
                                backup_path, snapshot, verify_hash=True):
                            return False
                        guard_path(original_path)
                        self.__rename_no_replace(backup_path, original_path)
                        return self.__file_snapshot_matches(original_path, snapshot)

                    def restore_moved_file(backup_path, original_path, moved_snapshot):
                        """Restore whatever exact inode we moved, including a racing writer's file."""
                        if not backup_path or not os.path.isfile(backup_path):
                            return False
                        if not self.__file_snapshot_matches(backup_path, moved_snapshot):
                            return False
                        guard_path(original_path)
                        self.__rename_no_replace(backup_path, original_path)
                        return self.__file_snapshot_matches(original_path, moved_snapshot)

                    def retire_source(original_path, backup_path, snapshot):
                        # Check once before, then again on the inode moved to our
                        # unique backup. This closes the check/rename race: if a
                        # producer swaps the source inside os.replace, its file
                        # is restored instead of being discarded.
                        guard_path(original_path)
                        if not self.__file_snapshot_matches(original_path, snapshot):
                            raise OSError(errno.EBUSY, "原字幕已被其他进程修改，已停止替换")
                        retired_record = {
                            "original": original_path,
                            "backup": backup_path,
                            "snapshot": snapshot
                        }
                        repair_manifest["retired"] = [
                            item for item in repair_manifest.get("retired") or []
                            if os.path.normcase(os.path.abspath(
                                str((item or {}).get("original") or "")))
                            != os.path.normcase(os.path.abspath(original_path))
                        ] + [retired_record]
                        repair_manifest["state"] = "retiring"
                        persist_repair_manifest()
                        os.replace(original_path, backup_path)
                        moved_snapshot = self.__file_snapshot(backup_path)
                        retired_record["moved_snapshot"] = moved_snapshot
                        persist_repair_manifest()
                        if not self.__file_snapshot_matches(
                                backup_path, snapshot,
                                cancel_check=operation_abort,
                                verify_hash=True):
                            try:
                                restore_moved_file(
                                    backup_path, original_path, moved_snapshot
                                )
                            except OSError:
                                pass
                            raise OSError(errno.EBUSY, "原字幕在替换瞬间发生变化，已停止替换")

                    def cleanup_retired(backup_path, snapshot):
                        if not backup_path or not os.path.isfile(backup_path):
                            return True
                        if not self.__file_snapshot_matches(
                                backup_path, snapshot, verify_hash=True):
                            return False
                        guard_path(backup_path)
                        os.remove(backup_path)
                        return True

                    if SubtitleHealth.language_defined(subtitle_file, media_file, server_type):
                        target_subtitle = subtitle_file
                        if is_vobsub:
                            # Binary VobSub bytes are not normalized in-place;
                            # the validated original pair is already canonical.
                            target_companion = source_companion
                        else:
                            backup_token = uuid.uuid4().hex
                            backup_primary = os.path.join(
                                os.path.dirname(subtitle_file),
                                f".{os.path.basename(subtitle_file)}.subtitle-repair-old-{backup_token}"
                            )
                            work_hash = self.__file_sha256(
                                work_file, cancel_check=operation_abort
                            )
                            work_identity = self.__file_identity(work_file)
                            retire_source(subtitle_file, backup_primary, source_snapshot)
                            try:
                                created_record = {
                                    "path": subtitle_file,
                                    "hash": work_hash,
                                    "identity": work_identity,
                                    "temporary_path": work_file,
                                    "published": False
                                }
                                repair_manifest["created"].append(created_record)
                                persist_repair_manifest()
                                guard_path(subtitle_file)
                                self.__rename_no_replace(work_file, subtitle_file)
                                created_record["published"] = True
                                created_records[
                                    os.path.normcase(os.path.abspath(subtitle_file))
                                ] = created_record
                                persist_repair_manifest()
                                work_file = ""
                            except Exception:
                                try:
                                    restore_retired(backup_primary, subtitle_file, source_snapshot)
                                except OSError:
                                    pass
                                raise
                            mark_repair_committed()
                            if not cleanup_retired(backup_primary, source_snapshot):
                                failures.append({
                                    "path": subtitle_file,
                                    "reason": "规范字幕已生效，但旧隐藏备份身份异常，未自动删除"
                                })
                    else:
                        target_subtitle = self.__build_subtitle_path(
                            media_file,
                            subtitle_profile,
                            sub_ext,
                            server_type
                        )
                        if is_vobsub:
                            target_companion = os.path.splitext(target_subtitle)[0] + ".idx"
                            try:
                                guard_path(target_companion)
                                created_companion = self.__publish_file_no_replace(
                                    work_companion, target_companion,
                                    cancel_check=operation_abort,
                                    ownership_intent=remember_publication,
                                    expected_hash=companion_snapshot.get("content_hash")
                                )
                                if created_companion:
                                    mark_created(target_companion)
                                guard_path(target_subtitle)
                                created_primary = self.__publish_file_no_replace(
                                    work_file, target_subtitle,
                                    cancel_check=operation_abort,
                                    ownership_intent=remember_publication,
                                    expected_hash=source_snapshot.get("content_hash")
                                )
                                if created_primary:
                                    mark_created(target_subtitle)
                            except Exception:
                                remove_created_targets()
                                raise
                        else:
                            target_hash = self.__file_sha256(
                                work_file, cancel_check=operation_abort
                            )
                            guard_path(target_subtitle)
                            created_primary = self.__publish_file_no_replace(
                                work_file, target_subtitle,
                                cancel_check=operation_abort,
                                ownership_intent=remember_publication,
                                expected_hash=target_hash
                            )
                            if created_primary:
                                mark_created(target_subtitle)

                        repaired_inspection = inspect(target_subtitle)
                        if repaired_inspection.get("canceled"):
                            canceled = user_canceled()
                            if not canceled:
                                limited = True
                                limit_reason = limit_reason or "字幕修复达到累计处理时间限制"
                            remove_created_targets()
                            break
                        if repaired_inspection.get("status") != "ok":
                            remove_created_targets()
                            failures.append({
                                "path": subtitle_file,
                                "reason": repaired_inspection.get("reason") or "规范化后仍无法识别"
                            })
                            continue
                        if is_vobsub:
                            # The new pair is already complete. Retire the old
                            # pair through same-directory atomic renames before
                            # deleting it, so a failure between the two
                            # components can never make us discard the only
                            # complete VobSub pair.
                            backup_token = uuid.uuid4().hex
                            backup_primary = os.path.join(
                                os.path.dirname(subtitle_file),
                                f".{os.path.basename(subtitle_file)}.subtitle-repair-old-{backup_token}"
                            )
                            backup_companion = os.path.join(
                                os.path.dirname(source_companion),
                                f".{os.path.basename(source_companion)}.subtitle-repair-old-{backup_token}"
                            )
                            moved_primary = False
                            moved_companion = False
                            try:
                                retire_source(
                                    source_companion, backup_companion, companion_snapshot
                                )
                                moved_companion = True
                                retire_source(
                                    subtitle_file, backup_primary, source_snapshot
                                )
                                moved_primary = True
                            except OSError as e:
                                restore_errors = []
                                for moved, backup_path, original_path, snapshot in [
                                    (moved_primary, backup_primary, subtitle_file, source_snapshot),
                                    (moved_companion, backup_companion, source_companion,
                                     companion_snapshot)
                                ]:
                                    if not moved or not os.path.exists(backup_path):
                                        continue
                                    try:
                                        restore_retired(backup_path, original_path, snapshot)
                                    except OSError as restore_error:
                                        restore_errors.append(str(restore_error))
                                old_pair_restored = self.__file_snapshot_matches(
                                    subtitle_file, source_snapshot
                                ) and self.__file_snapshot_matches(
                                    source_companion, companion_snapshot
                                )
                                if old_pair_restored:
                                    remove_created_targets()
                                    failures.append({
                                        "path": subtitle_file,
                                        "reason": f"替换原 VobSub 字幕对失败：{str(e)}"
                                    })
                                    continue

                                # Restoration could not be proven. Keep the
                                # already validated new pair; rolling it back
                                # here would leave only an orphaned old
                                # component. Report partial cleanup instead.
                                failures.append({
                                    "path": subtitle_file,
                                    "reason": "旧 VobSub 字幕对清理不完整，已保留新的完整字幕对："
                                    f"{str(e)}"
                                    + (f"；恢复失败：{'；'.join(restore_errors)}"
                                       if restore_errors else "")
                                })
                            else:
                                mark_repair_committed()
                                cleanup_errors = []
                                for backup_path, snapshot in [
                                    (backup_primary, source_snapshot),
                                    (backup_companion, companion_snapshot)
                                ]:
                                    try:
                                        if not cleanup_retired(backup_path, snapshot):
                                            cleanup_errors.append(
                                                f"{os.path.basename(backup_path)} 身份已变化"
                                            )
                                    except OSError as cleanup_error:
                                        cleanup_errors.append(str(cleanup_error))
                                if cleanup_errors:
                                    failures.append({
                                        "path": subtitle_file,
                                        "reason": "新 VobSub 字幕对已生效，但旧隐藏备份清理失败："
                                        + "；".join(cleanup_errors)
                                    })
                        else:
                            backup_token = uuid.uuid4().hex
                            backup_primary = os.path.join(
                                os.path.dirname(subtitle_file),
                                f".{os.path.basename(subtitle_file)}.subtitle-repair-old-{backup_token}"
                            )
                            try:
                                retire_source(
                                    subtitle_file, backup_primary, source_snapshot
                                )
                            except OSError as e:
                                remove_created_targets()
                                failures.append({
                                    "path": subtitle_file,
                                    "reason": f"替换原字幕失败：{str(e)}"
                                })
                                continue
                            mark_repair_committed()
                            try:
                                if not cleanup_retired(backup_primary, source_snapshot):
                                    failures.append({
                                        "path": subtitle_file,
                                        "reason": "新字幕已生效，但旧隐藏备份身份异常，未自动删除"
                                    })
                            except OSError as cleanup_error:
                                failures.append({
                                    "path": subtitle_file,
                                    "reason": "新字幕已生效，但旧隐藏备份清理失败："
                                    + str(cleanup_error)
                                })

                    mark_repair_committed()
                    processed.append({
                        "source": subtitle_file,
                        "target": target_subtitle,
                        "source_companion": source_companion if is_vobsub else "",
                        "target_companion": target_companion,
                        "language": subtitle_profile.get("language") or "",
                        "source_name": subtitle_profile.get("source") or ""
                    })
                except InterruptedError:
                    canceled = user_canceled()
                    if not canceled:
                        limited = True
                        limit_reason = limit_reason or "字幕修复达到累计处理时间限制"
                        stop_reason = stop_reason or "time_limit"
                    break
                except (FileExistsError, OSError) as e:
                    failures.append({"path": subtitle_file, "reason": str(e)})
                finally:
                    if work_file and self.__same_file_identity(
                            work_file, work_file_identity):
                        try:
                            os.remove(work_file)
                        except OSError:
                            pass
                    if work_companion and self.__same_file_identity(
                            work_companion, work_companion_identity):
                        try:
                            os.remove(work_companion)
                        except OSError:
                            pass
                    if repair_active_path and os.path.isfile(repair_active_path):
                        last_recovery_ok = self.__recover_repair_artifacts(
                            media_file, path_guard_check=path_guard_check
                        )

            if not last_recovery_ok and not limited:
                limited = True
                stop_reason = "transaction_pending"
                limit_reason = "字幕修复事务清理未完成，已停止后续项目"
                failures.append({"path": media_file, "reason": limit_reason})
            if item_limit_reached and not canceled and not limited:
                limited = True
                stop_reason = "item_limit"
                limit_reason = f"字幕修复最多处理 {max_items} 个逻辑字幕"

            try:
                final_cancel_check = operation_abort \
                    if cancel_check is not None or remaining_budget is not None else None
                results = [] if canceled or limited else SubtitleHealth.inspect_media_subtitles(
                    media_file, server_type,
                    cancel_check=final_cancel_check,
                    probe_timeout_seconds=probe_timeout,
                    heavy_operation=heavy_operation
                )
            except InterruptedError:
                canceled = True
                results = []
            if any(result.get("canceled") for result in results):
                canceled = True
            aggregate = SubtitleHealth.aggregate_media_subtitles(results)
            data = {
                "media_file": media_file,
                "server": server_type,
                "processed": processed,
                "skipped": skipped,
                "failures": failures,
                "subtitle_count": len(results),
                "status": aggregate.get("status") or "",
                "reason": aggregate.get("reason") or "",
                "canceled": canceled,
                "partial": limited,
                "stop_reason": stop_reason,
                "limit_reason": limit_reason,
                "processed_bytes": consumed_bytes,
                "max_items": max_items,
                "max_total_bytes": max_total_bytes
            }
            if canceled:
                if processed:
                    return True, f"任务已取消，已完成 {len(processed)} 个外挂字幕", data
                return False, "字幕二次处理已取消", data
            if limited:
                reason = limit_reason or "字幕修复达到资源限制"
                if processed:
                    return True, f"已完成 {len(processed)} 个外挂字幕，{reason}", data
                return False, reason, data
            if not processed:
                reason = failures[0].get("reason") if failures else "现有字幕已符合规范"
                return False, f"没有可完成二次处理的字幕：{reason}", data
            message = f"已完成 {len(processed)} 个外挂字幕的二次处理"
            if len(results) > 1:
                message += f"，保留 {len(results)} 个可独立选择的字幕轨道"
            if failures:
                message += f"，另有 {len(failures)} 个字幕处理失败"
            return True, message, data

    @classmethod
    def __guess_subtitle_profile(cls, file_name, default_language="zh-CN"):
        """
        根据字幕文件名识别语言标签和字幕标记
        匹配优先级：简体中文 > 繁体中文 > 英文 > 通用 ISO/BCP-47 标签 > 默认语言
        zh-CN 优先级最高，避免文件名同时含中英文标记（如 .chinese.eng.）时误判为英文
        """
        name = file_name or ""
        _zhcn_sub_re = r"([.\[(](((zh[-_])?(cn|ch[si]|sg|sc))|zho?|chi|chinese" \
                       r"|简[体中]?)[.\])])|中文字幕|简体|简中"
        _zhtw_sub_re = r"([.\[(](((zh[-_])?(hk|tw|cht|tc))|繁[体中]?)[.\])])" \
                       r"|繁体中[文字]|中[文字]繁体|繁体|繁中"
        _eng_sub_re = r"([.\[(](en|eng|english)[.\])])|英文"
        if re.search(_zhcn_sub_re, name, re.I):
            language = "zh-CN"
        elif re.search(_zhtw_sub_re, name, re.I):
            language = "zh-TW"
        elif re.search(_eng_sub_re, name, re.I):
            language = "eng"
        else:
            language = ""
            base_name = os.path.splitext(os.path.basename(name))[0]
            for token in reversed(re.split(r"[.\s\[\](){}]+", base_name)):
                language = cls.__normalize_language_token(token)
                if language:
                    break
            if not language:
                language = default_language
        # 识别字幕特殊标记（forced / SDH / CC）
        lower_name = name.lower()
        flags = []
        if re.search(r"(^|[.\-_\[( ])forced($|[.\-_\]) ])", lower_name):
            flags.append("forced")
        if re.search(r"(^|[.\-_\[( ])sdh($|[.\-_\]) ])", lower_name):
            flags.append("sdh")
        elif re.search(r"(^|[.\-_\[( ])cc($|[.\-_\]) ])", lower_name):
            flags.append("cc")
        return {"language": language, "flags": flags}

    @classmethod
    def __guess_subtitle_source(cls, file_name, media_file):
        """从同名字幕后缀提取来源标题，供 Jellyfin 区分同语种多字幕。"""
        subtitle_base = os.path.splitext(os.path.basename(file_name or ""))[0]
        media_base = os.path.splitext(os.path.basename(media_file or ""))[0]
        if not subtitle_base or not media_base \
                or not subtitle_base.casefold().startswith(media_base.casefold()):
            return ""
        suffix = subtitle_base[len(media_base):].strip(" .-_[]()")
        if not suffix:
            return ""
        ignored = {
            "zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "cn", "tw", "chi", "zho", "chs", "cht",
            "en", "eng", "english", "chinese", "forced", "foreign", "sdh", "cc", "hi",
            "subtitle", "sub", "简", "简中", "简体", "繁", "繁中", "繁体", "中文", "中文字幕"
        }
        tokens = []
        for token in re.split(r"[.\s\[\](){}]+", suffix):
            token = token.strip(" .-_")
            if not token or token.casefold() in ignored or cls.__normalize_language_token(token):
                continue
            tokens.append(token)
        source = "-".join(tokens)
        source = re.sub(r"[^\w-]+", "-", source, flags=re.UNICODE).strip("-_")
        return source[:32]

    @staticmethod
    def __normalize_language_token(token):
        token = str(token or "").strip().lower().replace("_", "-")
        if not token:
            return ""
        primary, _, region = token.partition("-")
        if primary not in SubtitleHealth._iso639_1_tokens \
                and token not in SubtitleHealth._jellyfin_language_tokens:
            return ""
        if region and re.fullmatch(r"[a-z0-9]{2,8}", region):
            region = region.upper() if len(region) == 2 and region.isalpha() else region
            return f"{primary}-{region}"
        return token

    @classmethod
    def __infer_subtitle_language(cls, subtitle_file, validation=None):
        """在文件名没有语言标记时，从规范化后的字幕正文推断常见语言。"""
        encoding = str((validation or {}).get("encoding") or "").lower().replace("-", "")
        if "big5" in encoding:
            return "zh-TW"
        try:
            with open(subtitle_file, "r", encoding="utf-8-sig", errors="ignore") as file_obj:
                text = file_obj.read(512 * 1024)
        except OSError:
            return ""
        if not text:
            return ""

        ext = os.path.splitext(subtitle_file)[-1].lower()
        if ext in [".ass", ".ssa"]:
            dialogue = []
            for line in text.splitlines():
                if line.lstrip().lower().startswith("dialogue:"):
                    dialogue.append(line.split(",", 9)[-1])
            if dialogue:
                text = "\n".join(dialogue)
        text = re.sub(r"\{[^}]*}|<[^>]*>|\\[NnH]", " ", text)

        if len(re.findall(r"[\u3040-\u30ff]", text)) >= 3:
            return "jpn"
        if len(re.findall(r"[\uac00-\ud7af]", text)) >= 3:
            return "kor"

        chinese_chars = re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text)
        if len(chinese_chars) >= 3:
            traditional_hits = sum(char in cls._traditional_chinese_chars for char in chinese_chars)
            simplified_hits = sum(char in cls._simplified_chinese_chars for char in chinese_chars)
            return "zh-TW" if traditional_hits > simplified_hits else "zh-CN"

        if len(re.findall(r"[\u0400-\u04ff]", text)) >= 3:
            return "rus"
        if len(re.findall(r"[\u0370-\u03ff]", text)) >= 3:
            return "gre"
        if len(re.findall(r"[\u0590-\u05ff]", text)) >= 3:
            return "heb"
        if len(re.findall(r"[\u0600-\u06ff]", text)) >= 3:
            return "ara"
        if len(re.findall(r"[\u0e00-\u0e7f]", text)) >= 3:
            return "tha"
        if len(re.findall(r"[a-zA-Z]", text)) >= 20:
            return "eng"
        return ""

    @classmethod
    def __build_subtitle_path(cls, media_file, subtitle_profile, sub_ext, server_type=None, companion_exts=None):
        """
        生成不覆盖已有文件的外挂字幕路径。
        命名格式：
          - Emby:          Movie.zh-CN.srt → Movie.zh-CN(1).srt → ...
          - Jellyfin:      Movie.chi.zh-cn.srt → Movie.source-2.chi.zh-cn.srt → ...
          - Plex:          Movie.zh-CN.srt → Movie(1).zh-CN.srt → ...
                           (forced/sdh/cc 标记拼接在语言标签后: Movie.en.forced.sdh.srt)
        重名时最多尝试 100 个编号，超过则抛出 FileExistsError。
        """
        media_base = os.path.splitext(media_file)[0]
        suffix_parts = [cls.__subtitle_language_tag(subtitle_profile.get("language"), server_type)]
        normalized_server = str(server_type or "").lower()
        is_plex = normalized_server == "plex"
        is_jellyfin = normalized_server == "jellyfin"
        if normalized_server in ["plex", "jellyfin"]:
            suffix_parts.extend(subtitle_profile.get("flags") or [])
        suffix = ".".join([part for part in suffix_parts if part])
        source = str(subtitle_profile.get("source") or "").strip(" .-_")
        target = f"{media_base}.{source}.{suffix}{sub_ext}" if is_jellyfin and source \
            else f"{media_base}.{suffix}{sub_ext}"
        companion_exts = [str(ext).lower() for ext in (companion_exts or []) if ext]

        def available(path):
            if os.path.exists(path):
                return False
            base = os.path.splitext(path)[0]
            return not any(os.path.exists(base + ext) for ext in companion_exts)

        if available(target):
            return target
        for index in range(1, 100):
            if is_plex:
                target = f"{media_base}({index}).{suffix}{sub_ext}"
            elif is_jellyfin:
                source_name = f"{source}-{index + 1}" if source else f"source-{index + 1}"
                target = f"{media_base}.{source_name}.{suffix}{sub_ext}"
            else:
                target = f"{media_base}.{suffix}({index}){sub_ext}"
            if available(target):
                return target
        raise FileExistsError("字幕文件重名过多")

    @classmethod
    def __subtitle_language_tag(cls, language_tag, server_type=None):
        """
        按媒体服务器规范转换外挂字幕语言标签。
        Jellyfin 使用 ISO 639-2 语言码并保留简繁标题，Plex 使用 ISO 639-1，Emby保留原始标签。
        """
        language_tag = language_tag or "zh-CN"
        server_type = str(server_type or "").lower()
        if server_type == "jellyfin":
            jellyfin_tags = {
                "zh-CN": "chi.zh-cn",
                "zh-TW": "chi.zh-tw",
                "eng": "eng"
            }
            if language_tag in jellyfin_tags:
                return jellyfin_tags[language_tag]
            normalized = language_tag.lower().replace("_", "-")
            primary, _, region = normalized.partition("-")
            jellyfin_language = cls._jellyfin_iso639_2.get(primary, primary)
            if region:
                return f"{jellyfin_language}.{normalized}"
            return jellyfin_language
        if server_type != "plex":
            return language_tag
        plex_tags = {
            "zh-CN": "zh-CN",
            "zh-TW": "zh-TW",
            "eng": "en"
        }
        return plex_tags.get(language_tag, language_tag.lower())

    @staticmethod
    def __save_upload_file_exclusive(upload_file, target_file):
        """
        独占创建并保存上传文件，避免并发上传覆盖同名字幕。
        """
        fd = None
        try:
            fd = os.open(target_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "wb") as file_obj:
                fd = None
                upload_file.save(file_obj)
        except OSError as e:
            if e.errno == errno.EEXIST:
                raise FileExistsError(target_file)
            if fd is not None:
                os.close(fd)
            if os.path.exists(target_file):
                try:
                    os.remove(target_file)
                except Exception:
                    pass
            raise
        except Exception:
            if fd is not None:
                os.close(fd)
            if os.path.exists(target_file):
                try:
                    os.remove(target_file)
                except Exception:
                    pass
            raise

    @staticmethod
    def __file_sha256(path, cancel_check=None):
        digest = hashlib.sha256()
        with open(path, "rb") as file_obj:
            while True:
                if cancel_check and cancel_check():
                    raise InterruptedError("任务已取消")
                chunk = file_obj.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def __atomic_copy_file(cls, source, target, cancel_check=None):
        """Copy to a sibling hidden file, fsync it and atomically replace a derived artifact."""
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        tmp_path = f"{target}.tmp-{uuid.uuid4().hex}"
        digest = hashlib.sha256()
        try:
            with open(source, "rb") as source_obj, open(tmp_path, "xb") as target_obj:
                while True:
                    if cancel_check and cancel_check():
                        raise InterruptedError("任务已取消")
                    chunk = source_obj.read(1024 * 1024)
                    if not chunk:
                        break
                    target_obj.write(chunk)
                    digest.update(chunk)
                target_obj.flush()
                os.fsync(target_obj.fileno())
            os.replace(tmp_path, target)
            tmp_path = ""
            return digest.hexdigest()
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def __read_artifact_marker(path):
        try:
            with open(path, "r", encoding="utf-8") as marker_obj:
                value = json.load(marker_obj)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @classmethod
    def __write_artifact_marker(cls, path, value):
        target_dir = os.path.dirname(os.path.abspath(path))
        os.makedirs(target_dir, exist_ok=True)
        tmp_path = f"{path}.tmp-{uuid.uuid4().hex}"
        try:
            with open(tmp_path, "x", encoding="utf-8", newline="\n") as marker_obj:
                json.dump(value, marker_obj, ensure_ascii=False, sort_keys=True)
                marker_obj.flush()
                os.fsync(marker_obj.fileno())
            os.replace(tmp_path, path)
            tmp_path = ""
            try:
                dir_fd = os.open(target_dir, getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @classmethod
    def __recover_repair_manifest(cls, marker_path, media_file,
                                  path_guard_check=None):
        """Safely finish or roll back one interrupted repair transaction."""
        manifest = cls.__read_artifact_marker(marker_path)
        media_dir = os.path.abspath(os.path.dirname(media_file))
        if not manifest or os.path.normcase(os.path.abspath(
                str(manifest.get("media_file") or ""))) != os.path.normcase(
                    os.path.abspath(media_file)):
            return False

        def allowed(path):
            if not path:
                return False
            absolute = os.path.abspath(path)
            if os.path.normcase(os.path.dirname(absolute)) != os.path.normcase(media_dir):
                return False
            if path_guard_check and path_guard_check(absolute) is False:
                return False
            return True

        committed = str(manifest.get("state") or "") == "committed"
        unresolved = False

        # Remove the exact new files only when the transaction never crossed
        # its durable commit boundary. Publication intents are recorded before
        # copying, so this also covers a stop immediately after link/rename.
        if not committed:
            for record in manifest.get("created") or []:
                path = str((record or {}).get("path") or "")
                if not allowed(path):
                    unresolved = True
                    continue
                if cls.__owned_file_matches(path, record):
                    try:
                        os.remove(path)
                    except OSError:
                        unresolved = True

        # A non-committed repair restores retired sources; a committed repair
        # only removes its exact old backups.
        for record in manifest.get("retired") or []:
            original = str((record or {}).get("original") or "")
            backup = str((record or {}).get("backup") or "")
            snapshot = (record or {}).get("snapshot") or {}
            moved_snapshot = (record or {}).get("moved_snapshot") or snapshot
            if not allowed(original) or not allowed(backup):
                unresolved = True
                continue
            if not os.path.isfile(backup):
                continue
            cleanup_snapshot = snapshot if committed else moved_snapshot
            if not cls.__file_snapshot_matches(
                    backup, cleanup_snapshot, verify_hash=committed):
                unresolved = True
                continue
            try:
                if committed:
                    os.remove(backup)
                elif not os.path.exists(original):
                    cls.__rename_no_replace(backup, original)
                elif cls.__file_snapshot_matches(original, moved_snapshot):
                    os.remove(backup)
                else:
                    unresolved = True
            except OSError:
                unresolved = True

        # Work copies and publish temps carry exact inode identities. Never
        # delete a same-named file after another process has replaced it.
        temporary_records = list(manifest.get("temporary") or [])
        for record in manifest.get("created") or []:
            temporary_path = str((record or {}).get("temporary_path") or "")
            if temporary_path:
                temporary_records.append({
                    "path": temporary_path,
                    "identity": (record or {}).get("identity") or {}
                })
        for record in temporary_records:
            path = str((record or {}).get("path") or "")
            if not path or not os.path.exists(path):
                continue
            if not allowed(path):
                unresolved = True
                continue
            if cls.__same_file_identity(path, (record or {}).get("identity") or {}):
                try:
                    os.remove(path)
                except OSError:
                    unresolved = True
            else:
                unresolved = True

        if not unresolved:
            try:
                os.remove(marker_path)
            except OSError:
                return False
            return True
        return False

    @classmethod
    def __recover_repair_artifacts(cls, media_file, path_guard_check=None):
        media_dir = os.path.abspath(os.path.dirname(media_file))
        media_name = os.path.basename(media_file)
        active_path = os.path.join(
            media_dir, f".{media_name}.subtitle-repair-active.json"
        )
        if not os.path.exists(active_path):
            return True
        pointer = cls.__read_artifact_marker(active_path)
        marker_path = os.path.abspath(str(pointer.get("manifest_path") or "")) \
            if pointer else ""
        expected_prefix = f".{media_name}.subtitle-repair-"
        if not marker_path \
                or os.path.normcase(os.path.dirname(marker_path)) != os.path.normcase(media_dir) \
                or not os.path.basename(marker_path).startswith(expected_prefix) \
                or not marker_path.endswith(".json"):
            return False
        if not os.path.isfile(marker_path):
            try:
                os.remove(active_path)
            except OSError:
                return False
            return True
        if cls.__recover_repair_manifest(
                marker_path, media_file,
                path_guard_check=path_guard_check):
            try:
                os.remove(active_path)
            except OSError:
                return False
            return True
        return False

    @classmethod
    def __artifact_marker_valid(cls, marker, expected_inputs, output_paths,
                                cancel_check=None):
        if not marker or marker.get("inputs") != expected_inputs:
            return False
        outputs = marker.get("outputs") or {}
        try:
            for path in output_paths:
                if not path or not os.path.isfile(path) \
                        or outputs.get(path) != cls.__file_sha256(
                            path, cancel_check=cancel_check
                        ):
                    return False
            return True
        except InterruptedError:
            raise
        except OSError:
            return False

    @classmethod
    def __publish_file_no_replace(cls, source, target, cancel_check=None,
                                  ownership_intent=None, expected_hash=None):
        """Publish a verified sibling temp file without overwriting an existing subtitle.

        Returns ``True`` only when this call created the destination.  An
        already-published file with the same hash is treated as a successful
        recovery and returns ``False``.
        """
        source_hash = str(expected_hash or "") or cls.__file_sha256(
            source, cancel_check=cancel_check
        )
        if os.path.exists(target):
            if os.path.isfile(target) and cls.__file_sha256(
                    target, cancel_check=cancel_check) == source_hash:
                return False
            raise FileExistsError(f"目标字幕文件已存在：{target}")
        target_dir = os.path.dirname(os.path.abspath(target))
        if not os.path.isdir(target_dir):
            raise FileNotFoundError(f"字幕目标目录不存在：{target_dir}")
        hidden_path = os.path.join(
            target_dir,
            f".{os.path.basename(target)}.subtitle-task-{uuid.uuid4().hex}.tmp"
        )
        created = False
        try:
            digest = hashlib.sha256()
            if ownership_intent:
                # Record the unpredictable path before it can contain data.
                ownership_intent(target, source_hash, {}, hidden_path, "")
            with open(source, "rb") as source_obj, open(hidden_path, "xb") as target_obj:
                intent_identity = cls.__file_identity(hidden_path)
                if ownership_intent:
                    # Persist the exact temporary inode before the first large
                    # write. A restart can then remove an incomplete copy
                    # without touching an unrelated file with the same name.
                    ownership_intent(
                        target, source_hash, intent_identity, hidden_path, ""
                    )
                while True:
                    if cancel_check and cancel_check():
                        raise InterruptedError("任务已取消")
                    chunk = source_obj.read(1024 * 1024)
                    if not chunk:
                        break
                    target_obj.write(chunk)
                    digest.update(chunk)
                target_obj.flush()
                os.fsync(target_obj.fileno())
            if digest.hexdigest() != source_hash:
                raise IOError("字幕发布前哈希校验失败")
            if ownership_intent:
                ownership_intent(
                    target, source_hash, intent_identity, hidden_path, source_hash
                )
            try:
                os.link(hidden_path, target)
                created = True
            except OSError as link_error:
                if os.path.exists(target):
                    if os.path.isfile(target) and cls.__file_sha256(
                            target, cancel_check=cancel_check) == source_hash:
                        return False
                    raise FileExistsError(f"目标字幕文件已存在：{target}") from link_error
                try:
                    if cancel_check and cancel_check():
                        raise InterruptedError("任务已取消")
                    cls.__rename_no_replace(hidden_path, target)
                    created = True
                except OSError as rename_error:
                    if os.path.exists(target):
                        if os.path.isfile(target) and cls.__file_sha256(
                                target, cancel_check=cancel_check) == source_hash:
                            return False
                        raise FileExistsError(f"目标字幕文件已存在：{target}") from rename_error
                    raise OSError(
                        getattr(rename_error, "errno", errno.ENOTSUP),
                        "目标文件系统不支持原子且不覆盖的字幕发布"
                    ) from link_error
            try:
                dir_fd = os.open(target_dir, getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
            return created
        finally:
            if os.path.exists(hidden_path):
                try:
                    os.remove(hidden_path)
                except OSError:
                    pass

    @staticmethod
    def __rename_no_replace(source, target):
        """Atomically rename ``source`` only when ``target`` does not exist."""
        if os.name == "nt":
            # Win32 MoveFile semantics used by os.rename do not replace an
            # existing destination.
            os.rename(source, target)
            return
        try:
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2")
            renameat2.argtypes = [
                ctypes.c_int, ctypes.c_char_p,
                ctypes.c_int, ctypes.c_char_p,
                ctypes.c_uint
            ]
            renameat2.restype = ctypes.c_int
            # Linux: AT_FDCWD and RENAME_NOREPLACE.
            if renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1) != 0:
                error_number = ctypes.get_errno()
                raise OSError(error_number, os.strerror(error_number), target)
        except AttributeError as error:
            raise OSError(errno.ENOTSUP, "renameat2(RENAME_NOREPLACE) 不可用") from error

    @staticmethod
    def __file_identity(path):
        try:
            file_stat = os.stat(path, follow_symlinks=False)
            return {
                "device": int(file_stat.st_dev),
                "inode": int(file_stat.st_ino)
            }
        except (OSError, ValueError, TypeError):
            return {}

    @classmethod
    def __same_file_identity(cls, path, expected_identity):
        expected = {
            "device": int((expected_identity or {}).get("device") or 0),
            "inode": int((expected_identity or {}).get("inode") or 0)
        }
        return expected["inode"] > 0 and cls.__file_identity(path) == expected

    @classmethod
    def __owned_file_matches(cls, path, record):
        """Return true only for the exact inode and bytes recorded as ours."""
        identity = (record or {}).get("identity") or {}
        expected_hash = str((record or {}).get("hash") or "")
        if not path or not expected_hash or not cls.__same_file_identity(path, identity):
            return False
        try:
            if cls.__file_sha256(path) != expected_hash:
                return False
        except OSError:
            return False
        # Close the replacement race between hash verification and deletion.
        return cls.__same_file_identity(path, identity)

    @staticmethod
    def __file_snapshot(path, content_hash=""):
        file_stat = os.stat(path, follow_symlinks=False)
        return {
            "path": os.path.abspath(path),
            "identity": {
                "device": int(file_stat.st_dev),
                "inode": int(file_stat.st_ino)
            },
            "size": int(file_stat.st_size),
            "mtime_ns": int(getattr(file_stat, "st_mtime_ns", file_stat.st_mtime * 1e9)),
            "content_hash": str(content_hash or "")
        }

    @classmethod
    def __file_snapshot_matches(cls, path, snapshot, cancel_check=None,
                                verify_hash=False):
        """Compare a path with the immutable source state captured for repair."""
        try:
            current = cls.__file_snapshot(path)
        except OSError:
            return False
        expected_size = (snapshot or {}).get("size")
        expected_mtime = (snapshot or {}).get("mtime_ns")
        if expected_size is None or expected_mtime is None \
                or current["size"] != int(expected_size) \
                or current["mtime_ns"] != int(expected_mtime):
            return False
        expected_identity = (snapshot or {}).get("identity") or {}
        stable_identity = int(expected_identity.get("inode") or 0) > 0
        if stable_identity and current["identity"] != {
                "device": int(expected_identity.get("device") or 0),
                "inode": int(expected_identity.get("inode") or 0)}:
            return False
        expected_hash = str((snapshot or {}).get("content_hash") or "")
        if expected_hash and (verify_hash or not stable_identity):
            try:
                return cls.__file_sha256(
                    path, cancel_check=cancel_check
                ) == expected_hash
            except InterruptedError:
                raise
            except OSError:
                return False
        return stable_identity or bool(expected_hash)

    @staticmethod
    def __copy_subtitle_exclusive(source_sub_file, target_sub_file):
        """
        独占复制字幕，目标存在时直接失败，避免硬链接降级复制时覆盖其它请求刚写入的字幕。
        """
        fd = None
        try:
            fd = os.open(target_sub_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with open(os.path.normpath(source_sub_file), "rb") as src_obj:
                with os.fdopen(fd, "wb") as dest_obj:
                    fd = None
                    shutil.copyfileobj(src_obj, dest_obj)
            shutil.copystat(os.path.normpath(source_sub_file), os.path.normpath(target_sub_file))
            return 0, ""
        except OSError as e:
            if fd is not None:
                os.close(fd)
            if e.errno == errno.EEXIST:
                return -1, "目标字幕文件已存在"
            if os.path.exists(target_sub_file):
                try:
                    os.remove(target_sub_file)
                except Exception:
                    pass
            ExceptionUtils.exception_traceback(e)
            return -1, str(e)
        except Exception as e:
            if fd is not None:
                os.close(fd)
            if os.path.exists(target_sub_file):
                try:
                    os.remove(target_sub_file)
                except Exception:
                    pass
            ExceptionUtils.exception_traceback(e)
            return -1, str(e)

    @staticmethod
    def __copy_file_bounded(source, target, max_bytes, cancel_check=None,
                            digest_callback=None):
        """Copy one repair component with a hard byte cap and cancel points."""
        copied = 0
        limit = max(int(max_bytes or 0), 0)
        digest = hashlib.sha256()
        with open(source, "rb") as source_obj, open(target, "wb") as target_obj:
            while True:
                if cancel_check and cancel_check():
                    raise InterruptedError("任务已取消")
                chunk = source_obj.read(1024 * 1024)
                if not chunk:
                    break
                copied += len(chunk)
                if copied > limit:
                    raise OSError(errno.EFBIG, "字幕修复组件超过任务大小限制")
                target_obj.write(chunk)
                digest.update(chunk)
            target_obj.flush()
            os.fsync(target_obj.fileno())
        if digest_callback:
            digest_callback(digest.hexdigest())
        return copied

    @classmethod
    def __sync_manual_subtitle(cls, source_sub_file, target_sub_file, rmt_mode=None):
        """
        根据整理模式同步手动上传字幕（链接/复制）
        硬链接失败时自动降级为复制，兼容跨文件系统场景
        """
        try:
            target_dir = os.path.dirname(target_sub_file)
            if target_dir and not os.path.exists(target_dir):
                os.makedirs(target_dir)
            if rmt_mode == RmtMode.LINK:
                retcode, retmsg = SystemUtils.link(source_sub_file, target_sub_file)
                if retcode != 0:
                    return cls.__copy_subtitle_exclusive(source_sub_file, target_sub_file)
                return 0, ""
            if rmt_mode == RmtMode.SOFTLINK:
                return SystemUtils.softlink(source_sub_file, target_sub_file)
            return cls.__copy_subtitle_exclusive(source_sub_file, target_sub_file)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return -1, str(e)

    def __search_opensubtitles(self, item):
        """
        爬取OpenSubtitles.org字幕
        """
        if not self.opensubtitles:
            return []
        return self.opensubtitles.search_subtitles(item)

    def __download_opensubtitles(self, items):
        """
        调用OpenSubtitles Api下载字幕
        """
        if not self.opensubtitles:
            return False, "未配置OpenSubtitles"
        subtitles_cache = {}
        success = False
        ret_msg = ""
        for item in items:
            if not item:
                continue
            if not item.get("name") or not item.get("file"):
                continue
            if item.get("type") == MediaType.TV and not item.get("imdbid"):
                log.warn("【Subtitle】电视剧类型需要imdbid检索字幕，跳过...")
                ret_msg = "电视剧需要imdbid检索字幕"
                continue
            subtitles = subtitles_cache.get(item.get("name"))
            if subtitles is None:
                log.info(
                    "【Subtitle】开始从Opensubtitle.org检索字幕: %s，imdbid=%s" % (item.get("name"), item.get("imdbid")))
                subtitles = self.__search_opensubtitles(item)
                if not subtitles:
                    subtitles_cache[item.get("name")] = []
                    log.info("【Subtitle】%s 未检索到字幕" % item.get("name"))
                    ret_msg = "%s 未检索到字幕" % item.get("name")
                else:
                    subtitles_cache[item.get("name")] = subtitles
                    log.info("【Subtitle】opensubtitles.org返回数据：%s" % len(subtitles))
            if not subtitles:
                continue
            # 成功数
            subtitle_count = 0
            for subtitle in subtitles:
                # 标题
                if not item.get("imdbid"):
                    if str(subtitle.get('title')) != "%s (%s)" % (item.get("name"), item.get("year")):
                        continue
                # 季
                if item.get('season') \
                        and str(subtitle.get('season').replace("Season", "").strip()) != str(item.get('season')):
                    continue
                # 集
                if item.get('episode') \
                        and str(subtitle.get('episode')) != str(item.get('episode')):
                    continue
                # 字幕文件名
                SubFileName = subtitle.get('description')
                # 下载链接
                Download_Link = subtitle.get('link')
                # 下载后的字幕文件路径
                Media_File = "%s.chi.zh-cn%s" % (item.get("file"), item.get("file_ext"))
                log.info("【Subtitle】正在从opensubtitles.org下载字幕 %s 到 %s " % (SubFileName, Media_File))
                # 下载
                ret = RequestUtils(cookies=self.opensubtitles.get_cookie(),
                                   headers=self.opensubtitles.get_ua()).get_res(Download_Link)
                if ret and ret.status_code == 200:
                    # 保存ZIP
                    file_name = self.__get_url_subtitle_name(ret.headers.get('content-disposition'), Download_Link)
                    if not file_name:
                        continue
                    zip_file = os.path.join(self._save_tmp_path, file_name)
                    zip_path = os.path.splitext(zip_file)[0]
                    with open(zip_file, 'wb') as f:
                        f.write(ret.content)
                    # 解压文件
                    shutil.unpack_archive(zip_file, zip_path, format='zip')
                    # 遍历转移文件
                    for sub_file in PathUtils.get_dir_files(in_path=zip_path, exts=RMT_SUBEXT):
                        self.__transfer_subtitle(sub_file, Media_File)
                    # 删除临时文件
                    try:
                        shutil.rmtree(zip_path)
                        os.remove(zip_file)
                    except Exception as err:
                        ExceptionUtils.exception_traceback(err)
                else:
                    log.error("【Subtitle】下载字幕文件失败：%s" % Download_Link)
                    continue
                # 最多下载3个字幕
                subtitle_count += 1
                if subtitle_count > 2:
                    break
            if not subtitle_count:
                if item.get('episode'):
                    log.info("【Subtitle】%s 第%s季 第%s集 未找到符合条件的字幕" % (
                        item.get("name"), item.get("season"), item.get("episode")))
                    ret_msg = "%s 第%s季 第%s集 未找到符合条件的字幕" % (
                        item.get("name"), item.get("season"), item.get("episode"))
                else:
                    log.info("【Subtitle】%s 未找到符合条件的字幕" % item.get("name"))
                    ret_msg = "%s 未找到符合条件的字幕" % item.get("name")
            else:
                log.info("【Subtitle】%s 共下载了 %s 个字幕" % (item.get("name"), subtitle_count))
                ret_msg = "%s 共下载了 %s 个字幕" % (item.get("name"), subtitle_count)
                success = True
        if success:
            return True, ret_msg
        else:
            return False, ret_msg

    def __download_chinesesubfinder(self, items):
        """
        调用ChineseSubFinder下载字幕
        """
        if not self._host or not self._api_key:
            return False, "未配置ChineseSubFinder"
        req_url = "%sapi/v1/add-job" % self._host
        notify_items = []
        success = False
        ret_msg = ""
        for item in items:
            if not item:
                continue
            if not item.get("name") or not item.get("file"):
                continue
            if item.get("bluray"):
                file_path = "%s.mp4" % item.get("file")
            else:
                if os.path.splitext(item.get("file"))[-1] != item.get("file_ext"):
                    file_path = "%s%s" % (item.get("file"), item.get("file_ext"))
                else:
                    file_path = item.get("file")

            # 路径替换
            if self._local_path and self._remote_path and file_path.startswith(self._local_path):
                file_path = file_path.replace(self._local_path, self._remote_path).replace('\\', '/')

            # 一个名称只建一个任务
            if file_path not in notify_items:
                notify_items.append(file_path)
                log.info("【Subtitle】通知ChineseSubFinder下载字幕: %s" % file_path)
                params = {
                    "video_type": 0 if item.get("type") == MediaType.MOVIE else 1,
                    "physical_video_file_full_path": file_path,
                    "task_priority_level": 3,
                    "media_server_inside_video_id": "",
                    "is_bluray": item.get("bluray")
                }
                try:
                    res = RequestUtils(headers={
                        "Authorization": "Bearer %s" % self._api_key
                    }).post(req_url, json=params)
                    if not res or res.status_code != 200:
                        log.error("【Subtitle】调用ChineseSubFinder API失败！")
                        ret_msg = "调用ChineseSubFinder API失败"
                    else:
                        # 如果文件目录没有识别的nfo元数据， 此接口会返回控制符，推测是ChineseSubFinder的原因
                        # emby refresh元数据时异步的
                        if res.text:
                            job_id = res.json().get("job_id")
                            message = res.json().get("message")
                            if not job_id:
                                log.warn("【Subtitle】ChineseSubFinder下载字幕出错：%s" % message)
                                ret_msg = "ChineseSubFinder下载字幕出错：%s" % message
                            else:
                                log.info("【Subtitle】ChineseSubFinder任务添加成功：%s" % job_id)
                                ret_msg = "ChineseSubFinder任务添加成功：%s" % job_id
                                success = True
                        else:
                            log.error("【Subtitle】%s 目录缺失nfo元数据" % file_path)
                            ret_msg = "%s 目录下缺失nfo元数据：" % file_path
                except Exception as e:
                    ExceptionUtils.exception_traceback(e)
                    log.error("【Subtitle】连接ChineseSubFinder出错：" + str(e))
                    ret_msg = "连接ChineseSubFinder出错：%s" % str(e)
        if success:
            return True, ret_msg
        else:
            return False, ret_msg

    @staticmethod
    def __transfer_subtitle(sub_file, media_file):
        """
        转移字幕
        """
        new_sub_file = "%s%s" % (os.path.splitext(media_file)[0], os.path.splitext(sub_file)[-1])
        if os.path.exists(new_sub_file):
            return 1
        else:
            return SystemUtils.copy(sub_file, new_sub_file)

    def download_subtitle_from_site(self, media_info, cookie, ua, download_dir):
        """
        从站点下载字幕文件，并保存到本地
        """
        if not media_info.page_url:
            return
        # 字幕下载目录
        log.info("【Subtitle】开始从站点下载字幕：%s" % media_info.page_url)
        if not download_dir:
            log.warn("【Subtitle】未找到字幕下载目录")
            return
        # 读取网站代码
        request = RequestUtils(cookies=cookie, headers=ua)
        res = request.get_res(media_info.page_url)
        if res and res.status_code == 200:
            if not res.text:
                log.warn(f"【Subtitle】读取页面代码失败：{media_info.page_url}")
                return
            html = etree.HTML(res.text)
            sublink_list = []
            for xpath in SiteConf.SITE_SUBTITLE_XPATH:
                sublinks = html.xpath(xpath)
                if sublinks:
                    for sublink in sublinks:
                        if not sublink:
                            continue
                        if not sublink.startswith("http"):
                            base_url = StringUtils.get_base_url(media_info.page_url)
                            if sublink.startswith("/"):
                                sublink = "%s%s" % (base_url, sublink)
                            else:
                                sublink = "%s/%s" % (base_url, sublink)
                        sublink_list.append(sublink)
            # 下载所有字幕文件
            for sublink in sublink_list:
                log.info(f"【Subtitle】找到字幕下载链接：{sublink}，开始下载...")
                # 下载
                ret = request.get_res(sublink)
                if ret and ret.status_code == 200:
                    # 创建目录
                    if not os.path.exists(download_dir):
                        os.makedirs(download_dir)
                    # 保存ZIP
                    file_name = self.__get_url_subtitle_name(ret.headers.get('content-disposition'), sublink)
                    if not file_name:
                        log.warn(f"【Subtitle】链接不是字幕文件：{sublink}")
                        continue
                    if file_name.lower().endswith(".zip"):
                        # ZIP包
                        zip_file = os.path.join(self._save_tmp_path, file_name)
                        # 解压路径
                        zip_path = os.path.splitext(zip_file)[0]
                        with open(zip_file, 'wb') as f:
                            f.write(ret.content)
                        # 解压文件
                        shutil.unpack_archive(zip_file, zip_path, format='zip')
                        # 遍历转移文件
                        for sub_file in PathUtils.get_dir_files(in_path=zip_path, exts=RMT_SUBEXT):
                            target_sub_file = os.path.join(download_dir,
                                                           os.path.splitext(os.path.basename(sub_file))[0])
                            log.info(f"【Subtitle】转移字幕 {sub_file} 到 {target_sub_file}")
                            self.__transfer_subtitle(sub_file, target_sub_file)
                        # 删除临时文件
                        try:
                            shutil.rmtree(zip_path)
                            os.remove(zip_file)
                        except Exception as err:
                            ExceptionUtils.exception_traceback(err)
                    else:
                        sub_file = os.path.join(self._save_tmp_path, file_name)
                        # 保存
                        with open(sub_file, 'wb') as f:
                            f.write(ret.content)
                        target_sub_file = os.path.join(download_dir,
                                                       os.path.splitext(os.path.basename(sub_file))[0])
                        log.info(f"【Subtitle】转移字幕 {sub_file} 到 {target_sub_file}")
                        self.__transfer_subtitle(sub_file, target_sub_file)
                else:
                    log.error(f"【Subtitle】下载字幕文件失败：{sublink}")
                    continue
            if sublink_list:
                log.info(f"【Subtitle】{media_info.page_url} 页面字幕下载完成")
        elif res is not None:
            log.warn(f"【Subtitle】连接 {media_info.page_url} 失败，状态码：{res.status_code}")
        else:
            log.warn(f"【Subtitle】无法打开链接：{media_info.page_url}")

    @staticmethod
    def __get_url_subtitle_name(disposition, url):
        """
        从下载请求中获取字幕文件名
        """
        file_name = re.findall(r"filename=\"?(.+)\"?", disposition or "")
        if file_name:
            file_name = str(file_name[0].encode('ISO-8859-1').decode()).split(";")[0].strip()
            if file_name.endswith('"'):
                file_name = file_name[:-1]
        elif url and os.path.splitext(url)[-1] in (RMT_SUBEXT + ['.zip']):
            file_name = url.split("/")[-1]
        else:
            file_name = str(datetime.datetime.now())
        return file_name
