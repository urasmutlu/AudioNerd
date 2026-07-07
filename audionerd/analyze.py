"""Estimate BPM (and musical key) from a 30-second preview clip.

This is the analysis source: when we want a tempo that doesn't depend on any
catalog, we download the preview and measure it directly. `librosa` is imported
lazily inside `estimate` so the app starts fast and only pays the (heavy) import
when analysis runs.

Rather than trust a single beat tracker, we run a few estimators and score how
much to trust the result:

  * a **tempo prior** (`start_bpm`) tuned for electronic music biases every
    estimator toward a sane octave, which is what kills most of the 2x / half-
    time errors metadata sources are prone to;
  * two independent estimators — the DP **beat tracker** and the
    **autocorrelation** tempo — cross-check the *octave*: if they disagree by a
    clean 2x, the reading is uncertain;
  * a **confidence score** built from signals that are independent of the tempo
    *value* — how percussive the audio is, whether detected beats land on real
    onsets, and whether sub-windows of the clip agree — catches the case metho-
    dological agreement can't: an ambient / beatless preview where every
    estimator locks onto the same spurious pulse.

`estimate()` returns the primary BPM plus `confidence` ("high"/"medium"/"low")
and a `detail` dict of the raw signals, so the caller can gap-fill from metadata
when confidence is low. Results are tagged `source="analyzed"` in the cache.
"""

from __future__ import annotations

import logging
import math
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

# Estimator prior. None lets librosa use its neutral defaults (start_bpm=120)
# rather than biasing toward an electronic tempo. Grounding against labelled
# data showed a fixed electronic prior *doubled* genuinely-slow (non-electronic)
# tracks, so we let the algorithm speak for itself and resolve octave elsewhere.
PRIOR_BPM = None

# Reference tempo used ONLY to align octaves when measuring agreement between
# estimators (window spread, A-vs-B). It does NOT bias the reported BPM.
FOLD_REF = 120.0


def _prior_kw() -> dict:
    """`start_bpm` kwarg only when a prior is set; empty dict => librosa default."""
    return {} if PRIOR_BPM is None else {"start_bpm": PRIOR_BPM}

# Krumhansl-Schmuckler key profiles + pitch-class names (sharps).
_PITCHES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_MAJOR_PROFILE = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
_MINOR_PROFILE = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]


def estimate(
    preview_url: str, *, want_key: bool = True
) -> Optional[dict[str, Any]]:
    """Download `preview_url` and estimate its tempo (and key).

    Returns a dict:
        {
          "bpm": float,                 # primary estimate (beat tracker, prior-biased)
          "music_key": str | None,
          "confidence": "high"|"medium"|"low",
          "confidence_score": float,    # 0..1
          "detail": { ...raw signals... },
        }
    or None if the clip can't be downloaded or decoded.
    """
    audio = _download(preview_url)
    if audio is None:
        return None

    # Imported here (not at module load) — librosa/numba import is slow.
    import numpy as np
    import librosa
    import librosa.feature.rhythm  # noqa: F401 — makes librosa.feature.rhythm.tempo available

    y, sr = _decode(audio, librosa)
    if y is None or len(y) < sr * 3:  # need a few seconds to say anything useful
        return None

    result = _analyze(y, sr, np, librosa)
    if want_key and result is not None:
        result["music_key"] = _estimate_key(y, sr, np, librosa)
    return result


