"""Release decimal labels are evidence keys, never TMDB episode numbers."""
import os
import re

import cn2an
from config import RMT_MEDIAEXT

_LABEL = r"\d{1,4}\.\d{1,2}[A-Za-z]?"
# A following dot is a release separator only when it does not start another number,
# except for a complete resolution token (13.5.1080p).
_END = r"(?![A-Za-z0-9])(?!(?:\.\d)(?!\d{2,3}[pi](?:\W|$)))"
# Dotted release metadata follows an integer episode without making it decimal.
_INTEGER_RESOURCE = re.compile(
    r"(?i)(?:S\d{1,2})?EP?\d+\.(?:\d{3,4}[pi]|(?:19|20)\d{2}|"
    r"(?:8|10|12|16)bit|[248]k)(?=[ ._-]|$)")


def episode_key(value):
    match = re.fullmatch(r"(\d+)\.(\d{1,2})([A-Za-z]?)", str(value or "").strip())
    if not match:
        return None
    return "%s.%s%s" % (match[1].lstrip("0") or "0", match[2], match[3].upper())


def protect_fractional_episode(title):
    """Protect explicit decimal labels, including suffixes, before tokenization."""
    if not title:
        return title, None
    stem, ext = os.path.splitext(title)
    if ext.lower() not in RMT_MEDIAEXT:
        stem, ext = title, ""
    # Mask full release dates while retaining offsets into the original filename.
    scan_stem = re.sub(r"(?<!\d)(?:19|20)\d{2}\.(?:0?[1-9]|1[0-2])\.(?:0?[1-9]|[12]\d|3[01])(?!\d)",
                       lambda match: " " * len(match[0]), stem)
    patterns = (
        (r"[\[【](?P<decimal>" + _LABEL + r")[\]】]", "bracket"),
        (r"(?<![A-Za-z0-9])(?P<season>S\d{1,2})?EP?(?P<decimal>" + _LABEL + r")" + _END, "episode_marker"),
        (r"第(?P<decimal>" + _LABEL + r")[集话話]", "chinese"),
        (r"\s+-\s+(?P<decimal>" + _LABEL + r")" + _END, "separator"),
    )
    for pattern, source in patterns:
        for match in re.finditer(pattern, scan_stem, re.I):
            if source == "episode_marker" and _INTEGER_RESOURCE.match(stem[match.start():]):
                continue
            raw = match["decimal"]
            # Bare channel layouts are ambiguous; E5.1 remains an explicit episode.
            if source == "bracket" and raw in ("2.0", "2.1", "5.1", "7.1"):
                continue
            season = match.groupdict().get("season") or ""
            tail = stem[match.end():]
            subtitle = re.match(r"\s*[-–—]\s*([^\[【(]+)", tail)
            # 单集标题只作匹配证据，不应污染用于查作品的片名。
            episode_title = None
            if subtitle:
                episode_title = subtitle[1].strip()
                resource = re.search(r"(?i)(?<!\w)(?:\d{3,4}[pi]|WEB[ ._-]?DL|WEBRip|BluRay|HEVC|[HX]26[45])\b", episode_title)
                metadata_tail = ""
                if resource:
                    metadata_tail = episode_title[resource.start():]
                    episode_title = episode_title[:resource.start()].strip(" ._-")
                tail = metadata_tail + tail[subtitle.end():]
            cleaned = stem[:match.start()] + season + " " + tail
            return cleaned + ext, {
                "raw": raw, "key": episode_key(raw), "source": source,
                "status": "unconfirmed", "source_season": int(season[1:]) if season else None,
                "episode_title": episode_title,
            }
    # Malformed explicit decimals must not fall through to E01. Known resolution
    # suffixes are not decimals; this also avoids taking a prefix of E01.123.
    malformed = re.search(r"(?i)(?<![A-Z0-9])(?:S\d{1,2})?EP?\d+\.\d[A-Z0-9.]*", scan_stem)
    if malformed and not _INTEGER_RESOURCE.match(stem[malformed.start():]):
        return stem[:malformed.start()] + " " + stem[malformed.end():] + ext, {
            "raw": malformed[0], "key": None, "source": "malformed",
            "status": "unconfirmed", "reason": "小数集格式不完整或有歧义",
        }
    # Keep malformed bracket/Chinese/separator labels out of generic integer parsing.
    malformed = re.search(
        r"[\[【]\d+\.\d+[A-Za-z.]*[\]】]|第\d+\.\d+[A-Za-z.]*[集话話]|\s+-\s+\d+\.\d+[A-Za-z.]*",
        scan_stem)
    if malformed:
        raw = malformed[0]
        # Common dotted frame sizes remain resource metadata even in brackets.
        if re.fullmatch(r"[\[【\s-]*(?:1280\.720|1920\.1080|2560\.1440|3840\.2160|4096\.2160)[\]】\s]*", raw):
            return title, None
        if raw not in ("[2.0]", "[2.1]", "[5.1]", "[7.1]", "【2.0】", "【2.1】", "【5.1】", "【7.1】"):
            return stem[:malformed.start()] + " " + stem[malformed.end():] + ext, {
                "raw": raw, "key": None, "source": "malformed", "status": "unconfirmed",
                "reason": "小数集格式不完整或有歧义"}
    return title, None


def release_references(text):
    """Read labelled release references only, not arbitrary decimals in prose."""
    patterns = (
        r"第(?P<season>[\d一二三四五六七八九十]+)季\s*(?:第)?(?P<label>" + _LABEL + r")" + _END,
        r"Season\s+(?P<season>\d+)\s*(?:Episode\s*)?(?P<label>" + _LABEL + r")" + _END,
        r"第(?P<label>" + _LABEL + r")[集话話]",
        r"\bEpisode\s+(?P<label>" + _LABEL + r")" + _END,
        r"#\s*(?P<label>" + _LABEL + r")" + _END,
    )
    spans, found = [], []
    for pattern in patterns:
        for match in re.finditer(pattern, text or "", re.I):
            if any(start <= match.start() < end for start, end in spans):
                continue
            season = match.groupdict().get("season")
            if season:
                season = int(cn2an.cn2an(season, "smart"))
            found.append((episode_key(match["label"]), season, match[0]))
            spans.append(match.span())
    return found
