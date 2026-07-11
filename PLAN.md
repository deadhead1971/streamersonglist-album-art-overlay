# StreamerSonglist Art Fetcher — Build Plan

Handover plan for the implementing agent. Read this whole document before writing code.

## What this is

A shareable tool for music streamers who use [StreamerSonglist](https://www.streamersonglist.com).
It has two halves:

1. **Dashboard** (local Flask web app, offline tool): pulls the streamer's songlist from the
   StreamerSonglist API, proposes album artwork for every song via the iTunes Search API,
   and lets the user confirm / reject / replace each image. Confirmed images form a local
   **artwork library** — the source of truth.
2. **Runtime watcher** (headless, launched by a `.bat` from TouchPortal at stream start):
   watches a `current song.txt` file, and on change writes `current_artwork.png` for OBS to
   display. Lookup order: **confirmed library → cache → live API cascade → fallback image**.
   Live (unverified) grabs are shown on stream but flagged into a review queue in the dashboard.

The predecessor is `D:\Python scripts\song_artwork_fetcher\song_artwork_fetcher_2.py`.
**Read it before starting** — the reflection effect, atomic PNG save (Windows/OBS file-lock
retry), song-file parsing, and MusicBrainz ranking logic should be ported from it, not
rewritten from scratch.

## Decisions already made with the user (do not re-litigate)

- Dashboard = **local web app** (Flask or FastAPI serving a browser UI). Drag-and-drop image
  upload must work.
- Live stream misses = **best guess + review queue** (search live, display it, flag it for
  post-stream review in the dashboard).
- Distribution = **Python repo only** (clone, `pip install -r requirements.txt`, run `.bat`).
  No PyInstaller.
- Watcher stays a **separate .bat** (TouchPortal launches it); the dashboard does not need to
  run during streams.
- Repo: https://github.com/deadhead1971/streamersonglist-art-fetcher (remote already added
  as `origin` in this folder).

## Verified API facts (checked 2026-07-11 — trust these, but re-verify pagination)

### StreamerSonglist (no auth needed for public reads)
- Resolve username → streamer id:
  `GET https://api.streamersonglist.com/v1/streamers/{username}?platform=twitch&isUsername=true`
  → JSON with `id` (int), `name`, etc. For the user's account `alanthompson_music`, id = `21485`.
- Fetch songs:
  `GET https://api.streamersonglist.com/v1/streamers/{id}/songs?size=100&current=0`
  → `{ "total": 185, "items": [ { "id", "title", "artist", "active", "timesPlayed", ... } ] }`
  `title` and `artist` are clean separate fields — no parsing needed.
  **Verify** whether `current` is a page index or an offset before relying on pagination.
- The dashboard should accept either a bare username or a pasted URL like
  `https://www.streamersonglist.com/t/alanthompson_music/songs` (extract the `/t/{username}/` part).

### iTunes Search API (primary artwork source; no key, no auth)
- `GET https://itunes.apple.com/search?term={artist}+{title}&entity=song&limit=5`
  → `results[]` with `trackName`, `artistName`, `collectionName`, `artworkUrl100`.
- Upscale artwork by string-replacing `100x100bb.jpg` → `600x600bb.jpg` or `1000x1000bb.jpg`
  (verified working).
- Add a configurable `country` param (default `GB` for this user).
- **Rate limit ~20 calls/minute.** The bulk proposal run over a whole songlist (185+ songs)
  must throttle (~3s/call), show progress in the UI, be resumable, and cache results so
  re-runs don't re-query.

### Fallback sources (port from `song_artwork_fetcher_2.py`)
- **Last.fm**: only if the user supplies their own API key in config — do NOT ship the
  hardcoded key from the old script; the source is simply skipped when no key is set.
  Reject Last.fm's placeholder image: any URL containing `2a96cbd8b46e442fc41c2b86b821562f`.
- **MusicBrainz + Cover Art Archive**: no key; requires descriptive User-Agent; max 1 req/sec.
  Improvement over v2: rank at **release-group** level using `first-release-date`, then fetch
  `https://coverartarchive.org/release-group/{mbid}/front` (serves art from whichever release
  in the group has a scan) instead of trying individual releases one by one.

