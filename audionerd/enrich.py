"""Merge Spotify playlist tracks with audio features from a layered set of
sources, through the SQLite cache.

For each track (on a cache miss) we try, in order, stopping at the first that
yields a BPM:
  1. GetSongBPM   — BPM + musical key + danceability (instant metadata)
  2. Deezer       — free BPM metadata (partial coverage) + a preview URL
  3. analysis     — librosa BPM (+ key) from Deezer's 30s preview (~full coverage)

The winning source is recorded on the row (`source`) so the UI can show where
each value came from. A track that yields nothing anywhere is negative-cached.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from . import cache
from .deezer import DeezerClient
from .getsongbpm import GetSongBPMClient

logger = logging.getLogger("audionerd.enrich")

# Feature fields copied from a cache row onto an output row.
_FEATURE_KEYS = (
    "bpm",
    "music_key",
    "open_key",
    "time_sig",
    "danceability",
    "acousticness",
    "source",
)


def enrich_tracks(
    tracks: list[dict[str, Any]],
    gsb: GetSongBPMClient,
    deezer: DeezerClient,
    *,
    analyze: bool = True,
    progress: Optional[Callable[[int, int], None]] = None,
) -> list[dict[str, Any]]:
    """Return the tracks with audio-feature fields (incl. `source`) merged in.

    `analyze=False` skips the preview-analysis fallback (source 3) for a faster
    but less-complete run. `progress(done, total)` is called after each track.
    """
    enriched: list[dict[str, Any]] = []
    total = len(tracks)

    for i, track in enumerate(tracks, start=1):
        row = {**track, **{k: None for k in _FEATURE_KEYS}}
        cached = cache.get_track_features(track["spotify_id"])

        if cached is None:
            features = _fetch(track, gsb, deezer, analyze=analyze)
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


def _fetch(
    track: dict[str, Any],
    gsb: GetSongBPMClient,
    deezer: DeezerClient,
    *,
    analyze: bool,
) -> Optional[dict[str, Any]]:
    """Try each source in order; return the first feature dict with a BPM."""
    title = track["title"]
    artist = track["primary_artist"] or track["artist"]

    # Source 1: GetSongBPM (also gives key + danceability).
    features = gsb.lookup(title, artist)
    if features and features.get("bpm") is not None:
        features["source"] = "getsongbpm"
        return features

    # Sources 2 & 3 both need Deezer (metadata BPM and/or a preview URL).
    dz = deezer.find(title, artist)

    # Source 2: Deezer metadata BPM (exact when present).
    if dz and dz.get("bpm") is not None:
        return {"bpm": dz["bpm"], "source": "deezer"}

    # Source 3: analyse Deezer's preview clip.
    if analyze and dz and dz.get("preview_url"):
        from . import analyze as audio  # lazy import — librosa is heavy

        est = audio.estimate(dz["preview_url"])
        if est and est.get("bpm") is not None:
            return {
                "bpm": est["bpm"],
                "music_key": est.get("music_key"),
                "source": "analyzed",
            }

    return None
