import ast
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

import log
from app.db.media_db import MediaDb
from app.helper.db_helper import DbHelper
from app.helper.subtitle_health import SubtitleHealth
from app.media.category import Category
from app.mediaserver import MediaServer
from app.utils import ExceptionUtils, PathUtils
from config import Config, RMT_MEDIAEXT, RMT_SUBEXT


class MediaLibrary:
    _subtitle_audit_filename = "subtitle-audit-history.json"
    _subtitle_audit_lock = threading.RLock()
    _subtitle_audit_store_cache = None
    _subtitle_dir_cache = {}
    _subtitle_dir_cache_lock = threading.RLock()
    _subtitle_dir_cache_ttl = 30
    _subtitle_dir_cache_limit = 4096
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
        sort_by = str(data.get("sort_by") or "default").lower()
        sort_order = str(data.get("sort_order") or "desc").lower()
        if sort_by not in ["default", "internal", "external", "audit"]:
            sort_by = "default"
        if sort_order not in ["asc", "desc"]:
            sort_order = "desc"
        keyword = str(data.get("keyword") or "").strip().lower()
        page = self.__safe_int(data.get("page"), 1)
        page_size = min(max(self.__safe_int(data.get("page_size"), 24), 1), 100)

        rows = self.mediadb.list_items(server_type=server_type)
        transfer_histories = self.dbhelper.get_transfer_histories_with_dest()
        transfer_history_index = self.__build_transfer_history_index(transfer_histories)
        audit_snapshots = self.__latest_audit_snapshots(server_type)
        movie_audit_snapshot = audit_snapshots["movie"]
        items = []
        categories = {"movie": set(), "tv": set(), "anime": set()}
        for row in rows:
            item = self.__build_item(row, transfer_histories=transfer_history_index, include_status=False)
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
                self.__fill_subtitle_summary(
                    item,
                    allow_ffprobe=False,
                    audit_snapshot=movie_audit_snapshot
                )
                if subtitle == "missing" and item["subtitle_status"] != "missing_chinese":
                    continue
                if subtitle == "ok" and item["subtitle_status"] == "missing_chinese":
                    continue
                enriched_items.append(item)
            items = enriched_items

        if sort_by != "default":
            for item in items:
                self.__fill_sort_metrics(item, audit_snapshots)
            items.sort(key=lambda item: (
                str(item.get("title") or item.get("original_title") or "").lower(),
                str(item.get("year") or ""),
                str(item.get("id") or "")
            ))
            items.sort(
                key=lambda item: item.get("_sort_%s" % sort_by, 0),
                reverse=sort_order == "desc"
            )

        total = len(items)
        start = (page - 1) * page_size
        end = start + page_size
        page_items = items[start:end]
        for item in page_items:
            if not item.get("subtitle_status"):
                self.__fill_subtitle_summary(
                    item,
                    allow_ffprobe=False,
                    audit_snapshot=movie_audit_snapshot
                )
            for key in ["_sort_internal", "_sort_external", "_sort_audit"]:
                item.pop(key, None)
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

    def audit_external_subtitles(self, category, subcategory=None):
        """按媒体分类检测外挂字幕能否被当前影视服务器识别。"""
        media_config = Config().get_config('media') or {}
        server_type = str(media_config.get('media_server') or "emby").lower()
        category = str(category or "").lower()
        subcategory = str(subcategory or "").strip()
        category_config = {
            "movie": ("电影", "movie_path"),
            "tv": ("电视剧", "tv_path"),
            "anime": ("动漫", "anime_path")
        }
        if category not in category_config:
            return {"code": -1, "msg": "请选择要检测的媒体分类"}
        category_name, path_key = category_config[category]
        paths = media_config.get(path_key) or []
        if category == "anime" and not paths:
            paths = media_config.get("tv_path") or []
        if isinstance(paths, str):
            paths = [paths]
        roots = []
        for path in paths:
            path = str(path or "").strip()
            if path and path not in roots:
                roots.append(path)
        if not roots:
            return {"code": -1, "msg": f"全局设置中未配置{category_name}媒体库目录"}
        if subcategory:
            valid_subcategories = set(self.__category_names(category))
            if subcategory not in valid_subcategories:
                return {"code": -1, "msg": f"{category_name}小分类无效或已从分类配置中删除"}
            roots = [os.path.join(root, subcategory) for root in roots]
        result = SubtitleHealth.audit_roots(roots, server_type)
        result["category"] = category
        result["category_name"] = category_name
        result["subcategory"] = subcategory
        result["scope_name"] = f"{category_name} / {subcategory}" if subcategory else f"全部{category_name}"
        try:
            result["history"] = self.__save_subtitle_audit(result)
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
            log.error("【MediaLibrary】保存字幕检测记录失败：%s" % str(e))
            result["history"] = self.get_external_subtitle_audit_history().get("history") or []
            result["history_warning"] = "检测完成，但保存检测记录失败"
        result.pop("media_statuses", None)
        return result

    def get_external_subtitle_audit_history(self):
        """返回最近 3 次外挂字幕检测记录。"""
        store = self.__load_subtitle_audit_store()
        return {"code": 0, "history": store.get("history") or []}

    def get_external_subtitle_audit_categories(self):
        """返回当前分类 YAML 中配置的媒体小分类。"""
        return {
            "code": 0,
            "categories": {
                "movie": self.__category_names("movie"),
                "tv": self.__category_names("tv"),
                "anime": self.__category_names("anime")
            }
        }

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
            "subtitle_audit_status": "",
            "subtitle_audit_label": "",
            "subtitle_audit_badge": "",
            "subtitle_audit_checked_at": "",
            "missing_count": 0
        }
        if include_status:
            self.__fill_subtitle_summary(item)
        return item

    def __fill_subtitle_summary(self, item, allow_ffprobe=True, audit_snapshot=None):
        if item.get("media_type") == "movie":
            detect_path = item.get("target_path") or item.get("path")
            status = self.detect_subtitle_status(detect_path, item.get("media_streams") or [], allow_ffprobe=allow_ffprobe)
            item["missing_count"] = 1 if status.get("status") == "missing_chinese" else 0
        else:
            linked_episodes = item.get("linked_episodes") or []
            episode_statuses = [
                self.detect_subtitle_status(
                    episode.get("path") or "",
                    episode.get("media_streams") or [],
                    allow_ffprobe=allow_ffprobe
                )
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
        if item.get("media_type") == "movie":
            self.__fill_movie_audit_summary(item, audit_snapshot)
        return item

    @classmethod
    def __fill_movie_audit_summary(cls, item, audit_snapshot):
        audit_snapshot = audit_snapshot or {}
        media_statuses = audit_snapshot.get("media_statuses") or {}
        media_path = item.get("target_path") or item.get("path") or ""
        key = os.path.normcase(os.path.normpath(media_path)) if media_path else ""
        audit_status = media_statuses.get(key) or {}
        status = audit_status.get("status") or ""
        labels = {
            "ok": ("外挂字幕检测通过", "bg-green-lt text-green"),
            "warning": ("外挂字幕语言需规范", "bg-yellow-lt text-yellow"),
            "error": ("外挂字幕无法识别", "bg-red-lt text-red")
        }
        label, badge = labels.get(status, ("", ""))
        item["subtitle_audit_status"] = status
        item["subtitle_audit_label"] = label
        item["subtitle_audit_badge"] = badge
        item["subtitle_audit_checked_at"] = audit_status.get("checked_at") \
            or audit_snapshot.get("checked_at") or ""

    @classmethod
    def __fill_sort_metrics(cls, item, audit_snapshots):
        media_type = item.get("media_type") or ""
        paths = []
        if media_type == "movie":
            media_path = item.get("target_path") or item.get("path") or ""
            if media_path:
                paths.append(media_path)
        else:
            paths.extend([
                episode.get("path") for episode in (item.get("linked_episodes") or [])
                if episode.get("path")
            ])

        has_internal, _ = cls.__detect_streams(item.get("media_streams") or [])
        if not has_internal and media_type != "movie":
            has_internal = any(
                cls.__detect_streams(episode.get("media_streams") or [])[0]
                for episode in (item.get("linked_episodes") or [])
            )
        snapshot = (audit_snapshots or {}).get(media_type) or {}
        media_statuses = snapshot.get("media_statuses") or {}
        # 数值表示待处理严重度：降序时将有问题的字幕排在最前。
        audit_priority = {"error": 3, "warning": 2, "ok": 0}
        audit_ranks = []
        has_external = False
        for media_path in paths:
            key = os.path.normcase(os.path.normpath(media_path))
            audit_status = (media_statuses.get(key) or {}).get("status") or ""
            if audit_status:
                has_external = True
                audit_ranks.append(audit_priority.get(audit_status, 0))
            elif cls.has_external_subtitle(media_path):
                has_external = True
        item["_sort_internal"] = 1 if has_internal else 0
        item["_sort_external"] = 1 if has_external else 0
        item["_sort_audit"] = max(audit_ranks) if audit_ranks else 1

    @classmethod
    def __latest_audit_snapshot(cls, category, server_type):
        return cls.__latest_audit_snapshots(server_type).get(category) or {}

    @classmethod
    def __latest_audit_snapshots(cls, server_type):
        store = cls.__load_subtitle_audit_store()
        snapshots = {}
        for category in ["movie", "tv", "anime"]:
            snapshot = (store.get("latest") or {}).get(category) or {}
            if str(snapshot.get("server") or "").lower() == str(server_type or "").lower():
                snapshots[category] = snapshot
            else:
                snapshots[category] = {}
        return snapshots

    @classmethod
    def __save_subtitle_audit(cls, result):
        checked_at = datetime.now().astimezone().isoformat(timespec="seconds")
        category = result.get("category") or ""
        record = {
            "checked_at": checked_at,
            "category": category,
            "category_name": result.get("category_name") or "",
            "subcategory": result.get("subcategory") or "",
            "scope_name": result.get("scope_name") or result.get("category_name") or "",
            "server": result.get("server") or "",
            "roots": result.get("roots") or [],
            "summary": result.get("summary") or {},
            "issues": result.get("issues") or [],
            "issues_truncated": result.get("issues_truncated") or 0,
            "probe_available": bool(result.get("probe_available"))
        }
        with cls._subtitle_audit_lock:
            store = cls.__load_subtitle_audit_store()
            latest = store.get("latest") or {}
            previous = latest.get(category) or {}
            if str(previous.get("server") or "").lower() == str(result.get("server") or "").lower():
                media_statuses = dict(previous.get("media_statuses") or {})
            else:
                media_statuses = {}
            inaccessible = {
                os.path.normcase(os.path.normpath(path))
                for path in (result.get("inaccessible_roots") or [])
            }
            scanned_roots = [
                root for root in (result.get("roots") or [])
                if os.path.normcase(os.path.normpath(root)) not in inaccessible
            ]
            for media_path in list(media_statuses.keys()):
                if cls.__path_in_roots(media_path, scanned_roots):
                    media_statuses.pop(media_path, None)
            for media_path, media_status in (result.get("media_statuses") or {}).items():
                media_status = dict(media_status or {})
                media_status["checked_at"] = checked_at
                media_statuses[media_path] = media_status
            latest[category] = {
                "checked_at": checked_at,
                "server": result.get("server") or "",
                "media_statuses": media_statuses
            }
            history = [record] + (store.get("history") or [])
            store = {"version": 1, "latest": latest, "history": history[:3]}
            cls.__write_subtitle_audit_store(store)
        return store["history"]

    @classmethod
    def __load_subtitle_audit_store(cls):
        with cls._subtitle_audit_lock:
            history_file = cls.__subtitle_audit_path()
            if not os.path.isfile(history_file):
                cls._subtitle_audit_store_cache = None
                return {"version": 1, "latest": {}, "history": []}
            try:
                modified_at = os.path.getmtime(history_file)
                cached = cls._subtitle_audit_store_cache
                if cached and cached[0] == history_file and cached[1] == modified_at:
                    return cached[2]
                with open(history_file, "r", encoding="utf-8") as file_obj:
                    store = json.load(file_obj)
                if not isinstance(store, dict):
                    raise ValueError("字幕检测记录格式无效")
                store = {
                    "version": 1,
                    "latest": store.get("latest") or {},
                    "history": (store.get("history") or [])[:3]
                }
                cls._subtitle_audit_store_cache = (history_file, modified_at, store)
                return store
            except Exception as e:
                ExceptionUtils.exception_traceback(e)
                log.error("【MediaLibrary】读取字幕检测记录失败：%s" % str(e))
                return {"version": 1, "latest": {}, "history": []}

    @classmethod
    def __write_subtitle_audit_store(cls, store):
        history_file = cls.__subtitle_audit_path()
        os.makedirs(os.path.dirname(history_file), exist_ok=True)
        temp_file = "%s.tmp" % history_file
        try:
            with open(temp_file, "w", encoding="utf-8") as file_obj:
                json.dump(store, file_obj, ensure_ascii=False, indent=2)
            os.replace(temp_file, history_file)
            cls._subtitle_audit_store_cache = (history_file, os.path.getmtime(history_file), store)
        finally:
            if os.path.exists(temp_file):
                os.remove(temp_file)

    @classmethod
    def __subtitle_audit_path(cls):
        return os.path.join(Config().get_config_path(), cls._subtitle_audit_filename)

    @staticmethod
    def __path_in_roots(path, roots):
        try:
            path = os.path.abspath(os.path.normpath(path))
            for root in roots or []:
                root = os.path.abspath(os.path.normpath(root))
                if os.path.commonpath([path, root]) == root:
                    return True
        except (OSError, ValueError):
            return False
        return False

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
        histories = transfer_histories or []
        if isinstance(histories, dict):
            group = "movie" if media_type == "movie" else "series"
            candidates = (histories.get("tmdb") or {}).get((group, row_tmdbid)) if row_tmdbid else None
            if not candidates and row_title:
                candidates = (histories.get("title") or {}).get((group, row_title))
            histories = candidates or histories.get("all") or []
        for history in histories:
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

    @classmethod
    def __build_transfer_history_index(cls, histories):
        index = {"all": histories or [], "tmdb": {}, "title": {}}
        for history in histories or []:
            history_type = cls.__history_media_type(history.TYPE)
            if not history_type:
                continue
            group = "movie" if history_type == "movie" else "series"
            tmdbid = str(history.TMDBID or "")
            title = str(history.TITLE or "").strip()
            if tmdbid:
                index["tmdb"].setdefault((group, tmdbid), []).append(history)
            if title:
                index["title"].setdefault((group, title), []).append(history)
        return index

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
    def detect_subtitle_status(cls, media_file, media_streams=None, allow_ffprobe=True):
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
        if allow_ffprobe and not media_streams:
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
            for file_name in cls.__cached_directory_files(media_dir):
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

    @classmethod
    def has_external_subtitle(cls, media_file):
        """快速判断媒体文件是否关联任意外挂字幕。"""
        if not media_file:
            return False
        media_dir = os.path.dirname(media_file)
        media_base = os.path.splitext(os.path.basename(media_file))[0]
        if not media_dir or not os.path.isdir(media_dir):
            return False
        try:
            for file_name in cls.__cached_directory_files(media_dir):
                if os.path.splitext(file_name)[-1].lower() not in RMT_SUBEXT:
                    continue
                sub_base = os.path.splitext(file_name)[0]
                if sub_base == media_base or sub_base.startswith(
                        (f"{media_base}.", f"{media_base}-", f"{media_base}_", f"{media_base}(")):
                    return True
        except Exception as e:
            ExceptionUtils.exception_traceback(e)
        return False

    @classmethod
    def invalidate_subtitle_directory_cache(cls, media_path=None):
        """上传字幕后使目标目录的短时缓存失效。"""
        with cls._subtitle_dir_cache_lock:
            if not media_path:
                cls._subtitle_dir_cache.clear()
                return
            media_dir = media_path if os.path.isdir(media_path) else os.path.dirname(media_path)
            key = os.path.normcase(os.path.normpath(media_dir)) if media_dir else ""
            cls._subtitle_dir_cache.pop(key, None)

    @classmethod
    def __cached_directory_files(cls, media_dir):
        key = os.path.normcase(os.path.normpath(media_dir))
        now = time.monotonic()
        with cls._subtitle_dir_cache_lock:
            cached = cls._subtitle_dir_cache.get(key)
            if cached and now - cached[0] < cls._subtitle_dir_cache_ttl:
                return cached[1]
        file_names = os.listdir(media_dir)
        with cls._subtitle_dir_cache_lock:
            cls._subtitle_dir_cache[key] = (now, file_names)
            while len(cls._subtitle_dir_cache) > cls._subtitle_dir_cache_limit:
                cls._subtitle_dir_cache.pop(next(iter(cls._subtitle_dir_cache)))
        return file_names

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
