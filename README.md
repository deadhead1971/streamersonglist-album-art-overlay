# StreamerSonglist Album Art Overlay

An OBS overlay for music streamers: album art for the current song, plus a song
queue overlay, fed live from your StreamerSonglist queue — automatically.

This tool is for music streamers who use [StreamerSonglist](https://www.streamersonglist.com).
One app — the **dashboard** — does two jobs:

- **Between streams** it pulls your songlist from StreamerSonglist, finds album
  artwork for every song, and lets you confirm, reject, or upload your own image
  for each one. The images you approve become your **artwork library**.
- **During your stream** its built-in live service follows your StreamerSonglist
  queue live, takes whatever is in your now-playing slot as the current song, writes
  an artwork image file for OBS, and serves the queue and now-playing overlays. (It can also read
  a text file instead, if you drive your current song some other way.) It uses
  your approved library first; if a song isn't in the library yet, it grabs a
  best guess live and flags it so you can review it after the stream.

Artwork comes from the **iTunes Search API** first, then optionally **Last.fm**
(if you add your own key) and **MusicBrainz / Cover Art Archive**.

[![Watch the install & setup walkthrough on YouTube](docs/screenshots/install-video-thumbnail.png)](https://www.youtube.com/watch?v=1lnzu5O0h8Q)

![The Songs page: your songlist with confirmed artwork](docs/screenshots/songlist-artwork.png)

---

## 1. What you need

- **Windows** (built and tested there; the file-saving is tuned for OBS on
  Windows). **macOS/Linux should work but are untested** — it's plain Python.
  The `.bat` launcher is Windows-only, so start it from a terminal instead:
  `python3 -m app.dashboard`. If you try it, let me know how it goes via an
  issue!
- **Python 3.10 or newer**. Get it from [python.org](https://www.python.org/downloads/).
  During install, tick **"Add Python to PATH"**.
- A **StreamerSonglist** account with some songs in your list. During a stream,
  keep the song you're playing in your SSL **now-playing** slot (or at the top of
  the queue — SSL promotes it for you). (Alternatively, point it at a text file
  your own setup writes.)
- A **StreamerSonglist API token**. StreamerSonglist's API now requires one for
  every request, so the app can't read your songlist or queue without it. It's
  free and takes a moment to create — see the next step.

## 2. Install

Download/clone this repository, open a terminal in the folder, and run:

```
pip install -r requirements.txt
```

## 3. Get your StreamerSonglist API token

StreamerSonglist's API requires a token for every request, so you need one before
the app can see your songlist.

StreamerSonglist has **two kinds** of token, and the app needs to know which one
you have — they look identical, so it can't tell on its own. Either works:

| Token | Where you create it | Covers |
|---|---|---|
| **User** | Your **profile → API Access** | Every channel your account owns or administrates |
| **Streamer** | Your channel's **Settings → Access** | That one channel |

1. Sign in at [streamersonglist.com](https://www.streamersonglist.com).
2. Create **either** token above — a **user access token** from your profile →
   **API Access** is the usual choice.
3. Copy it — you'll paste it into the app's Settings page in the next step, and
   set **Token type** to match where you created it.

If you're not sure which kind you have, paste it and click **Test connection** —
the app tries the other type for you and sets the field to whichever works.

Keep it private. It's stored only in your local `config.json` (which is never
committed to git) and sent only to StreamerSonglist. Treat it like a password:
it can read *and* write every channel your account administrates, so don't paste
it into screenshots, streams, or bug reports. If it ever leaks, create a new one
from the same page.

## 4. First run — the dashboard

Double-click **`run_dashboard.bat`** (or run `python -m app.dashboard`).

- On the very first run it creates a `config.json` for you and opens the
  **Settings** page in your browser at `http://127.0.0.1:5050`.
- Fill in:
  - **Username or songlist URL** — e.g. `yourname` or
    `https://www.streamersonglist.com/t/yourname/songs`.
  - **API token** — paste the token from step 3, and set **Token type** to
    match where you made it (User or Streamer). Then click **Test connection**:
    it should show your channel name, avatar and song count, and if you picked
    the wrong type it corrects the field for you. (Once saved, the field goes
    blank — leaving it blank keeps the saved token, and there's a tickbox to
    remove it.)
  - **Platform** — `twitch` unless your SSL channel is on YouTube or Kick.
  - **Current song source** — leave on **StreamerSonglist queue** (recommended);
    the live service reads the top of your live queue. Only switch to **Text
    file** if another tool writes your current song to a file, and set its path
    below.
  - **Output image** — where the artwork PNG should be written (point OBS at this).
  - **Fallback image** — shown for your own originals and for songs with no art.
  - **Skip artists** — your own artist name(s), comma separated. Songs by these
    artists use the fallback image instead of searching.
  - **Instant updates** — on by default. The app subscribes to StreamerSonglist's
    live event stream so the artwork and overlays change the moment your queue
    does, instead of waiting for the next check. If the connection isn't
    available it quietly goes back to checking on a timer, so there's nothing to
    do if it drops.
  - (Optional) **iTunes country**, **Last.fm API key**, reflection/size settings.
- Click **Save settings**.

## 5. Build your artwork library

On the **Songs** page:

1. **Sync songlist** — pulls all your songs from StreamerSonglist.
2. **Find artwork** — searches iTunes for every song without art. This is
   throttled (about 3 seconds per song, to respect iTunes' rate limit), so a large
   list takes a while. You can watch the progress bar, and it's **resumable** — if
   you stop and start again it picks up where it left off.
   Songs you add to StreamerSonglist later don't need this button: a **Sync
   songlist** searches the new songs on its own, so they turn up on the Review
   page with artwork ready to approve.
3. Go to the **Review** page to approve artwork one song at a time:
   - **Confirm** (`Y`) — keep this image.
   - **Reject → next** (`N`) — try the next candidate (more iTunes results, then
     Last.fm, then MusicBrainz — fetched on demand).
   - **Upload** (`U`) — drag & drop or pick your own image (JPG/PNG/WebP).
   - **Skip** (`→`) — leave it for later.

The keyboard shortcuts make reviewing a long list fast.

![The Review page: proposed artwork with alternative candidates](docs/screenshots/review-artwork.png)

## 6. During your stream

Keep the **dashboard** (`run_dashboard.bat`) running while you're live. It runs
a built-in live service that watches your StreamerSonglist queue and, whenever
the now-playing song changes, writes the artwork image for OBS — normally
within about half a second. Play through your queue as usual and the artwork
follows automatically —
StreamerSonglist promotes the top of your queue into the now-playing slot, and
if that slot is empty the app falls back to the top of the queue. When your
queue is empty entirely, the fallback image is shown.
The header shows a status indicator (current song and queue length) so you can
sanity-check it before going live; a ⚡ next to it means instant updates are
connected. (With the **Text file** source the live
service reads your song file instead of the queue — everything else works the
same.)

### The queue overlay

The dashboard also serves an **up-next table** for OBS: your queue with
artwork, title, artist and requester. Open the **Overlay** page in the
dashboard to style it (presets, fields, sizes, how many songs) with a live
preview, then copy the URL into an OBS **Browser** source. The background is
transparent; style changes apply within seconds without touching OBS.

![The Overlay settings page with live queue preview](docs/screenshots/queue-overlay.png)

Lookup order for each song:

1. Your **confirmed** library image (also uses proposed/live images if that's all
   there is).
2. A **live search** if the song isn't in your library — the result is shown on
   stream and added to the **Review queue** in the dashboard, flagged as
   *unverified* so you can check it later.
3. The **fallback image** if nothing is found.

After a stream, open the dashboard's **Queue** page to review anything that was
grabbed live and confirm or replace it.

### When the queue is empty

Rather than sitting empty between requests, either overlay can show a card
inviting viewers to request something — your own message, with your channel
avatar (or any image you pick) in place of the album art. It is off by default:
tick **Show the empty-queue card** on the Overlay page for whichever overlays
you want it on, and write the message under **Empty-queue card**.

The card appears once the queue has been empty for a few seconds — so it does
not flash up between songs — and is replaced by the real queue the moment a
request lands.

It also knows whether you are **taking requests**. When your songlist is closed
it switches to a second message you set separately (*"Requests open at 8pm"*),
which covers the pre-stream window too — or shows nothing at all, if you leave
that message blank. Use the **Preview the empty-queue card** dropdown under
either preview to see both states without touching your live queue or your
request settings.

## 7. Your artwork library

Images live in the `library/` folder, named `Artist - Title.png`, at full
resolution. Resizing and the reflection effect happen when the image is written for
OBS, so changing the image size or reflection settings never means re-fetching.

If a proposed image is wrong, you can just **delete the file** in `library/` — the
tool notices it's gone and treats that song as needing art again.

## 8. Staying up to date

When the dashboard starts it asks GitHub whether there is a newer release. If
there is, a banner appears at the top of every dashboard page with the version,
what the release is called, and a link to the full notes. It never appears on
your overlays — those are separate pages, so nothing can show up in OBS or on
stream.

**Dismiss** hides it for that version only. The next release brings it back.

To update, download the new version (or `git pull` if you cloned), then restart
the dashboard. Check the release notes for whether you also need to re-run
`pip install -r requirements.txt` or refresh your OBS browser sources.

The check is a single anonymous request to `api.github.com`. It sends nothing
about you, your songlist or your library — only the app's name and version. Turn
it off under **Settings → Updates** if you'd rather check
[the releases page](https://github.com/deadhead1971/streamersonglist-album-art-overlay/releases)
yourself, and use **Check now** on that same page any time.

## Files in this repo

| File | What it does |
|------|--------------|
| `run_dashboard.bat` | Start the dashboard (also run this during streams). |
| `config.example.json` | Template copied to `config.json` on first run. |
| `app/` | The application code. |
| `tools/probe_api.py` | Diagnostic: checks which StreamerSonglist API is answering and whether your token works. |
| `tools/release.py` | For maintainers: bumps the version, commits and tags a release. |

`config.json` (which holds your API token), your `library/`, and logs are **not**
committed to git — they're yours and local.

## Troubleshooting

- **`python` not found** — reinstall Python with "Add Python to PATH" ticked, or
  reopen your terminal.
- **Test connection fails** — most often a missing or mistyped **API token**
  (step 3); the app will say so if that's the cause. Otherwise check the
  username/URL — you can also paste your full songlist URL.
- **"StreamerSonglist rejected this token"** — the token didn't paste in full,
  or the **Token type** doesn't match where you created it. Click **Test
  connection**: it tries the other types and sets the right one for you.
- **"This token is not authorised for that channel"** — a **Streamer** token
  only works for the channel it was created in, so this usually means a typo in
  the username. Check it, or use a **User** token instead.
- **"StreamerSonglist has upgraded its API and now requires a token"** — exactly
  what it says: create a token (step 3) and paste it into Settings.
- **Which API am I talking to?** — run `python -m tools.probe_api` for a quick
  read-out of the API host, the detected version, your channel and queue. It
  never prints your token.
- **No artwork for a song** — use **Reject → next** to try other sources, or
  **Upload** your own image.
- **Last.fm is skipped** — that's expected unless you add your own free API key in
  Settings.
- **No ⚡ in the header** — instant updates aren't connected, and the app is
  checking your queue on a timer instead. Everything still works, just a little
  less immediately. It reconnects on its own.
- **"Couldn't reach github.com" when checking for updates** — you're offline, a
  firewall is in the way, or you've checked many times in one hour and hit
  GitHub's anonymous rate limit. It has no effect on anything else the app does;
  try again later, or look at
  [the releases page](https://github.com/deadhead1971/streamersonglist-album-art-overlay/releases).
- **`pip install` broke another Python project** — this app's realtime library
  pulls in `protobuf`, which some other tools (TensorFlow, Google APIs) pin to
  an older version. If that affects you, run `pip install "protobuf<6"`
  afterwards, or install this app in its own virtual environment.
- **Logs** — see `artwork_fetcher.log` in the project folder.
