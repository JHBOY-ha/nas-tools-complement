"""Complete file-manager subtitle downloads against their recorded library target."""
import os
import time

from app.conf import ModuleConf
from app.helper import DbHelper
from app.helper.subtitle_health import SubtitleHealth
from app.subtitle import Subtitle
from app.utils.types import RmtMode
from config import Config, RMT_MEDIAEXT


class MediaFileSubtitles:
    # ChineseSubFinder only acknowledges a queued job. Keep the existing action
    # task running until a valid, stable subtitle is visible, with a finite deadline.
    WAIT_SECONDS = 600
    POLL_SECONDS = 2

    @staticmethod
    def _inside_library(path, roots):
        parent = os.path.realpath(os.path.dirname(os.path.abspath(path)))
        for root in roots:
            try:
                real_root = os.path.realpath(root)
                if os.path.commonpath((parent, real_root)) == real_root:
                    return True
            except (OSError, ValueError, TypeError):
                continue
        return False

    @staticmethod
    def _identity(path):
        stat = os.stat(path)
        link = os.lstat(path)
        return (os.path.realpath(os.path.dirname(path)), os.path.realpath(path),
                link.st_dev, link.st_ino, stat.st_dev, stat.st_ino,
                stat.st_size, stat.st_mtime_ns)

    def _wait_for_subtitle(self, subtitle, item, guard):
        deadline = time.monotonic() + self.WAIT_SECONDS
        previous = None
        while True:
            guard()
            path = subtitle.find_downloaded_subtitle(item)
            try:
                current = (path, self._identity(path)) if path and not os.path.islink(path) else None
                # A valid prefix may still be downloading. Require an unchanged
                # file across polls before publishing it next to the library video.
                if current and current == previous and SubtitleHealth.validate_subtitle(path).get("valid"):
                    return path
                previous = current
            except OSError:
                previous = None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("ChineseSubFinder字幕尚未落盘，未同步或刷新；下载完成后可再次点击补同步")
            time.sleep(min(self.POLL_SECONDS, remaining))

    def download(self, item, selected_file_id=None):
        """Return the legacy success/message pair, preserving candidate selection."""
        subtitle = Subtitle()
        media_file = os.path.normpath(item["file"] + item["file_ext"])
        media_config = Config().get_config("media") or {}
        roots = []
        for key in ("movie_path", "tv_path", "anime_path"):
            value = media_config.get(key) or []
            roots.extend(value if isinstance(value, list) else [value])
        dbhelper = DbHelper()
        history = dbhelper.get_latest_transfer_history_by_source_full_path(media_file)
        target = ""
        if history and history.DEST_PATH and history.DEST_FILENAME:
            target = os.path.normpath(os.path.join(history.DEST_PATH, history.DEST_FILENAME))
            # Directory-sync rules can have their own library destination,
            # separate from the global movie/TV/anime roots.
            roots.extend(row.DEST for row in dbhelper.get_config_sync_paths() or [] if row.DEST)
        elif self._inside_library(media_file, roots):
            target = media_file

        try:
            if not os.path.isfile(media_file) or os.path.splitext(media_file)[1].lower() not in RMT_MEDIAEXT:
                raise ValueError("媒体文件不存在或格式无效")
            if not target:
                success, message = subtitle.download_subtitle([item], selected_file_id=selected_file_id)
                if success:
                    message += "，未找到整理历史，下载的字幕仅保存在原目录，不自动同步"
                return success, message
            # Never guess a destination or recreate a missing historical video.
            # Check before spending quota and again at every publication boundary.
            if not self._inside_library(target, roots) or not os.path.isfile(target) \
                    or os.path.splitext(target)[1].lower() not in RMT_MEDIAEXT:
                raise ValueError("整理历史中的媒体库目标不存在或不在已配置媒体库/同步目标目录内，未下载字幕")
            paths = {path: self._identity(path) for path in (media_file, target)}

            def guard():
                if not self._inside_library(target, roots) \
                        or any(self._identity(path) != identity for path, identity in paths.items()):
                    raise PermissionError("媒体文件或整理目标发生变化，请重新选择后重试")

            # Reuse existing downloads even if API credentials have since expired.
            # Do not queue a provider job that could later rewrite a shared inode.
            asynchronous = subtitle.is_chinesesubfinder()
            source = subtitle.find_downloaded_subtitle(item)
            existing = bool(source and not os.path.islink(source)
                            and SubtitleHealth.validate_subtitle(source).get("valid"))
            if not existing:
                success, message = subtitle.download_subtitle([item], selected_file_id=selected_file_id)
                if not success:
                    return success, message
                source = self._wait_for_subtitle(subtitle, item, guard) if asynchronous \
                    else subtitle.find_downloaded_subtitle(item)
            if not source:
                raise ValueError("下载已返回，但未找到对应字幕，未同步或刷新")
            guard()
            server = str(media_config.get("media_server") or "emby").lower()
            mode = ModuleConf.get_enum_item(RmtMode, history.MODE) if history and history.MODE else None
            success, message, _result = subtitle.sync_downloaded_subtitle(
                source, media_file, target, rmt_mode=mode, server_type=server,
                path_guard_check=guard
            )
            if not success:
                return False, message
            guard()
            # Reuse the same validated item/episode refresh used by upload tasks;
            # it reports failures and never falls back to a whole-library scan.
            from app.helper.subtitle_task_processors import _localized_refresh
            refresh = _localized_refresh({}, target, server)
            if refresh.get("status") != "refreshed":
                return False, message + "，影视服务器刷新未完成：" + (
                    refresh.get("message") or refresh.get("status") or "未知错误")
            return True, message + "，已刷新影视服务器"
        except (OSError, ValueError) as error:
            return False, str(error)
