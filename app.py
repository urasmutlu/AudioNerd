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

from audionerd import cache, enrich
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


def render_stats(spotify: SpotifyClient) -> None:
    st.subheader("Your top stats")
    label = st.radio(
        "Time frame", list(TIME_RANGES.values()), horizontal=True, key="stats_range"
    )
    time_range = next(k for k, v in TIME_RANGES.items() if v == label)

    col_tracks, col_artists = st.columns(2)

    with col_tracks:
        st.markdown("#### 🎵 Top tracks")
        tracks = load_top(spotify, "tracks", time_range)
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
        )

    with col_artists:
        st.markdown("#### 🎤 Top artists")
        artists = load_top(spotify, "artists", time_range)
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
        )


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

    display = df_sorted[["title", "artist", "bpm", "music_key", "time_sig", "danceability", "source"]].copy()
    display.columns = ["Title", "Artist", "BPM", "Key", "Time sig", "Danceability", "Source"]
    display.insert(0, "#", range(1, len(display) + 1))
    st.dataframe(display, hide_index=True, use_container_width=True)
    st.caption(
        "Source: `getsongbpm` = BPM+key metadata · `deezer` = Deezer BPM metadata · "
        "`analyzed` = estimated from the 30s preview (BPM/key are approximate)."
    )

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
        render_stats(spotify)
    with playlists_tab:
        render_playlists(spotify, gsb, deezer)


if __name__ == "__main__":
    main()
