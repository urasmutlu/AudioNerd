"""Minimal GetSongBPM API client.

Docs: https://getsongbpm.com/api

We use the combined song+artist search, take the best match, and (when the
search result is thin) fetch the song detail for danceability/acousticness.
The caller is expected to cache results — GetSongBPM's free tier is rate
limited, so we never want to look up the same track twice.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from .textmatch import artist_matches, clean_title as _clean_title, norm as _norm

logger = logging.getLogger("audionerd.getsongbpm")

# The public docs advertise api.getsongbpm.com, but that host sits behind a
# Cloudflare "managed challenge" that returns an HTML 403 to any non-browser
# client. The original api.getsong.co host serves the same JSON API without the
# challenge, so we use it directly.
API_BASE = "https://api.getsong.co"


class GetSongBPMClient:
    def __init__(self, api_key: str, *, debug: bool = False) -> None:
        self._api_key = api_key
        self._debug = debug
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": "AudioNerd/0.1 (+https://github.com/urasmutlu/AudioNerd)",
                "Accept": "application/json",
            }
        )

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {"api_key": self._api_key, **params}
        resp = self._session.get(f"{API_BASE}{path}", params=params, timeout=15)
        if not resp.ok and self._debug:
            safe = {**params, "api_key": "***"}
            logger.warning(
                "GetSongBPM GET %s params=%s -> HTTP %s\n%s",
                path,
                safe,
                resp.status_code,
                resp.text[:1000],
            )
        resp.raise_for_status()
        return resp.json()

    def _search(self, lookup: str, search_type: str) -> list[dict[str, Any]]:
        try:
            data = self._get("/search/", {"type": search_type, "lookup": lookup})
        except requests.HTTPError:
            return []
        results = data.get("search")
        # GetSongBPM returns {"error": "no result"} (a dict) when nothing matched.
        return results if isinstance(results, list) else []

    def lookup(self, title: str, artist: str) -> Optional[dict[str, Any]]:
        """Return a normalised feature dict for a track, or None if not found.

        GetSongBPM's search is picky: remix/version suffixes ("- Extended Mix")
        make it return nothing, and it caps at 30 results with no paging. So we
        try, in order:
          1. combined "song:<clean title> artist:<artist>"  (API filters artist)
          2. combined with the raw title (in case cleaning was wrong)
          3. song-only on the clean title, then filter to the artist ourselves
        Keys returned: bpm, music_key, open_key, time_sig, danceability,
        acousticness, getsongbpm_id.
        """
        clean = _clean_title(title)
        titles = [clean] if clean.lower() == title.lower() else [clean, title]

        match = None
        for t in titles:  # strategies 1 & 2: combined, API does the artist filter
            results = self._search(f"song:{t} artist:{artist}", "both")
            if results:
                match = self._best_match(results, t, artist)
                if match:
                    break

        if match is None:  # strategy 3: song-only, we filter by artist strictly
            results = self._search(clean, "song")
            match = self._match_by_artist(results, artist)

        if match is None:
            if self._debug:
                logger.info("miss (not in GetSongBPM catalog) for %r by %r", title, artist)
            return None

        if self._debug:
            logger.debug("hit %r by %r -> id=%s", title, artist, match.get("id"))

        features = {
            "bpm": _to_float(match.get("tempo")),
            "music_key": match.get("key_of"),
            "open_key": match.get("open_key"),
            "time_sig": match.get("time_sig"),
            "danceability": _to_float(match.get("danceability")),
            "acousticness": _to_float(match.get("acousticness")),
            "getsongbpm_id": match.get("id"),
        }

        # Search results often omit danceability/acousticness; fill them from
        # the song detail endpoint when we have an id and they're missing.
        if match.get("id") and features["danceability"] is None:
            detail = self._song_detail(match["id"])
            if detail:
                features["danceability"] = _to_float(detail.get("danceability"))
                features["acousticness"] = _to_float(detail.get("acousticness"))
                features["time_sig"] = features["time_sig"] or detail.get("time_sig")
        return features

    def _song_detail(self, song_id: str) -> Optional[dict[str, Any]]:
        try:
            data = self._get("/song/", {"id": song_id})
        except requests.HTTPError:
            return None
        song = data.get("song")
        return song if isinstance(song, dict) else None

    @staticmethod
    def _best_match(
        results: list[dict[str, Any]], title: str, artist: str
    ) -> Optional[dict[str, Any]]:
        # Prefer a result whose artist matches; fall back to first result.
        for r in results:
            if artist_matches((r.get("artist") or {}).get("name", ""), artist):
                return r
        want_title = _norm(title)
        for r in results:
            if _norm(r.get("title", "")) == want_title:
                return r
        return results[0]

    @staticmethod
    def _match_by_artist(
        results: list[dict[str, Any]], artist: str
    ) -> Optional[dict[str, Any]]:
        """Like _best_match but REQUIRES an artist match (no first-result fallback).

        Used for the song-only fallback, where results aren't pre-filtered by
        the API — taking the first result would grab a same-titled song by a
        completely different artist.
        """
        for r in results:
            if artist_matches((r.get("artist") or {}).get("name", ""), artist):
                return r
        return None


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
