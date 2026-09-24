import os.path
import regex as re

import log
from app.helper import WordsHelper
from app.media.meta.llm_parser import LLMMetaParser
from app.media.meta._base import MetaBase
from app.media.meta.metaanime import MetaAnime
from app.media.meta.metavideo import MetaVideo
from app.utils.types import MediaType
from config import RMT_MEDIAEXT


def explicit_extra_reason(title):
    """Recognize only standalone bracketed extras in the file's own name."""
    if not title:
        return None
    name = os.path.basename(title)
    for block in re.findall(r"[\[【]([^\]】]+)[\]】]", name):
        labels = re.split(r"\s*[&+＋]\s*", block.strip().upper())
        if labels and all(re.fullmatch(r"(?:NCOP|NCED|ED|PV|SP)\d*", label)
                          for label in labels):
            return "附加内容标签：%s" % block
    return None


def protect_fractional_episode(title):
    """Remove only explicit decimal episode markers before generic tokenization."""
    if not title:
        return title, None
    stem, extension = os.path.splitext(title)
    if extension.lower() not in RMT_MEDIAEXT:
        stem, extension = title, ""
    patterns = (
        # 单位数字 [5.1] 常表示声道；裸方括号须有两位整数部分。
        (r"[\[【](\d{2,3}\.\d{1,2})[\]】]", "bracket"),
        (r"(?i)(?<![A-Z0-9])E(\d{1,3}\.\d{1,2})(?![\d.A-Z])", "episode_marker"),
        (r"\s+-\s+(\d{2,3}\.\d{1,2})(?![\d.])", "separator"),
    )
    for pattern, source in patterns:
        match = re.search(pattern, stem)
        if match:
            cleaned = stem[:match.start()] + " " + stem[match.end():]
            return cleaned + extension, {"raw": match.group(1), "source": source,
                                         "status": "unconfirmed"}
    return title, None


def MetaInfo(title, subtitle=None, mtype=None, use_llm=True):
    """
    媒体整理入口，根据名称和副标题，判断是哪种类型的识别，返回对应对象
    :param title: 标题、种子名、文件名
    :param subtitle: 副标题、描述
    :param mtype: 指定识别类型，为空则自动识别类型
    :param use_llm: 是否启用LLM增强识别
    :return: MetaAnime、MetaVideo
    """

    # 在自定义词、规则和 LLM 处理前判断原始文件标签，保留括号上下文。
    extra_reason = explicit_extra_reason(title)
    if extra_reason:
        meta_info = MetaBase(title, subtitle)
        meta_info.skip_reason = extra_reason
        return meta_info

    original_title = title
    title, fractional_episode = protect_fractional_episode(title)

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

    if mtype == MediaType.ANIME or is_anime(title):
        meta_info = MetaAnime(title, subtitle, fileflag)
    else:
        meta_info = MetaVideo(title, subtitle, fileflag)

    meta_info.ignored_words = used_info.get("ignored")
    meta_info.replaced_words = used_info.get("replaced")
    meta_info.offset_words = used_info.get("offset")

    if use_llm and not fractional_episode:
        # LLM增强识别（配置关闭或调用失败时会自动回落规则识别结果）
        meta_info = LLMMetaParser().merge_into(meta_info=meta_info,
                                               title=title,
                                               subtitle=subtitle,
                                               mtype_hint=mtype)

    # 外部强制指定类型优先
    if mtype:
        meta_info.type = mtype

    if fractional_episode:
        # 未确认小数集不得由 LLM 或普通整数解析补空、取整或变成区间。
        meta_info.org_string = original_title
        meta_info.begin_episode = None
        meta_info.end_episode = None
        meta_info.total_episodes = 0
        meta_info.type = MediaType.TV if not mtype else mtype
        meta_info.note["fractional_episode"] = fractional_episode

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
