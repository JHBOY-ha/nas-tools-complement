import ast
import json
import os
import re
import shutil
import subprocess

import log
from app.db.media_db import MediaDb
from app.helper.db_helper import DbHelper
from app.media.category import Category
from app.mediaserver import MediaServer
from app.utils import ExceptionUtils, PathUtils
from config import Config, RMT_MEDIAEXT, RMT_SUBEXT


class MediaLibrary:
    _chinese_sub_re = re.compile(
        r"(^|[.\-_\[\( ])(zh[-_]?(cn|hans|chs|sg|sc|tw|hant|cht|hk)|"
        r"zho|chi|chs|cht|cn|sc|tc|简|简中|简体|繁|繁中|繁体|中文|中文字幕)"
        r"($|[.\-_\]\) ])",
        re.I
    )
    _chinese_langs = {
        "zh", "zho", "chi", "chs", "cht", "cn", "sc", "tc",
        "zh-cn", "zh_cn", "zh-hans", "zh_hans", "zh-sg", "zh_sg",
        "zh-tw", "zh_tw", "zh-hant", "zh_hant", "zh-hk", "zh_hk"
    }

    def __init__(self):
        self.mediadb = MediaDb()
        self.dbhelper = DbHelper()
        self.category = Category()
        self.media_server = MediaServer()

    def list_items(self, data=None):
        """
        查询首页媒体库展示数据
        """
        data = data or {}
        current_server_type = self.media_server.get_type()
        server_type = current_server_type.value if current_server_type else Config().get_config('media').get('media_server')
        media_type = data.get("type") or "all"
        category = data.get("category") or ""
        subtitle = data.get("subtitle") or "all"
        keyword = str(data.get("keyword") or "").strip().lower()
        page = self.__safe_int(data.get("page"), 1)
        page_size = min(max(self.__safe_int(data.get("page_size"), 24), 1), 100)

        rows = self.mediadb.list_items(server_type=server_type)
        transfer_histories = self.dbhelper.get_transfer_histories_with_dest()
        items = []
        categories = {"movie": set(), "tv": set(), "anime": set()}
        for row in rows:
            item = self.__build_item(row, transfer_histories=transfer_histories, include_status=False)
            if not item:
                continue
            if item["media_type"] in categories:
                categories[item["media_type"]].add(item["category"])
            if media_type != "all" and item["media_type"] != media_type:
                continue
            if category and item["category"] != category:
                continue
            if keyword and keyword not in f"{item['title']} {item['original_title']} {item['year']}".lower():
                continue
            items.append(item)

        if subtitle != "all":
            enriched_items = []
            for item in items:
                self.__fill_subtitle_summary(item)
                if subtitle == "missing" and item["subtitle_status"] != "missing_chinese":
                    continue
                if subtitle == "ok" and item["subtitle_status"] == "missing_chinese":
                    continue
                enriched_items.append(item)
            items = enriched_items

        total = len(items)
        start = (page - 1) * page_size
        end = start + page_size
        page_items = items[start:end]
        for item in page_items:
            if not item.get("subtitle_status"):
                self.__fill_subtitle_summary(item)
            item.pop("media_streams", None)
            item.pop("linked_episodes", None)
        return {
            "code": 0,
            "items": page_items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "categories": {key: sorted(value) for key, value in categories.items()},
            "server": server_type
        }

    def get_episodes(self, data=None):
        """
        查询电视剧/动漫剧集及字幕状态
        """
        data = data or {}
        item_id = data.get("item_id")
        if not item_id:
            return {"code": -1, "msg": "缺少媒体项目ID"}
        current_server_type = self.media_server.get_type()
        server_type = current_server_type.value if current_server_type else Config().get_config('media').get('media_server')
        row = None
        for media_item in self.mediadb.list_items(server_type=server_type):
            if str(media_item.ITEM_ID) == str(item_id):
                row = media_item
                break
        if not row:
            return {"code": -1, "msg": "未找到媒体库同步项目"}
        media_type, _ = self.classify_path(row.PATH, row.ITEM_TYPE)
        matches = self.__find_transfer_matches(row, media_type, self.dbhelper.get_transfer_histories_with_dest())
        episodes = self.__series_history_items(matches)
        if not episodes:
            episodes = self.media_server.get_episodes(item_id) or []
        ret_items = []
        for episode in episodes:
            media_path = episode.get("path") or ""
            status = self.detect_subtitle_status(media_path, episode.get("media_streams") or [])
            ret_items.append({
                "id": episode.get("id"),
                "title": episode.get("title") or "",
                "season": episode.get("season") or "",
                "episode": episode.get("episode") or "",
                "season_episode": self.__season_episode(episode.get("season"), episode.get("episode")),
                "path": media_path,
                "subtitle_status": status.get("status"),
                "subtitle_label": status.get("label"),
                "subtitle_badge": status.get("badge"),
                "can_upload": bool(media_path and os.path.isfile(media_path))
            })
        return {"code": 0, "items": ret_items, "total": len(ret_items)}

    def get_local_poster_file(self, item_id):
        """
        根据媒体库项目ID查找已媒体链接目录中的本地海报
        """
        if not item_id:
            return ""
        current_server_type = self.media_server.get_type()
        server_type = current_server_type.value if current_server_type else Config().get_config('media').get('media_server')
        transfer_histories = self.dbhelper.get_transfer_histories_with_dest()
        for row in self.mediadb.list_items(server_type=server_type):
            if str(row.ITEM_ID) != str(item_id):
                continue
            item = self.__build_item(row, transfer_histories=transfer_histories, include_status=False)
            if not item:
                return ""
            search_dirs = []
            if item.get("target_path"):
                search_dirs.append(os.path.dirname(item.get("target_path")))
            if item.get("path") and os.path.isdir(item.get("path")):
                search_dirs.append(item.get("path"))
            for search_dir in search_dirs:
                poster_file = self.__find_local_poster(search_dir)
                if poster_file:
                    return poster_file
        return ""

    def __build_item(self, row, transfer_histories=None, include_status=True):
        item_json = self.__loads_json(row.JSON)
        media_path = row.PATH or item_json.get("Path") or ""
        media_type, category = self.classify_path(media_path, row.ITEM_TYPE)
        if not media_type:
            return None
        matches = self.__find_transfer_matches(row, media_type, transfer_histories or [])
        linked = bool(matches)
        target_path = ""
        display_path = media_path
        if linked:
            primary_history = matches[0]
            history_media_type = self.__history_media_type(primary_history.TYPE)
            if history_media_type:
                media_type = history_media_type
            target_path = self.__history_target_file(primary_history)
            category = primary_history.CATEGORY or self.classify_path(target_path, row.ITEM_TYPE)[1]
            display_path = target_path
            if media_type == "movie":
                target_path = target_path if target_path and os.path.isfile(target_path) else ""
                display_path = target_path
            elif primary_history.DEST_PATH:
                display_path = primary_history.DEST_PATH
        elif media_type == "movie" and media_path and os.path.isfile(media_path):
            target_path = media_path

        item = {
            "id": row.ITEM_ID,
            "library": row.LIBRARY or "",
            "item_type": row.ITEM_TYPE or "",
            "media_type": media_type,
            "media_type_name": {"movie": "电影", "tv": "电视剧", "anime": "动漫"}.get(media_type, "媒体"),
            "category": category,
            "title": row.TITLE or "",
            "original_title": row.ORGIN_TITLE or "",
            "year": row.YEAR or "",
            "tmdbid": row.TMDBID or "",
            "imdbid": row.IMDBID or "",
            "path": display_path,
            "server_path": media_path,
            "target_path": target_path,
            "linked": linked,
            "can_upload": bool(target_path and os.path.isfile(target_path)),
            "poster_url": f"/library/image/{row.ITEM_ID}",
            "media_streams": item_json.get("MediaStreams") or [],
            "linked_episodes": self.__series_history_items(matches) if media_type != "movie" else [],
            "subtitle_status": "",
            "subtitle_label": "",
            "subtitle_badge": "",
            "missing_count": 0
        }
        if include_status:
            self.__fill_subtitle_summary(item)
        return item

    def __fill_subtitle_summary(self, item):
        if item.get("media_type") == "movie":
            detect_path = item.get("target_path") or item.get("path")
            status = self.detect_subtitle_status(detect_path, item.get("media_streams") or [])
            item["missing_count"] = 1 if status.get("status") == "missing_chinese" else 0
        else:
            linked_episodes = item.get("linked_episodes") or []
            episode_statuses = [
                self.detect_subtitle_status(episode.get("path") or "", episode.get("media_streams") or [])
                for episode in linked_episodes
            ]
            missing_count = len([status for status in episode_statuses if status.get("status") == "missing_chinese"])
            item["missing_count"] = missing_count
            if episode_statuses:
                status = self.__summary_series_status(episode_statuses, missing_count)
            else:
                status = {"status": "unknown", "label": "未检测", "badge": "bg-secondary"}
        item["subtitle_status"] = status.get("status")
        item["subtitle_label"] = status.get("label")
        item["subtitle_badge"] = status.get("badge")
        item.pop("media_streams", None)
        item.pop("linked_episodes", None)
        return item

    def __get_series_episode_statuses(self, item_id, fallback_path):
        episodes = self.media_server.get_episodes(item_id) or []
        if episodes:
            return [
                self.detect_subtitle_status(episode.get("path") or "", episode.get("media_streams") or [])
                for episode in episodes
            ]
        media_files = self.__resolve_media_files(fallback_path)
        return [self.detect_subtitle_status(media_file, []) for media_file in media_files]

    def __find_transfer_matches(self, row, media_type, transfer_histories):
        matches = []
        seen_ids = set()
        row_tmdbid = str(row.TMDBID or "")
        row_title = str(row.TITLE or "").strip()
        row_year = str(row.YEAR or "").strip()
        row_path_name = os.path.splitext(os.path.basename(row.PATH or ""))[0].lower()
        for history in transfer_histories or []:
            history_media_type = self.__history_media_type(history.TYPE)
            if media_type == "movie" and history_media_type != "movie":
                continue
            if media_type in ["tv", "anime"] and history_media_type not in ["tv", "anime"]:
                continue
            matched = False
            if row_tmdbid and history.TMDBID and row_tmdbid == str(history.TMDBID):
                matched = True
            elif row_title and str(history.TITLE or "").strip() == row_title:
                if not row_year or not history.YEAR or str(history.YEAR) == row_year:
                    matched = True
            elif row_path_name:
                dest_base = os.path.splitext(str(history.DEST_FILENAME or ""))[0].lower()
                matched = bool(dest_base and (row_path_name == dest_base or row_path_name in dest_base))
            if matched and history.ID not in seen_ids:
                matches.append(history)
                seen_ids.add(history.ID)
        return matches

    @staticmethod
    def __history_media_type(history_type):
        history_type = str(history_type or "").upper()
        if history_type in ["电影", "MOV", "MOVIE"]:
            return "movie"
        if history_type in ["动漫", "ANI", "ANIME"]:
            return "anime"
        if history_type in ["电视剧", "TV", "SERIES"]:
            return "tv"
        return ""

    @staticmethod
    def __history_target_file(history):
        if not history or not history.DEST_PATH or not history.DEST_FILENAME:
            return ""
        return os.path.join(history.DEST_PATH, history.DEST_FILENAME)

    def __series_history_items(self, histories):
        items = []
        for history in histories or []:
            media_path = self.__history_target_file(history)
            if not media_path:
                continue
            season_episode = history.SEASON_EPISODE or ""
            items.append({
                "id": history.ID,
                "title": history.DEST_FILENAME or history.TITLE or "",
                "season": self.__parse_season_episode(season_episode)[0],
                "episode": self.__parse_season_episode(season_episode)[1],
                "season_episode": season_episode,
                "path": media_path,
                "media_streams": []
            })
        return items

    @classmethod
    def detect_subtitle_status(cls, media_file, media_streams=None):
        """
        判断单个媒体文件是否已有中文字幕
        """
        if not media_file or not os.path.isfile(media_file):
            return {"status": "unknown", "label": "未检测", "badge": "bg-secondary"}
        if cls.has_external_chinese_subtitle(media_file):
            return {"status": "has_chinese_external", "label": "已有外挂中文字幕", "badge": "bg-green"}

        has_subtitle_stream, has_chinese_stream = cls.__detect_streams(media_streams or [])
        if has_chinese_stream:
            return {"status": "has_chinese_internal", "label": "已有内嵌中文字幕", "badge": "bg-azure"}
        if not media_streams:
            has_subtitle_stream, has_chinese_stream = cls.__ffprobe_subtitle_streams(media_file)
            if has_chinese_stream:
                return {"status": "has_chinese_internal", "label": "已有内嵌中文字幕", "badge": "bg-azure"}

        if has_subtitle_stream:
            return {"status": "missing_chinese", "label": "缺中文字幕", "badge": "bg-orange"}
        return {"status": "missing_chinese", "label": "缺中文字幕", "badge": "bg-orange"}

    @classmethod
    def has_external_chinese_subtitle(cls, media_file):
        if not media_file:
            return False
        media_dir = os.path.dirname(media_file)
        media_base = os.path.splitext(os.path.basename(media_file))[0]
        if not media_dir or not os.path.isdir(media_dir):
            return False
        try:
            for file_name in os.listdir(media_dir):
                sub_ext = os.path.splitext(file_name)[-1].lower()
                if sub_ext not in RMT_SUBEXT:
                    continue
                sub_base = os.path.splitext(file_name)[0]
                if sub_base == media_base:
                    return True
                if sub_base.startswith(f"{media_base}.") \
                        or sub_base.startswith(f"{media_base}-") \
                        or sub_base.startswith(f"{media_base}_"):
                    suffix = sub_base[len(media_base):]
                    if cls._chinese_sub_re.search(suffix):
                        return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
        return False

    def classify_path(self, media_path, item_type=None):
        """
        按媒体库根路径和二级分类配置判断首页展示分类
        """
        media_type = self.__classify_media_type(media_path, item_type)
        if not media_type:
            return "", "未分类"
        root_path = self.__matched_root_path(media_type, media_path)
        category_names = self.__category_names(media_type)
        if not root_path or not category_names:
            return media_type, "未分类"
        try:
            rel_path = os.path.relpath(os.path.normpath(media_path), os.path.normpath(root_path))
        except ValueError:
            return media_type, "未分类"
        if rel_path == "." or rel_path.startswith(".."):
            return media_type, "未分类"
        first_part = re.split(r"[\\/]", rel_path)[0]
        if first_part in category_names:
            return media_type, first_part
        return media_type, "未分类"

    @classmethod
    def __detect_streams(cls, streams):
        has_subtitle = False
        has_chinese = False
        for stream in streams or []:
            stream_type = str(stream.get("Type") or stream.get("codec_type") or "").lower()
            if stream_type != "subtitle":
                continue
            has_subtitle = True
            language = str(stream.get("Language") or stream.get("language") or "").lower()
            tags = stream.get("tags") or {}
            text = " ".join([
                language,
                str(stream.get("Title") or ""),
                str(stream.get("DisplayTitle") or ""),
                str(stream.get("Codec") or ""),
                str(tags.get("language") or ""),
                str(tags.get("title") or "")
            ]).lower()
            if language in cls._chinese_langs or cls._chinese_sub_re.search(text):
                has_chinese = True
        return has_subtitle, has_chinese

    @classmethod
    def __ffprobe_subtitle_streams(cls, media_file):
        if not shutil.which("ffprobe"):
            log.warn("【MediaLibrary】ffprobe 不可用，跳过内嵌字幕兜底检测")
            return False, False
        try:
            ret = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", media_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                text=True
            )
            if ret.returncode != 0 or not ret.stdout:
                return False, False
            streams = json.loads(ret.stdout).get("streams") or []
            return cls.__detect_streams(streams)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            return False, False

    def __classify_media_type(self, media_path, item_type=None):
        if media_path:
            if self.__matched_root_path("movie", media_path):
                return "movie"
            if self.__matched_root_path("anime", media_path):
                return "anime"
            if self.__matched_root_path("tv", media_path):
                return "tv"
        item_type = str(item_type or "").lower()
        if item_type in ["movie"]:
            return "movie"
        if item_type in ["series", "show"]:
            return "tv"
        return ""

    def __matched_root_path(self, media_type, media_path):
        for root_path in self.__media_paths(media_type):
            if PathUtils.is_path_in_path(root_path, media_path):
                return root_path
        return ""

    @staticmethod
    def __media_paths(media_type):
        media = Config().get_config('media') or {}
        key = {"movie": "movie_path", "tv": "tv_path", "anime": "anime_path"}.get(media_type)
        paths = media.get(key) if key else []
        if media_type == "anime" and not paths:
            paths = media.get("tv_path")
        if not isinstance(paths, list):
            paths = [paths] if paths else []
        return [path for path in paths if path]

    def __category_names(self, media_type):
        if media_type == "movie":
            return list(self.category.get_movie_categorys())
        if media_type == "anime":
            return list(self.category.get_anime_categorys())
        if media_type == "tv":
            return list(self.category.get_tv_categorys())
        return []

    @staticmethod
    def __resolve_media_files(media_path):
        if not media_path or not os.path.exists(media_path):
            return []
        if os.path.isfile(media_path):
            if os.path.splitext(media_path)[-1].lower() in RMT_MEDIAEXT:
                return [media_path]
            return []
        return PathUtils.get_dir_files(media_path, exts=RMT_MEDIAEXT)

    @staticmethod
    def __find_local_poster(media_dir):
        if not media_dir or not os.path.isdir(media_dir):
            return ""
        image_exts = [".jpg", ".jpeg", ".png", ".webp"]
        poster_names = ["poster", "folder", "cover", "front", "thumb"]
        for poster_name in poster_names:
            for image_ext in image_exts:
                poster_file = os.path.join(media_dir, f"{poster_name}{image_ext}")
                if os.path.isfile(poster_file):
                    return poster_file
        try:
            for file_name in os.listdir(media_dir):
                name, ext = os.path.splitext(file_name)
                if ext.lower() in image_exts and "poster" in name.lower():
                    poster_file = os.path.join(media_dir, file_name)
                    if os.path.isfile(poster_file):
                        return poster_file
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
        return ""

    @staticmethod
    def __loads_json(value):
        if not value:
            return {}
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value)
        except Exception:
            try:
                parsed = ast.literal_eval(value)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}

    @staticmethod
    def __summary_series_status(statuses, missing_count):
        if missing_count:
            return {"status": "missing_chinese", "label": f"缺中文字幕 {missing_count} 集", "badge": "bg-orange"}
        if any(status.get("status") == "has_chinese_external" for status in statuses):
            return {"status": "has_chinese_external", "label": "已有外挂中文字幕", "badge": "bg-green"}
        if any(status.get("status") == "has_chinese_internal" for status in statuses):
            return {"status": "has_chinese_internal", "label": "已有内嵌中文字幕", "badge": "bg-azure"}
        return {"status": "unknown", "label": "未检测", "badge": "bg-secondary"}

    @staticmethod
    def __safe_int(value, default):
        try:
            value = int(value)
            return value if value > 0 else default
        except Exception:
            return default

    @staticmethod
    def __season_episode(season, episode):
        season = str(season).rjust(2, "0") if season not in [None, ""] else ""
        episode = str(episode).rjust(2, "0") if episode not in [None, ""] else ""
        if season and episode:
            return f"S{season}E{episode}"
        if episode:
            return f"E{episode}"
        return ""

    @staticmethod
    def __parse_season_episode(season_episode):
        match = re.search(r"S(\d+)E(\d+)", str(season_episode or ""), re.I)
        if not match:
            return "", ""
        return match.group(1), match.group(2)
