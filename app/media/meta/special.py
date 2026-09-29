"""Release labels describe content, not TMDB types or episode ordinals."""
import os
import re
from functools import lru_cache

import cn2an

from config import RMT_MEDIAEXT

# Prefer the complete word when reading an invalid suffix with match(), not only
# when fullmatch() can backtrack past the shorter SP prefix.
_SPECIAL = r"OVA|OAD|SPECIAL|SP"
_EXTRA = r"NCOP|NCED|OP|ED|PV|TRAILER|INTERVIEW|BEHIND[ _-]THE[ _-]SCENES"
_LABEL = re.compile(r"(?P<kind>" + _SPECIAL + "|" + _EXTRA + r")\s*(?P<number>\d{1,4})?", re.I)
_TECH = re.compile(r"(?i)(?<![A-Z0-9])(?:Ma|Hi)?(?:8|10|12)p(?![A-Z0-9])")


def normalized(value):
    """Only punctuation/case normalization, never fuzzy episode matching."""
    return re.sub(r"[\W_]+", "", str(value or "").casefold())


def extract_special(title, include_extras=False):
    """Recognize bounded metadata fields; leave ordinary title words alone."""
    title = title or ""
    stem, ext = os.path.splitext(title)
    if ext.lower() not in RMT_MEDIAEXT:
        stem, ext = title, ""
    matches = []
    malformed = False
    for block in re.finditer(r"[\[【]([^\]】]+)[\]】]", stem):
        parts = re.split(r"\s*[&+]\s*", block[1].strip())
        labels = [_LABEL.fullmatch(part) for part in parts]
        if all(labels):
            matches.extend((label, block.span()) for label in labels)
        elif re.match(r"(?i)^(?:" + _SPECIAL + r")\s*\d", block[1]):
            label = _LABEL.match(block[1])
            if label:
                matches.append((label, block.span()))
                malformed = True
    # Consume unbracketed ranges before the single-label suffix fallback can
    # mistake OVA01-OVA02 for only OVA02. Ordinary title words stay out of scope.
    ambiguous = (r"(?i)(?:\s+-\s+|(?<!\w)(?=(?:OVA|OAD)\s*\d))"
                 r"(?P<content>(?P<label>(?:" + _SPECIAL + r")\s*\d{1,4})"
                 r"(?:[.．]\d[A-Z0-9.]*|\s*(?:[-–—~～至到/＋+&,]|to\b|and\b)\s*"
                 r"(?:(?:" + _SPECIAL + r")\s*)?\d+[A-Z0-9.]*)+)"
                 r"(?=\s*\[|\s+-\s+|$)")
    for match in re.finditer(ambiguous, stem):
        if not any(start <= match.start("content") < end for _, (start, end) in matches):
            matches.append((_LABEL.fullmatch(match["label"]), match.span("content")))
            malformed = True
    # Unbracketed labels require a release separator or numbered OVA/OAD suffix.
    for match in re.finditer(r"(?i)(?:\s+-\s+)((?:" + _SPECIAL + "|" + _EXTRA +
                             r")\s*\d{0,4})(?=\s+-\s+|\s*\[|$)", stem):
        label = _LABEL.fullmatch(match[1])
        if label:
            matches.append((label, match.span(1)))
    if not matches:
        match = re.search(r"(?i)(?<!\w)((?:OVA|OAD)\s*\d{1,4})(?=\s*\[|$)", stem)
        if match:
            matches.append((_LABEL.fullmatch(match[1]), match.span(1)))
    formal = re.search(r"(?i)(?<![A-Z0-9])S00EP?(\d{1,4})(?![A-Z0-9])", stem)
    formal_range = bool(formal and re.match(r"(?i)\s*-\s*(?:E|EP)?\d", stem[formal.end():]))
    formal_list = []
    cursor = formal.end() if formal else len(stem)
    while formal:
        following = re.match(r"(?i)[ ._+&]+(?:S(\d{1,2}))?EP?(\d{1,4})(?![A-Z0-9])", stem[cursor:])
        if not following:
            break
        formal_list.append((int(following[1] or 0), int(following[2])))
        cursor += following.end()
    consecutive = bool(formal_list) and all(
        season == 0 and episode == int(formal[1]) + offset
        for offset, (season, episode) in enumerate(formal_list, 1))
    if (formal_range or consecutive) and not matches:
        # Preserve the normal parser's explicit continuous multi-episode support
        # for dots, spaces and plus signs as well as hyphenated ranges.
        if consecutive:
            # The generic parser deliberately caps unmarked multi-file guesses
            # at two episodes. Canonicalize this fully enumerated continuous list
            # into its explicit range syntax before that safeguard is applied.
            stem = (stem[:formal.start()] + "S00E%02d-E%02d" % (int(formal[1]), formal_list[-1][1])
                    + stem[cursor:])
            return stem + ext, None
        return title, None
    if not matches and not formal:
        return title, None
    special_matches = [m for m in matches if m[0]["kind"].upper() in ("OVA", "OAD", "SP", "SPECIAL")]
    if not special_matches and matches and not include_extras:
        return title, None
    selected = special_matches or matches
    labels = {(re.sub(r"[ _-]", "", m["kind"].upper()),
               str(int(m["number"])) if m["number"] else None) for m, _ in selected}
    is_extra = bool(matches and not special_matches)
    kind, number = sorted(labels, key=str)[0] if labels else ("FORMAL", None)
    data = {"kind": kind, "number": number, "raw": [m[0] for m, _ in selected],
            "status": "unconfirmed", "episode_title": None,
            "formal": (0, int(formal[1])) if formal else None,
            "source_filename": os.path.basename(title), "is_extra": is_extra}
    if malformed or formal_range or formal_list:
        data["reason"] = "特殊集发布编号范围不明确，保留待确认"
    if len(labels) > 1 and not (is_extra and all(k in ("NCOP", "NCED", "OP", "ED") for k, _ in labels)):
        data["reason"] = "特殊内容标签或编号冲突"
    if is_extra:
        data["category"] = {"PV": "trailers", "TRAILER": "trailers", "INTERVIEW": "interviews",
                            "BEHINDTHESCENES": "behind the scenes"}.get(kind, "other")
    spans = {span for _, span in matches}
    if selected:
        end = max(span[1] for _, span in selected)
        subtitle = re.match(r"\s*[-–—]\s*([^\[【]+)", stem[end:])
        if subtitle:
            text = subtitle[1]
            resource = re.search(r"(?i)(?<!\w)(?:\d{3,4}[pi]|WEB[ ._-]?DL|BluRay|[HX]26[45])\b", text)
            stop = resource.start() if resource else len(text)
            data["episode_title"] = text[:stop].strip(" ._-") or None
            spans.add((end, end + subtitle.start(1) + stop))
    for start, end in sorted(spans, reverse=True):
        stem = stem[:start] + " " + stem[end:]
    # Keep codec/resolution fields but remove known bit-depth shorthands from names.
    stem = _TECH.sub(" ", stem)
    return stem + ext, data


