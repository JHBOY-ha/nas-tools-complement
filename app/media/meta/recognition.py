"""Transient recognition decisions; never stored in the work-identity cache."""
from dataclasses import dataclass
from enum import Enum


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
