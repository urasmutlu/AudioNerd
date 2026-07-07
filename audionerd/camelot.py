"""Camelot-wheel helpers: map musical keys to Camelot codes and draw the wheel.

The Camelot wheel is the DJ's harmonic-mixing map: every key gets a code like
`8A` (A minor) or `8B` (C major). Tracks whose codes are adjacent on the wheel
(same number, or ±1) mix together harmonically. Plotting a playlist's key
distribution on the wheel shows at a glance whether a set is harmonically tight
or scattered.

Key strings come from a few sources in different spellings — analysed keys use
ASCII sharps (`F#m`), GetSongBPM uses Unicode sharps (`C♯`, `G♯m`) — so
`to_camelot` normalises both (and flats) before mapping.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

# Root note name (normalised, lowercase) -> pitch class 0..11.
_PITCH_CLASS = {
    "c": 0, "b#": 0,
    "c#": 1, "db": 1,
    "d": 2,
    "d#": 3, "eb": 3,
    "e": 4, "fb": 4,
    "f": 5, "e#": 5,
    "f#": 6, "gb": 6,
    "g": 7,
    "g#": 8, "ab": 8,
    "a": 9,
    "a#": 10, "bb": 10,
    "b": 11, "cb": 11,
}

# Pitch class -> Camelot code, for major (B ring) and minor (A ring).
_MAJOR = {0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
          6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B"}
_MINOR = {0: "5A", 1: "12A", 2: "7A", 3: "2A", 4: "9A", 5: "4A",
          6: "11A", 7: "6A", 8: "1A", 9: "8A", 10: "3A", 11: "10A"}

# Pitch-class note names (sharps, to match the key spellings in the data).
_NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Camelot code -> musical key name, inverted from the maps above so the two
# can't drift apart (e.g. "8B" -> "C", "8A" -> "Am").
_CODE_TO_KEY = {code: _NOTE[pc] for pc, code in _MAJOR.items()}
_CODE_TO_KEY.update({code: _NOTE[pc] + "m" for pc, code in _MINOR.items()})


def to_camelot(key: Optional[str]) -> Optional[str]:
    """Map a key name (e.g. 'F#m', 'C♯', 'Am', 'Bb') to a Camelot code, or None."""
    if not key:
        return None
    s = key.strip().replace("♯", "#").replace("♭", "b").lower()
    s = s.replace("maj", "").replace("major", "").replace("min", "m").replace("minor", "m")
    s = s.strip()
    minor = s.endswith("m")
    if minor:
        s = s[:-1].strip()
    pc = _PITCH_CLASS.get(s)
    if pc is None:
        return None
    return (_MINOR if minor else _MAJOR)[pc]


def camelot_counts(keys: Iterable[Optional[str]]) -> dict[str, int]:
    """Count how many keys fall in each Camelot code (unmappable keys ignored)."""
    counts: Counter[str] = Counter()
    for k in keys:
        code = to_camelot(k)
        if code:
            counts[code] += 1
    return dict(counts)


def wheel_figure(counts: dict[str, int], title: Optional[str] = None):
    """Return a matplotlib Figure of the Camelot wheel, shaded by track count.

    `counts` maps Camelot codes ('8A', '8B', …) to a number of tracks. Segments
    are labelled with musical key names (Am, C, F#…); the inner ring is minor
    keys, the outer ring major; brighter = more tracks.
    """
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patheffects as pe

    fig, ax = plt.subplots(figsize=(5.2, 5.2), subplot_kw={"projection": "polar"})
    fig.patch.set_alpha(0.0)
    ax.patch.set_alpha(0.0)
    ax.set_theta_zero_location("N")   # position 1 at top…
    ax.set_theta_direction(-1)        # …and numbers increase clockwise
    ax.set_ylim(0, 3.15)
    ax.axis("off")

    width = 2 * np.pi / 12
    vmax = max(counts.values()) if counts else 0
    cmap = plt.get_cmap("plasma")
    stroke = [pe.withStroke(linewidth=2.0, foreground="black", alpha=0.35)]

    for n in range(1, 13):
        center = (n - 1) * width
        # (ring bottom radius, Camelot letter)
        for bottom, letter in ((1.0, "A"), (2.0, "B")):
            code = f"{n}{letter}"
            c = counts.get(code, 0)
            face = cmap(c / vmax) if (c > 0 and vmax > 0) else (0.6, 0.6, 0.6, 0.16)
            ax.bar(center, 1.0, width=width * 0.96, bottom=bottom, color=face,
                   edgecolor=(1, 1, 1, 0.5), linewidth=0.8, align="center")
            name = _CODE_TO_KEY[code]
            label = f"{name}\n{c}" if c > 0 else name
            txt = ax.text(
                center, bottom + 0.5, label, ha="center", va="center",
                fontsize=8.5 if c > 0 else 7,
                color="white" if c > 0 else (0.5, 0.5, 0.5, 0.9),
                fontweight="bold" if c > 0 else "normal",
            )
            if c > 0:
                txt.set_path_effects(stroke)

    if title:
        ax.set_title(title, pad=16, fontsize=13, color="#888", fontweight="bold")
    fig.text(0.5, 0.02, "inner ring = minor (A)    ·    outer ring = major (B)",
             ha="center", fontsize=8, color="#999")
    return fig
