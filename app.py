"""AudioNerd — a local Streamlit dashboard for your Spotify stats and playlist
audio features (tempo/key/danceability via GetSongBPM).

Run with:  uv run streamlit run app.py
"""

from __future__ import annotations

import os

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from audionerd import cache, enrich
from audionerd.getsongbpm import GetSongBPMClient
from audionerd.spotify import TIME_RANGES, SpotifyClient

load_dotenv()

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
def get_clients() -> tuple[SpotifyClient, GetSongBPMClient]:
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
    )
    gsb = GetSongBPMClient(os.environ["GETSONGBPM_API_KEY"])
    return spotify, gsb


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


def render_playlists(spotify: SpotifyClient, gsb: GetSongBPMClient) -> None:
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
            bar = st.progress(0, text="Looking up tempos…")
            rows = enrich.enrich_tracks(
                tracks, gsb, progress=lambda done, total: bar.progress(done / total, text=f"{done}/{total} tracks")
            )
            status.update(label=f"Loaded {len(rows)} tracks", state="complete")
        store[pid] = rows
    rows = store[pid]

    if not rows:
        st.warning("No playable tracks found in this playlist.")
        return

    df = pd.DataFrame(rows)
    matched = df["bpm"].notna().sum()
    st.caption(f"Tempo/key found for {matched} of {len(df)} tracks via GetSongBPM.")

    sort_label = st.selectbox("Sort by", list(SORT_OPTIONS.keys()))
    column, ascending = SORT_OPTIONS[sort_label]
    df_sorted = df.sort_values(by=column, ascending=ascending, na_position="last").reset_index(drop=True)

    display = df_sorted[["title", "artist", "bpm", "music_key", "time_sig", "danceability"]].copy()
    display.columns = ["Title", "Artist", "BPM", "Key", "Time sig", "Danceability"]
    display.insert(0, "#", range(1, len(display) + 1))
    st.dataframe(display, hide_index=True, use_container_width=True)

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
    spotify, gsb = get_clients()

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
        render_playlists(spotify, gsb)


if __name__ == "__main__":
    main()
