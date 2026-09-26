import os.path
import regex as re

import log
from app.helper import WordsHelper
from app.media.meta.llm_parser import LLMMetaParser
from app.media.meta._base import MetaBase
from app.media.meta.metaanime import MetaAnime
from app.media.meta.metavideo import MetaVideo
from app.utils.types import MediaType
from config import Config, RMT_MEDIAEXT
from app.media.meta.fractional import protect_fractional_episode
from app.media.meta.release_version import extract_cut
from app.media.meta.special import extract_special


def MetaInfo(title, subtitle=None, mtype=None, use_llm=True):
    """
    媒体整理入口，根据名称和副标题，判断是哪种类型的识别，返回对应对象
    :param title: 标题、种子名、文件名
    :param subtitle: 副标题、描述
    :param mtype: 指定识别类型，为空则自动识别类型
    :param use_llm: 是否启用LLM增强识别
    :return: MetaAnime、MetaVideo
    """

    # 内容过滤由转移忽略词配置控制，解析器不按附加内容标签提前返回。
    # 明确选择电影时，纯数字是片名；未知类型仍兼容 0001.mkv 等剧集编号。
    numeric_title, extension = os.path.splitext(title or "")
    if extension.lower() not in RMT_MEDIAEXT:
        numeric_title = title or ""
    if mtype == MediaType.MOVIE and numeric_title.strip().isdigit():
        meta_info = MetaBase(title, subtitle)
        meta_info.en_name = numeric_title.strip()
        meta_info.type = MediaType.MOVIE
        return meta_info

    original_title = title
    title, fractional_episode = protect_fractional_episode(title)
    # Specials retain release evidence outside the generic integer parser.
    extras_enabled = ((Config().get_config("media") or {}).get("extras") or {}).get("enabled") is True
    title, special = extract_special(title, include_extras=extras_enabled)
    # 原名中的剪辑版先提取，避免标题清理丢失或与 edition 混合。
    title, cut = extract_cut(title)

    # 应用自定义识别词
    title, msg, used_info = WordsHelper().process(title)
    if subtitle:
        subtitle, _, _ = WordsHelper().process(subtitle)

    if msg:
        for msg_item in msg:
            log.warn("【Meta】%s" % msg_item)

    # 判断是否处理文件
    if title and os.path.splitext(title)[-1] in RMT_MEDIAEXT:
        fileflag = True
    else:
        fileflag = False

    if special or mtype == MediaType.ANIME or is_anime(title):
        meta_info = MetaAnime(title, subtitle, fileflag)
    else:
        meta_info = MetaVideo(title, subtitle, fileflag)

    meta_info.ignored_words = used_info.get("ignored")
    meta_info.replaced_words = used_info.get("replaced")
    meta_info.offset_words = used_info.get("offset")

    if special:
        # anitopy's short-title fallback can mistake a resolution bracket for a title.
        name_info = MetaVideo(title, subtitle, fileflag)
        if (re.fullmatch(r"\d{3,4}[pi]", meta_info.get_name() or "", re.I)
                or (not meta_info.year and name_info.year and name_info.get_name())):
            meta_info.cn_name, meta_info.en_name = name_info.cn_name, name_info.en_name
            meta_info._name = name_info.get_name()
            meta_info.year = name_info.year
        special["source_season"] = meta_info.begin_season
        if meta_info.end_season is not None and meta_info.end_season != meta_info.begin_season:
            special["reason"] = "特殊内容发布季范围不明确"
        special["rule_names"] = [name for name in (meta_info.cn_name, meta_info.en_name) if name]
        special["source_year"] = meta_info.year
        # anitopy reads groups from the untouched name; cleaned names must not lose them.
        import anitopy
        origin = anitopy.parse(original_title) or {}
        group = origin.get("release_group")
        # A trailing [OVA]/[OAD] is metadata, never a fansub group.
        leading = re.match(r"^\[([^\]]+)\]", original_title)
        if leading and group == leading[1] and group.upper() not in special["raw"]:
            meta_info.resource_team = group
        elif meta_info.resource_team in special["raw"]:
            meta_info.resource_team = None
        # Ma10p is a bit-depth marker, not the video codec.
        codec = re.search(r"(?i)(?<![A-Z0-9])(x26[45]|h26[45]|HEVC|AVC)(?![A-Z0-9])", original_title)
        if codec:
            meta_info.video_encode = codec[1].upper()

    if use_llm and not fractional_episode and not special:
        # LLM增强识别（配置关闭或调用失败时会自动回落规则识别结果）
        meta_info = LLMMetaParser().merge_into(meta_info=meta_info,
                                               title=title,
                                               subtitle=subtitle,
                                               mtype_hint=mtype)

    meta_info.cut = cut
    meta_info.org_string = original_title

    # 外部强制指定类型优先
    if mtype:
        meta_info.type = mtype

    if fractional_episode:
        # 未确认小数集不得由 LLM 或普通整数解析补空、取整或变成区间。
        meta_info.begin_episode = None
        meta_info.end_episode = None
        meta_info.total_episodes = 0
        meta_info.type = MediaType.TV if not mtype else mtype
        fractional_episode["source_season"] = meta_info.begin_season
        meta_info.note["fractional_episode"] = fractional_episode

    if special:
        # LLM only supplies candidate names; it cannot manufacture final numbering.
        if use_llm and not fractional_episode:
            proposed = LLMMetaParser().parse(title=title, subtitle=subtitle, mtype_hint=mtype)
            special["candidate_names"] = [proposed[key] for key in ("cn_name", "en_name") if proposed.get(key)]
        special["anime_hint"] = special["kind"] in ("OVA", "OAD") or mtype == MediaType.ANIME
        meta_info.begin_episode = meta_info.end_episode = None
        meta_info.total_episodes = 0
        if special.get("formal") and not special["is_extra"]:
            # Formal S00 strings also appear in transfer history and dedupe queries.
            meta_info.begin_season, meta_info.begin_episode = special["formal"]
            meta_info.total_seasons = meta_info.total_episodes = 1
        special["source_filename"] = os.path.basename(original_title)
        meta_info.note["extra" if special["is_extra"] else "special_episode"] = special
        # Fractional confirmation remains authoritative when both label types appear.
        if fractional_episode:
            fractional_episode["episode_title"] = special.get("episode_title") or fractional_episode.get("episode_title")

    return meta_info


def is_anime(name):
    """
    判断是否为动漫
    :param name: 名称
    :return: 是否动漫
    """
    if not name:
        return False
    if re.search(r'【[+0-9XVPI-]+】\s*【', name, re.IGNORECASE):
        return True
    # 带连字符的四位发行年份不作为动漫绝对集号。
    if re.search(r'\s+-\s+(?!(?:19|20)\d{2}(\s+|$))[\dv]{1,4}(\s+|$)', name, re.IGNORECASE):
        return True
    if re.search(r"S\d{2}\s*-\s*S\d{2}|S\d{2}|\s+S\d{1,2}|EP?\d{2,4}\s*-\s*EP?\d{2,4}|EP?\d{2,4}|\s+EP?\d{1,4}", name,
                 re.IGNORECASE):
        return False
    if re.search(r'\[[+0-9XVPI-]+]\s*\[', name, re.IGNORECASE):
        return True
    return False
