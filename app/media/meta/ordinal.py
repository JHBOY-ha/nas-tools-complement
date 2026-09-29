"""Resolve release interpretations with positive, negative and unknown evidence.

Evidence is owned by a single recognition/file batch. No global negative cache or
ordinary work-cache entry may be derived from a selected ordinal interpretation.
"""
import copy
from enum import Enum

from config import Config
from app.media.meta.llm_parser import LLMMetaParser
from app.media.meta.recognition_rules import DEFAULT_NAME_ALIASES
from app.media.meta.special import normalized
from app.media.meta.special_resolver import SpecialResolver, identity, tmdb_type
from app.utils.types import MediaType


class Evidence(Enum):
    VALID = "成立"
    INVALID = "不成立"
    UNKNOWN = "未知"


class OrdinalResolver:
    def __init__(self, media, cache=None):
        self.media = media
        self.evidence = SpecialResolver(media, cache)
        self.aliases = dict(DEFAULT_NAME_ALIASES)
        self.aliases.update((Config().get_config("media") or {}).get("name_aliases") or {})

    def names(self, meta):
        names = [meta.get_name(), meta.cn_name, meta.en_name]
        # Configured aliases are explicit name evidence, scoped to this parse.
        names += [self.aliases.get(name) for name in names if name]
        return list(dict.fromkeys(n for n in names if isinstance(n, str) and n))

    @staticmethod
    def matches(names, info):
        actual = [info.get(k) for k in ("name", "title", "original_name", "original_title")]
        aliases = info.get("alternative_titles") or {}
        actual += [a.get("title") for a in aliases.get("results", aliases.get("titles", []))]
        for entry in (info.get("translations") or {}).get("translations", []):
            actual += [entry.get("data", {}).get(k) for k in ("name", "title")]
        return bool({normalized(n) for n in names if n} & {normalized(n) for n in actual if n})

    def search(self, meta):
        names = self.names(meta)
        infos = self.evidence.search(names, year=meta.year, exact=False)
        matches = {identity(i): i for i in infos if self.matches(names, i)}
        if not matches:
            # Query both interpretations independently, even if the short name won.
            # Bangumi names locate candidates; exact provider names still verify them.
            aliases = self.evidence.cached(("ordinal_alias", meta.get_name(), meta.subtitle),
                lambda: LLMMetaParser().get_alias_candidates(title=meta.get_name(), subtitle=meta.subtitle))
            if aliases:
                infos = self.evidence.search(aliases, year=meta.year, exact=False)
                matches = {identity(i): i for i in infos if self.matches(aliases, i)}
        # A complete search page is not a complete manifest of the world's aliases.
        return list(matches.values())

    def prefers_alternate(self, meta, info):
        """Only positive, contrary title evidence overrides the two-digit default."""
        alternate = copy.copy(meta)
        for key, value in meta.note["ordinal_default"][1].items():
            setattr(alternate, key, value)
        return (self.matches(self.names(alternate), info)
                and not self.matches(self.names(meta), info))

    def validate(self, meta, info, mtype_hint=None, manual=False):
        """INVALID requires complete contrary facts; failed validation is UNKNOWN."""
        if not info or not info.get("id") or not tmdb_type(info.get("media_type")):
            return Evidence.UNKNOWN
        if tmdb_type(info.get("media_type")) != MediaType.TV:
            return Evidence.INVALID
        if mtype_hint and tmdb_type(mtype_hint) != MediaType.TV:
            return Evidence.INVALID
        seasons = info.get("seasons")
        if not isinstance(seasons, list) or not seasons:
            return Evidence.UNKNOWN
        numbers = [s.get("season_number") for s in seasons if isinstance(s, dict)]
        if (len(numbers) != len(seasons) or any(type(n) is not int or n < 0 for n in numbers)
                or len(set(numbers)) != len(numbers)):
            return Evidence.UNKNOWN
        count = info.get("number_of_seasons")
        if type(count) is int and count != len([n for n in numbers if n > 0]):
            return Evidence.UNKNOWN
        if not manual:
            # Mapping errors (including unavailable target details) are not negatives.
            self.media._apply_episode_mapping(meta, info, season_fetch=self.evidence.season)
            # Prepare before inspecting final coverage: a verified LLM identity may
            # also supply numbering. Never validate one season then publish another.
            prepared = self.media._prepare_media_identity(meta, info, mtype_hint=mtype_hint)
        else:
            prepared = True
        wanted = meta.get_season_list()
        if not wanted:
            return Evidence.UNKNOWN
        if not set(wanted).issubset(numbers):
            # Presence proves a season exists; absence needs a complete manifest.
            if (meta.note.get("llm") or {}).get("season_verified") and not prepared:
                return Evidence.UNKNOWN
            return Evidence.INVALID if type(count) is int and count >= 0 else Evidence.UNKNOWN
        if not prepared:
            return Evidence.UNKNOWN
        for season in wanted:
            expected = next(s.get("episode_count") for s in seasons if s["season_number"] == season)
            if type(expected) is not int or expected < 0:
                return Evidence.UNKNOWN
            detail = self.evidence.season(info, season)
            episodes = detail.get("episodes") if detail else None
            if not isinstance(episodes, list) or len(episodes) != expected:
                return Evidence.UNKNOWN
            found = []
            for episode in episodes:
                if not isinstance(episode, dict):
                    return Evidence.UNKNOWN
                try:
                    self.evidence.validate_episode(info, season, episode)
                except ValueError:
                    return Evidence.UNKNOWN
                found.append(episode["episode_number"])
            if len(set(found)) != len(found):
                return Evidence.UNKNOWN
            if not set(meta.get_episode_list()).issubset(found):
                return Evidence.INVALID
        if manual:
            # The selected work overrides the filename, but not type/genre constraints.
            checked = copy.copy(meta)
            checked.cn_name = None
            checked.en_name = info.get("name") or info.get("original_name")
            valid = self.media._valid_media_identity(checked, info)
            if mtype_hint == MediaType.ANIME:
                genres = info.get("genre_ids") or [g.get("id") for g in info.get("genres") or []]
                valid = valid and (16 in genres or "16" in genres)
        else:
            valid = True
        return Evidence.VALID if valid else Evidence.UNKNOWN

    @staticmethod
    def apply(target, chosen, source):
        chosen.note.pop("ordinal_candidates", None)
        chosen.note.pop("ordinal_default", None)
        # Retain provenance after clearing the block, so callers cannot cache a
        # full-title identity under the earlier, shortened release-name key.
        chosen.note["ordinal_resolution"] = {"source": source, "release": target.org_string}
        if target.note.get("ordinal_evidence"):
            chosen.note["ordinal_evidence"] = target.note["ordinal_evidence"]
        chosen.skip_reason = None
        target.__dict__.update(chosen.__dict__)

    def resolve(self, meta, bound=None, mtype_hint=None, manual=None):
        fields_list = meta.note.get("ordinal_candidates") or meta.note.get("ordinal_default")
        if not fields_list:
            return {}
        if manual is not None:
            chosen = copy.deepcopy(meta)
            for key, value in manual.items():
                setattr(chosen, key, value)
            try:
                begin, end = chosen.begin_episode, chosen.end_episode
                valid_range = (type(begin) is int and begin > 0
                    and (end is None or (type(end) is int and end >= begin)))
                if valid_range and self.validate(chosen, bound, mtype_hint, manual=True) == Evidence.VALID:
                    self.apply(meta, chosen, "manual")
                    return bound
            except Exception:
                pass
            meta.skip_reason = "人工序数季目标缺少可核验的作品或季集证据"
            return {}
        accepted, states = [], []
        for fields in fields_list:
            parsed = copy.deepcopy(meta)
            parsed.note.pop("ordinal_candidates", None)
            # Selected candidates must not recursively re-enter compatibility routing.
            parsed.note.pop("ordinal_default", None)
            parsed.skip_reason = None
            for key, value in fields.items():
                setattr(parsed, key, value)
            try:
                if bound:
                    # An independently supplied identity constrains the work universe;
                    # it does not replace season/episode verification within that work.
                    infos = [bound] if self.matches(self.names(parsed), bound) else []
                    state = Evidence.INVALID if not infos else None
                else:
                    infos = self.search(parsed)
                    state = Evidence.UNKNOWN if not infos else None
                results = []
                for info in infos:
                    candidate = copy.deepcopy(parsed)
                    result = self.validate(candidate, info, mtype_hint)
                    results.append(result)
                    if result == Evidence.VALID:
                        accepted.append((candidate, info))
                states.append(state or (Evidence.UNKNOWN if Evidence.UNKNOWN in results
                    else Evidence.VALID if Evidence.VALID in results else Evidence.INVALID))
            except Exception:
                states.append(Evidence.UNKNOWN)
        meta.note["ordinal_evidence"] = [state.value for state in states]
        if len(accepted) == 1 and Evidence.UNKNOWN not in states:
            chosen, info = accepted[0]
            self.apply(meta, chosen, "bound" if bound else "automatic")
            return info
        meta.skip_reason = "序数词可能属于片名，季集解释尚未唯一确认"
        return {}
