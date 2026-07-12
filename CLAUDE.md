# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A tool for music streamers using StreamerSonglist. Two halves sharing one artwork library:

1. **Dashboard** (`python -m app.dashboard`, or `run_dashboard.bat`) — local Flask app at `http://127.0.0.1:5050`. Syncs the songlist from the StreamerSonglist API, bulk-proposes artwork via iTunes Search, and provides a review UI to confirm/reject/upload images.
2. **Watcher** (`python -m app.watcher`, or `run_watcher.bat` / `fetch_once.bat`) — headless runtime launched at stream start (by TouchPortal). Resolves the current song (default: top of the live SSL queue; alternative: a watched text file) and writes `current_artwork.png` for OBS.

`PLAN.md` is the original build plan — the authoritative record of design decisions (API facts, rate limits, validation rules). `README.md` is user-facing.

## Commands

- Install: `pip install -r requirements.txt` (Flask, requests, Pillow, watchdog)
- Run dashboard: `python -m app.dashboard`
- Run watcher: `python -m app.watcher` (add `--once` behaviour via `fetch_once.bat`)
- No tests or linter are configured. Verify by running the app; logs go to `artwork_fetcher.log`.

Windows environment (PowerShell 5.1); paths contain spaces — quote everything in `.bat` files.

## Architecture

Modules in `app/`:

- `config.py` — loads/saves `config.json` (gitignored; created from `config.example.json` on first run). All tunables live here: username, song source, output/fallback image paths, `skip_artists`, reflection settings.
- `songlist.py` — StreamerSonglist API client (public, no auth): resolve username → streamer id, fetch songs, poll live queue.
- `sources.py` — artwork search cascade: iTunes (primary) → Last.fm (only if user supplies a key — never hardcode one) → MusicBrainz/Cover Art Archive (release-group level, 1 req/sec, descriptive User-Agent). Every result passes artist fuzzy-match (~0.6) and a compilation/karaoke blocklist that deprioritises (not rejects).
- `library.py` — the artwork library: `library/manifest.json` + full-resolution PNGs named `Artist - Title.png`. Statuses: `proposed` / `confirmed` / `unverified` (grabbed live, needs review) / `rejected_all`. The manifest tolerates manually deleted image files (entry reverts to needing art).
- `artwork.py` — shared propose/fetch logic used by both dashboard and watcher.
- `imaging.py` — resize, reflection effect, and the **atomic PNG save with PermissionError retry** (OBS holds a read handle on the output file — keep this exactly as is).
- `watcher.py` — runtime loop. Lookup order: skip_artists → fallback; library (confirmed, else proposed/unverified); live cascade (result saved as `unverified` for post-stream review); fallback + `rejected_all` stub.
- `dashboard.py` — Flask routes; templates in `app/templates/`, CSS in `app/static/`.

### Key joints

- `library.normalize_key(title, artist)` is the shared normalisation (lowercase, strip diacritics/punctuation/`(live)` suffixes) used by **both** dashboard and watcher — it's what makes library lookups hit. Search terms are stripped of suffixes, but library/cache keys use the original title.
- Images are stored at source resolution; resize + reflection happen at output time in the watcher, so config changes never require re-fetching.
- Bulk propose is throttled (~3s/call, iTunes limit ~20/min), runs in a background thread with a polled progress endpoint, and is resumable — never block a request handler on it.
- Flask binds `127.0.0.1` only. Dashboard port is **5050**.

## Constraints

- Do not commit `config.json`, `library/`, logs, or any API key.
- URL-encode search terms via `requests` params (titles contain `&`, apostrophes); escape quotes in MusicBrainz Lucene queries.
- Filename sanitisation for library PNGs: strip `\/:*?"<>|`, trim trailing dots/spaces, cap length.
- Reject Last.fm's placeholder image (URL containing `2a96cbd8b46e442fc41c2b86b821562f`).
