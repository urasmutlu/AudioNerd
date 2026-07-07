# AudioNerd 🎧

A locally-run dashboard for your Spotify listening stats and playlist audio
features. See your top tracks and artists across different time frames, inspect
the **tempo (BPM)**, **key**, and **danceability** of every track in your
playlists, and generate **new, sorted playlists** — without touching the
originals.

Because Spotify [deprecated its own audio-features / tempo endpoints in 2024](https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api),
AudioNerd sources tempo/key data from a **layered set of fallbacks** and caches
every result locally in SQLite so repeat loads are instant and API-friendly:

1. **GetSongBPM** — BPM + musical key + danceability (fast metadata lookup)
2. **Deezer** — free BPM metadata for tracks GetSongBPM lacks
3. **Preview analysis** — when no metadata source has it, AudioNerd downloads
   Deezer's 30-second preview and estimates BPM (and key) locally with
   [`librosa`](https://librosa.org). This gives near-complete coverage
   regardless of genre, at the cost of a slower first load per track.

Each row is tagged with the source it came from. Deezer's public API is
rate-limited (50 req / 5 s); AudioNerd stays safely under that with a
sliding-window limiter.

## Features

- **Stats view** — your top tracks and artists over the last 4 weeks, 6 months,
  or all time.
- **Playlists view** — pick any playlist you own, see BPM / key / time
  signature / danceability per track, **sort by** any of them, and create a new
  sorted playlist in one click (the original is never modified).
- **Local SQLite cache** — every GetSongBPM lookup is stored (including
  "no match" results) so a track is never looked up twice.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- A **Spotify Premium** account (required for developer-mode API access as of 2026)
- A Spotify app (Client ID + Secret) from the [developer dashboard](https://developer.spotify.com/dashboard)
- A [GetSongBPM API key](https://getsongbpm.com/api)

## Setup

1. **Install dependencies** (uv creates the virtualenv automatically):

   ```bash
   uv sync
   ```

2. **Register the redirect URI** in your Spotify app settings
   (Dashboard → your app → Settings → Redirect URIs). Add exactly:

   ```
   http://127.0.0.1:8888/callback
   ```

   > Spotify requires `127.0.0.1` (not `localhost`) for loopback redirects.

3. **Add your credentials**. Copy the example env file and fill it in:

   ```bash
   cp .env.example .env
   # then edit .env with your Client ID, Client Secret, and GetSongBPM key
   ```

## Run

```bash
uv run streamlit run app.py
```

The first launch opens a browser tab to authorize AudioNerd against your Spotify
account. After you approve, the token is cached locally (`.cache`) and reused.

## Debug mode

To troubleshoot API issues, run with `--debug` (note the `--`, which tells
Streamlit to pass the flag through to the app):

```bash
uv run streamlit run app.py -- --debug
```

Or set the environment variable instead: `AUDIONERD_DEBUG=1`.

In debug mode, any **non-OK** response from Spotify, GetSongBPM, or Deezer is
logged to the terminal with its status code and body (API keys are redacted),
along with per-track source hits/misses. The sidebar also shows a banner.

### Metadata-only mode

To skip the preview-analysis fallback (faster, but lower coverage), add
`--no-analyze` (or set `AUDIONERD_NO_ANALYZE=1`):

```bash
uv run streamlit run app.py -- --no-analyze
```

## Project layout

```
AudioNerd/
├── app.py                 # Streamlit dashboard (Stats + Playlists views)
├── audionerd/
│   ├── spotify.py         # OAuth + Spotify Web API (Feb-2026 endpoints)
│   ├── getsongbpm.py      # GetSongBPM lookup client (source 1)
│   ├── deezer.py          # Deezer client + rate limiter (source 2 + previews)
│   ├── analyze.py         # librosa BPM/key from a preview clip (source 3)
│   ├── textmatch.py       # shared title/artist matching helpers
│   ├── cache.py           # SQLite cache (track features + API responses)
│   └── enrich.py          # layers the sources; merges features onto tracks
├── pyproject.toml         # uv project + dependencies
└── .env.example           # credential template (copy to .env)
```

## Notes

- Only playlists **you own** are shown (ones you merely follow are excluded,
  since you can't reorder those).
- Podcast episodes and local files are skipped — they have no tempo to look up.
- **Coverage** is near-complete thanks to the three-source fallback. Tracks
  resolved via `analyzed` are estimates from a 30s clip — BPM is reliable for
  steady-beat tracks (±1-2 BPM) but can be octave-off or wrong on ambient
  material, and the key is approximate. Metadata sources (`getsongbpm`,
  `deezer`) are exact and always tried first. Run with `--debug` to see the
  per-track source, or check the **Source** column in the table.

## Attribution

BPM and tempo data provided by <a href="https://getsongbpm.com">GetSongBPM</a>.
