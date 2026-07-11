"""
StreamerSonglist API client (public reads, no auth needed).

Verified live 2026-07-11:
- Resolve username -> id:
  GET /v1/streamers/{username}?platform=twitch&isUsername=true  -> {id, name, ...}
- Fetch songs:
  GET /v1/streamers/{id}/songs?size=100&current=0
  -> {total, items:[{id,title,artist,active,timesPlayed,...}]}
  `title` and `artist` are clean separate fields.
- `current` is a PAGE INDEX, not an offset (verified: size=5&current=1 returns
  items 5-9 with no overlap with page 0). Paginate current = 0, 1, 2, ...
"""

import math
import re

import requests

from .config import USER_AGENT

API_BASE = "https://api.streamersonglist.com/v1"
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# Matches .../t/{username}/... in a pasted StreamerSonglist URL.
_URL_USERNAME_RE = re.compile(r"streamersonglist\.com/t/([^/\s?#]+)", re.IGNORECASE)


def extract_username(text: str) -> str:
    """
    Accept either a bare username or a pasted URL like
    https://www.streamersonglist.com/t/alanthompson_music/songs and return the
    username.
    """
    text = (text or "").strip()
    match = _URL_USERNAME_RE.search(text)
    if match:
        return match.group(1)
    # Not a SSL URL — treat as a bare username, but strip any stray URL bits.
    return text.strip().strip("/")


def resolve_streamer(username: str) -> dict:
    """
    Resolve a username to the streamer record ({id, name, ...}).
    Raises requests.HTTPError on failure.
    """
    username = extract_username(username)
    resp = requests.get(
        f"{API_BASE}/streamers/{username}",
        params={"platform": "twitch", "isUsername": "true"},
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_songs_page(streamer_id, size: int = 100, current: int = 0) -> dict:
    """Fetch one page. Returns the raw {total, items:[...]} response."""
    resp = requests.get(
        f"{API_BASE}/streamers/{streamer_id}/songs",
        params={"size": size, "current": current},
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_all_songs(streamer_id, size: int = 100) -> list:
    """
    Fetch every song across all pages. Returns a list of song dicts
    ({id, title, artist, active, timesPlayed, ...}).
    """
    first = fetch_songs_page(streamer_id, size=size, current=0)
    total = int(first.get("total", 0))
    items = list(first.get("items", []))

    if total <= len(items):
        return items

    pages = math.ceil(total / size)
    for page in range(1, pages):
        data = fetch_songs_page(streamer_id, size=size, current=page)
        page_items = data.get("items", [])
        if not page_items:
            break
        items.extend(page_items)

    return items


# ---------------------------------------------------------------------------
# Live queue (the runtime signal — top of the queue is the current song)
# ---------------------------------------------------------------------------
#
# Verified live 2026-07-11:
#   GET /v1/streamers/{id}/queue
#   -> {"list":[{position, song:{title, artist, ...}, nonlistSong, ...}], "status":{...}}
# The list is ordered top-first; list[0] is position 1 (the current/next song).
# An empty list means nothing is queued.

def fetch_queue(streamer_id) -> list:
    """Fetch the live request queue (top-first). Returns the raw list of items."""
    resp = requests.get(
        f"{API_BASE}/streamers/{streamer_id}/queue",
        headers=_HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("list", []) if isinstance(data, dict) else []


def _queue_item_song(item: dict):
    """
    Pull (title, artist) out of one queue item. Prefers the linked library
    ``song``; falls back to a ``nonlistSong`` (a viewer's off-list request).
    Returns (title, artist) or None if nothing usable is present.
    """
    if not isinstance(item, dict):
        return None

    song = item.get("song")
    if isinstance(song, dict):
        title = (song.get("title") or "").strip()
        artist = (song.get("artist") or "").strip()
        if title:
            return title, artist

    # nonlistSong: shape isn't guaranteed by the API — handle the plausible
    # cases (a bare "Title - Artist" string or a {title, artist} dict) and skip
    # anything else rather than guessing.
    nonlist = item.get("nonlistSong")
    if isinstance(nonlist, dict):
        title = (nonlist.get("title") or "").strip()
        artist = (nonlist.get("artist") or "").strip()
        if title:
            return title, artist
    elif isinstance(nonlist, str) and nonlist.strip():
        text = nonlist.strip()
        for sep in (" - ", " – ", " — "):
            if sep in text:
                title, artist = text.split(sep, maxsplit=1)
                return title.strip(), artist.strip()
        return text, ""

    return None


def fetch_current_song(streamer_id):
    """
    Return (title, artist) for the song at the top of the queue (position 1),
    or None if the queue is empty / has no usable song. Raises
    requests.RequestException on a network/HTTP failure so callers can tell a
    transient error apart from a genuinely empty queue.
    """
    queue = fetch_queue(streamer_id)
    if not queue:
        return None
    return _queue_item_song(queue[0])
