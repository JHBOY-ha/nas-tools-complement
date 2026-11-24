"""Transient recognition decisions; never stored in the work-identity cache."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum


_query_cache = ContextVar("recognition_query_cache", default=None)


@contextmanager
def recognition_query_scope():
    """Share provider reads only inside one recognition or file batch."""
    if _query_cache.get() is not None:
        yield
        return
    token = _query_cache.set({})
    try:
        yield
    finally:
        _query_cache.reset(token)


def cached_recognition_query(key, fetch):
    cache = _query_cache.get()
    if cache is None:
        return fetch()
    if key not in cache:
        value = fetch()
        # Exceptions and unavailable responses are not known empty results.
        if value is None:
            return None
        cache[key] = deepcopy(value)
    return deepcopy(cache[key])


def query_tmdb(search, kind, params):
    """The SDK's implicit page 1 and explicit page 1 are the same query."""
    canonical = dict(params)
    canonical.setdefault("page", 1)
    key = ("tmdb", kind, tuple(sorted((name, str(value)) for name, value in canonical.items())),
           tuple(os.environ.get(name) for name in
                 ("TMDB_DOMAIN", "TMDB_API_KEY", "TMDB_LANGUAGE", "TMDB_PROXIES")))
    return cached_recognition_query(key, lambda: getattr(search, kind)(params))


# Candidate detail reads allowed per selection round (TV search, special and
# ordinal work search). Reads already made in the same recognition are free.
CANDIDATE_DETAIL_LIMIT = 6


class RecognitionPending(ValueError):
    """Required evidence is unavailable; trying another work is not authorized."""


class NumberingState(Enum):
    NOT_REQUESTED = "not_requested"
    VERIFIED = "verified"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class NumberingResult:
    state: NumberingState
    reason: str = ""
    target: tuple = ()
    season_authorized: bool = False

    @property
    def ready(self):
        return self.state != NumberingState.UNRESOLVED

    @staticmethod
    def target_of(meta, info):
        return (info.get("media_type"), str(info.get("id")),
                meta.begin_season, getattr(meta, "end_season", None),
                meta.begin_episode, getattr(meta, "end_episode", None))

    @classmethod
    def verified(cls, meta, info, *, season_authorized=True):
        return cls(NumberingState.VERIFIED, target=cls.target_of(meta, info),
                   season_authorized=season_authorized)

    def matches(self, meta, info):
        return self.state != NumberingState.VERIFIED or self.target == self.target_of(meta, info)

    def authorizes_season(self, meta, info):
        # Explicit episode corrections can retain evidence for an unchanged
        # work/season, but cannot transfer that authority to another season.
        return (self.state == NumberingState.VERIFIED and self.season_authorized
                and self.target[:4] == self.target_of(meta, info)[:4])
