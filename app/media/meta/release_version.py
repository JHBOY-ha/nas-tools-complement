"""Keep editorial cuts separate from source/technical edition fields."""
import re

# Each cut owns extraction aliases and its legacy name-cleanup subset.
# Cleanup keeps its old boundaries; it must not infer a cut or expand title removal.
_CUTS = (
    ("Extended", r"extended(?:[ ._-]+(?:cut|version|edition))?|加长版|加長版", r"Extended$|Extended Version$"),
    ("Directors Cut", r"director(?:['’]s|s)?[ ._-]+cut|导演剪辑版|導演剪輯版", ""),
    ("Theatrical", r"theatrical(?:[ ._-]+(?:cut|version|edition))?|院线版|院線版", ""),
    ("Unrated", r"unrated|未分级版|未分級版", r"UNRATE$"),
    ("Uncut", r"uncut|未删减版|未刪減版", r"未删减版|UNCUT$"),
)


LEGACY_CUT_NAME_PATTERN = "|".join(legacy for _, _, legacy in _CUTS if legacy)


def clean_cut_tokens(text):
    """Remove only known cut tokens from resource fields returned by the LLM."""
    for _, pattern, _ in _CUTS:
        text = re.sub(r"(?<![A-Za-z])(?:" + pattern + r")(?![A-Za-z])", "", text or "", flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" ._-")


def extract_cut(title):
    """Extract from release metadata or standalone brackets, preserving title words."""
    title = title or ""
    spans = []
    # Last year handles numeric movie titles such as '2001 ... 1968'.
    years = list(re.finditer(r"(?<!\d)(?:19|20)\d{2}(?!\d)", title))
    if years:
        spans.append((years[-1].end(), len(title)))
        # 发布名也会写成 Rambo.Extended.Cut.2008：只接受紧贴年份的
        # 完整剪辑短语，且前面须有有效片名、后面须有资源标记。
        # 不扩大到任意位置的 Cut/Extended，避免误删真实片名。
        year = years[-1]
        before_year = title[:year.start()].rstrip(" ._-")
        pre_year = re.search(
            r"(?i)(?<![A-Za-z])(?:extended[ ._-]+(?:cut|version|edition)|"
            r"director(?:['’]s|s)?[ ._-]+cut|theatrical[ ._-]+(?:cut|version|edition)|"
            r"加长版|加長版|导演剪辑版|導演剪輯版|院线版|院線版|未分级版|未分級版|未删减版|未刪減版)$",
            before_year)
        release_tail = re.search(
            r"(?i)(?<![A-Za-z0-9])(?:BluRay|WEB[ ._-]?DL|WEBRip|BDRip|HDTV|\d{3,4}[pi])(?![A-Za-z0-9])",
            title[year.end():])
        if pre_year and release_tail:
            name = before_year[:pre_year.start()].strip(" ._-")
            if name and name.casefold() not in ("the", "a", "an"):
                spans.append(pre_year.span())
    for block in re.finditer(r"[\[【(]([^\]】)]+)[\]】)]", title):
        if not clean_cut_tokens(block[1]):
            spans.append((block.start(1), block.end(1)))
    found, removals = [], []
    for label, pattern, _ in _CUTS:
        for match in re.finditer(r"(?<![A-Za-z])(?:" + pattern + r")(?![A-Za-z])", title, re.I):
            if any(start <= match.start() and match.end() <= end for start, end in spans):
                if label not in found:
                    found.append(label)
                removals.append(match.span())
    for start, end in sorted(set(removals), reverse=True):
        title = title[:start] + " " + title[end:]
    return title, " ".join(found) or None
