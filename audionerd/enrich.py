"""Tie Spotify playlist tracks to GetSongBPM features, through the SQLite cache.

For each track we check the cache first; only on a miss do we call GetSongBPM,
then write the result back (including a negative-cache flag when there's no
match, so we never re-query a track GetSongBPM doesn't know).
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from . import cache
from .getsongbpm import GetSongBPMClient

# Feature fields copied from a cache row / GetSongBPM result onto an output row.
_FEATURE_KEYS = (
    "bpm",
    "music_key",
    "open_key",
    "time_sig",
    "danceability",
    "acousticness",
)


def enrich_tracks(
    tracks: list[dict[str, Any]],
    gsb: GetSongBPMClient,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
) -> list[dict[str, Any]]:
    """Return the tracks with audio-feature fields merged in.

    `progress(done, total)` is called after each track so the UI can show a
    progress bar. Cache hits make repeat loads effectively instant.
    """
    enriched: list[dict[str, Any]] = []
    total = len(tracks)

    for i, track in enumerate(tracks, start=1):
        row = {**track, **{k: None for k in _FEATURE_KEYS}}
        cached = cache.get_track_features(track["spotify_id"])

        if cached is None:
            features = gsb.lookup(track["title"], track["primary_artist"] or track["artist"])
            if features is None:
                cache.upsert_track_features(
                    track["spotify_id"], track["title"], track["artist"], not_found=True
                )
            else:
                cache.upsert_track_features(
                    track["spotify_id"], track["title"], track["artist"], **features
                )
                row.update({k: features.get(k) for k in _FEATURE_KEYS})
        elif not cached.get("not_found"):
            row.update({k: cached.get(k) for k in _FEATURE_KEYS})

        enriched.append(row)
        if progress:
            progress(i, total)

    return enriched
