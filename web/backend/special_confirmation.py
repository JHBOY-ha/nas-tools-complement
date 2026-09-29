"""Read-only TMDB candidates and file-scoped, explicitly confirmed transfers."""
import os

import log

from app.media.meta import MetaInfo
from app.media.meta.special_resolver import tmdb_type
from app.utils import EpisodeFormat
from app.utils.types import MediaType, RmtMode, SyncType
from app.conf import ModuleConf


def special_file(path):
    """Only single special episodes enter this dialog; directories retain batch behavior."""
    if not path or not os.path.isfile(path):
        return None
    meta = MetaInfo(os.path.basename(path), use_llm=False)
    note = meta.note or {}
    special = note.get("special_episode") or {}
    return meta if note.get("fractional_episode") or (special and not special.get("is_extra")) else None


class SpecialConfirmation:
    def __init__(self, db, media, transfer):
        self.db, self.media, self.transfer = db, media, transfer

    def run(self, data):
        """Resolve the source from its database record, never a submitted file path."""
        try:
            flag = data.get("flag")
            if flag == "unidentification":
                rows = self.db.get_unknown_path_by_id(data.get("id"))
            elif flag == "history":
                rows = self.db.get_transfer_path_by_id(data.get("id"))
            else:
                raise ValueError("识别记录类型无效")
            if not rows:
                raise ValueError("记录已不存在，请刷新列表")
            row = rows[0]
            path = row.PATH if flag == "unidentification" else os.path.join(row.SOURCE_PATH, row.SOURCE_FILENAME)
            meta = special_file(path)
            if not meta:
                raise ValueError("请选择一个仍然存在的特殊集文件；目录请逐文件处理")
            stat = os.stat(path)
            # Nanosecond timestamps exceed JavaScript's safe integer range.
            fingerprint = [str(v) for v in (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)]
            stage = data.get("stage", "search")
            if stage == "search":
                query = str(data.get("query") or meta.get_name() or "").strip()[:200]
                results = self.media.get_tmdb_infos(title=query)
                if results is None:
                    raise ValueError("TMDB 查询失败，请重试")
                works = []
                for item in results:
                    kind = tmdb_type(item.get("media_type"))
                    if kind and item.get("id"):
                        works.append(self.work(item, kind))
                return dict(retcode=0, query=query, filename=os.path.basename(path),
                            fingerprint=fingerprint, works=works)
            kind = tmdb_type(data.get("type"))
            tmdbid = self.number(data.get("tmdb_id"), positive=True)
            if not kind:
                raise ValueError("请选择作品类型")
            info = self.media.get_tmdb_info(mtype=kind, tmdbid=tmdbid)
            if not info or str(info.get("id")) != str(tmdbid):
                raise ValueError("TMDB 作品详情加载失败，请重试")
            # Decimal release labels require a TV episode, never a movie fallback.
            if (meta.note or {}).get("fractional_episode") and kind == MediaType.MOVIE:
                raise ValueError("小数集请选择电视剧及其对应单集")
            seasons = sorted({s["season_number"] for s in info.get("seasons") or []
                              if type(s.get("season_number")) is int and s["season_number"] >= 0})
            episodes, season, ep = [], None, None
            if kind == MediaType.TV:
                season = self.number(data.get("season", seasons[0] if seasons else 0))
                if season not in seasons:
                    raise ValueError("TMDB 未收录该季")
                detail = self.media.get_tmdb_tv_season_detail(tmdbid, season)
                if not detail or not isinstance(detail.get("episodes"), list):
                    raise ValueError("TMDB 剧集加载失败，请重试")
                episodes = [dict(episode=e["episode_number"], title=e.get("name") or "未命名单集",
                                 date=e.get("air_date") or "", overview=e.get("overview") or "",
                                 link=f"https://www.themoviedb.org/tv/{tmdbid}/season/{season}/episode/{e['episode_number']}")
                            for e in detail["episodes"] if type(e.get("episode_number")) is int]
                if stage == "confirm":
                    number = self.number(data.get("episode"))
                    ep = next((e for e in episodes if e["episode"] == number), None)
                    if ep is None:
                        raise ValueError("所选剧集已不存在，请重新查询")
            if stage == "detail":
                return dict(retcode=0, work=self.work(info, kind), seasons=seasons,
                            season=season, episodes=episodes)
            if stage != "confirm" or data.get("confirmed") is not True:
                raise ValueError("请核对 TMDB 信息后确认")
            # TMDB may be slow; recheck after the lookup, immediately before transfer.
            latest = os.stat(path)
            fingerprint = [str(v) for v in (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns)]
            if data.get("fingerprint") != fingerprint:
                raise ValueError("源文件已变化，请关闭窗口并重新查询")
            # Copy SDK/cache data: confirmation is transient and applies to this exact source only.
            info = dict(info)
            if ep and (meta.note or {}).get("fractional_episode"):
                info["_file_episode_confirmation"] = dict(path=path, season=season, episode=ep["episode"])
            mode = ModuleConf.get_enum_item(RmtMode, row.MODE) if row.MODE else None
            ok, message = self.transfer.transfer_media(
                in_from=SyncType.MAN, in_path=path, target_dir=row.DEST or "", rmt_mode=mode,
                tmdb_info=info, media_type=kind, season=season,
                episode=(EpisodeFormat(None, str(ep["episode"])) if ep else None, False))
            if ok and flag == "unidentification":
                self.db.update_transfer_unknown_state(path)
            return dict(retcode=0 if ok else 2, retmsg=message or ("重新识别并整理完成" if ok else "整理失败"))
        except (ValueError, TypeError, OSError) as err:
            return dict(retcode=2, retmsg=str(err))
        except Exception as err:
            log.error("【特殊集确认】查询或整理异常：%s" % err)
            return dict(retcode=2, retmsg="TMDB 查询或整理失败，请查看日志后重试")

    @staticmethod
    def number(value, positive=False):
        if type(value) is not int or value < (1 if positive else 0):
            raise ValueError("TMDB 编号必须为有效整数")
        return value

    @staticmethod
    def work(info, kind):
        type_name = "movie" if kind == MediaType.MOVIE else "tv"
        return dict(tmdb_id=int(info["id"]), type=type_name,
                    title=info.get("name") or info.get("title") or "未命名作品",
                    date=info.get("first_air_date") or info.get("release_date") or "",
                    overview=info.get("overview") or "",
                    link=f"https://www.themoviedb.org/{type_name}/{int(info['id'])}")
