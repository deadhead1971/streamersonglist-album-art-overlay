# StreamerSonglist Album Art Overlay

An OBS overlay for music streamers: album art for the current song, plus a song
queue overlay, fed live from your StreamerSonglist queue — automatically.

This tool is for music streamers who use [StreamerSonglist](https://www.streamersonglist.com).

**[⬇ Download for Windows](https://github.com/deadhead1971/streamersonglist-album-art-overlay/releases/latest)**
— get `AlbumArtOverlay-Setup-….exe` from the latest release and run it. No
Python needed. (You can also [run it from source](#run-from-source-python).)

One app — with its **dashboard** in your browser — does two jobs:

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

▶️ **[Watch the install & setup walkthrough on YouTube](https://www.youtube.com/watch?v=1lnzu5O0h8Q)** — just under 7 minutes.

![The Songs page: your songlist with confirmed artwork](docs/screenshots/songlist-artwork.png)

---

## 1. What you need

- **Windows 10 or 11.** The download is a Windows app. (Running from source,
  macOS and Linux should work but are untested — see below.)
- A **StreamerSonglist** account with some songs in your list. During a stream,
  keep the song you're playing in your SSL **now-playing** slot (or at the top of
  the queue — SSL promotes it for you). (Alternatively, point it at a text file
  your own setup writes.)
- A **StreamerSonglist API token**. StreamerSonglist's API now requires one for
  every request, so the app can't read your songlist or queue without it. It's
  free and takes a moment to create — see the next step.

## 2. Install

### Download for Windows (recommended)

1. Open the [latest release](https://github.com/deadhead1971/streamersonglist-album-art-overlay/releases/latest)
   and download **`AlbumArtOverlay-Setup-x.y.z.exe`**.
2. Your browser may say the file **isn't commonly downloaded**. Choose **Keep**
   (in Edge it's under the **…** menu next to the download).
3. Run it. Windows may show **"Windows protected your PC"**. Click **More
   info**, then **Run anyway**.

   Why the warnings? The app isn't code-signed yet, so Windows doesn't know
   the publisher, and every new version starts out unknown to it. The source
   code is all here, and each release lists a **SHA256** checksum for the
   installer if you want to check your download.
4. The installer doesn't ask for admin rights. Click **Install**, leave
   **Launch Album Art Overlay** ticked, then **Finish**.

You'll find **Album Art Overlay** in your Start menu and on your desktop.

### Run from source (Python)

For macOS/Linux, or if you'd rather run the code directly.

1. Install **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/).
   During install, tick **"Add Python to PATH"**.
2. Download or clone this repository, open a terminal in the folder, and run:

   ```
   pip install -r requirements.txt
   ```
3. Start it with **`run_dashboard.bat`** (Windows) or `python -m app.dashboard`
   (`python3` on macOS/Linux). Keep that window open while you use the app.

Everything else in this guide is the same either way. Where it differs, it
says so.

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

Keep it private. It's stored only in your local `config.json` (see
[Where your files live](#where-your-files-live)) and sent only to StreamerSonglist. Treat it like a password:
it can read *and* write every channel your account administrates, so don't paste
it into screenshots, streams, or bug reports. If it ever leaks, create a new one
from the same page.

## 4. First run — the dashboard

Start **Album Art Overlay** from the Start menu or your desktop (from source:
`run_dashboard.bat`).

- On the very first run it creates your settings and opens the **Settings**
  page in your browser at `http://127.0.0.1:5050`. The downloaded app also puts
  an icon in the **system tray** (bottom right, by the clock) and says so once.
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
  - **Output image** — where the artwork PNG should be written. Point an OBS
    **Image** source at it; **Copy path** puts it on your clipboard. The
    downloaded app fills this in for you, inside its data folder, and you can
    leave it there.
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

Keep the app running while you're live. The downloaded app **lives in the
system tray**: closing the browser tab doesn't stop it, and your overlays keep
working. Click the tray icon to open the dashboard again, or start the app from
the Start menu again, which just reopens the dashboard. To stop it, right-click
the tray icon and choose **Quit** (or use **Quit app** at the bottom of
Settings). From source, keep the `run_dashboard.bat` window open instead.

It runs
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

### The art wall

Once you have a library, the dashboard can show it off. The **art wall** is a
grid of your album covers that fills whatever OBS source you give it — a
full-screen background, a starting-soon or intermission scene, or a panel
beside your other overlays. It reads nothing from your queue, so it works
offline and before you go live.

Set it up on the **Overlay** page under *Art wall*. You choose the number of
**columns**; the wall works out how many rows fit the source you gave it, so
the same setting suits a wide scene and a tall one. Gap and corner radius are
yours to set: leave both at 0 for a gapless mosaic that fills the screen like
wallpaper, or raise them for a gallery of separate covers.

The wall does not sit still. Every few seconds one cover cross-fades to another
from your library, each tile drifts very slowly on its own cycle, and covers
fade in one at a time in random places when the scene starts. All of it is
adjustable, and setting the swap interval to 0 holds it still.

**Arrangement.** Shuffled is the default, but the wall can also lay your covers
out in order:

- **Dark to light** — a brightness gradient across the grid. Works on any
  library, and it is the one that looks most deliberate.
- **By colour** — warm reds and oranges through to cool blues, with
  black-and-white sleeves grouped in their own band. How well this reads
  depends on your library: record sleeves cluster at the warm end, so expect a
  warm-to-cool sweep rather than a full rainbow.
- **By artist** — an artist's covers sit together.

Choose whether the order **runs across in rows** or **down in columns** —
horizontal or vertical bands. A sorted wall still opens on a different set of
records every time the scene starts, and swaps only ever exchange a cover for
another from the same part of the order, so a gradient keeps its shape however
long you leave it running.

A few things worth knowing:

- **Confirmed, proposed and live-grabbed artwork all count**, so the wall looks
  good before you have reviewed anything. If you have no artwork at all yet,
  the browser source simply renders nothing — run *Sync songlist* and *Find
  artwork* on the Songs page and it fills itself in.
- **Each cover appears once.** Several songs usually share an album, and the
  wall recognises the same cover even when it was downloaded twice.
- **Click any cover in the preview to hide it** from the wall. Hidden covers
  stay in your library; you can bring them all back from the same panel.
- **Tick "Shutdown source when not visible"** on this browser source in OBS. It
  stops the animation using CPU behind a scene you are not showing — and it
  means the wall reshuffles and replays its reveal each time you cut back to
  that scene, so it looks different every time.

## 7. Your artwork library

Images live in the `library` folder inside your data folder (see
[Where your files live](#where-your-files-live)), named `Artist - Title.png`, at full
resolution. Resizing and the reflection effect happen when the image is written for
OBS, so changing the image size or reflection settings never means re-fetching.

If a proposed image is wrong, you can just **delete the file** in `library/` — the
tool notices it's gone and treats that song as needing art again.

Images you **confirmed or uploaded are never overwritten**. If you later pick a
different image for one of those songs, the new one is saved beside it as
`Artist - Title (2).png` and the old file stays where it is.

Your review decisions live in `library/manifest.json`. Each time it is saved,
the previous version is kept as `manifest.json.bak`. If `manifest.json` is ever
damaged or goes missing, the app puts the backup back on its own and shows a
banner saying so. If there is no usable backup either, it won't touch your
library at all rather than start it over, and the banner explains what's wrong.

**Sharper covers for older libraries.** Earlier versions saved iTunes covers at
600×600, a little smaller than the image written for OBS. New ones are saved at
1000×1000. To re-fetch the covers you already have at that size, without
re-reviewing anything, close the dashboard and run this from the source folder
(the tools come with the Python version, not the download):

```
python -m tools.upgrade_art           # shows what it would do, changes nothing
python -m tools.upgrade_art --apply   # does it
```

It only replaces a cover when the larger download is the same picture. Your
originals are copied to `library/pre-upgrade/` first, so you can put them back;
delete that folder once you're happy. Expect roughly 1 MB more disk space per
cover.

## 8. Staying up to date

When the app starts it asks GitHub whether there is a newer release. If
there is, a banner appears at the top of every dashboard page with the version,
what the release is called, and a link to the full notes. It never appears on
your overlays — those are separate pages, so nothing can show up in OBS or on
stream.

**Dismiss** hides it for that version only. The next release brings it back.

**Downloaded app:** click **Download update** in the banner and run the
installer. It closes the running app for you, keeps your library and settings,
and starts the new version when you click Finish. Your OBS sources keep
working, because nothing they point at moves.

**From source:** download the new version (or `git pull` if you cloned), then
restart the dashboard. Check the release notes for whether you also need to
re-run `pip install -r requirements.txt` or refresh your OBS browser sources.

The check is a single anonymous request to `api.github.com`. It sends nothing
about you, your songlist or your library — only the app's name and version. Turn
it off under **Settings → Updates** if you'd rather check
[the releases page](https://github.com/deadhead1971/streamersonglist-album-art-overlay/releases)
yourself, and use **Check now** on that same page any time.

## Where your files live

| | Downloaded app | From source |
|---|---|---|
| Your data | `%LOCALAPPDATA%\AlbumArtOverlay` (paste that into Explorer's address bar) | the project folder |
| The app itself | `%LOCALAPPDATA%\Programs\Album Art Overlay` | the project folder |

Your data is:
- `config.json`: your settings, including your API token
- `library\`: your artwork and your review decisions
- `artwork_fetcher.log`: the log
- `obs\`: the OBS loaders (downloaded app only; from source they're in the
  project folder's `obs\`)

**Settings → Your files** shows these paths with **Open** and **Copy**
buttons, and the tray menu has **Open data folder** and **Open log folder**.

**Backing up:** quit the app, then copy the whole data folder somewhere safe.
Your artwork choices are the part worth keeping. They take time to make, and
the app can't recreate them.

## Moving from the Python version

If you've been running from source and want the downloaded app instead:

1. Quit the Python version (close its window).
2. If you set it up before version 1.7, optionally run
   `python -m tools.upgrade_art --apply` first for sharper covers (section 7).
   The downloaded app doesn't include the tools.
3. Install the app, let it start, then **Quit** it from the tray icon.
4. Copy `config.json` and the `library` folder from your project folder into
   `%LOCALAPPDATA%\AlbumArtOverlay`, replacing what's there.
5. Start the app. Everything is where you left it.

Your OBS **Image** source keeps working, because the output image path in your
settings doesn't change. Your OBS browser sources keep working too, as long as
you keep the old folder: its loaders only point at the app's address. To tidy
up, re-point them at the loaders shown on the **Overlay** page (**Copy path**),
and then you can delete the old folder.

## Uninstalling

**Windows Settings → Apps**, find **Album Art Overlay**, and choose **Uninstall**.
This removes the program but **keeps your data folder**, so a reinstall picks
up where you left off. To remove everything, delete
`%LOCALAPPDATA%\AlbumArtOverlay` afterwards too.

## Files in this repo

| File | What it does |
|------|--------------|
| `run_dashboard.bat` | Start the dashboard from source (also run this during streams). |
| `config.example.json` | Template copied to `config.json` on first run. |
| `app/` | The application code. |
| `tools/probe_api.py` | Diagnostic: checks that StreamerSonglist is answering and whether your token works. |
| `tools/upgrade_art.py` | Re-fetches the iTunes covers already in your library at the current, larger size (see section 7). |
| `tools/release.py` | For maintainers: bumps the version, commits and tags a release. |
| `packaging/` | For maintainers: builds the Windows app and its installer. |

`config.json` (which holds your API token), your `library/`, and logs are **not**
committed to git — they're yours and local.

## Troubleshooting

- **I can't find the tray icon** — Windows 11 tucks new icons away under the
  **^** arrow by the clock. Drag the icon out onto the taskbar to keep it in
  view. You don't need it day to day: starting the app from the Start menu
  opens the dashboard, and Settings has a **Quit app** button.
- **My antivirus deleted or blocked the app** — some antivirus products are
  wary of new, unsigned programs. Restore it from the antivirus quarantine and
  mark it as allowed, or reinstall. If it keeps happening, please open an issue
  and say which antivirus you use.
- **"Another program is already using port 5050"** — the app talks to your
  browser and OBS on port 5050, and something else has it. Close that program
  (a second copy of this app from source counts), then start the app again.
- **The artwork image never updates, and the log says "Permission denied" or
  "Access is denied"** — Windows **Controlled folder access** (part of
  ransomware protection) blocks unknown apps from writing to Documents, Pictures
  and the Desktop. Point **Output image** at a folder outside those, such as
  the default in the app's data folder, or allow the app under Windows Security
  → Virus & threat protection → Ransomware protection.
- **`python` not found** (from source) — reinstall Python with "Add Python to
  PATH" ticked, or reopen your terminal.
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
- **"StreamerSonglist has no Twitch channel called …"** — the username doesn't
  match a StreamerSonglist channel on that platform. Check the spelling and the
  **Platform** setting, or paste your songlist's URL from your browser instead.
- **"Your artwork library can't be read"** — `library/manifest.json` is damaged
  and there's no usable backup, so the app is leaving your library alone. If you
  edited that file by hand, the banner says which line to fix; otherwise put
  back a copy from your own backups. The dashboard notices the fix without a
  restart.
- **Is StreamerSonglist answering?** — from source, run
  `python -m tools.probe_api` for a quick read-out of the API host, your channel
  and your queue. It never prints your token.
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
- **Logs** — `artwork_fetcher.log` in your data folder. The tray menu's **Open
  log folder** takes you there. Attach it to a bug report; it never contains
  your token.
