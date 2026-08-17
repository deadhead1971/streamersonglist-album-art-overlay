"""
StreamerSonglist API client — speaks both the v1 and v2 APIs.

StreamerSonglist rewrote its API; the new one replaces production at an
unannounced date. Rather than a hard cutover (which would break the app the
moment SSL flips), this module detects which API is live once per session and
keeps one stable set of public functions on top of both:

    extract_username, resolve_streamer, fetch_song_count, fetch_all_songs,
    fetch_queue, fetch_current_song, queue_item_view

Backend differences, verified live against staging 2026-08-05:

  v1  https://api.streamersonglist.com/v1   public reads, no auth
      GET /streamers/{username}?platform=twitch&isUsername=true
      GET /streamers/{id}/songs?size&current      (current = PAGE INDEX)
      GET /streamers/{id}/queue -> {"list": [...]}   list[0] IS the current song

  v2  https://api.streamersonglist.com       EVERY read needs Authorization
      GET /streamers?streamer_name=&platform=
      GET /songs?streamer_id=&limit=&after=       (cursor; next cursor = "token")
      GET /queue?streamer_id=  -> {"playing", "items", "total"}
                                  `playing` is the now-playing song and
                                  `items` is UPCOMING ONLY (1-based);
                                  `total` excludes `playing`.

The v1/v2 queue split is the dangerous difference: under v2, queue[0] is the
NEXT song, not the current one. ``fetch_queue`` normalises both backends to
``(playing, upcoming)`` so callers never have to care — under v1 the list is
split at index 0, which reproduces v1 behaviour exactly.
"""

import logging
import math
import re
import threading

import requests

from .config import USER_AGENT, load_config

log = logging.getLogger("artwork_fetcher")

# v2 production host. v1 lives at {DEFAULT_HOST}/v1; v2 sits at the root.
DEFAULT_HOST = "https://api.streamersonglist.com"

_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# Shown verbatim in the dashboard when a v2 read is rejected — the whole point
# of the dual-backend build is that cutover day produces this instead of a dead
# overlay and a silent log line.
AUTH_MESSAGE = ("StreamerSonglist has upgraded its API and now requires a "
                "token. Add your API token in Settings → StreamerSonglist "
                "(create one at your SSL profile → API Access).")

# Matches .../t/{username}/... in a pasted StreamerSonglist URL. The new site
# uses the same /t/{username}/songs shape, so this is unchanged.
_URL_USERNAME_RE = re.compile(r"streamersonglist\.com/t/([^/\s?#]+)", re.IGNORECASE)


class AuthRequired(requests.RequestException):
    """
    A read was rejected for missing/invalid credentials (HTTP 401).

    Subclasses RequestException on purpose: every caller already treats a
    RequestException as "fetch failed, keep the last known state", so an
    expired token can never be mistaken for an empty queue and silently swap
    in the fallback image.
    """


class RateLimited(requests.RequestException):
    """HTTP 429. v2 documents it but publishes no numbers; callers back off."""


def extract_username(text: str) -> str:
    """
    Accept either a bare username or a pasted URL like
    https://www.streamersonglist.com/t/alanthompson_music/songs and return the
    username.

    Still lowercased: v1 400s on mixed case (v2 resolves either), and this app
    has to keep working on v1 until SSL flips.
    """
    text = (text or "").strip()
    match = _URL_USERNAME_RE.search(text)
    if match:
        return match.group(1)
    # Not a SSL URL — treat as a bare username, but strip any stray URL bits.
    return text.strip().strip("/")


# ---------------------------------------------------------------------------
# Config-derived request plumbing
# ---------------------------------------------------------------------------

def _cfg(cfg: dict = None) -> dict:
    return cfg if cfg is not None else load_config()


def _host(cfg: dict) -> str:
    """API host. Blank config = production; set it to test against staging."""
    return (cfg.get("api_base") or "").strip().rstrip("/") or DEFAULT_HOST


def _platform(cfg: dict) -> str:
    return (cfg.get("platform") or "").strip() or "twitch"


