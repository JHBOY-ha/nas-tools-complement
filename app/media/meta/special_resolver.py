"""Confirm special content against typed TMDB identities before any publication."""
from config import Config
from app.utils.types import MediaType
from app.media.meta.special import normalized, requires_confirmation, special_references
from app.media.meta.llm_parser import LLMMetaParser


def tmdb_type(value):
    if value in (MediaType.MOVIE, "movie", "MOVIE"):
        return MediaType.MOVIE
    if value in (MediaType.TV, MediaType.ANIME, "tv", "TV", "ANIME"):
        return MediaType.TV
    return None


def identity(info):
    return (tmdb_type(info.get("media_type")), str(info.get("id"))) if info else None


class SpecialResolver:
    """Use batch-local caches; failures never poison later recognition batches."""
    def __init__(self, media, cache=None):
        self.media = media
        self.cache = cache if cache is not None else {}
        language = getattr(media.tmdb, "language", "zh-CN")
        self.language = language if isinstance(language, str) else "zh-CN"

    def cached(self, key, fetch):
        key = ("special", self.language) + key
        if key not in self.cache:
            try:
                self.cache[key] = fetch()
            except Exception:
                self.cache[key] = None
        return self.cache[key]

    def detail(self, kind, tmdbid):
        return self.cached(("detail", kind, str(tmdbid)), lambda: self.media.get_tmdb_info(
            mtype=kind, tmdbid=tmdbid, append_to_response="alternative_titles"))

    def season(self, info, number):
        return self.cached(("season", str(info["id"]), number),
                           lambda: self.media.get_tmdb_tv_season_detail(info["id"], number))

    def search(self, names, year=None, anime=False):
        candidates = {}
        for name in dict.fromkeys(name for name in names if name):
            items = self.cached(("search", name), lambda: self.media.get_tmdb_infos(title=name))
            # A full page is not proof of an exhaustive, unique result.
            if items is None or len(items) >= 20:
                raise ValueError("作品候选查询失败或结果不完整")
            for item in items:
                kind = tmdb_type(item.get("media_type"))
                if not kind or not item.get("id"):
                    continue
                info = self.detail(kind, item["id"])
                if not info:
                    raise ValueError("作品详情查询失败")
                genres = info.get("genre_ids") or [g.get("id") for g in info.get("genres") or []]
                if anime and "16" not in {str(g) for g in genres}:
                    continue
                actual_year = str(info.get("release_date") or info.get("first_air_date") or "")[:4]
                if year and kind == MediaType.MOVIE and str(year) != actual_year:
                    continue
                titles = [info.get(k) for k in ("title", "name", "original_title", "original_name")]
                aliases = info.get("alternative_titles") or {}
                titles.extend(a.get("title") for a in aliases.get("titles", aliases.get("results", [])))
                if normalized(name) not in {normalized(t) for t in titles if t}:
                    continue
                candidates[identity(info)] = info
        return list(candidates.values())

    def parent(self, meta, bound):
        if bound:
            if not tmdb_type(bound.get("media_type")) or not bound.get("id"):
                raise ValueError("绑定的作品身份无效")
            return bound
        note = requires_confirmation(meta)
        names = note["rule_names"] + note.get("candidate_names", [])
        found = self.search(names, note.get("source_year"), note.get("anime_hint"))
        if not found:
            # Reuse the name-candidate workflow from fix-meta-name-retrieval; names
            # from Bangumi still require a typed TMDB identity and animation genre.
            aliases = LLMMetaParser().get_alias_candidates(meta.get_name())
            found = self.search(aliases, note.get("source_year"), note.get("anime_hint"))
        if len(found) != 1:
            raise ValueError("主体作品无唯一可靠 TMDB 候选")
        return found[0]

    def episodes(self, info):
        seasons = info.get("seasons")
        if not seasons:
            info = self.detail(MediaType.TV, info["id"]) or {}
            seasons = info.get("seasons")
        if not isinstance(seasons, list) or not seasons:
            raise ValueError("TMDB 季列表不完整")
        result = []
        seen = set()
        for entry in sorted(seasons, key=lambda s: s.get("season_number", -1)):
            number = entry.get("season_number")
            if type(number) is not int or number < 0 or number in seen:
                raise ValueError("TMDB 季列表无效")
            seen.add(number)
            detail = self.season(info, number)
            items = detail.get("episodes") if detail else None
            count = entry.get("episode_count")
            if not isinstance(items, list) or (type(count) is int and len(items) != count):
                raise ValueError("TMDB 季集数据不完整")
            numbers = set()
            for ep in items:
                self.validate_episode(info, number, ep)
                if ep["episode_number"] in numbers:
                    raise ValueError("TMDB 集列表重复")
                numbers.add(ep["episode_number"])
                result.append((number, ep))
        return result

    @staticmethod
    def validate_episode(info, season, ep):
        if (type(ep.get("episode_number")) is not int or ep["episode_number"] < 1
                or ep.get("season_number", season) != season
                or str(ep.get("show_id", info["id"])) != str(info["id"])):
            raise ValueError("TMDB 单集身份无效")

    def target_episode(self, info, season, number):
        if type(season) is not int or season < 0 or type(number) is not int or number < 1:
            raise ValueError("正式季集编号无效")
        detail = self.season(info, season)
        if not detail or not isinstance(detail.get("episodes"), list):
            raise ValueError("目标季查询失败")
        items = [ep for ep in detail["episodes"] if ep.get("episode_number") == number]
        if len(items) != 1:
            raise ValueError("目标单集不存在或重复")
        self.validate_episode(info, season, items[0])
        return season, items[0]

    def configured_target(self, note, parent):
        rules = (Config().get_config("media") or {}).get("special_episode_mappings") or []
        matches = []
        for rule in rules:
            if (tmdb_type(rule.get("source_type")) != identity(parent)[0]
                    or str(rule.get("source_tmdb_id")) != str(parent["id"])
                    or str(rule.get("kind", "")).upper() != note["kind"]):
                continue
            # Missing source evidence may only be selected by an exact filename.
            selectors = {"source_filename": note["source_filename"], "source_season": note.get("source_season"),
                         "source_number": note.get("number"), "source_title": note.get("episode_title")}
            if any(key in rule and str(rule[key]) != str(value) for key, value in selectors.items()):
                continue
            if not any(rule.get(key) is not None for key in ("source_filename", "source_number", "source_title")):
                raise ValueError("特殊集映射必须限定文件名、发布编号或单集标题")
            if not note.get("number") and not note.get("episode_title") and not rule.get("source_filename"):
                raise ValueError("无编号特殊集映射必须限定完整源文件名")
            matches.append(rule)
        if len(matches) > 1:
            raise ValueError("特殊集配置映射重复或冲突")
        if matches and (not isinstance(matches[0].get("target"), dict) or not matches[0]["target"]):
            raise ValueError("特殊集配置缺少有效目标")
        return matches[0]["target"] if matches else None

    @staticmethod
    def context_allows(note, parent, target, season, number, context):
        if not context:
            return True
        # No implicit cross-work/cross-type inheritance from a download task.
        if identity(parent) != identity(target):
            return False
        saved = context.get("special_target")
        if saved and not note.get("is_extra"):
            # The torrent's verified target is a constraint, not evidence that an
            # arbitrary member is that episode. Legacy records are reverified too.
            if (tmdb_type(saved.get("media_type")) != identity(target)[0]
                    or str(saved.get("tmdb_id")) != str(target["id"])
                    or saved.get("season") != season or saved.get("episode") != number):
                return False
        if identity(target)[0] == MediaType.MOVIE:
            return True
        seasons, episodes = context.get("seasons") or [], context.get("episodes") or []
        if note.get("is_extra"):
            release_seasons = context.get("release_seasons") or seasons
            return (not episodes and (note.get("source_season") is None or not release_seasons
                                      or note["source_season"] in release_seasons))
        if context.get("numbering") == "release":
            return ((not seasons or note.get("source_season") in seasons)
                    and (not episodes or note.get("number") in {str(e) for e in episodes}))
        if (not seasons or season in seasons) and (not episodes or number in episodes):
            return True
        return (context.get("numbering") == "tmdb" and context.get("scope") == "season_pack"
                and not episodes and season == 0 and note.get("source_season") is not None
                and note["source_season"] in (context.get("release_seasons") or []))

    def resolve(self, meta, bound=None, context=None, manual=None):
        note = requires_confirmation(meta)
        try:
            if note.get("reason"):
                raise ValueError(note["reason"])
            parent = self.parent(meta, bound)
            note["parent"] = {"media_type": identity(parent)[0].name, "id": parent["id"]}
            meta.set_tmdb_info(parent)
            if note.get("is_extra"):
                if not self.context_allows(note, parent, parent, None, None, context):
                    raise ValueError("附加内容与选集任务约束冲突")
                note["status"] = "confirmed"
                return meta
            target = self.configured_target(note, parent)
            if manual:
                def target_key(value):
                    return (tmdb_type(value.get("media_type")), str(value.get("tmdb_id")),
                            value.get("season"), value.get("episode"))
                if target and target_key(target) != target_key(manual):
                    raise ValueError("手动指定与特殊集配置映射冲突")
                target = manual
            ep = None
            target_info = parent
            evidence = None
            if target:
                kind = tmdb_type(target.get("media_type"))
                if not kind or not target.get("tmdb_id"):
                    raise ValueError("特殊集映射目标无效")
                target_info = self.detail(kind, target["tmdb_id"])
                if not target_info:
                    raise ValueError("特殊集映射目标查询失败")
                if kind == MediaType.TV:
                    season, ep = self.target_episode(target_info, target.get("season"), target.get("episode"))
                evidence = "人工指定并核验目标存在"
            elif note.get("formal"):
                if identity(parent)[0] != MediaType.TV:
                    raise ValueError("正式季集编号与电影身份冲突")
                season, ep = self.target_episode(parent, *note["formal"])
                evidence = "文件中的正式季集编号"
            elif identity(parent)[0] == MediaType.TV:
                candidates = []
                all_episodes = self.episodes(parent)
                # Standalone OVA series (TMDB type=Video) use their own regular numbering.
                regular_seasons = {s for s, _ in all_episodes if s > 0}
                for s, episode in all_episodes:
                    # An OVA release year may differ from the parent show's premiere.
                    if note.get("source_year") and episode.get("air_date") and str(note["source_year"]) != episode["air_date"][:4]:
                        continue
                    refs = special_references("%s\n%s" % (episode.get("name", ""), episode.get("overview", "")))
                    title_ok = bool(note.get("episode_title") and normalized(note["episode_title"]) == normalized(episode.get("name")))
                    same_refs = [ref for ref in refs if ref[:2] == (note["kind"], note.get("number"))]
                    season_conflict = any(ref[2] is not None and note.get("source_season") is not None
                                          and ref[2] != note["source_season"] for ref in same_refs)
                    ref_ok = bool(same_refs) and not season_conflict
                    if title_ok and (season_conflict or (refs and not ref_ok and note.get("number"))):
                        raise ValueError("特殊集标题与发布编号证据冲突")
                    video_ok = (parent.get("type") == "Video" and note.get("number")
                                and episode["episode_number"] == int(note["number"])
                                and (s == note.get("source_season") or
                                     (note.get("source_season") is None and regular_seasons == {s})))
                    if title_ok or ref_ok or video_ok:
                        candidates.append((s, episode))
                if not candidates and note.get("episode_title"):
                    # A full independent title may identify a movie; a bare OVA label
                    # never triggers a cross-type fallback.
                    standalone = self.search([note["episode_title"]], note.get("source_year"), note.get("anime_hint"))
                    if len(standalone) == 1 and identity(standalone[0])[0] == MediaType.MOVIE:
                        target_info = standalone[0]
                        evidence = "独立作品完整标题"
                    else:
                        raise ValueError("作品已确认，特殊集无唯一证据，保留待确认")
                elif len(candidates) != 1:
                    raise ValueError("作品已确认，特殊集无唯一证据，保留待确认")
                if candidates:
                    season, ep = candidates[0]
                    evidence = ep.get("name") or "TMDB 明确发布编号"
            else:
                # A cleaned parent movie title alone does not identify an attached OVA.
                if not note.get("episode_title"):
                    if not note.get("source_year") or note["kind"] not in ("OVA", "OAD"):
                        raise ValueError("主体为电影，附加文件缺少独立作品证据")
                    if bound:
                        # A previously confirmed standalone movie can survive a
                        # restart only if this file independently identifies it.
                        if not (context or {}).get("special_target"):
                            raise ValueError("主体为电影，附加文件缺少独立作品证据")
                        found = self.search(note["rule_names"], note["source_year"], note.get("anime_hint"))
                        if len(found) != 1 or identity(found[0]) != identity(parent):
                            raise ValueError("独立电影文件与下载任务身份冲突")
                    evidence = "独立 OVA 的唯一完整片名及明确年份"
                else:
                    candidates = self.search([note["episode_title"]], note.get("source_year"), note.get("anime_hint"))
                    movies = [i for i in candidates if identity(i)[0] == MediaType.MOVIE and identity(i) != identity(parent)]
                    if len(candidates) != 1 or len(movies) != 1:
                        raise ValueError("附加文件无唯一独立电影目标")
                    target_info = movies[0]
                    evidence = "独立作品完整标题"
            season, number = (season, ep["episode_number"]) if ep else (None, None)
            if not self.context_allows(note, parent, target_info, season, number, context):
                raise ValueError("特殊集映射与下载任务约束冲突")
            meta.begin_season = meta.end_season = meta.begin_episode = meta.end_episode = None
            meta.set_tmdb_info(target_info)
            meta.begin_season, meta.begin_episode = season, number
            meta.total_seasons = meta.total_episodes = 1 if ep else 0
            note.update(status="confirmed", provider="tmdb", target_type=identity(target_info)[0].name,
                        target_id=target_info["id"], episode_id=ep.get("id") if ep else None)
            meta.note["episode_mapping"] = {"source_season": note.get("source_season"),
                "source_episode": note.get("number"), "target_season": season,
                "target_episode": number, "evidence": evidence, "provider": "tmdb"}
            meta.skip_reason = None
        except Exception as err:
            note.update(status="unconfirmed", reason=str(err))
            meta.skip_reason = "特殊内容待确认：%s" % err
        return meta
