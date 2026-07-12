# StreamerSonglist Art Fetcher

Show album artwork on your stream for whatever song you're playing — automatically.

This tool is for music streamers who use [StreamerSonglist](https://www.streamersonglist.com).
It has two halves:

- **Dashboard** — a small web app you run on your own PC (offline, not during a
  stream). It pulls your songlist from StreamerSonglist, finds album artwork for
  every song, and lets you confirm, reject, or upload your own image for each one.
  The images you approve become your **artwork library**.
- **Watcher** — runs during your stream. By default it polls your
  StreamerSonglist queue and treats the song at the top as the current one,
  writing an image file that OBS displays. (It can also watch a text file
  instead, if you drive your current song some other way.) It uses your approved
  library first; if a song isn't in the library yet, it grabs a best guess live
  and flags it so you can review it after the stream.

Artwork comes from the **iTunes Search API** first, then optionally **Last.fm**
(if you add your own key) and **MusicBrainz / Cover Art Archive**.

---

## 1. What you need

- **Windows** (the file-saving is tuned for OBS on Windows).
- **Python 3.10 or newer**. Get it from [python.org](https://www.python.org/downloads/).
  During install, tick **"Add Python to PATH"**.
- A **StreamerSonglist** account with some songs in your list. During a stream,
  keep the song you're playing at the **top of your SSL queue** — that's all the
  watcher needs. (Alternatively, point it at a text file your own setup writes.)

## 2. Install

Download/clone this repository, open a terminal in the folder, and run:

```
pip install -r requirements.txt
```

## 3. First run — the dashboard

Double-click **`run_dashboard.bat`** (or run `python -m app.dashboard`).

- On the very first run it creates a `config.json` for you and opens the
  **Settings** page in your browser at `http://127.0.0.1:5050`.
- Fill in:
  - **Username or songlist URL** — e.g. `yourname` or
    `https://www.streamersonglist.com/t/yourname/songs`. Click **Test connection**
    to check it works and see your song count.
  - **Current song source** — leave on **StreamerSonglist queue** (recommended);
    the watcher reads the top of your live queue. Only switch to **Text file** if
    another tool writes your current song to a file, and set its path below.
  - **Output image** — where the artwork PNG should be written (point OBS at this).
  - **Fallback image** — shown for your own originals and for songs with no art.
  - **Skip artists** — your own artist name(s), comma separated. Songs by these
    artists use the fallback image instead of searching.
  - (Optional) **iTunes country**, **Last.fm API key**, reflection/size settings.
- Click **Save settings**.

## 4. Build your artwork library

On the **Songs** page:

1. **Sync songlist** — pulls all your songs from StreamerSonglist.
2. **Bulk propose artwork** — searches iTunes for every song without art. This is
   throttled (about 3 seconds per song, to respect iTunes' rate limit), so a large
   list takes a while. You can watch the progress bar, and it's **resumable** — if
   you stop and start again it picks up where it left off.
3. Go to the **Review** page to approve artwork one song at a time:
   - **Confirm** (`Y`) — keep this image.
   - **Reject → next** (`N`) — try the next candidate (more iTunes results, then
     Last.fm, then MusicBrainz — fetched on demand).
   - **Upload** (`U`) — drag & drop or pick your own image (JPG/PNG/WebP).
   - **Skip** (`→`) — leave it for later.

The keyboard shortcuts make reviewing a long list fast.

## 5. During your stream

Keep the **dashboard** (`run_dashboard.bat`) running while you're live. It runs
a built-in live service that polls your StreamerSonglist queue every few
seconds and, whenever the song at the top changes, writes the artwork image for
OBS. Keep the song you're playing at the top of your queue and the artwork
follows automatically. When your queue is empty, the fallback image is shown.
The header shows a status indicator (current song and queue length) so you can
sanity-check it before going live.

Prefer not to have the dashboard open? The standalone watcher
(**`run_watcher.bat`**) does the same PNG job headlessly — but the queue
overlay below needs the dashboard. (With the **Text file** source the live
service handles the overlay while the standalone watcher watches your file.)

### The queue overlay

The dashboard also serves an **up-next table** for OBS: your queue with
artwork, title, artist and requester. Open the **Overlay** page in the
dashboard to style it (presets, fields, sizes, how many songs) with a live
preview, then copy the URL into an OBS **Browser** source. The background is
transparent; style changes apply within seconds without touching OBS.

Lookup order for each song:

1. Your **confirmed** library image (also uses proposed/live images if that's all
   there is).
2. A **live search** if the song isn't in your library — the result is shown on
   stream and added to the **Review queue** in the dashboard, flagged as
   *unverified* so you can check it later.
3. The **fallback image** if nothing is found.

Prefer to trigger per-song instead of running a watch loop? Use
**`fetch_once.bat`**, which does a single fetch and exits.

After a stream, open the dashboard's **Queue** page to review anything the watcher
grabbed live and confirm or replace it.

## 6. Your artwork library

Images live in the `library/` folder, named `Artist - Title.png`, at full
resolution. Resizing and the reflection effect happen when the image is written for
OBS, so changing the image size or reflection settings never means re-fetching.

If a proposed image is wrong, you can just **delete the file** in `library/` — the
tool notices it's gone and treats that song as needing art again.

## Files in this repo

| File | What it does |
|------|--------------|
| `run_dashboard.bat` | Start the dashboard web app (offline tool). |
| `run_watcher.bat` | Start the runtime watcher (during streams). |
| `fetch_once.bat` | Fetch artwork for the current song once, then exit. |
| `config.example.json` | Template copied to `config.json` on first run. |
| `app/` | The application code. |

`config.json`, your `library/`, and logs are **not** committed to git — they're
yours and local.

## Troubleshooting

- **`python` not found** — reinstall Python with "Add Python to PATH" ticked, or
  reopen your terminal.
- **Test connection fails** — check the username/URL; you can also paste your full
  songlist URL.
- **No artwork for a song** — use **Reject → next** to try other sources, or
  **Upload** your own image.
- **Last.fm is skipped** — that's expected unless you add your own free API key in
  Settings.
- **Logs** — see `artwork_fetcher.log` in the project folder.
