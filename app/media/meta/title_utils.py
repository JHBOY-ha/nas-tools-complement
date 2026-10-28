import re


_ANIME_PART_MARKER_RE = re.compile(
    r"(?<![A-Z0-9])(?:COUR|PART)[ ._-]*"
    r"(?:[1-9][0-9]?|VIII|VII|VI|IV|IX|III|II|V|X|I)(?![A-Z0-9]|[.．][0-9])"
    r"|(?<![A-Z0-9])[1-9][0-9]?(?:ST|ND|RD|TH)\s+COUR(?![A-Z0-9])"
    r"|第\s*[0-9一二三四五六七八九十百]{1,4}\s*(?:部分|クール)"
    r"|(?<![A-Z0-9])[1-9][0-9]?\s*クール", re.I)


def strip_anime_part_markers(title):
    """Keep release-part ordinals out of episode parsing; callers retain raw evidence."""
    return _ANIME_PART_MARKER_RE.sub(" ", str(title or ""))


def promote_bracket_title(title):
    """Keep a release group's following title block visible to name parsers."""
    title = str(title or "").replace("【", "[").replace("】", "]")
    match = re.match(r"^(\[[^\]]*(?:字幕组|字幕組|TSDM)[^\]]*\])\s*\[([^\]]+)\]", title, re.I)
    if (match and re.search(r"[\u4e00-\u9fffA-Za-z]", match.group(2))
            and not re.match(r"\d{2,4}年|\d+月|\d+[pP]$|(?:HEVC|AVC|MKV|MP4)\b", match.group(2), re.I)):
        return "%s %s %s" % (match.group(1), match.group(2), title[match.end():])
    return title
