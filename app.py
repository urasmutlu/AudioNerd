"""AudioNerd — a local Streamlit dashboard for your Spotify stats and playlist
audio features (tempo/key/danceability via GetSongBPM).

Run with:  uv run streamlit run app.py
"""

from __future__ import annotations

import argparse
import logging
import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from audionerd import cache, camelot, enrich
from audionerd.deezer import DeezerClient
from audionerd.getsongbpm import GetSongBPMClient
from audionerd.spotify import TIME_RANGES, SpotifyClient

load_dotenv()


def _parse_flags() -> argparse.Namespace:
    """Flags via `streamlit run app.py -- --debug --no-analyze` (note the `--`)."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-analyze", action="store_true")
    args, _ = parser.parse_known_args()
    return args


def _configure_debug_logging() -> None:
    """Attach our own handler to the `audionerd` logger.

    Streamlit configures the root logger before running this script, which makes
    `logging.basicConfig()` a silent no-op. So we attach a StreamHandler directly
    to our logger (writing to stderr, which shows up in the terminal) and stop it
    propagating to Streamlit's handlers. Guarded so Streamlit's per-interaction
    reruns don't stack duplicate handlers.
    """
    log = logging.getLogger("audionerd")
    log.setLevel(logging.DEBUG)
    log.propagate = False
    if not getattr(log, "_audionerd_handler_added", False):
        handler = logging.StreamHandler()  # -> sys.stderr -> your terminal
        handler.setFormatter(
            logging.Formatter("%(asctime)s  %(name)s  %(levelname)s  %(message)s")
        )
        log.addHandler(handler)
        log._audionerd_handler_added = True  # type: ignore[attr-defined]
    logging.getLogger("urllib3").setLevel(logging.WARNING)


_FLAGS = _parse_flags()
DEBUG = _FLAGS.debug or os.getenv("AUDIONERD_DEBUG") == "1"
# Preview-analysis fallback (source 3) is on unless disabled.
ANALYZE = not (_FLAGS.no_analyze or os.getenv("AUDIONERD_NO_ANALYZE") == "1")
if DEBUG:
    _configure_debug_logging()

st.set_page_config(page_title="AudioNerd", page_icon="🎧", layout="wide")

# Sort options for the playlist view: label -> (column, ascending).
SORT_OPTIONS = {
    "BPM (slow → fast)": ("bpm", True),
    "BPM (fast → slow)": ("bpm", False),
    "Danceability (high → low)": ("danceability", False),
    "Key": ("music_key", True),
    "Title (A → Z)": ("title", True),
    "Artist (A → Z)": ("artist", True),
}


# Streamlit dataframe geometry: ~35px per row + ~38px header. Sizing a table to
# `max_rows` means those rows are visible without an inner scrollbar; anything
# beyond scrolls inside the table.
_ROW_PX = 35
_HEADER_PX = 38


def _table_height(n_rows: int, max_rows: int = 25) -> int:
    return _HEADER_PX + min(n_rows, max_rows) * _ROW_PX


def _normalize_track(t: dict) -> dict:
    """Shape a raw Spotify track object into what `enrich.enrich_tracks` expects."""
    artists = t.get("artists", []) or []
    return {
        "spotify_id": t.get("id"),
        "title": t.get("name"),
        "artist": ", ".join(a["name"] for a in artists),
        "primary_artist": artists[0]["name"] if artists else "",
        "uri": t.get("uri"),
    }


def _show_wheel(counts: dict[str, int], title: str | None = None) -> None:
    """Render a Camelot wheel and release the figure (avoids a memory leak)."""
    import matplotlib.pyplot as plt

    fig = camelot.wheel_figure(counts, title=title)
    st.pyplot(fig, use_container_width=True)
    plt.close(fig)


# --- resources (built once per session) ----------------------------------


@st.cache_resource
def get_clients() -> tuple[SpotifyClient, GetSongBPMClient, DeezerClient]:
    required = ["SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI", "GETSONGBPM_API_KEY"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        st.error(
            "Missing environment variables: "
            + ", ".join(missing)
            + ".\n\nCopy `.env.example` to `.env` and fill in your credentials."
        )
        st.stop()

    cache.init_db()
    spotify = SpotifyClient(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],
        debug=DEBUG,
    )
    gsb = GetSongBPMClient(os.environ["GETSONGBPM_API_KEY"], debug=DEBUG)
    deezer = DeezerClient(debug=DEBUG)
    return spotify, gsb, deezer


@st.cache_data(ttl=900, show_spinner=False)
def load_top(_spotify: SpotifyClient, item_type: str, time_range: str) -> list[dict]:
    return _spotify.top_items(item_type, time_range=time_range, limit=25)


@st.cache_data(ttl=900, show_spinner=False)
def load_my_playlists(_spotify: SpotifyClient) -> list[dict]:
    return _spotify.my_playlists()


# --- views ----------------------------------------------------------------


def _top_track_keys(
    gsb: GetSongBPMClient, deezer: DeezerClient, tracks: list[dict], time_range: str
) -> list[str]:
    """Enrich the top tracks (once per session per time frame) and return their keys."""
    store = st.session_state.setdefault("top_enriched", {})
    if time_range not in store:
        normalized = [_normalize_track(t) for t in tracks]
        with st.spinner("Analyzing keys for your top tracks…"):
            rows = enrich.enrich_tracks(normalized, gsb, deezer, analyze=ANALYZE)
        store[time_range] = [r["music_key"] for r in rows if r.get("music_key")]
    return store[time_range]


def render_stats(
    spotify: SpotifyClient, gsb: GetSongBPMClient, deezer: DeezerClient
) -> None:
    st.subheader("Your top stats")
    label = st.radio(
        "Time frame", list(TIME_RANGES.values()), horizontal=True, key="stats_range"
    )
    time_range = next(k for k, v in TIME_RANGES.items() if v == label)

    tracks = load_top(spotify, "tracks", time_range)
    artists = load_top(spotify, "artists", time_range)
    col_tracks, col_artists = st.columns(2)

    with col_tracks:
        st.markdown("#### 🎵 Top tracks")
        st.dataframe(
            pd.DataFrame(
                {
                    "#": range(1, len(tracks) + 1),
                    "Title": [t.get("name") for t in tracks],
                    "Artist": [", ".join(a["name"] for a in t.get("artists", [])) for t in tracks],
                    "Album": [t.get("album", {}).get("name") for t in tracks],
                }
            ),
            hide_index=True,
            use_container_width=True,
            height=_table_height(len(tracks)),  # all 25 visible; page scrolls past
        )

    with col_artists:
        st.markdown("#### 🎤 Top artists")
        st.dataframe(
            pd.DataFrame(
                {
                    "#": range(1, len(artists) + 1),
                    "Artist": [a.get("name") for a in artists],
                    "Genres": [", ".join(a.get("genres", [])[:3]) for a in artists],
                }
            ),
            hide_index=True,
            use_container_width=True,
            height=_table_height(len(artists), max_rows=12),  # shorter; leaves room below
        )
        # Camelot wheel for the top tracks, filling the space under the artists list.
        st.markdown("#### 🎡 Key wheel · top tracks")
        keys = _top_track_keys(gsb, deezer, tracks, time_range)
        counts = camelot.camelot_counts(keys)
        if any(counts.values()):
            _show_wheel(counts)
            st.caption(f"Harmonic map of {len(keys)} of {len(tracks)} top tracks with a known key.")
        else:
            st.info("No key data available for your top tracks yet.")


def render_playlists(
    spotify: SpotifyClient, gsb: GetSongBPMClient, deezer: DeezerClient
) -> None:
    st.subheader("Playlists")
    playlists = load_my_playlists(spotify)
    if not playlists:
        st.info("You don't own any playlists yet.")
        return

    by_name = {f"{pl['name']}  ({pl['tracks']['total']} tracks)": pl for pl in playlists}
    choice = st.selectbox("Pick one of your playlists", list(by_name.keys()))
    playlist = by_name[choice]
    pid = playlist["id"]

    # Enrich this playlist's tracks (cached per session so sorting is instant).
    store = st.session_state.setdefault("enriched", {})
    if pid not in store:
        with st.status("Fetching tracks and audio features…", expanded=True) as status:
            tracks = spotify.playlist_tracks(pid)
            label = "Looking up tempos" + ("" if ANALYZE else " (metadata only)") + "…"
            bar = st.progress(0, text=label)
            rows = enrich.enrich_tracks(
                tracks,
                gsb,
                deezer,
                analyze=ANALYZE,
                progress=lambda done, total: bar.progress(done / total, text=f"{done}/{total} tracks"),
            )
            status.update(label=f"Loaded {len(rows)} tracks", state="complete")
        store[pid] = rows
    rows = store[pid]

    if not rows:
        st.warning("No playable tracks found in this playlist.")
        return

    df = pd.DataFrame(rows)
    matched = int(df["bpm"].notna().sum())
    by_source = df[df["bpm"].notna()]["source"].value_counts().to_dict()
    breakdown = ", ".join(f"{v} {k}" for k, v in by_source.items()) or "none"
    st.caption(f"Tempo found for **{matched} of {len(df)}** tracks  ·  by source: {breakdown}")

    sort_label = st.selectbox("Sort by", list(SORT_OPTIONS.keys()))
    column, ascending = SORT_OPTIONS[sort_label]
    df_sorted = df.sort_values(by=column, ascending=ascending, na_position="last").reset_index(drop=True)

    def _fmt_bpm(r) -> str:
        if pd.isna(r["bpm"]):
            return ""
        # `~` marks an estimate from preview analysis (the least reliable source).
        prefix = "~" if r["source"] == "analyzed" else ""
        return f"{prefix}{r['bpm']:.0f}"

    def _fmt_source(r) -> str:
        if r["source"] == "analyzed":
            return f"analyzed ({r.get('confidence') or '?'})"
        return r["source"] or ""

    display = pd.DataFrame(
        {
            "Title": df_sorted["title"],
            "Artist": df_sorted["artist"],
            "BPM": df_sorted.apply(_fmt_bpm, axis=1),
            "Key": df_sorted["music_key"],
            "Time sig": df_sorted["time_sig"],
            "Danceability": df_sorted["danceability"],
            "Source": df_sorted.apply(_fmt_source, axis=1),
        }
    )
    display.insert(0, "#", range(1, len(display) + 1))
    st.dataframe(
        display,
        hide_index=True,
        use_container_width=True,
        height=_table_height(len(display)),  # 25 rows tall, then scroll within the table
    )
    st.caption(
        "**`~`** marks an estimate from preview analysis — reliable for steady-beat "
        "tracks but prone to octave (half/double) errors on slow or ambient songs; "
        "the confidence in the Source column reflects this. "
        "`getsongbpm` and `deezer` are exact metadata and always preferred."
    )

    # Camelot wheel — this playlist's harmonic fingerprint (below the track table).
    key_counts = camelot.camelot_counts(df["music_key"].dropna().tolist())
    if any(key_counts.values()):
        st.markdown("#### 🎡 Key wheel")
        mid = st.columns([1, 2, 1])[1]
        with mid:
            _show_wheel(key_counts)
        n_keyed = int(df["music_key"].notna().sum())
        st.caption(f"Harmonic map of {n_keyed} of {len(df)} tracks with a known key.")

    # Create a NEW sorted playlist (never modifies the original).
    st.divider()
    default_name = f"{playlist['name']} · {sort_label} (AudioNerd)"
    new_name = st.text_input("New playlist name", value=default_name)
    if st.button("➕ Create sorted playlist", type="primary"):
        with st.spinner("Creating playlist…"):
            created = spotify.create_playlist(
                new_name,
                description=f"Sorted by {sort_label} with AudioNerd.",
                public=False,
            )
            spotify.add_items(created["id"], df_sorted["uri"].tolist())
        url = created.get("external_urls", {}).get("spotify", "")
        st.success(f"Created **{new_name}** with {len(df_sorted)} tracks.")
        if url:
            st.markdown(f"[Open it in Spotify]({url})")


# --- main -----------------------------------------------------------------


def main() -> None:
    st.title("🎧 AudioNerd")
    if DEBUG:
        st.sidebar.warning("🐞 Debug mode ON — non-OK API responses are logged to the terminal.")
    if not ANALYZE:
        st.sidebar.info("⚡ Analysis off — metadata sources only (faster, less coverage).")
    spotify, gsb, deezer = get_clients()

    try:
        user = spotify.me()
    except Exception as exc:  # noqa: BLE001 — surface any auth/setup error to the user
        st.error(f"Could not reach Spotify: {exc}")
        st.stop()

    st.caption(f"Signed in as **{user.get('display_name') or user.get('id')}**")
    stats_tab, playlists_tab = st.tabs(["📊 Stats", "🎚️ Playlists"])
    with stats_tab:
        render_stats(spotify, gsb, deezer)
    with playlists_tab:
        render_playlists(spotify, gsb, deezer)


if __name__ == "__main__":
    main()
