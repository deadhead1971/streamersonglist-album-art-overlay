# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A tool for music streamers using StreamerSonglist. One app — the **dashboard** (`python -m app.dashboard`, or `run_dashboard.bat`) — a local Flask app at `http://127.0.0.1:5050`. It syncs the songlist from the StreamerSonglist API, bulk-proposes artwork via iTunes Search, and provides a review UI to confirm/reject/upload images. On startup it also launches the **runtime service** (`app/runtime.py`): a background thread that resolves the current song (live SSL queue top, or a text file in `file` song-source mode), writes `current_artwork.png` for OBS, and feeds the **queue overlay** (`/overlay/queue`, an OBS browser source; configured at `/overlay`). The dashboard is the only process, offline and during a live stream. (A standalone headless watcher existed historically; it was folded into the runtime service in July 2026.)

`README.md` is user-facing. (`PLAN.md`, the original build plan with design decisions — API facts, rate limits, validation rules — is kept locally and not committed; ignore references to it if you don't have the file.)

## Commands

- Install: `pip install -r requirements.txt` (Flask, requests, Pillow)
- Run dashboard: `python -m app.dashboard`
- No tests or linter are configured. Verify by running the app; logs go to `artwork_fetcher.log`.

Windows environment (PowerShell 5.1); paths contain spaces — quote everything in `.bat` files.

## Architecture

Modules in `app/`:

- `config.py` — loads/saves `config.json` (gitignored; created from `config.example.json` on first run). All tunables live here: username, song source, output/fallback image paths, `skip_artists`, reflection settings.
- `songlist.py` — StreamerSonglist API client (public, no auth): resolve username → streamer id, fetch songs, poll live queue.
- `sources.py` — artwork search cascade: iTunes (primary) → Last.fm (only if user supplies a key — never hardcode one) → MusicBrainz/Cover Art Archive (release-group level, 1 req/sec, descriptive User-Agent). Every result passes artist fuzzy-match (~0.6) and a compilation/karaoke blocklist that deprioritises (not rejects).
- `library.py` — the artwork library: `library/manifest.json` + full-resolution PNGs named `Artist - Title.png`. Statuses: `proposed` / `confirmed` / `unverified` (grabbed live, needs review) / `rejected_all`. The manifest tolerates manually deleted image files (entry reverts to needing art).
- `artwork.py` — shared artwork logic used by both the runtime service and dashboard request handlers: propose/reject-cycle/upload, plus `resolve_artwork_for_song` (the live lookup order: skip_artists → fallback; library confirmed, else proposed/unverified; live cascade, result saved as `unverified` for post-stream review; fallback + `rejected_all` stub), `apply_fallback`, and `read_song_file`.
- `imaging.py` — resize, reflection effect, and the **atomic PNG save with PermissionError retry** (OBS holds a read handle on the output file — keep this exactly as is).
- `runtime.py` — in-process background service (started by the dashboard). One tick per interval polls the queue (feeds the cached queue behind `/overlay/queue.json`) and resolves the current song — queue top, or the song file in `file` mode — to the PNG output via `artwork.resolve_artwork_for_song`. Creates a fresh `Library` per resolve; all manifest I/O is serialised via `library.MANIFEST_LOCK`. In `file` mode it runs even without an SSL username (PNG only, empty overlay).
- `dashboard.py` — Flask routes; templates in `app/templates/`, CSS in `app/static/`. Overlay routes: `/overlay` (settings + OBS URL), `/overlay/queue` (transparent browser-source page; style presets, config-driven with query-param overrides), `/overlay/queue.json` (data; never triggers artwork fetches — library hit or fallback only).

### Key joints

- `library.normalize_key(title, artist)` is the shared normalisation (lowercase, strip diacritics/punctuation/`(live)` suffixes) used by **both** dashboard and runtime — it's what makes library lookups hit. Search terms are stripped of suffixes, but library/cache keys use the original title.
- Images are stored at source resolution; resize + reflection happen at output time, so config changes never require re-fetching.
- Bulk propose is throttled (~3s/call, iTunes limit ~20/min), runs in a background thread with a polled progress endpoint, and is resumable — never block a request handler on it.
- Flask binds `127.0.0.1` only. Dashboard port is **5050**.

## Constraints

- Do not commit `config.json`, `library/`, logs, or any API key.
- URL-encode search terms via `requests` params (titles contain `&`, apostrophes); escape quotes in MusicBrainz Lucene queries.
- Filename sanitisation for library PNGs: strip `\/:*?"<>|`, trim trailing dots/spaces, cap length.
- Reject Last.fm's placeholder image (URL containing `2a96cbd8b46e442fc41c2b86b821562f`).
