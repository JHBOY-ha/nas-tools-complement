import json
import re
import time
from copy import deepcopy
from urllib.parse import quote

import log
from app.media.tmdbv3api import TMDb, Search, TV, TMDbException
from app.media.meta.title_utils import promote_bracket_title
from app.utils import ExceptionUtils, RequestUtils, StringUtils
from app.utils.llm_client import LLMClient
from app.utils.commons import singleton
from app.utils.types import MediaType
from config import Config, DEFAULT_TMDB_PROXY


@singleton
class LLMMetaParser(object):
    """
    基于 OpenAI 兼容接口的媒体识别增强器
    """
    _allowed_modes = {"rule_first", "llm_first", "hybrid"}
    _llm_fields = [
        "type",
        "cn_name",
        "en_name",
        "year",
        "begin_season",
        "end_season",
        "total_seasons",
        "begin_episode",
        "end_episode",
        "total_episodes",
        "part",
        "resource_type",
        "resource_effect",
        "resource_pix",
        "resource_team",
        "video_encode",
        "audio_encode"
    ]

    def __init__(self):
        self._client = None
        self._client_config = {}
        self._enabled = False
        self._mode = "rule_first"
        self._base_url = ""
        self._api_key = ""
        self._model = ""
        self._timeout = 20
        self._max_tokens = 1024
        self._thinking = ""
        self._confidence_threshold = 0.75
        self._search_context_enable = False
        self._search_max_results = 3
        self._search_timeout = 8
        self._parse_cache = {}
        self._parse_cache_ttl = 60
        self.init_config()

    def init_config(self):
        config = Config().get_config("llm") or {}
        self._client_config = deepcopy(config)
        self._enabled = StringUtils.to_bool(
            config.get("enable", config.get("enabled")), False
        )
        mode = str(config.get("mode") or "rule_first").strip().lower()
        if mode not in self._allowed_modes:
            mode = "rule_first"
        self._mode = mode
        self._base_url = str(config.get("base_url") or config.get("api_base") or "").strip()
        self._api_key = str(config.get("api_key") or "").strip()
        self._model = str(config.get("model") or "").strip()
        self._timeout = self.__parse_int(config.get("timeout"), min_val=1, default=20)
        self._max_tokens = self.__parse_int(config.get("max_tokens"), min_val=1, default=1024)
        self._thinking = config.get("thinking") or ""
        self._confidence_threshold = self.__parse_float(
            config.get("confidence_threshold"), min_val=0, max_val=1, default=0.75
        )
        self._search_context_enable = StringUtils.to_bool(
            config.get("search_context_enable"), False
        )
        self._search_max_results = self.__parse_int(
            config.get("search_max_results"), min_val=1, max_val=10, default=3
        )
        self._search_timeout = self.__parse_int(
            config.get("search_timeout"), min_val=1, max_val=30, default=8
        )
        self._parse_cache = {}
        self._client = None

    def get_status(self, config=None):
        """
        测试连通性（用于设置页测试按钮）。传入 config 时仅使用表单中的临时配置，
        不读取或修改已保存配置。
        """
        try:
            client = LLMClient(config) if config is not None else self.__get_client()
            if not client:
                return False
            return client.get_status()
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            log.error("【Meta】LLM 连接测试失败：%s" % str(err))
            return False

    def parse(self, title, subtitle=None, mtype_hint=None):
        """
        调用 LLM 解析媒体名称并返回标准化字段
        """
        if not title or not self.__is_client_ready(require_enable=True):
            return {}
        cache_key = self.__make_parse_cache_key(title=title, subtitle=subtitle, mtype_hint=mtype_hint)
        cached_result = self.__get_cached_parse_result(cache_key)
        if cached_result is not None:
            return cached_result
        try:
            client = self.__get_client()
            if not client:
                return {}
            hint = ""
            if mtype_hint:
                if mtype_hint == MediaType.MOVIE:
                    hint = "movie"
                elif mtype_hint == MediaType.ANIME:
                    hint = "anime"
                else:
                    hint = "tv"
            external_candidates, candidate_payload = self.__build_external_candidates(
                title=title,
                subtitle=subtitle,
                mtype_hint=mtype_hint
            )
            system_prompt = (
                "你是媒体文件名解析助手。"
                "请严格返回 JSON 对象，不要输出任何额外文本。"
                "字段仅允许："
                "type,cn_name,en_name,year,begin_season,end_season,begin_episode,end_episode,"
                "part,resource_type,resource_effect,resource_pix,resource_team,video_encode,audio_encode,"
                "tmdb_id,tmdb_type,tmdb_season,tmdb_season_name,tmdb_episode,tmdb_pick_reason。"
                "其中 type 只允许 movie/tv/anime；tmdb_type 只允许 movie/tv。"
                "季集号规则："
                "1) begin_season 只填标题/副标题里明确出现的季标记（如 S01、Season 1、第1季、第二季）对应的数字，标题写第几季就填几，不要换算成 TMDB 的季号。"
                "2) 含“月”字的时间表达（如 7月新番、04月新番、2022年7月番）是月份信息，不是季数，禁止据此填写 season。"
                "3) 如果标题是“片名 + 单个数字”且没有明确季标记（例如“西部世界 12”），该数字优先视为单集，填写 begin_episode=12，不要推断多季或区间。"
                "4) 只有原文明确出现区间（如 S01-S02、E01-E03、第1-3集）才填写 end_season/end_episode；单点值不要补 end 字段。"
                "TMDB 季号规则（仅在给出 external_candidates.tmdb 的 seasons 时判断）："
                "1) tmdb_season 只能取自候选 seasons 里的 n，不得自造；判断不出就省略。"
                "2) 发布方写的季号与 TMDB 分季经常不一致：发布版“第4季”可能对应 TMDB 第1季，多季也可能被 TMDB 合并成第1季。要结合 seasons 的 name(季名/篇章名)、year(首播年份)、eps(集数) 判断标题实际对应哪一季，不要照抄标题里的季标记。"
                "3) 标题或副标题里出现篇章名/arc 名且与某季 name 语义一致时（例如“飙马野郎”对应“飙马野郎篇”、“Steel Ball Run”对应“STEEL BALL RUN”），采用该季并把该季显示的季名回填到 tmdb_season_name。"
                "4) 标题季标记与候选证据冲突时以候选证据为准；找不到任何证据时保持 begin_season 原值，不要改。"
                "5) tmdb_episode 填该季内部的集号，用于发布方按放送季编号而 TMDB 合并成单季的情况（如发布版“第4季第18集”在 TMDB 是第1季第84集）；能确定才填，不确定就省略。"
                "候选选择规则（external_candidates.tmdb 有多项时）："
                "1) 优先选片名或别名与标题完全对应的一项；出现同名作品时，用标题/副标题里的年份与候选 year 对齐后再选。"
                "2) 同名条目优先选带 imdb 或 tvdb 外链、votes 更高的那一项：那是 TMDB 的正式条目；没有外链的条目多是用户自建的重复条目（随时可能被合并删除），即使它的季结构看起来更贴合发布编号也不要选它。"
                "3) 年份和外链都对不出时，用类型（电影/剧集/动画）以及候选 aliases 与发布信息（制作组、字幕组、来源站）的吻合度判断；仍无法确定就省略 tmdb_id，不要随便挑一个。"
                "4) 选定后把依据写进 tmdb_pick_reason（一句话，如“片名一致且标题年份2024与候选year相符”）。"
                "年份规则："
                "1) 仅在出现明确四位年份时填写 year（1900-2100），例如 '(2022)'、' 2022 '。"
                "2) '2022年7月番'、'7月新番'、'04月新番' 这类“年+月/仅月”发布时间标签，不作为 year。"
                "3) 存在多个候选年份且无法确定时，省略 year。"
                "4) 可参考 external_candidates 的候选结果回填 year；当候选冲突时优先与标题语义最一致的一项。"
                "5) 若标题仅出现“\\d{4}年\\d{1,2}月番/新番”等发布时间标签，且候选无可靠年份，则 year 留空。"
                "资源字段规则："
                "1) resource_type 仅接受常见来源词：WEB-DL/WEBRip/BluRay/Remux/HDTV/DVD/BDRip。"
                "2) 资源容器或字幕语言（如 MP4/MKV/内封/外挂/简繁/中日双语/GB/CHT）不要写入 resource_type/resource_effect/audio_encode。"
                "2.1) resource_effect 仅填写画质/视觉效果（如 HDR/DoVi/3D/10bit/UHD），字幕与语言标签一律不要填写到 resource_effect。"
                "3) video_encode 仅填写编码词（HEVC/H264/H265/X264/X265/AVC/AV1/VP9）；audio_encode 仅填写音频编码（AAC/AC3/EAC3/DDP/TrueHD/DTS/FLAC/LPCM，可带声道如 5.1/7.1）。"
                "4) resource_type/resource_effect/video_encode/audio_encode 仅在 title/subtitle 明确出现对应关键词时填写，不得猜测或借 external_candidates 脑补。"
                "5) 不确定就留空，不要猜测。"
                "你可能会收到 external_candidates 字段，包含 TMDB/Bangumi 检索候选，仅供参考。"
                "external_candidates.tmdb 的每一项包含 id、name、type、year、aliases(别名)、imdb/tvdb(外链，缺省表示没有)、votes(投票数) 与 seasons(该剧各季列表，n=季号、name=季名、year=首播年份、eps=集数)。"
                "如果你能从 external_candidates.tmdb 明确匹配到目标，可额外返回 tmdb_id(整数)、tmdb_type(movie/tv) 以及 tmdb_season、tmdb_season_name、tmdb_episode、tmdb_pick_reason。"
                "不要返回其他字段。"
            )
            user_prompt = (
                "请解析下列媒体名称并提取结构化信息。\n"
                f"title: {title}\n"
                f"subtitle: {subtitle or ''}\n"
                f"type_hint: {hint}\n"
            )
            if external_candidates:
                user_prompt += f"external_candidates: {external_candidates}\n"
            user_prompt += "若字段无法判断请省略该字段。"
            content = client.complete_text(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                max_tokens=512
            )
            if content:
                log.info("【Meta】LLM原始返回：%s" % self.__shorten_text(content, 2000))
            else:
                log.info("【Meta】LLM原始返回为空")
            parsed = self.__parse_json(content)
            if not parsed:
                self.__set_cached_parse_result(cache_key, {})
                return {}
            result = self.__normalize_result(parsed)
            # 逐层核对：候选存在性、季号存在性、季名证据一致性。
            result.update(self.__verify_candidate_binding(result=result,
                                                          candidate_payload=candidate_payload,
                                                          title=title,
                                                          subtitle=subtitle))
            self.__set_cached_parse_result(cache_key, result)
            return result
        except Exception as err:
            ExceptionUtils.exception_traceback(err)
            log.error("【Meta】LLM 识别失败：%s" % str(err))
            self.__set_cached_parse_result(cache_key, {})
            return {}

    def merge_into(self, meta_info, title, subtitle=None, mtype_hint=None):
        """
        将 LLM 识别结果与规则识别结果合并
        """
        if not meta_info:
            return meta_info

        llm_result = self.parse(title=title, subtitle=subtitle, mtype_hint=mtype_hint)

        note = dict(meta_info.note or {})
        note["llm"] = {
            "enabled": self._enabled,
            "mode": self._mode,
            "applied": False
        }

        if llm_result:
            original_year = meta_info.year
            original_season = meta_info.begin_season
            self.__apply_result(meta_info, llm_result)
            note["llm"].update({
                "applied": True,
                "confidence": llm_result.get("confidence", 0),
                "field_confidence": llm_result.get("field_confidence", {})
            })
            note["llm"]["inferred_year"] = bool(
                not original_year and meta_info.year
                and not re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", title or "")
            )
            if llm_result.get("tmdb_id"):
                note["llm"].update({
                    "tmdb_id": llm_result.get("tmdb_id"),
                    "tmdb_type": llm_result.get("tmdb_type"),
                    "candidate_verified": llm_result.get("candidate_verified", False)
                })
                log.info(
                    "【Meta】LLM直出TMDB候选：id=%s, type=%s"
                    % (llm_result.get("tmdb_id"), llm_result.get("tmdb_type") or "")
                )
                pick_reason = self.__clean_text(llm_result.get("tmdb_pick_reason"), max_len=120)
                if pick_reason:
                    note["llm"]["pick_reason"] = pick_reason
                    log.info("【Meta】LLM候选选择依据：%s" % pick_reason)
            release_season = self.__parse_int(llm_result.get("release_season", original_season),
                                              min_val=0, max_val=999)
            if release_season is not None:
                note["llm"]["release_season"] = release_season
            if llm_result.get("season_verified"):
                note["llm"].update({
                    "tmdb_season": llm_result.get("tmdb_season"),
                    "tmdb_season_name": llm_result.get("tmdb_season_name"),
                    "tmdb_episode": llm_result.get("tmdb_episode"),
                    "season_verified": True,
                    "season_evidence": llm_result.get("season_evidence")
                })
                log.info(
                    "【Meta】LLM季号通过校验：TMDB季=%s(%s), 依据=%s, 标题季标记=%s"
                    % (llm_result.get("tmdb_season"),
                       llm_result.get("tmdb_season_name") or "",
                       llm_result.get("season_evidence") or "",
                       self.__clean_text(release_season, max_len=10) or "无")
                )
            elif llm_result.get("tmdb_season") is not None:
                log.warn("【Meta】LLM季号未通过校验，忽略：%s" % llm_result.get("tmdb_season"))

        meta_info.note = note

        # A verified external candidate can correct the rule parser's default movie type.
        if llm_result and llm_result.get("candidate_verified") and not mtype_hint:
            if llm_result.get("type") == MediaType.ANIME:
                meta_info.type = MediaType.ANIME
            elif meta_info.type == MediaType.MOVIE and llm_result.get("tmdb_type") == "tv":
                meta_info.type = MediaType.TV

        # 外部强制指定类型优先
        if mtype_hint:
            meta_info.type = mtype_hint

        return meta_info

    def __apply_result(self, meta_info, llm_result):
        for field in self._llm_fields:
            if field not in llm_result:
                continue
            value = llm_result.get(field)
            if self.__is_empty(value):
                continue
            if not self.__should_apply(field, getattr(meta_info, field, None), llm_result):
                continue
            setattr(meta_info, field, value)

    def __should_apply(self, field, current_value, llm_result):
        if self._mode == "llm_first":
            return True
        if self._mode == "rule_first":
            return self.__is_empty(current_value)

        # hybrid：空值直接补齐，非空值按置信度覆盖
        if self.__is_empty(current_value):
            return True
        field_confidence = llm_result.get("field_confidence", {}).get(field)
        if field_confidence is None:
            field_confidence = llm_result.get("confidence", 0)
        return field_confidence >= self._confidence_threshold

    def __normalize_result(self, parsed):
        result = {}
        media_type = self.__normalize_type(parsed.get("type"))
        if media_type:
            result["type"] = media_type

        for key in [
            "cn_name",
            "en_name",
            "part",
            "resource_type",
            "resource_effect",
            "resource_pix",
            "resource_team",
            "video_encode",
            "audio_encode"
        ]:
            text = self.__clean_text(parsed.get(key))
            if text:
                result[key] = text

        year = self.__parse_int(parsed.get("year"), min_val=1900, max_val=2100)
        if year:
            result["year"] = str(year)

        begin_season = self.__parse_int(parsed.get("begin_season"), min_val=1, max_val=999)
        end_season = self.__parse_int(parsed.get("end_season"), min_val=1, max_val=999)
        if begin_season and end_season and end_season < begin_season:
            begin_season, end_season = end_season, begin_season
        if begin_season:
            result["begin_season"] = begin_season
        if end_season and end_season != begin_season:
            result["end_season"] = end_season
        if begin_season:
            result["total_seasons"] = (result.get("end_season") or begin_season) - begin_season + 1

        begin_episode = self.__parse_int(parsed.get("begin_episode"), min_val=1, max_val=99999)
        end_episode = self.__parse_int(parsed.get("end_episode"), min_val=1, max_val=99999)
        if begin_episode and end_episode and end_episode < begin_episode:
            begin_episode, end_episode = end_episode, begin_episode
        if begin_episode:
            result["begin_episode"] = begin_episode
        if end_episode and end_episode != begin_episode:
            result["end_episode"] = end_episode
        if begin_episode:
            result["total_episodes"] = (result.get("end_episode") or begin_episode) - begin_episode + 1

        result["confidence"] = self.__parse_float(
            parsed.get("confidence"), min_val=0, max_val=1, default=0
        )
        field_confidence = {}
        raw_field_confidence = parsed.get("field_confidence")
        if isinstance(raw_field_confidence, dict):
            for key in self._llm_fields:
                if key not in raw_field_confidence:
                    continue
                val = self.__parse_float(raw_field_confidence.get(key), min_val=0, max_val=1)
                if val is not None:
                    field_confidence[key] = val
        result["field_confidence"] = field_confidence
        tmdb_id = self.__parse_int(parsed.get("tmdb_id"), min_val=1, max_val=999999999)
        if tmdb_id:
            result["tmdb_id"] = tmdb_id
            tmdb_type = self.__normalize_tmdb_type(parsed.get("tmdb_type"))
            if not tmdb_type:
                if result.get("type") == MediaType.MOVIE:
                    tmdb_type = "movie"
                elif result.get("type") in [MediaType.TV, MediaType.ANIME]:
                    tmdb_type = "tv"
            if tmdb_type:
                result["tmdb_type"] = tmdb_type
        tmdb_season = self.__parse_int(parsed.get("tmdb_season"), min_val=0, max_val=999)
        if tmdb_season is not None:
            result["tmdb_season"] = tmdb_season
        tmdb_episode = self.__parse_int(parsed.get("tmdb_episode"), min_val=1, max_val=99999)
        if tmdb_episode is not None:
            result["tmdb_episode"] = tmdb_episode
        tmdb_season_name = self.__clean_text(parsed.get("tmdb_season_name"), max_len=60)
        if tmdb_season_name:
            result["tmdb_season_name"] = tmdb_season_name
        tmdb_pick_reason = self.__clean_text(parsed.get("tmdb_pick_reason"), max_len=120)
        if tmdb_pick_reason:
            result["tmdb_pick_reason"] = tmdb_pick_reason
        return result

    def __build_external_candidates(self, title, subtitle=None, mtype_hint=None):
        if not self._search_context_enable:
            return "", {}
        query_list = self.__build_search_queries(title=title, subtitle=subtitle)
        if not query_list:
            return "", {}
        query = query_list[0]
        year_hint = self.__extract_year_hint(title, subtitle)

        payload = {}
        tmdb_candidates = self.__search_candidates_by_queries(
            search_func=lambda q: self.__search_tmdb_candidates(query=q, mtype_hint=mtype_hint, year=year_hint),
            query_list=query_list
        )
        bangumi_candidates = self.__search_candidates_by_queries(
            search_func=self.__search_bangumi_candidates,
            query_list=query_list
        )

        if tmdb_candidates:
            payload["tmdb"] = tmdb_candidates
        if bangumi_candidates:
            payload["bangumi"] = bangumi_candidates

        log.info(
            "【Meta】LLM检索增强候选：query=%s, 年份=%s, tmdb=%s, bangumi=%s"
            % (
                self.__shorten_text(query, 80),
                year_hint or "无",
                len(tmdb_candidates),
                len(bangumi_candidates)
            )
        )
        if not payload:
            return "", {}
        season_total = sum(len(item.get("seasons") or []) for item in tmdb_candidates)
        log.info("【Meta】LLM候选季列表：候选=%s, 季条目=%s" % (len(tmdb_candidates), season_total))
        return self.__serialize_candidate_payload(payload), payload

    @classmethod
    def __serialize_candidate_payload(cls, payload, budget=6000):
        """
        在预算内序列化候选信息：保证输出始终是合法 JSON，优先保留 id/季列表，逐级丢弃别名、年份、
        Bangumi 候选等次要信息，最后才裁剪候选数量。
        """
        compact = deepcopy(payload)
        text = json.dumps(compact, ensure_ascii=False)
        if len(text) <= budget:
            return text

        # 1) 丢弃别名（对季号判断贡献最低）
        for item in compact.get("tmdb") or []:
            item.pop("aliases", None)
        text = json.dumps(compact, ensure_ascii=False)
        if len(text) <= budget:
            return text

        # 2) 丢弃 Bangumi 候选
        compact.pop("bangumi", None)
        text = json.dumps(compact, ensure_ascii=False)
        if len(text) <= budget:
            return text

        # 3) 季列表瘦身：只保留季号与截断的季名
        for item in compact.get("tmdb") or []:
            seasons = []
            for season in item.get("seasons") or []:
                slim = {"n": season.get("n")}
                name = str(season.get("name") or "")[:12]
                if name:
                    slim["name"] = name
                seasons.append(slim)
            if seasons:
                item["seasons"] = seasons
        text = json.dumps(compact, ensure_ascii=False)
        if len(text) <= budget:
            return text

        # 4) 只保留季号
        for item in compact.get("tmdb") or []:
            if item.get("seasons"):
                item["seasons"] = [{"n": season.get("n")} for season in item["seasons"]]
        text = json.dumps(compact, ensure_ascii=False)
        if len(text) <= budget:
            return text

        # 5) 只有首个候选保留季列表
        for item in (compact.get("tmdb") or [])[1:]:
            item.pop("seasons", None)
        text = json.dumps(compact, ensure_ascii=False)
        if len(text) <= budget:
            return text

        # 6) 末位裁剪候选，仍保留 JSON 结构完整
        candidates = compact.get("tmdb") or []
        while len(candidates) > 1 and len(text) > budget:
            candidates.pop()
            text = json.dumps(compact, ensure_ascii=False)
        return text

    def __fetch_tv_candidate_extra(self, tmdb_id):
        """
        取单个电视剧候选的季列表、别名与外部链接（一次 TMDB 请求，结果走请求缓存）。
        """
        if not tmdb_id:
            return {}
        try:
            detail = TV().details(tmdb_id, append_to_response="alternative_titles,external_ids")
        except Exception as err:
            log.debug("【Meta】TMDB候选季信息获取失败：%s" % str(err))
            return {}
        if not detail:
            return {}
        seasons = []
        for item in detail.get("seasons") or []:
            number = self.__parse_int(item.get("n", item.get("season_number")), min_val=0, max_val=999)
            if number is None:
                continue
            season = {"n": number}
            name = self.__clean_text(item.get("name"), max_len=40)
            if name:
                season["name"] = name
            air_date = str(item.get("air_date") or "").strip()
            if len(air_date) >= 4 and air_date[:4].isdigit():
                season["year"] = air_date[:4]
            episode_count = self.__parse_int(item.get("episode_count"), min_val=0, max_val=99999)
            if episode_count is not None:
                season["eps"] = episode_count
            seasons.append(season)
        aliases = []
        titles = detail.get("alternative_titles")
        for item in (titles.get("results") if titles else None) or []:
            country = str(item.get("iso_3166_1") or "").upper()
            if country not in ("CN", "HK", "TW", "JP", "US"):
                continue
            alias = self.__clean_text(item.get("title"), max_len=60)
            if alias and alias not in aliases:
                aliases.append(alias)
        extra = {}
        if seasons:
            extra["seasons"] = seasons
        if aliases:
            extra["aliases"] = aliases[:8]
        external = detail.get("external_ids")
        if external:
            imdb_id = self.__clean_text(external.get("imdb_id"), max_len=30)
            if imdb_id:
                extra["imdb"] = imdb_id
            tvdb_id = self.__parse_int(external.get("tvdb_id"), min_val=1, max_val=99999999)
            if tvdb_id is not None:
                extra["tvdb"] = tvdb_id
        votes = self.__parse_int(detail.get("vote_count"), min_val=0, max_val=9999999)
        if votes is not None:
            extra["votes"] = votes
        return extra

    def __verify_candidate_binding(self, result, candidate_payload, title, subtitle=None):
        """
        核对 LLM 给出的 TMDB 作品与季号：候选必须真实存在，季号必须在该候选的季列表内，
        标题季标记与候选季名证据冲突时以季名为准；拿不出证据时保留标题季标记。
        """
        verified = {"candidate_verified": False, "season_verified": False}
        if not result or not isinstance(candidate_payload, dict):
            return verified
        tmdb_id = result.get("tmdb_id")
        if not tmdb_id:
            return verified
        tmdb_type = str(result.get("tmdb_type") or "").lower()
        candidate = None
        for item in candidate_payload.get("tmdb") or []:
            if str(item.get("id")) != str(tmdb_id):
                continue
            item_type = str(item.get("type") or item.get("media_type") or "").lower()
            if tmdb_type and item_type and item_type != tmdb_type:
                continue
            candidate = item
            break
        if not candidate:
            return verified
        verified["candidate_verified"] = True

        seasons = {}
        for item in candidate.get("seasons") or []:
            number = self.__parse_int(item.get("n"), min_val=0, max_val=999)
            if number is not None:
                seasons[number] = item
        release_season = self.__extract_release_season(title, subtitle)
        if release_season is not None:
            verified["release_season"] = release_season
        season = self.__parse_int(result.get("tmdb_season"), min_val=0, max_val=999)
        if season is None:
            return verified
        if season not in seasons:
            log.warn("【Meta】LLM季号不在候选季列表中，忽略：%s" % result.get("tmdb_season"))
            return verified

        evidence_text = " ".join([
            str(title or ""),
            str(subtitle or ""),
            str(result.get("cn_name") or ""),
            str(result.get("en_name") or "")
        ])
        name_matched = self.__match_seasons_by_name(evidence_text, seasons)
        evidence = "llm_only"
        if name_matched:
            if season in name_matched:
                evidence = "season_name"
            elif len(name_matched) == 1:
                log.warn("【Meta】LLM季号与TMDB季名证据不符，按季名修正：%s -> %s"
                         % (result.get("tmdb_season"), name_matched[0]))
                season = name_matched[0]
                evidence = "season_name_override"
        if season not in seasons:
            return verified
        if release_season is not None and season != release_season and evidence == "llm_only":
            log.warn("【Meta】LLM季号%s与标题季标记%s冲突且缺少季名证据，保留标题季标记"
                     % (season, release_season))
            return verified
        verified["season_verified"] = True
        verified["tmdb_season"] = season
        verified["season_evidence"] = evidence
        season_name = self.__clean_text(result.get("tmdb_season_name"), max_len=60)
        if not season_name:
            season_name = self.__clean_text(seasons.get(season, {}).get("name"), max_len=60)
        if season_name:
            verified["tmdb_season_name"] = season_name
        return verified

    @classmethod
    def __match_seasons_by_name(cls, text, seasons):
        """
        用季名（篇章名/arc 名）反向匹配标题文本，返回命中的季号列表。
        """
        normalized_text = cls.__normalize_match_text(text)
        if not normalized_text:
            return []
        matched = []
        for number, season in seasons.items():
            name = cls.__normalize_match_text(season.get("name") or "")
            if not name:
                continue
            for suffix in ("season", "specials", "special", "part", "篇", "編", "编", "章", "季"):
                if name.endswith(suffix) and len(name) > len(suffix) + 1:
                    name = name[: -len(suffix)]
            if len(name) < 2 or name.isdigit():
                continue
            if name in normalized_text:
                matched.append(number)
        return matched

    @staticmethod
    def __normalize_match_text(text):
        if not text:
            return ""
        text = str(text).lower()
        text = re.sub(r"[\s._\-\[\]\(\)\{\}【】（）「」:：·・/\\|,，+&]+", "", text)
        return text

    @classmethod
    def __extract_release_season(cls, title, subtitle=None):
        """
        取标题里明确写出的发布季号（S02、Season 2、第2季），取不到返回 None。
        """
        text = " ".join([str(title or ""), str(subtitle or "")])
        if not text.strip():
            return None
        patterns = [
            r"(?<![A-Za-z0-9])S(\d{1,2})(?!\d)",
            r"(?i)\bSeason[ ._-]*(\d{1,2})\b",
            r"第\s*(\d{1,2})\s*[季部]",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if not match:
                continue
            number = cls.__parse_int(match.group(1), min_val=0, max_val=999)
            if number is not None:
                return number
        match = re.search(r"第\s*([一二三四五六七八九十两]+)\s*[季部]", text)
        if match:
            try:
                import cn2an
                return int(cn2an.cn2an(match.group(1), mode="smart"))
            except Exception:
                return None
        return None

    @classmethod
    def __build_search_query(cls, title, subtitle=None):
        text = "%s %s" % (
            cls.__strip_title_tail_noise(title),
            subtitle or ""
        )
        return cls.__normalize_query_for_search(text)

    @classmethod
    def __build_search_queries(cls, title, subtitle=None):
        query_set = []
        raw_title = promote_bracket_title(str(title or "").strip())
        core_query = cls.__extract_core_title_query(raw_title)
        if core_query:
            query_set.append(core_query)
        base_query = cls.__build_search_query(title=title, subtitle=subtitle)
        if base_query:
            query_set.append(base_query)
        normalized_title = cls.__normalize_query_for_search(raw_title)
        if normalized_title:
            query_set.append(normalized_title)

        # 标题含中英文双名时，尝试分别检索每一段
        for segment in re.split(r"[／/|]+", raw_title):
            segment_query = cls.__normalize_query_for_search(segment)
            if segment_query:
                query_set.append(segment_query)

        # 额外构造去季集号候选
        reduced_candidates = []
        for query in list(query_set):
            reduced = re.sub(r"第\s*\d+\s*季", " ", query, flags=re.IGNORECASE)
            reduced = re.sub(r"第\s*\d+\s*[集话回]", " ", reduced, flags=re.IGNORECASE)
            reduced = re.sub(r"\bS\d{1,2}\b", " ", reduced, flags=re.IGNORECASE)
            reduced = re.sub(r"\b(?:E|EP)\d{1,4}\b", " ", reduced, flags=re.IGNORECASE)
            reduced = re.sub(r"\s+", " ", reduced).strip()
            reduced = cls.__normalize_query_for_search(reduced)
            if reduced:
                reduced_candidates.append(reduced)
        query_set.extend(reduced_candidates)

        # 纯方括号命名的发布资源（如 [组名][作品名][01][1080p HEVC]）在剥离尾部括号后会丢失片名，
        # 这里回退到"最像标题的括号段"，避免整条识别链路拿不到检索词。
        bracket_query = cls.__extract_bracket_title_query(raw_title)
        if bracket_query:
            query_set.append(bracket_query)

        # 去重并保持顺序
        query_list = []
        for query in query_set:
            if query and query not in query_list:
                query_list.append(query)
        return query_list[:6]

    @classmethod
    def __extract_bracket_title_query(cls, raw_title):
        text = promote_bracket_title(str(raw_title or "").strip())
        best_query = ""
        best_score = 0
        for first, second in re.findall(r"\[([^\]]+)]|【([^】]+)】", text):
            block = (first or second or "").strip()
            if cls.__is_release_meta_block(block):
                continue
            query = cls.__normalize_query_for_search(block)
            if not query:
                continue
            score = len(query)
            if " " in block:
                score += 10
            if re.search(r"[a-z]", block):
                score += 5
            if score > best_score:
                best_query, best_score = query, score
        return best_query

    @classmethod
    def __is_release_meta_block(cls, block):
        """
        判断括号段是否只是发布/技术信息（集号、画质、编码、字幕语言、发布组等）。
        """
        text = str(block or "").strip()
        if not text or len(text) < 3:
            return True
        if re.fullmatch(r"[\d\s._\-]+", text):
            return True
        if re.search(r"(?i)\b\d{3,4}[pi]\b|\b\d{1,2}\s*bit\b", text):
            return True
        if re.search(
                r"(?i)\b(?:HEVC|AVC|H\.?26[45]|X26[45]|AV1|VP9|AAC|AC3|EAC3|DDP?|TRUEHD|DTS(?:-?HD)?|"
                r"LPCM|FLAC|ATMOS|WEB[- ]?DL|WEB[- ]?RIP|BLU-?RAY|BDRIP|REMUX|HDTV|UHD|HDR|DOVI|SDR|"
                r"MKV|MP4|AVI|CHS|CHT|BIG5|JPSC)\b",
                text):
            return True
        if re.search(r"字幕|简繁|内封|内嵌|外挂|中字|双语|繁体|简体|生肉|熟肉|音轨|配音|国配|粤语", text):
            return True
        # 发布组标记：常见后缀词，或全大写带分隔符的组名
        if re.search(r"(?i)(?:raws|fansub|subs?|team|group|studio|house|压制|发布)\s*$", text):
            return True
        if re.fullmatch(r"[A-Z0-9]+(?:[-_.][A-Z0-9]+)+", text):
            return True
        return False

    @classmethod
    def __normalize_query_for_search(cls, text):
        text = str(text or "").strip()
        if not text:
            return ""
        text = cls.__strip_title_tail_noise(text)
        text = re.sub(r"\[[^\]]*]", " ", text)
        text = re.sub(r"[【】\[\]\(\)\{\}]+", " ", text)
        text = re.sub(r"[._\-]+", " ", text)
        text = text.replace("+", " ")
        text = re.sub(
            r"\b(?:S\d{1,2}E\d{1,4}|S\d{1,2}|E\d{1,4}|EP\d{1,4}|SEASON|WEB[- ]?DL|WEBRIP|WEB|"
            r"BLURAY|BDRIP|REMUX|HDTV|UHD|2160P|1080P|720P|"
            r"HEVC|H\.?26[45]|X26[45]|AVC|AV1|VP9|"
            r"AAC|AC3|EAC3|DDP?|TRUEHD|DTS(?:-?HD)?|LPCM|ATMOS|"
            r"NF|NETFLIX|AMZN|AMAZON|ATVP|APPLETV|DSNP|DISNEY\+?|HMAX|MAX|PARAMOUNT\+?|CR|KKTV|BAHA|"
            r"BLACKTV)\b",
            " ",
            text,
            flags=re.IGNORECASE
        )
        text = re.sub(
            r"\b(?:MKV|MP4|AVI|WMV|TS|M2TS|MOV|FLV|RMVB|ISO|MPG|MPEG)\b",
            " ",
            text,
            flags=re.IGNORECASE
        )
        text = re.sub(r"\b\d\.\d\b", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\b\d{4}\b", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\bH\s*264\b|\bH\s*265\b", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\bX\s*264\b|\bX\s*265\b", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:120]

    @classmethod
    def __extract_core_title_query(cls, raw_title):
        raw_title = cls.__strip_title_tail_noise(raw_title)
        if not raw_title:
            return ""
        text = re.sub(r"\[[^\]]*]", " ", raw_title)
        text = re.sub(r"[【】\[\]\(\)\{\}]+", " ", text)
        text = text.replace("+", " ")
        slash_parts = [part.strip() for part in re.split(r"\s*[／/|]\s*", text) if part.strip()]
        if len(slash_parts) > 1 and StringUtils.is_chinese(slash_parts[0]):
            text = slash_parts[0]
        tokens = re.split(r"[.\s/_\-]+", text)
        title_tokens = []
        for token in tokens:
            token = str(token or "").strip()
            if not token:
                continue
            if cls.__is_meta_query_token(token):
                break
            title_tokens.append(token)
            if len(title_tokens) >= 8:
                break
        if not title_tokens:
            return ""
        return cls.__normalize_query_for_search(" ".join(title_tokens))

    @staticmethod
    def __strip_title_tail_noise(text):
        text = str(text or "").strip()
        if not text:
            return ""

        text = re.sub(
            r"(?i)\.(?:mkv|mp4|avi|wmv|ts|m2ts|mov|flv|rmvb|iso|mpg|mpeg)$",
            "",
            text
        ).strip()

        while True:
            original_text = text
            bracket_trimmed = re.sub(
                r"\s*(?:\[[^\]]*]|【[^】]*】|\([^\)]*\)|\{[^}]*})\s*$",
                "",
                text
            ).strip()
            if bracket_trimmed and bracket_trimmed != text:
                text = bracket_trimmed

            updated = re.sub(
                r"(?i)\s*[-_.]+\s*(?:第\s*\d{1,4}\s*[集话回]|(?:E|EP)\s*\d{1,4}|\d{1,4}(?:v\d{1,2})?)\s*$",
                "",
                text
            ).strip()
            updated = re.sub(r"[\s._\-]+$", "", updated).strip()
            if updated == original_text:
                break
            text = updated

        return text

    @staticmethod
    def __is_meta_query_token(token):
        token = str(token or "").strip()
        if not token:
            return False
        up = token.upper()
        if re.match(r"^S\d{1,2}(E\d{1,4})?$", up):
            return True
        if re.match(r"^(E|EP)\d{1,4}$", up):
            return True
        if re.match(r"^\d{3,4}P$", up):
            return True
        if re.match(r"^\d\.\d$", up):
            return True
        if re.match(r"^\d{4}$", up):
            return True
        if up in {
            "WEB", "WEBDL", "WEB-DL", "WEBRIP", "BLURAY", "BDRIP", "REMUX", "HDTV", "UHD",
            "HEVC", "H264", "H265", "X264", "X265", "AVC", "AV1", "AAC", "AC3", "EAC3", "DD", "DDP",
            "TRUEHD", "DTS", "LPCM", "ATMOS", "NF", "NETFLIX", "AMZN", "ATVP", "DSNP", "HMAX",
            "MAX", "PARAMOUNT", "PARAMOUNT+", "CR", "KKTV", "BAHA", "BLACKTV"
        }:
            return True
        if up.endswith("TV") and len(up) >= 5:
            return True
        return False

    def __search_candidates_by_queries(self, search_func, query_list):
        candidates = []
        seen = set()
        for query in query_list:
            if len(candidates) >= self._search_max_results:
                break
            query_candidates = search_func(query) or []
            for item in query_candidates:
                item_key = json.dumps(item, ensure_ascii=False, sort_keys=True)
                if item_key in seen:
                    continue
                seen.add(item_key)
                candidates.append(item)
                if len(candidates) >= self._search_max_results:
                    break
        return candidates

    def __make_parse_cache_key(self, title, subtitle=None, mtype_hint=None):
        mtype = ""
        if mtype_hint:
            mtype = str(getattr(mtype_hint, "value", mtype_hint))
        return "%s|%s|%s|%s|%s|%s" % (
            str(title or "").strip(),
            str(subtitle or "").strip(),
            mtype,
            self._mode,
            self._model,
            str(self._search_context_enable)
        )

    def __get_cached_parse_result(self, cache_key):
        cache_item = self._parse_cache.get(cache_key)
        if not cache_item:
            return None
        ts = cache_item.get("ts", 0)
        if time.time() - ts > self._parse_cache_ttl:
            self._parse_cache.pop(cache_key, None)
            return None
        return deepcopy(cache_item.get("result", {}))

    def __set_cached_parse_result(self, cache_key, result):
        self._parse_cache[cache_key] = {
            "ts": time.time(),
            "result": deepcopy(result or {})
        }

    def __search_tmdb_candidates(self, query, mtype_hint=None, year=None):
        app_conf = Config().get_config("app") or {}
        tmdb_key = str(app_conf.get("rmt_tmdbkey") or "").strip()
        if not tmdb_key:
            return []
        try:
            tmdb = TMDb()
            laboratory_conf = Config().get_config("laboratory") or {}
            if laboratory_conf.get("tmdb_proxy"):
                tmdb.domain = DEFAULT_TMDB_PROXY
            else:
                tmdb.domain = app_conf.get("tmdb_domain")
            tmdb.cache = True
            tmdb.api_key = tmdb_key
            tmdb.language = "zh-CN"
            tmdb.proxies = Config().get_proxies()

            search = Search()
            params = {"query": query, "page": 1}
            if mtype_hint == MediaType.MOVIE:
                raw_results = search.movies(params)
            elif mtype_hint in [MediaType.TV, MediaType.ANIME]:
                raw_results = search.tv_shows(params)
            else:
                raw_results = search.multi(params)

            raw_items = []
            for item in (raw_results or []):
                genres = getattr(item, "genre_ids", None) or []
                if mtype_hint == MediaType.ANIME and 16 not in genres:
                    continue
                raw_items.append(item)
            # 同名作品（原版/翻拍/同名剧集）优先把年份对得上的候选排前面，交给LLM判断；
            # 这里只重排不做过滤，避免发布年份与首播年份不同的剧集被筛掉。
            raw_items = self.__prioritize_raw_by_year(raw_items, year)

            candidates = []
            index = 0
            while index < len(raw_items) and len(candidates) < self._search_max_results:
                candidate = self.__build_tmdb_candidate(raw_items[index], mtype_hint)
                index += 1
                if candidate:
                    candidates.append(candidate)

            # 同名重复条目：补看紧随其后的同名候选，拿到外链信息后再决定保留哪一条
            if candidates:
                built_names = {self.__candidate_group_key(item.get("name")) for item in candidates}
                for item in raw_items[index:index + 3]:
                    name = self.__clean_text(
                        getattr(item, "title", None) or getattr(item, "name", None), max_len=120)
                    if not name or self.__candidate_group_key(name) not in built_names:
                        continue
                    candidate = self.__build_tmdb_candidate(item, mtype_hint)
                    if candidate:
                        candidates.append(candidate)

            candidates = self.__filter_duplicate_candidates(candidates)
            return candidates[:self._search_max_results]
        except TMDbException as err:
            log.debug("【Meta】TMDB候选检索失败：%s" % str(err))
            return []
        except Exception as err:
            log.debug("【Meta】TMDB候选检索异常：%s" % str(err))
            return []

    def __build_tmdb_candidate(self, item, mtype_hint=None):
        genres = getattr(item, "genre_ids", None) or []
        if mtype_hint == MediaType.ANIME and 16 not in genres:
            return {}
        name = self.__clean_text(
            getattr(item, "title", None) or getattr(item, "name", None), max_len=120)
        if not name:
            return {}
        release_date = str(
            getattr(item, "release_date", "") or getattr(item, "first_air_date", "")
        ).strip()
        candidate_year = release_date[:4] if len(release_date) >= 4 and release_date[:4].isdigit() else ""
        item_type = self.__clean_text(getattr(item, "media_type", ""), max_len=20).lower()
        if not item_type:
            if mtype_hint == MediaType.MOVIE:
                item_type = "movie"
            elif mtype_hint in [MediaType.TV, MediaType.ANIME]:
                item_type = "tv"
        candidate = {
            "id": getattr(item, "id", None),
            "name": name,
            "genre_ids": genres
        }
        if candidate_year:
            candidate["year"] = candidate_year
        if item_type:
            candidate["type"] = item_type
        if item_type == "tv" and candidate["id"]:
            candidate.update(self.__fetch_tv_candidate_extra(candidate["id"]))
        return candidate

    @staticmethod
    def __candidate_authority(candidate):
        """
        候选的权威性：带 IMDb/TVDB 外链的是 TMDB 正式条目，重复条目通常一个外链都没有。
        """
        if not candidate:
            return 0
        return 1 if (candidate.get("imdb") or candidate.get("tvdb")) else 0

    @classmethod
    def __filter_duplicate_candidates(cls, candidates):
        """
        同名作品出现多条候选时，丢弃没有外链的疑似重复条目，保留正式条目。
        """
        if not candidates:
            return candidates
        grouped = {}
        for candidate in candidates:
            grouped.setdefault(cls.__candidate_group_key(candidate.get("name")), []).append(candidate)
        kept = []
        for candidate in candidates:
            group = grouped.get(cls.__candidate_group_key(candidate.get("name"))) or []
            if len(group) > 1 and not cls.__candidate_authority(candidate):
                authoritative = [item for item in group if cls.__candidate_authority(item)]
                if authoritative:
                    log.warn(
                        "【Meta】同名候选存在带外链的正式条目，忽略疑似重复条目：id=%s(%s) -> 保留 id=%s(%s)"
                        % (candidate.get("id"), candidate.get("name"),
                           authoritative[0].get("id"), authoritative[0].get("name"))
                    )
                    continue
            kept.append(candidate)
        return kept

    @classmethod
    def __candidate_group_key(cls, name):
        """
        用于判断候选是否同一作品的键：忽略大小写、标点，并去掉名称尾部的年份后缀
        （重复条目常见写法是「作品名（2016）」）。
        """
        key = cls.__normalize_match_text(name)
        return re.sub(r"(?:19|20)\d{2}$", "", key).strip()

    @staticmethod
    def __raw_release_year(item):
        date = str(getattr(item, "release_date", "") or getattr(item, "first_air_date", "") or "").strip()
        return date[:4] if len(date) >= 4 and date[:4].isdigit() else ""

    @classmethod
    def __prioritize_raw_by_year(cls, items, year):
        if not year or not items:
            return items
        target = str(year)
        matched = [item for item in items if cls.__raw_release_year(item) == target]
        if not matched:
            return items
        matched_ids = {id(item) for item in matched}
        return matched + [item for item in items if id(item) not in matched_ids]

    @classmethod
    def __extract_year_hint(cls, title, subtitle=None):
        """
        从标题里取用于候选排序的年份；"2026年7月番"这类放送月份标签不算年份。
        """
        text = " ".join([str(title or ""), str(subtitle or "")])
        for match in re.finditer(r"(?<!\d)(19\d{2}|20\d{2})(?!\d)", text):
            tail = text[match.end():match.end() + 4]
            if re.match(r"\s*年\s*\d{1,2}\s*月", tail):
                continue
            return match.group(1)
        return None

    def __search_bangumi_candidates(self, query):
        try:
            req_url = "https://api.bgm.tv/search/subject/%s" % quote(query)
            response = RequestUtils(
                proxies=Config().get_proxies(),
                timeout=self._search_timeout
            ).get_res(
                url=req_url,
                params={
                    "type": 2,
                    "responseGroup": "small",
                    "max_results": self._search_max_results
                }
            )
            if not response or not response.ok:
                return []
            data = response.json()
            items = data.get("list") or data.get("data") or []
            candidates = []
            for item in items[:self._search_max_results]:
                name = self.__clean_text(item.get("name"), max_len=120)
                name_cn = self.__clean_text(item.get("name_cn"), max_len=120)
                if not name and not name_cn:
                    continue
                air_date = str(item.get("air_date") or item.get("date") or "").strip()
                year = air_date[:4] if len(air_date) >= 4 and air_date[:4].isdigit() else ""
                candidate = {
                    "id": item.get("id")
                }
                if name_cn:
                    candidate["name_cn"] = name_cn
                if name:
                    candidate["name"] = name
                if year:
                    candidate["year"] = year
                candidates.append(candidate)
            return candidates
        except Exception as err:
            log.debug("【Meta】Bangumi候选检索异常：%s" % str(err))
            return []

    def __extract_content(self, response):
        if not response or not getattr(response, "choices", None):
            return ""
        message = self.__extract_message(response)
        content = self.__get_value(message, "content", "")
        return self.__extract_text_content(content)

    @classmethod
    def __extract_reasoning_content(cls, response):
        if not response or not getattr(response, "choices", None):
            return ""
        message = cls.__extract_message(response)
        reasoning_content = cls.__get_value(message, "reasoning_content", "")
        return cls.__extract_text_content(reasoning_content)

    @classmethod
    def __extract_message(cls, response):
        choice = response.choices[0]
        return cls.__get_value(choice, "message")

    @classmethod
    def __extract_finish_reason(cls, response):
        if not response or not getattr(response, "choices", None):
            return ""
        return cls.__get_value(response.choices[0], "finish_reason", "") or ""

    @classmethod
    def __extract_text_content(cls, content):
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            text_list = []
            for item in content:
                if isinstance(item, str):
                    text_list.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        text_list.append(str(text))
                else:
                    text = cls.__get_value(item, "text") or cls.__get_value(item, "content")
                    if text:
                        text_list.append(str(text))
            return "\n".join(text_list).strip()
        return str(content).strip() if content else ""

    @staticmethod
    def __get_value(obj, key, default=None):
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @staticmethod
    def __parse_json(content):
        if not content:
            return {}
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(content[start:end + 1])
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    @staticmethod
    def __normalize_type(value):
        if not value:
            return None
        text = str(value).strip().lower()
        if text in ["movie", "mov", "film", "电影"]:
            return MediaType.MOVIE
        if text in ["tv", "series", "电视剧"]:
            return MediaType.TV
        if text in ["anime", "ani", "动漫", "动画"]:
            return MediaType.ANIME
        return None

    @staticmethod
    def __normalize_tmdb_type(value):
        if not value:
            return None
        text = str(value).strip().lower()
        if text in ["movie", "mov", "film", "电影"]:
            return "movie"
        if text in ["tv", "series", "show", "电视剧", "anime", "ani", "动漫", "动画"]:
            return "tv"
        return None

    @staticmethod
    def __clean_text(value, max_len=200):
        if value is None:
            return ""
        text = str(value).strip()
        text = re.sub(r"\s+", " ", text)
        if not text:
            return ""
        return text[:max_len]

    @staticmethod
    def __is_empty(value):
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, (int, float)):
            return value == 0
        if isinstance(value, (list, dict, tuple, set)):
            return len(value) == 0
        return False

    @staticmethod
    def __parse_int(value, min_val=None, max_val=None, default=None):
        if value is None or str(value).strip() == "":
            return default
        try:
            number = int(float(value))
        except Exception:
            return default
        if min_val is not None and number < min_val:
            return default
        if max_val is not None and number > max_val:
            return default
        return number

    @staticmethod
    def __parse_float(value, min_val=None, max_val=None, default=None):
        if value is None or str(value).strip() == "":
            return default
        try:
            number = float(value)
        except Exception:
            return default
        if min_val is not None and number < min_val:
            return default
        if max_val is not None and number > max_val:
            return default
        return number

    def __is_client_ready(self, require_enable=True):
        if require_enable and not self._enabled:
            return False
        if not self._base_url or not self._api_key or not self._model:
            return False
        return True

    def __get_client(self):
        if not self.__is_client_ready(require_enable=False):
            return None
        if not self._client:
            client_config = deepcopy(self._client_config)
            client_config.update({
                "base_url": self._base_url,
                "api_key": self._api_key,
                "model": self._model,
                "timeout": self._timeout,
                "max_tokens": self._max_tokens,
                "thinking": self._thinking,
                "enable": self._enabled
            })
            self._client = LLMClient(client_config)
        return self._client

    @staticmethod
    def __shorten_text(text, max_len):
        if not text:
            return ""
        text = str(text).strip()
        if len(text) <= max_len:
            return text
        return "%s ...[truncated]" % text[:max_len]
