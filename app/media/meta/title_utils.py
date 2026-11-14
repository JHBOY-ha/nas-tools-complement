import re


def promote_bracket_title(title, release_group=None):
    """Keep a release group's following title block visible to name parsers."""
    title = str(title or "").replace("【", "[").replace("】", "]")
    match = re.match(r"^(\[[^\]]+\])\s*\[([^\]]+)\]", title)
    # Only a separately identified release group can broaden the legacy rule.
    # A lone title bracket remains ineligible; callers without group evidence
    # still use the legacy group-name markers.
    known_group = match and (re.search(r"字幕组|字幕組|TSDM", match[1], re.I)
        or (release_group and match[1][1:-1].strip().casefold() == str(release_group).strip().casefold()))
    if (known_group and "[" not in match[2] and re.search(r"[\u4e00-\u9fffA-Za-z]", match.group(2))
            and not re.fullmatch(r"国漫|日漫|美漫|动漫|动画|电影|电视剧|综艺|国剧|韩剧|日剧|美剧|简体|繁体", match[2])
            # Completion blocks describe episodes, even though END contains letters.
            and not re.fullmatch(r"\s*\d{1,4}[\s_]*(?:END|FIN(?:AL|ALE)?|COMPLETE)\s*", match[2], re.I)
            and not re.match(r"\d{2,4}年|\d+月|\d+[pP](?:\b|_)|(?:HEVC|AVC|MKV|MP4|"
                             r"[HX]26[45]|AAC|FLAC|OPUS|BD|WEB[ ._-]?DL|TV\s*\d|S\d|EP?\d)\b",
                             match.group(2), re.I)):
        return "%s %s %s" % (match.group(1), match.group(2), title[match.end():])
    return title
