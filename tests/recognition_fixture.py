"""Configurable offline TMDB for recognition invariant tests.

Models same-name works, incomplete season manifests, per-candidate and per-query
failures, mixed movie/TV search results and warm/cold identity caches. Never
contacts TMDB, Bangumi or an LLM; run through ``tests.run_recognition``.
"""
import copy
import re
from unittest.mock import MagicMock, patch

from app.media.media import Media
from app.utils.types import MediaType, MatchMode


def _norm(text):
    return re.sub(r"[\W_]+", "", str(text or "")).upper()


class FakeTmdb:
    def __init__(self, testcase, media_config=None):
        self.works = {}
        self.episodes = {}
        self.fail = set()
        # Works TMDB returns for every query (popular unrelated results).
        self.noise = []
        self.calls = []
        self.cache = {}
        self.media_config = media_config or {}
        for target in ("app.media.meta.metainfo.WordsHelper",):
            words = patch(target).start()
            words.return_value.process.side_effect = lambda title: (title, [], {})
        # The LLM is disabled by the runner configuration; external alias
        # candidates (Bangumi) are always unavailable in this fixture.
        for module in ("app.media.media", "app.media.meta.special_resolver", "app.media.meta.ordinal"):
            patch(module + ".LLMMetaParser").start().return_value.get_alias_candidates.return_value = []
        for module in ("app.media.media", "app.media.meta.special_resolver", "app.media.meta.ordinal"):
            config = patch(module + ".Config").start()
            config.return_value.get_config.side_effect = (
                lambda key: copy.deepcopy(self.media_config) if key == "media" else {})
        testcase.addCleanup(patch.stopall)
        self.media = self._media()

    # ----- catalogue -------------------------------------------------------
    def tv(self, tmdb_id, name, original=None, aliases=(), year="2020", genres=(18,),
           seasons=None, number_of_seasons="auto", language="en", season_years=None):
        seasons = {1: 10} if seasons is None else seasons
        season_years = season_years or {}
        detail = dict(
            id=tmdb_id, name=name, original_name=original or name, first_air_date="%s-01-01" % year,
            genres=[dict(id=g) for g in genres], original_language=language,
            number_of_seasons=(len([s for s in seasons if s > 0]) if number_of_seasons == "auto"
                               else number_of_seasons),
            seasons=[dict(season_number=s, episode_count=n,
                          air_date="%s-01-01" % season_years.get(s, year)) for s, n in sorted(seasons.items())],
            alternative_titles=dict(results=[dict(title=a) for a in aliases]),
            translations=dict(translations=[]))
        self.works[(MediaType.TV, tmdb_id)] = detail
        for season, count in seasons.items():
            self.episodes[(tmdb_id, season)] = [
                dict(id=tmdb_id * 1000 + season * 100 + n, episode_number=n, season_number=season,
                     show_id=tmdb_id, name="Episode %s" % n, air_date="%s-01-01" % year)
                for n in range(1, count + 1)]
        return detail

    def movie(self, tmdb_id, title, original=None, aliases=(), year="2000", genres=(18,)):
        detail = dict(id=tmdb_id, title=title, original_title=original or title,
                      release_date="%s-01-01" % year, genres=[dict(id=g) for g in genres],
                      alternative_titles=dict(titles=[dict(title=a) for a in aliases]),
                      translations=dict(translations=[]))
        self.works[(MediaType.MOVIE, tmdb_id)] = detail
        return detail

    # ----- provider behaviour ----------------------------------------------
    def _names(self, detail):
        names = [detail.get(k) for k in ("name", "original_name", "title", "original_title")]
        alt = detail.get("alternative_titles") or {}
        names += [a.get("title") for a in alt.get("results", alt.get("titles", []))]
        return [n for n in names if n]

    def _matches(self, query, detail):
        q = _norm(query)
        return bool(q) and any(q in _norm(n) or _norm(n) in q for n in self._names(detail))

    def _summary(self, kind, detail, typed=False):
        keys = ("id", "name", "original_name", "first_air_date") if kind == MediaType.TV else \
            ("id", "title", "original_title", "release_date")
        item = {k: detail.get(k) for k in keys}
        item["genre_ids"] = [g["id"] for g in detail.get("genres") or []]
        if typed:
            item["media_type"] = "tv" if kind == MediaType.TV else "movie"
        return item

    def _search(self, kind, params, typed=False):
        query = params.get("query")
        self.calls.append(("search", kind, query))
        if ("search", query) in self.fail:
            raise RuntimeError("offline fixture: search failed")
        year = params.get("first_air_date_year") or params.get("year")
        result = []
        for (work_kind, _), detail in self.works.items():
            if kind and work_kind != kind:
                continue
            date = detail.get("first_air_date") or detail.get("release_date") or ""
            if year and date[:4] != str(year):
                continue
            if self._matches(query, detail) or (work_kind, detail["id"]) in self.noise:
                result.append(self._summary(work_kind, detail, typed))
        # Providers rank popular unrelated works first.
        result.sort(key=lambda item: (MediaType.TV if "name" in item else MediaType.MOVIE, item["id"]) not in self.noise)
        return result

    def get_tmdb_info(self, mtype=None, tmdbid=None, **_kwargs):
        kind = MediaType.MOVIE if mtype == MediaType.MOVIE else MediaType.TV
        self.calls.append(("detail", kind, int(tmdbid)))
        if ("detail", int(tmdbid)) in self.fail:
            return None
        detail = self.works.get((kind, int(tmdbid)))
        if not detail:
            return None
        info = copy.deepcopy(detail)
        info["media_type"] = kind
        info["genre_ids"] = [g["id"] for g in info.get("genres") or []]
        return info

    def get_season(self, tmdbid, season):
        self.calls.append(("season", int(tmdbid), season))
        if ("season", int(tmdbid), season) in self.fail:
            return None
        episodes = self.episodes.get((int(tmdbid), int(season)))
        return {"episodes": copy.deepcopy(episodes)} if episodes is not None else {}

    def search_page(self, title, page=1):
        return self._search(None, {"query": title}, typed=True) if page == 1 else []

    def detail_calls(self):
        return [c for c in self.calls if c[0] == "detail"]

    def _media(self):
        media = Media.__new__(Media)
        media.tmdb = MagicMock(language="zh-CN")
        search = MagicMock()
        search.total_results = 0
        search.tv_shows.side_effect = lambda params: self._search(MediaType.TV, params)
        search.movies.side_effect = lambda params: self._search(MediaType.MOVIE, params)
        search.multi.side_effect = lambda params: self._search(None, params, typed=True)
        media.search = search
        media.meta = MagicMock()
        media.meta.get_meta_data_by_key.side_effect = lambda key: copy.deepcopy(self.cache.get(key, {}))
        media.meta.update_meta_data.side_effect = self.cache.update
        media.meta.delete_meta_data.side_effect = lambda key: self.cache.pop(key, None)
        media._rmt_match_mode = MatchMode.NORMAL
        media._search_tmdbweb = False
        media._search_keyword = False
        media.get_tmdb_info = self.get_tmdb_info
        media.get_tmdb_tv_season_detail = lambda tmdbid, season: self.get_season(tmdbid, season)
        media.get_tmdb_search_page = self.search_page
        # The real manual-cache writer keeps its own guards; it writes to self.cache.
        return media

    # ----- helpers ----------------------------------------------------------
    def warm(self, key, tmdb_id, kind=MediaType.TV, title="cached"):
        self.cache[key] = {"id": tmdb_id, "type": kind, "year": None, "title": title}

    def llm(self, **note):
        """Simulate an already verified LLM result at the parser boundary; no LLM runs."""
        def merge_into(meta_info, **_kwargs):
            meta_info.note["llm"] = dict(copy.deepcopy(note), enabled=True, applied=True)
            return meta_info
        parser = patch("app.media.meta.metainfo.LLMMetaParser").start()
        parser.return_value.merge_into.side_effect = merge_into
        parser.return_value.parse.return_value = {}


def outcome(meta):
    """Compact, comparable recognition outcome."""
    if meta is None:
        return None
    info = meta.tmdb_info or {}
    return dict(id=info.get("id"), skip=bool(meta.skip_reason),
                seasons=meta.get_season_list() if info else None,
                episodes=meta.get_episode_list() if info else None)