### Result validation (applies to every source)
- Artist similarity check: fuzzy-compare returned artist vs requested
  (`difflib.SequenceMatcher`, threshold ~0.6) before accepting.
- Compilation blocklist: deprioritise (don't hard-reject) results whose album/collection name
  contains: greatest hits, best of, collection, anthology, essential, live, deluxe, remaster,
  compilation, karaoke, tribute, cover(s). Prefer a clean-titled result; use a blocklisted one
  only if nothing better exists.
- Title hygiene: strip parenthetical/dash suffixes like `(Live)`, `(Acoustic)`, `- Radio Edit`
  from the search term (but key the library/cache on the original title).

## Architecture

```
streamersonglist_art_fetcher/
├── PLAN.md                    (this file)
├── README.md                  (user-facing: setup for any streamer)
├── requirements.txt           (flask, requests, Pillow, watchdog)
├── config.example.json        (copy to config.json on first run; config.json is gitignored)
├── .gitignore                 (config.json, library/, cache/, *.log, __pycache__)
├── run_dashboard.bat          (starts Flask, opens browser)
├── run_watcher.bat            (watch mode — TouchPortal launches this at stream start)
├── fetch_once.bat             (single fetch, for TouchPortal per-song triggering)
└── app/
    ├── __init__.py
    ├── config.py              (load/save config.json; defaults)
    ├── songlist.py            (StreamerSonglist API client)
    ├── sources.py             (iTunes / Last.fm / MusicBrainz search cascade + validation)
    ├── library.py             (artwork library: manifest + image files + review queue)
    ├── imaging.py             (resize, reflection effect, atomic save — port from v2)
    ├── watcher.py             (runtime: song-file watch loop + single-shot fetch)
    ├── dashboard.py           (Flask app + routes)
    ├── templates/             (Jinja2)
    └── static/                (css/js)
```

### config.json (managed via a dashboard settings page)
```json
{
  "streamersonglist_username": "alanthompson_music",
  "song_file": "D:\\Twitch Streaming\\current song.txt",
  "output_image": "D:\\Twitch Streaming\\current_artwork.png",
  "fallback_image": "D:\\Twitch Streaming\\default_artwork.png",
  "skip_artists": ["alan thompson"],
  "itunes_country": "GB",
  "lastfm_api_key": "",
  "image_size": 640,
  "reflection": { "enabled": true, "height": 0.6, "opacity": 1.0, "gap": 5,
                  "perspective": 0.55, "fade_power": 0.5 }
}
```
All the hardcoded constants in v2 become config. `skip_artists` = the streamer's own
originals → fallback image, no search (port this behaviour).

### Artwork library (`library/` + `library/manifest.json`)
- Images stored at their **source resolution** (up to ~1000px), named human-readably:
  `{artist} - {title}.png` (sanitised for Windows filenames). Resizing + reflection happen at
  output time in the watcher, so changing image_size/reflection config never requires re-fetching.
- `manifest.json` maps a **normalised key** → entry:
  ```json
  { "wish you were here|pink floyd": {
      "title": "Wish You Were Here", "artist": "Pink Floyd",
      "file": "Pink Floyd - Wish You Were Here.png",
      "status": "confirmed",            // proposed | confirmed | unverified | rejected_all
      "source": "itunes",               // itunes | lastfm | musicbrainz | manual
      "candidates_tried": ["<url>", "..."],
      "updated_at": "2026-07-11T12:00:00Z" } }
  ```
- **Normalisation** (one shared function used by dashboard AND watcher — this is the joint
  that makes library lookups hit): lowercase, unicode-normalise/strip diacritics, collapse
  whitespace, strip punctuation, strip `(live)`-style suffixes. Key = `title|artist`.
- `status` meanings: `proposed` = auto-fetched, awaiting review; `confirmed` = user approved;
  `unverified` = grabbed live during a stream (review queue); `rejected_all` = user rejected
  every candidate and hasn't uploaded one yet.

## Dashboard (Flask) — pages/flows

1. **Setup / Settings**: enter StreamerSonglist username or URL; edit all config.json fields;
   test-connection button that resolves the username and shows song count.
2. **Songlist sync**: fetch all songs (paginated), merge into manifest (new songs → no entry;
   removed songs stay but can be filtered). Show table: song, artist, status, thumbnail.
3. **Bulk propose**: for every song without art, run the iTunes search (throttled, progress
   bar via polling or SSE, resumable). Store top result as `proposed` + keep the other
   candidates in the entry for quick rejection cycling.
4. **Review flow** (the core UI — make this fast to use for 185 songs):
   card with big artwork preview, song/artist, album name it came from, source badge.
   Buttons: **Confirm** / **Reject → next candidate** (next iTunes result, then Last.fm,
   then MusicBrainz — live API call on demand) / **Upload** (drag-and-drop or file picker;
   accepts jpg/png/webp, converted to PNG) / **Skip**.
   Keyboard shortcuts (y / n / arrows) are worth the small effort at this volume.
5. **Review queue**: filtered view of `unverified` entries created by the watcher during
   streams — same card UI to confirm/replace.

## Runtime watcher

Port the v2 skeleton (`--watch` via watchdog + single-shot mode, debounce, seed-last-content
fix, atomic save with PermissionError retry — that atomic save exists because OBS holds a
read handle on the PNG; keep it exactly).

Changed lookup order in `fetch_artwork()`:
1. Parse `Song - Artist` from song file (port v2 parsing incl. en/em-dash variants).
2. `skip_artists` → fallback image.
3. **Library lookup** (normalised key, status `confirmed` — also accept `proposed`/`unverified`
   as better-than-nothing): apply resize + reflection → atomic save. Done.
4. Live cascade: iTunes → Last.fm (if key) → MusicBrainz. On success: save image into the
   library as `unverified` (so it appears in the dashboard review queue), then output it.
5. Nothing found → fallback image, and record a `rejected_all`-style stub so the dashboard
   shows the miss.

The old md5 `artwork_cache` is replaced by the library itself (human-readable filenames were
an explicit user request — bad images must be easy to find and delete/replace by hand; the
manifest should tolerate a manually deleted file and treat that entry as needing art again).

## Build order (each milestone runnable/testable)

1. **Core modules**: `config.py`, `songlist.py`, `sources.py`, `imaging.py` (port reflection +
   atomic save from v2), `library.py`. Smoke-test each against the real APIs (use
   `alanthompson_music` / id 21485; the endpoints above are verified live).
2. **Watcher**: full runtime path working end-to-end with a hand-populated library folder.
   Test: write `Wish You Were Here - Pink Floyd` into a test song file → correct PNG appears.
3. **Dashboard**: settings → sync → bulk propose → review flow → review queue.
4. **Polish for sharing**: README (written for a streamer who is not the author — assume only
   basic Python), `.bat` files, `config.example.json`, `.gitignore`. First run with no
   config.json should copy the example and open the settings page rather than crash.

## Pitfalls / notes for the implementing agent

- Windows environment, PowerShell 5.1. Paths contain spaces (`D:\Python scripts\...`,
  `D:\Twitch Streaming\...`) — quote everything in the .bat files.
- Filename sanitisation for `{artist} - {title}.png`: strip `\/:*?"<>|`, trim trailing
  dots/spaces (Windows), cap length.
- Flask must bind localhost only (`127.0.0.1`) — this is a local tool.
- Bulk propose is long-running: run it in a background thread with a progress endpoint;
  don't block a request handler for 10 minutes.
- MusicBrainz User-Agent: identify the app + repo URL
  (`StreamerSonglistArtFetcher/1.0 (+https://github.com/deadhead1971/streamersonglist-art-fetcher)`),
  not the streamer personally.
- Do not commit: `config.json`, `library/`, logs, or any API key. The old script's Last.fm
  key must not appear anywhere in this repo.
- Song titles with `&`, apostrophes, quotes: URL-encode via `requests` params (never manual
  string concat), and escape quotes if building MusicBrainz Lucene queries.
- iTunes sometimes returns karaoke/tribute versions for less-common songs — the artist
  similarity check plus blocklist handles most of it; the review flow is the real safety net.
