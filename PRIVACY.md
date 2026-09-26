# Privacy policy

Album Art Overlay runs entirely on your own computer. It has **no accounts, no
analytics and no telemetry**, and it sends **nothing to the author**. Everything
it keeps (your settings, your artwork library and its log) stays on your PC.

It does talk to a few online services, because that's how it gets your
songlist and finds artwork. This page lists every one, and exactly what each
is sent.

## What the app contacts, and why

| Service | When | What it's sent |
|---|---|---|
| **StreamerSonglist** (`api.streamersonglist.com`) | when you test your connection, sync, or stream; the live service checks your queue about every 10 seconds while it runs (less often while instant updates are connected) | your channel name and platform, and your **API token**, which proves you're allowed to read your songlist. Your token is sent **only** to StreamerSonglist |
| **StreamerSonglist live events** (`events.streamersonglist.com`) | while the live service runs, if **Instant updates** is on | a connection to your channel's public event feed, so queue changes arrive instantly. No token is sent. Turn it off in **Settings**, and the app checks on a timer instead |
| **Apple iTunes Search** (`itunes.apple.com`) | when finding artwork: on the Songs and Review pages, and during a stream for a song that isn't in your library yet | the song's **title and artist**, and your iTunes country setting |
| **MusicBrainz and Cover Art Archive** (`musicbrainz.org`, `coverartarchive.org`) | when you ask for more artwork choices on the Review page, and during a stream when iTunes (and Last.fm, if set up) find nothing for a song | the song's **title and artist** |
| **Last.fm** (`ws.audioscrobbler.com`) | only if you add your own Last.fm API key in Settings | the song's **title and artist**, and your Last.fm key |
| **Image downloads** | when a search finds artwork | a normal download of the cover image from the address the service gave (for example Apple's image servers) |
| **Your StreamerSonglist avatar** | only if the empty-queue card is on and uses your avatar | OBS or your browser loads the picture straight from the address StreamerSonglist gives for it, which is often your streaming platform's image server |
| **GitHub** (`api.github.com`) | once each time the app starts, to check for a newer version | nothing but the app's name and version. Turn it off in **Settings → Updates** |

Requests to StreamerSonglist, iTunes, MusicBrainz and GitHub, and artwork
downloads, identify themselves as *StreamerSonglist Album Art Overlay*, with
its version number and this project's web address; MusicBrainz asks apps to do
this. Last.fm lookups and the live events connection use the standard
identification of the software libraries they're made with. None of it
contains anything about you.

Each of these services has its own privacy policy, which covers what it does
with a request once it gets it:
[StreamerSonglist](https://www.streamersonglist.com),
[Apple](https://www.apple.com/legal/privacy/),
[MusicBrainz](https://metabrainz.org/privacy),
[Internet Archive (Cover Art Archive)](https://archive.org/about/terms.php),
[Last.fm](https://www.last.fm/legal/privacy) and
[GitHub](https://docs.github.com/site-policy/privacy-policies/github-general-privacy-statement).

## What stays on your computer

- **Settings**, including your StreamerSonglist API token, in `config.json`.
- **Your artwork library** and your review decisions.
- **A log file**, `artwork_fetcher.log`, which records what the app did so
  problems can be diagnosed. It never contains your API token. You choose
  whether to share it, for example with a bug report.

The Windows app keeps these in `%LOCALAPPDATA%\AlbumArtOverlay`; the Python
version keeps them in its own folder. Uninstalling leaves them there, so
delete that folder too if you want everything gone.

## The dashboard is private to your PC

The dashboard and overlays are served at `http://127.0.0.1:5050`, an address
that only your own computer can reach. Nothing on your network or the internet
can connect to it.

## Questions

Open an issue at
<https://github.com/deadhead1971/streamersonglist-album-art-overlay/issues>.
