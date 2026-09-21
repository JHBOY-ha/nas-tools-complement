import re


def promote_bracket_title(title):
    """Keep a release group's following title block visible to name parsers."""
    title = str(title or "").replace("【", "[").replace("】", "]")
    match = re.match(r"^(\[[^\]]*(?:字幕组|字幕組|TSDM)[^\]]*\])\s*\[([^\]]+)\]", title, re.I)
    if (match and re.search(r"[\u4e00-\u9fffA-Za-z]", match.group(2))
            and not re.match(r"\d{2,4}年|\d+月|\d+[pP]$|(?:HEVC|AVC|MKV|MP4)\b", match.group(2), re.I)):
        return "%s %s %s" % (match.group(1), match.group(2), title[match.end():])
    return title
