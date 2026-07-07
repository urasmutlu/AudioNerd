"""Thin Spotify Web API client tuned for the Feb-2026 dev-mode endpoint set.

We use spotipy only for the OAuth dance (it spins up the local callback server
and caches/refreshes the token). Every API call is made with `requests` so we
control exactly which endpoints we hit — several changed in Feb 2026 and
spotipy's built-in helpers still target the old, now-removed routes:

  - Create playlist:  POST /users/{id}/playlists  ->  POST /me/playlists
  - Playlist items:   /playlists/{id}/tracks       ->  /playlists/{id}/items

For the read/write of playlist items we try the new `/items` path first and
fall back to `/tracks`, so this keeps working whichever your app enforces.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator, Optional

import requests
from spotipy.oauth2 import SpotifyOAuth

logger = logging.getLogger("audionerd.spotify")

API_BASE = "https://api.spotify.com/v1"

# Scopes: read your listening stats + read and modify your playlists.
SCOPES = "user-top-read playlist-read-private playlist-modify-private playlist-modify-public"

# time_range values Spotify accepts for GET /me/top/{type}, with friendly labels.
TIME_RANGES = {
    "short_term": "Last 4 weeks",
    "medium_term": "Last 6 months",
    "long_term": "All time",
}


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        cache_path: str = ".cache",
        debug: bool = False,
    ) -> None:
        self._debug = debug
        self._auth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scope=SCOPES,
            cache_path=cache_path,
            open_browser=True,
        )
        self._session = requests.Session()
        self._user_id: Optional[str] = None

    # --- low-level -------------------------------------------------------

    def _token(self) -> str:
        # Triggers the browser auth flow on first use, then reads the cached
        # token and refreshes it automatically when expired.
        tok = self._auth.get_access_token()
        return tok["access_token"] if isinstance(tok, dict) else tok

    def _request(
        self, method: str, path: str, *, params=None, json=None
    ) -> requests.Response:
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        for attempt in range(5):
            resp = self._session.request(
                method,
                url,
                params=params,
                json=json,
                headers={"Authorization": f"Bearer {self._token()}"},
            )
            if resp.status_code == 429:  # rate limited — honour Retry-After
                wait = int(resp.headers.get("Retry-After", "1")) + 1
                time.sleep(min(wait, 30))
                continue
            if not resp.ok and self._debug:
                # Token lives in the Authorization header, so the URL is safe to log.
                logger.warning(
                    "Spotify %s %s -> HTTP %s\n%s",
                    method,
                    url,
                    resp.status_code,
                    resp.text[:1000],
                )
            return resp
        return resp  # return the last response so the caller can raise

    def _get(self, path: str, params=None) -> dict[str, Any]:
        resp = self._request("GET", path, params=params)
        resp.raise_for_status()
        return resp.json()

    def _paginate(self, path: str, params=None) -> Iterator[dict[str, Any]]:
        """Yield every item across a paged Spotify list endpoint."""
        params = dict(params or {})
        params.setdefault("limit", 50)
        params.setdefault("offset", 0)
        while True:
            page = self._get(path, params=params)
            items = page.get("items", [])
            yield from items
            if page.get("next"):
                params["offset"] += len(items)
            else:
                return

    # --- user & stats ----------------------------------------------------

    def me(self) -> dict[str, Any]:
        return self._get("/me")

    def user_id(self) -> str:
        if self._user_id is None:
            self._user_id = self.me()["id"]
        return self._user_id

    def top_items(
        self, item_type: str, time_range: str = "medium_term", limit: int = 20
    ) -> list[dict[str, Any]]:
        """item_type is 'tracks' or 'artists'."""
        data = self._get(
            f"/me/top/{item_type}",
            params={"time_range": time_range, "limit": limit},
        )
        return data.get("items", [])

    # --- playlists -------------------------------------------------------

    def my_playlists(self) -> list[dict[str, Any]]:
        """All playlists owned by the current user (excludes ones you only follow)."""
        uid = self.user_id()
        return [
            pl
            for pl in self._paginate("/me/playlists")
            if pl.get("owner", {}).get("id") == uid
        ]

    def playlist_tracks(self, playlist_id: str) -> list[dict[str, Any]]:
        """Return simplified track dicts for a playlist.

        Tries the new /items route, falls back to /tracks. Skips local files
        and podcast episodes (no artist/tempo to look up).
        """
        for path in (f"/playlists/{playlist_id}/items", f"/playlists/{playlist_id}/tracks"):
            try:
                elements = list(self._paginate(path))
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code in (403, 404):
                    continue  # endpoint not available for this app — try the other
                raise
            return [t for t in map(self._simplify_track, elements) if t]
        return []

    @staticmethod
    def _simplify_track(element: dict[str, Any]) -> Optional[dict[str, Any]]:
        # Playlist entries wrap the track under "track" (old) or "item" (new).
        track = element.get("track") or element.get("item") or element
        if not track or track.get("type") == "episode":
            return None
        tid = track.get("id")
        uri = track.get("uri")
        if not tid or not uri or uri.startswith("spotify:local"):
            return None
        artists = track.get("artists") or []
        return {
            "spotify_id": tid,
            "uri": uri,
            "title": track.get("name", ""),
            "artist": ", ".join(a.get("name", "") for a in artists) or "Unknown",
            "primary_artist": artists[0].get("name", "") if artists else "",
            "duration_ms": track.get("duration_ms"),
        }

    def create_playlist(
        self, name: str, description: str = "", public: bool = False
    ) -> dict[str, Any]:
        """Create a playlist for the current user (POST /me/playlists)."""
        resp = self._request(
            "POST",
            "/me/playlists",
            json={"name": name, "description": description, "public": public},
        )
        resp.raise_for_status()
        return resp.json()

    def add_items(self, playlist_id: str, uris: list[str]) -> None:
        """Add track URIs to a playlist, 100 at a time, newest endpoint first."""
        for i in range(0, len(uris), 100):
            chunk = uris[i : i + 100]
            for path in (
                f"/playlists/{playlist_id}/items",
                f"/playlists/{playlist_id}/tracks",
            ):
                resp = self._request("POST", path, json={"uris": chunk})
                if resp.status_code in (403, 404):
                    continue  # try the other endpoint spelling
                resp.raise_for_status()
                break