def _headers(cfg: dict) -> dict:
    """
    Request headers, with the v2 credential attached when one is configured.
    Harmless on v1, which ignores it.
    """
    headers = dict(_HEADERS)
    token = (cfg.get("api_token") or "").strip()
    if token:
        token_type = (cfg.get("api_token_type") or "").strip() or "User"
        headers["Authorization"] = f"{token_type} {token}"
    return headers


def _get(url: str, params: dict, cfg: dict, timeout: int = 15) -> dict:
    """GET + JSON, mapping 401/429 onto the typed exceptions above."""
    resp = requests.get(url, params=params, headers=_headers(cfg), timeout=timeout)
    if resp.status_code == 401:
        raise AuthRequired(AUTH_MESSAGE, response=resp)
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "?")
        raise RateLimited(
            f"StreamerSonglist rate limit hit (retry after {retry}s)",
            response=resp,
        )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Backend detection — once per session, re-probed when settings change
# ---------------------------------------------------------------------------
#
# Probe with the endpoint we actually need, and branch on the response:
#   404 "Cannot GET /streamers"  -> still v1
#   401                          -> v2 live, token missing/bad (actionable)
#   200 / 400 / 422              -> v2 live
# A 5xx is transient, so it is raised rather than cached as a verdict.

_DETECT_LOCK = threading.Lock()
_DETECTED = {"key": None, "backend": None}


def _cache_key(cfg: dict) -> tuple:
    return (_host(cfg), (cfg.get("api_token") or "").strip(),
            (cfg.get("api_token_type") or "").strip(), _platform(cfg))


def reset_backend() -> None:
    """Forget the detected backend (called after a settings save)."""
    with _DETECT_LOCK:
        _DETECTED["key"] = None
        _DETECTED["backend"] = None


def detect_backend(cfg: dict = None, username: str = "") -> str:
    """
    Return "v1" or "v2", probing once and caching the verdict for the session.
    Raises AuthRequired if v2 is live but the token is missing or bad, and
    requests.RequestException if the probe itself fails.
    """
    cfg = _cfg(cfg)
    key = _cache_key(cfg)
    with _DETECT_LOCK:
        if _DETECTED["key"] == key and _DETECTED["backend"]:
            return _DETECTED["backend"]

    host = _host(cfg)
    name = extract_username(username or cfg.get("streamersonglist_username") or "")
    resp = requests.get(
        f"{host}/streamers",
        params={"streamer_name": name, "platform": _platform(cfg)},
        headers=_headers(cfg),
        timeout=15,
    )

    if resp.status_code >= 500:
        # Transient — don't freeze a verdict on a server hiccup.
        resp.raise_for_status()

    backend = "v1" if resp.status_code == 404 else "v2"
    with _DETECT_LOCK:
        _DETECTED["key"] = key
        _DETECTED["backend"] = backend
    log.info("StreamerSonglist API detected: %s (%s)", backend, host)

    if resp.status_code == 401:
        raise AuthRequired(AUTH_MESSAGE, response=resp)
    return backend


def backend_state() -> dict:
    """Current detection state, for diagnostics/the probe tool."""
    with _DETECT_LOCK:
        return {"backend": _DETECTED["backend"], "detected": bool(_DETECTED["key"])}


# ---------------------------------------------------------------------------
# Streamer resolution
# ---------------------------------------------------------------------------

