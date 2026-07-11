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
