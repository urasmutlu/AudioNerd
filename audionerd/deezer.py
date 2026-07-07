"""Deezer API client — free BPM metadata and 30s preview URLs.

Deezer's public catalog/search needs no auth, but enforces a hard rate limit of
50 requests / 5 seconds per IP. `RateLimiter` keeps us safely under that with a
sliding window and a safety margin; every api.deezer.com call goes through it.
(Preview downloads hit a separate CDN host and don't count against this limit.)
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any, Optional

import requests

from .textmatch import artist_matches, clean_title

logger = logging.getLogger("audionerd.deezer")

API_BASE = "https://api.deezer.com"


class RateLimiter:
    """Thread-safe sliding-window limiter: at most `max_calls` per `period`s.

    `acquire()` blocks until making a call would keep us within the window.
    We apply a safety factor so we stay comfortably under the provider's limit
    even if their clock and ours disagree slightly.
    """

    def __init__(self, max_calls: int, period: float, safety: float = 0.9) -> None:
        self.max_calls = max(1, int(max_calls * safety))
        self.period = period
        self._calls: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            self._purge(now)
            if len(self._calls) >= self.max_calls:
                # Wait until the oldest call ages out of the window.
                sleep_for = self.period - (now - self._calls[0]) + 0.01
                if sleep_for > 0:
                    time.sleep(sleep_for)
                self._purge(time.monotonic())
            self._calls.append(time.monotonic())

    def _purge(self, now: float) -> None:
        while self._calls and now - self._calls[0] >= self.period:
            self._calls.popleft()


class DeezerClient:
    def __init__(
        self,
        *,
        debug: bool = False,
        limiter: Optional[RateLimiter] = None,
    ) -> None:
        self._debug = debug
        # 50 req / 5s is Deezer's documented ceiling; default keeps us at ~45.
        self._limiter = limiter or RateLimiter(50, 5.0)
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "AudioNerd/0.1"})

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        for attempt in range(4):
            self._limiter.acquire()
            try:
                resp = self._session.get(f"{API_BASE}{path}", params=params, timeout=15)
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                if self._debug:
                    logger.warning("Deezer GET %s failed: %s", path, exc)
                return {}
            # Deezer signals quota exhaustion as a 200 with an error body (code 4).
            err = data.get("error") if isinstance(data, dict) else None
            if isinstance(err, dict) and err.get("code") in (4, 700):
                backoff = 2 ** attempt
                if self._debug:
                    logger.warning("Deezer quota hit on %s; backing off %ss", path, backoff)
                time.sleep(backoff)
                continue
            if not resp.ok and self._debug:
                logger.warning("Deezer GET %s -> HTTP %s", path, resp.status_code)
            return data if isinstance(data, dict) else {}
        return {}

    def find(self, title: str, artist: str) -> Optional[dict[str, Any]]:
        """Locate a track and return its Deezer data, or None if no artist match.

        Returns: {deezer_id, preview_url, bpm (float|None), gain, matched_artist}.
        `bpm` is None when Deezer has no tempo for the track (a common case);
        `preview_url` is almost always present and is what the analysis fallback
        consumes.
        """
        clean = clean_title(title)
        # Fielded search is most precise; fall back to a plain query.
        results = self._search(f'artist:"{artist}" track:"{clean}"')
        if not results:
            results = self._search(f"{clean} {artist}")

        match = next(
            (r for r in results if artist_matches((r.get("artist") or {}).get("name", ""), artist)),
            None,
        )
        if match is None:
            if self._debug:
                logger.info("Deezer: no match for %r by %r", title, artist)
            return None

        detail = self._get(f"/track/{match['id']}")
        bpm = detail.get("bpm")
        return {
            "deezer_id": match["id"],
            "preview_url": match.get("preview") or detail.get("preview"),
            "bpm": float(bpm) if isinstance(bpm, (int, float)) and bpm > 0 else None,
            "gain": detail.get("gain"),
            "matched_artist": (match.get("artist") or {}).get("name"),
        }

    def _search(self, query: str) -> list[dict[str, Any]]:
        data = self._get("/search", q=query)
        results = data.get("data")
        return results if isinstance(results, list) else []
