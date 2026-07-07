"""SQLite cache for GetSongBPM features and short-lived Spotify responses.

Two tables:
  - track_features: one row per Spotify track id, holding the audio features we
    fetched from GetSongBPM (or a negative-cache flag when GetSongBPM had no
    match). This never expires — a track's tempo/key does not change.
  - api_cache: generic key -> JSON blob with a timestamp, used to avoid
    re-hitting Spotify for things like "top tracks (last 6 months)" on every
    dashboard rerun. Callers pass a TTL when reading.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "audionerd.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS track_features (
                spotify_id    TEXT PRIMARY KEY,
                title         TEXT,
                artist        TEXT,
                bpm           REAL,
                music_key     TEXT,
                open_key      TEXT,
                time_sig      TEXT,
                danceability  REAL,
                acousticness  REAL,
                getsongbpm_id TEXT,
                source        TEXT,
                not_found     INTEGER NOT NULL DEFAULT 0,
                fetched_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS api_cache (
                cache_key  TEXT PRIMARY KEY,
                payload    TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            """
        )
        # Migration: add `source` to pre-existing databases that lack it.
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(track_features)")}
        if "source" not in cols:
            conn.execute("ALTER TABLE track_features ADD COLUMN source TEXT")


# --- track_features -------------------------------------------------------


def get_track_features(spotify_id: str) -> Optional[dict[str, Any]]:
    """Return the cached row for a track, or None on a cache miss.

    A returned row may have not_found=1, meaning we already asked GetSongBPM
    and it had no match — callers should treat that as "don't ask again".
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM track_features WHERE spotify_id = ?", (spotify_id,)
        ).fetchone()
    return dict(row) if row else None


def upsert_track_features(
    spotify_id: str,
    title: str,
    artist: str,
    *,
    bpm: Optional[float] = None,
    music_key: Optional[str] = None,
    open_key: Optional[str] = None,
    time_sig: Optional[str] = None,
    danceability: Optional[float] = None,
    acousticness: Optional[float] = None,
    getsongbpm_id: Optional[str] = None,
    source: Optional[str] = None,
    not_found: bool = False,
) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO track_features (
                spotify_id, title, artist, bpm, music_key, open_key, time_sig,
                danceability, acousticness, getsongbpm_id, source, not_found, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(spotify_id) DO UPDATE SET
                title=excluded.title,
                artist=excluded.artist,
                bpm=excluded.bpm,
                music_key=excluded.music_key,
                open_key=excluded.open_key,
                time_sig=excluded.time_sig,
                danceability=excluded.danceability,
                acousticness=excluded.acousticness,
                getsongbpm_id=excluded.getsongbpm_id,
                source=excluded.source,
                not_found=excluded.not_found,
                fetched_at=excluded.fetched_at
            """,
            (
                spotify_id,
                title,
                artist,
                bpm,
                music_key,
                open_key,
                time_sig,
                danceability,
                acousticness,
                getsongbpm_id,
                source,
                1 if not_found else 0,
                _now(),
            ),
        )


# --- api_cache ------------------------------------------------------------


def get_api_cache(cache_key: str, ttl_seconds: int) -> Optional[Any]:
    """Return the cached JSON payload if present and fresher than ttl_seconds."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT payload, fetched_at FROM api_cache WHERE cache_key = ?",
            (cache_key,),
        ).fetchone()
    if not row:
        return None
    fetched_at = datetime.fromisoformat(row["fetched_at"])
    age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
    if age > ttl_seconds:
        return None
    return json.loads(row["payload"])


def set_api_cache(cache_key: str, payload: Any) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO api_cache (cache_key, payload, fetched_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                payload=excluded.payload,
                fetched_at=excluded.fetched_at
            """,
            (cache_key, json.dumps(payload), _now()),
        )