def resolve_streamer(username: str, cfg: dict = None) -> dict:
    """
    Resolve a username to a normalised streamer record:
        {"id", "name", "avatar", "platform", "backend", "raw"}

    The id is resolved at runtime and never persisted — production and staging
    hand out different ids for the same channel, and a stale id doesn't error,
    it silently syncs someone else's catalogue.

    Raises requests.RequestException (incl. AuthRequired) on failure.
    """
    cfg = _cfg(cfg)
    username = extract_username(username)
    backend = detect_backend(cfg, username)
    host = _host(cfg)
    platform = _platform(cfg)

    if backend == "v1":
        data = _get(f"{host}/v1/streamers/{username}",
                    {"platform": platform, "isUsername": "true"}, cfg)
        return {
            "id": data.get("id"),
            "name": data.get("name") or username,
            "avatar": None,
            "platform": platform,
            "backend": "v1",
            "raw": data,
        }

    data = _get(f"{host}/streamers",
                {"streamer_name": username, "platform": platform}, cfg)
    # v2 dropped the top-level `name` (it comes back None). The display name and
    # avatar live under platforms.<platform>, and the username there is properly
    # cased — nicer than anything v1 gave us.
    platforms = data.get("platforms")
    entry = platforms.get(platform) if isinstance(platforms, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    return {
        "id": data.get("id"),
        "name": (entry.get("username") or data.get("name") or username),
        "avatar": entry.get("profileImageUrl") or None,
        "platform": platform,
        "backend": "v2",
        "raw": data,
    }


# ---------------------------------------------------------------------------
# Songs
# ---------------------------------------------------------------------------
#
# v1's /streamers/{id}/songs was already active-only (185 songs), and v2's
# /songs matches it exactly. /songs/all returns 193 — it includes deactivated
# songs, which would newly pull 8 dead entries into the library and start
# proposing artwork for them. That's a behaviour change, not a fix: stay on
# /songs.

def _songs_page_v1(cfg: dict, streamer_id, size: int = 100, current: int = 0) -> dict:
    """One v1 page. `current` is a page index, not an offset."""
    return _get(f"{_host(cfg)}/v1/streamers/{streamer_id}/songs",
                {"size": size, "current": current}, cfg)


def _songs_page_v2(cfg: dict, streamer_id, limit: int = 100, after=None) -> dict:
    params = {"streamer_id": streamer_id, "limit": min(int(limit), 100)}
    if after:
        params["after"] = after
    return _get(f"{_host(cfg)}/songs", params, cfg)


def fetch_song_count(streamer_id, cfg: dict = None) -> int:
    """Total songs in the songlist (active only, on both backends)."""
    cfg = _cfg(cfg)
    if detect_backend(cfg) == "v1":
        return int(_songs_page_v1(cfg, streamer_id, size=1, current=0).get("total", 0))
    data = _songs_page_v2(cfg, streamer_id, limit=1)
    total = data.get("total")
    if total is None:
        # Older/leaner responses may omit it — fall back to a real count rather
        # than reporting 0 songs at someone who has plenty.
        return len(fetch_all_songs(streamer_id, cfg=cfg))
    return int(total)


def fetch_all_songs(streamer_id, size: int = 100, cfg: dict = None) -> list:
    """
    Fetch every song. Returns a list of song dicts
    ({id, title, artist, active, ...}) — `title` and `artist` are clean
    separate fields on both backends.
    """
    cfg = _cfg(cfg)

    if detect_backend(cfg) == "v1":
        first = _songs_page_v1(cfg, streamer_id, size=size, current=0)
        total = int(first.get("total", 0))
        items = list(first.get("items", []))
        if total <= len(items):
            return items
        for page in range(1, math.ceil(total / size)):
            page_items = _songs_page_v1(cfg, streamer_id, size=size,
                                        current=page).get("items", [])
            if not page_items:
                break
            items.extend(page_items)
        return items

    # v2: cursor pagination. Stop when the cursor is absent, the page is empty,
    # or the cursor stops advancing (a stuck token would otherwise spin here).
    items = []
    after = None
    while True:
        data = _songs_page_v2(cfg, streamer_id, limit=size, after=after)
        page_items = data.get("items") or []
        if not page_items:
            break
        items.extend(page_items)
        token = data.get("token")
        if not token or token == after:
            break
        after = token
    return items


# ---------------------------------------------------------------------------
# Live queue
# ---------------------------------------------------------------------------

def fetch_queue(streamer_id, cfg: dict = None):
    """
    Fetch the live queue as ``(playing, upcoming)``:

      playing  — the now-playing item, or None if the slot is empty
      upcoming — the queue behind it, top-first

    Both backends are normalised to this shape. v2 hands them over already
    split; v1 returns one list whose head is the current song, so it is split
    at index 0 — which reproduces today's v1 behaviour exactly.

    Raises requests.RequestException so callers can tell a transient failure
    apart from a genuinely empty queue.
    """
    cfg = _cfg(cfg)

    if detect_backend(cfg) == "v1":
        data = _get(f"{_host(cfg)}/v1/streamers/{streamer_id}/queue", None, cfg)
        items = data.get("list", []) if isinstance(data, dict) else []
        if not items:
            return None, []
        return items[0], items[1:]

    data = _get(f"{_host(cfg)}/queue", {"streamer_id": streamer_id}, cfg)
    if not isinstance(data, dict):
        return None, []
    playing = data.get("playing")
    if not isinstance(playing, dict):
        playing = None
    items = data.get("items")
    return playing, list(items) if isinstance(items, list) else []


def _queue_item_song(item: dict):
    """
    Pull (title, artist) out of one queue item. Prefers the linked library
    ``song``; falls back to a ``nonlistSong`` (a viewer's off-list request).
    Returns (title, artist) or None if nothing usable is present.

    v2 types nonlistSong as `string | null`; v1 never formally documented it,
    so both the string and dict shapes stay handled.
    """
    if not isinstance(item, dict):
        return None

    song = item.get("song")
    if isinstance(song, dict):
        title = (song.get("title") or "").strip()
        artist = (song.get("artist") or "").strip()
        if title:
            return title, artist

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


def queue_item_view(item: dict, position: int) -> dict:
    """
    Flatten one queue item for the overlay: {id, position, title, artist,
    requester}. Returns None if the item has no usable song.

    ``position`` is the caller's display position and is authoritative — the
    API's own numbering differs between backends (v2 puts the now-playing song
    at 0 and starts the upcoming queue at 1), so trusting it would make the
    overlay renumber itself on cutover day.

    ``requests`` is a list of request rows — multiple requesters are joined.

    Reading the requester needs both fields. v1 filled ``name`` on every row.
    v2 leaves it "" for platform requests (``source: "twitch"``) and puts the
    identity in ``user.username`` — verified live 2026-08-17, which is why the
    overlays showed no "requested by" line after cutover. ``name`` still comes
    first: manual adds carry free text there that ``user`` cannot reproduce
    ("<viewer> (needs dusting)"), and website/v1-era rows keep rendering
    exactly as they did.
    """
    parsed = _queue_item_song(item)
    if parsed is None:
        return None
    title, artist = parsed

    names = []
    reqs = item.get("requests")
    if isinstance(reqs, list):
        for r in reqs:
            if isinstance(r, dict):
                name = (r.get("name") or "").strip()
                if not name:
                    user = r.get("user")
                    if isinstance(user, dict):
                        name = (user.get("username") or "").strip()
                if name:
                    names.append(name)
            elif isinstance(r, str) and r.strip():
                names.append(r.strip())

    return {
        # Queue item id — stable identity for overlay row animations (survives
        # position changes; distinguishes the same song queued twice).
        "id": item.get("id"),
        "position": position,
        "title": title,
        "artist": artist,
        "requester": ", ".join(names),
    }


def fetch_current_song(streamer_id, cfg: dict = None):
    """
    Return (title, artist) for the song that is playing right now — the
    now-playing slot, falling back to the top of the upcoming queue when that
    slot is empty. None if there's nothing to show.

    Raises requests.RequestException on a network/HTTP failure so callers can
    tell a transient error apart from a genuinely empty queue.
    """
    playing, upcoming = fetch_queue(streamer_id, cfg=cfg)
    return current_from_queue(playing, upcoming)


def current_from_queue(playing, upcoming):
    """
    The now-playing item given a normalised queue, or None.

    `playing` is optional in v2's response, so an empty slot falls back to the
    head of the upcoming queue — which is also exactly what v1 meant.
    """
    if isinstance(playing, dict):
        parsed = _queue_item_song(playing)
        if parsed is not None:
            return parsed
    if upcoming:
        return _queue_item_song(upcoming[0])
    return None
