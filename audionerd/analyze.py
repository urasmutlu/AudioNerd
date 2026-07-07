"""Estimate BPM (and musical key) from a 30-second preview clip.

This is the last-resort source: when neither GetSongBPM nor Deezer metadata has
a tempo, we download the preview and analyse the audio directly, so coverage no
longer depends on any catalog. `librosa` is imported lazily inside `estimate`
so the app starts fast and only pays the (heavy) import when analysis runs.

Accuracy note: BPM is reliable for tracks with a steady beat (±1-2 BPM) but can
be octave-off or wrong on ambient/sparse material; musical key is a reasonable
estimate, not ground truth. Results should be treated as estimates and are
tagged `source="analyzed"` in the cache.
"""

from __future__ import annotations

import logging
import os
import tempfile
import warnings
from typing import Any, Optional

import requests

logger = logging.getLogger("audionerd.analyze")

# Preview clips live on Deezer's CDN, which is a different host from the rate-
# limited API, so a plain session is fine here.
_session = requests.Session()
_session.headers.update({"User-Agent": "AudioNerd/0.1"})

# Krumhansl-Schmuckler key profiles + pitch-class names (sharps).
_PITCHES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def estimate(
    preview_url: str, *, want_key: bool = True
) -> Optional[dict[str, Any]]:
    """Download `preview_url` and return {"bpm": float, "music_key": str|None}.

    Returns None if the clip can't be downloaded or decoded.
    """
    audio = _download(preview_url)
    if audio is None:
        return None

    # Imported here (not at module load) — librosa/numba import is slow.
    import numpy as np
    import librosa

    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
            fh.write(audio)
            path = fh.name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            y, sr = librosa.load(path, sr=22050, mono=True)
    except Exception as exc:  # noqa: BLE001 — any decode failure is just a miss
        logger.warning("analysis failed to decode preview: %s", exc)
        return None
    finally:
        if path and os.path.exists(path):
            os.unlink(path)

    if y is None or len(y) < sr * 3:  # need a few seconds to say anything useful
        return None

    tempo = float(np.ravel(librosa.beat.beat_track(y=y, sr=sr)[0])[0])
    result: dict[str, Any] = {"bpm": round(tempo, 1), "music_key": None}
    if want_key:
        result["music_key"] = _estimate_key(y, sr, np, librosa)
    return result


def _download(url: str) -> Optional[bytes]:
    if not url:
        return None
    try:
        resp = _session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as exc:
        logger.warning("preview download failed: %s", exc)
        return None


def _estimate_key(y, sr, np, librosa) -> Optional[str]:
    """Estimate musical key via Krumhansl-Schmuckler correlation on mean chroma."""
    try:
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    except Exception:  # noqa: BLE001
        return None
    profile = chroma.mean(axis=1)
    if not np.any(profile):
        return None

    major = np.array(_MAJOR_PROFILE)
    minor = np.array(_MINOR_PROFILE)
    best_score, best_key = -2.0, None
    for i in range(12):
        for prof, suffix in ((major, ""), (minor, "m")):
            score = float(np.corrcoef(profile, np.roll(prof, i))[0, 1])
            if score > best_score:
                best_score, best_key = score, _PITCHES[i] + suffix
    return best_key