def special_references(text):
    """Reuse bounded pure-text evidence, returning a fresh collection to callers."""
    text = text or ""
    # Oversized overviews are parsed in full without occupying the process cache.
    parse = _cached_special_references if len(text) <= 2048 else _parse_special_references
    return set(parse(text))


@lru_cache(maxsize=2048)
def _cached_special_references(text):
    # Only immutable evidence is shared; resource-specific decisions are not cached.
    return tuple(_parse_special_references(text))


def _parse_special_references(text):
    """Only labelled references are evidence; generic integers are never ordinals."""
    result = set()
    # Consume ambiguous decimals/ranges as a whole so neither endpoint becomes an
    # integer reference (including ranges that repeat the label, e.g. OVA 1-OVA 2).
    pattern = (r"(?i)(?<![A-Z0-9])(" + _SPECIAL + r")[ ._-]*(\d{1,4})(?![A-Z0-9])"
               r"(?P<ambiguous>(?:[.．]\d[A-Z0-9.]*|\s*(?:[-–—~～至到/＋+&,]|to\b|and\b)\s*"
               r"(?:(?:" + _SPECIAL + r")[ ._-]*)?\d+[A-Z0-9.]*)+)?")
    for match in re.finditer(pattern, text or ""):
        if match["ambiguous"]:
            continue
        prefix = (text or "")[:match.start()]
        season = re.search(r"(?i)(?:Season\s*(\d+)|S(\d{1,2})|第([\d一二三四五六七八九十]+)季)[ :_-]*$", prefix)
        number = next((v for v in season.groups() if v), None) if season else None
        result.add((match[1].upper(), str(int(match[2])), int(cn2an.cn2an(number, "smart")) if number else None))
    return result


def requires_confirmation(meta):
    note = getattr(meta, "note", None) or {}
    return note.get("special_episode") or note.get("extra")


def download_block_reason(meta):
    """Keep unresolved content and local extras out of regular download coverage."""
    reason = getattr(meta, "skip_reason", None)
    if reason:
        return reason
    note = getattr(meta, "note", None) or {}
    if note.get("extra"):
        return "附加内容不参与正片下载和缺集统计"
    for key in ("special_episode", "fractional_episode"):
        evidence = note.get(key)
        if evidence and evidence.get("status") != "confirmed":
            return evidence.get("reason") or "特殊内容季集尚未确认，保留待确认"
    return None