def _analyze(y, sr, np, librosa) -> Optional[dict[str, Any]]:
    """Run the estimators + confidence scoring on a decoded mono signal."""
    # Isolate the percussive component — beats live here, and its relative
    # strength is itself a "is there even a beat?" signal.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        y_harm, y_perc = librosa.effects.hpss(y)
        oenv = librosa.onset.onset_strength(y=y_perc, sr=sr)

    if not np.any(oenv):
        return None

    # Estimator A: DP beat tracker (also gives beat positions), prior-biased.
    bt_bpm, beats = librosa.beat.beat_track(
        onset_envelope=oenv, sr=sr, trim=False, **_prior_kw()
    )
    bt_bpm = float(np.ravel(bt_bpm)[0])

    # Estimator B: autocorrelation tempo — independent of the DP path.
    ac_bpm = float(
        np.ravel(
            librosa.feature.rhythm.tempo(
                onset_envelope=oenv, sr=sr, aggregate=np.median, **_prior_kw()
            )
        )[0]
    )

    # --- confidence signals (independent of the tempo *value*) --------------
    # 1) percussive ratio: ambient/beatless material has little percussive energy.
    rms_p = float(np.sqrt(np.mean(y_perc**2)))
    rms_h = float(np.sqrt(np.mean(y_harm**2)))
    percussive_ratio = rms_p / (rms_p + rms_h + 1e-9)

    # 2) beat strength: do detected beats land on strong onsets, or is the
    #    tracker forcing a grid onto noise? Ratio of onset energy at beats to mean.
    oenv_mean = float(oenv.mean()) + 1e-9
    beat_strength = float(oenv[beats].mean()) / oenv_mean if len(beats) else 0.0

    # 3) window agreement: split the clip into thirds and re-estimate tempo on
    #    each. A real groove is stable across the clip; a preview that includes a
    #    beatless intro/breakdown scatters. Compare octave-folded to the prior.
    window_bpms = _window_tempos(y_perc, sr, np, librosa)
    folded = [_fold_octave(b, FOLD_REF) for b in window_bpms]
    window_spread = float(np.std(folded)) if len(folded) >= 2 else 0.0

    # Octave cross-check between A and B (folded so only the octave matters).
    octave_agree = abs(_fold_octave(bt_bpm, FOLD_REF) - _fold_octave(ac_bpm, FOLD_REF)) <= 3.0

    score = _confidence(percussive_ratio, beat_strength, window_spread, octave_agree)

    return {
        "bpm": round(bt_bpm, 1),
        "music_key": None,
        "confidence": _label(score),
        "confidence_score": round(score, 2),
        "detail": {
            "bt_bpm": round(bt_bpm, 1),
            "ac_bpm": round(ac_bpm, 1),
            "octave_agree": octave_agree,
            "percussive_ratio": round(percussive_ratio, 3),
            "beat_strength": round(beat_strength, 2),
            "window_bpms": [round(b, 1) for b in window_bpms],
            "window_spread": round(window_spread, 1),
        },
    }


def _window_tempos(y_perc, sr, np, librosa) -> list[float]:
    """Autocorrelation tempo on each third of the (percussive) clip."""
    n = len(y_perc)
    if n < sr * 6:  # too short to split meaningfully
        return []
    bpms: list[float] = []
    for start in (0, n // 3, 2 * n // 3):
        chunk = y_perc[start : start + n // 3]
        if len(chunk) < sr * 2:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            oenv = librosa.onset.onset_strength(y=chunk, sr=sr)
        if not np.any(oenv):
            continue
        bpm = float(
            np.ravel(
                librosa.feature.rhythm.tempo(
                    onset_envelope=oenv, sr=sr, aggregate=np.median, **_prior_kw()
                )
            )[0]
        )
        if bpm > 0:
            bpms.append(bpm)
    return bpms


def _fold_octave(bpm: float, prior: float) -> float:
    """Multiply/divide `bpm` by powers of two to land nearest the prior (log2)."""
    if bpm <= 0:
        return bpm
    best, best_dist = bpm, abs(math.log2(bpm / prior))
    for k in (0.25, 0.5, 2.0, 4.0):
        cand = bpm * k
        dist = abs(math.log2(cand / prior))
        if dist < best_dist:
            best, best_dist = cand, dist
    return best


def _confidence(
    percussive_ratio: float,
    beat_strength: float,
    window_spread: float,
    octave_agree: bool,
) -> float:
    """Blend the raw signals into a 0..1 confidence score.

    Thresholds here are first-pass estimates — the comparison script prints the
    raw signals so we can calibrate them against real data before wiring the
    confidence into the enrichment fallback.
    """
    # Each sub-score in [0, 1].
    s_perc = _ramp(percussive_ratio, lo=0.15, hi=0.40)
    s_beat = _ramp(beat_strength, lo=1.0, hi=2.0)
    s_win = 1.0 - _ramp(window_spread, lo=3.0, hi=25.0)
    s_oct = 1.0 if octave_agree else 0.5
    # Weighted mean — percussiveness and beat strength (the ambient tells) matter
    # most; window stability and octave agreement refine.
    return 0.35 * s_perc + 0.30 * s_beat + 0.20 * s_win + 0.15 * s_oct


def _ramp(x: float, *, lo: float, hi: float) -> float:
    """Linear ramp: 0 at/below lo, 1 at/above hi."""
    if hi <= lo:
        return 1.0 if x >= hi else 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _label(score: float) -> str:
    if score >= 0.66:
        return "high"
    if score >= 0.40:
        return "medium"
    return "low"


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


def _decode(audio: bytes, librosa):
    """Write bytes to a temp file and decode to mono @ 22.05k. Returns (y, sr)."""
    path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as fh:
            fh.write(audio)
            path = fh.name
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return librosa.load(path, sr=22050, mono=True)
    except Exception as exc:  # noqa: BLE001 — any decode failure is just a miss
        logger.warning("analysis failed to decode preview: %s", exc)
        return None, None
    finally:
        if path and os.path.exists(path):
            os.unlink(path)


def _estimate_key(y, sr, np, librosa) -> Optional[str]:
    """Estimate musical key via Krumhansl-Schmuckler correlation on mean chroma."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
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
