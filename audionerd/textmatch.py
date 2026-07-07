"""Shared text helpers for matching Spotify tracks against other catalogs."""

from __future__ import annotations

import re

# Suffixes Spotify appends after " - " that third-party search engines choke on.
_VERSION_SUFFIX = re.compile(
    r"\s*-\s*.*?\b(remix|mix|edit|version|rework|rmx|dub|bootleg|mixed|remaster|"
    r"remastered|live|acoustic|instrumental|extended|radio)\b.*$",
    re.IGNORECASE,
)


def norm(s: str) -> str:
    """Loose normalisation for comparing titles/artists across services."""
    s = s.lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)  # drop "(feat. …)", "[remix]"
    s = re.sub(r"[^a-z0-9]+", " ", s)       # keep alnum only
    return re.sub(r"\s+", " ", s).strip()


def clean_title(title: str) -> str:
    """Strip remix/version suffixes and feat. tags that break catalog search.

    "Come Together - Extended Mix"  -> "Come Together"
    "Lightenup - Alex Metric Remix" -> "Lightenup"
    "Song (feat. Someone)"          -> "Song"
    Leaves ordinary hyphenated titles ("Sky and Sand") untouched.
    """
    t = _VERSION_SUFFIX.sub("", title)
    t = re.sub(r"\(.*?feat.*?\)|\[.*?\]", "", t, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", t).strip() or title


def artist_matches(candidate: str, wanted: str) -> bool:
    """True if two artist strings plausibly refer to the same artist."""
    c, w = norm(candidate), norm(wanted)
    return bool(c) and (c in w or w in c)
