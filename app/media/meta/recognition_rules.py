"""Explicit release/TMDB numbering differences verified on 2026-09-21.

Rules are bounded to the verified release range and recheck the target episode
against live TMDB data before applying. media.episode_mappings can replace the
defaults (an empty list disables them); never infer an offset for unrelated shows.
"""

DEFAULT_NAME_ALIASES = {
    "海贼王": "航海王",
    "画完这个就去死": "Kore Kaite Shine",
}

DEFAULT_EPISODE_MAPPINGS = [
    {"tmdb_id": 65942, "source_season": 4, "source_begin": 1, "source_end": 19,
     "target_season": 1, "offset": 66},
    {"tmdb_id": 37854, "source_season": 1, "source_begin": 1156, "source_end": 1181,
     "target_season": 23, "offset": 0},
]
