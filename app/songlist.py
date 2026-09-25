"""
StreamerSonglist API client (the v2 API, live since 2026-08-12).

    GET /streamers?streamer_name=&platform=
    GET /songs?streamer_id=&limit=&after=       (cursor; next cursor = "token")
    GET /queue?streamer_id=  -> {"playing", "items", "total"}
                                `playing` is the now-playing song and
                                `items` is UPCOMING ONLY (1-based);
                                `total` excludes `playing`.

Every read needs an Authorization header (see TOKEN_TYPES). The queue split
is the one to remember: items[0] is the NEXT song, not the current one, so
``fetch_queue`` hands back ``(playing, upcoming)`` and callers never index the
raw list.

This module used to speak the old v1 API too, choosing between them by probing
/streamers once per session: a 404 meant v1. v1 has since been retired — its
URLs answer with v2's own 404 (checked 2026-09-25) — and that rule turned into
a trap, because v2 also answers 404 for a username it doesn't know. One typo in
Test connection was cached as "v1" for the whole process, so the corrected name
failed too, and so did the running live service, until a settings save. With
v1 gone there is nothing to detect, and a 404 means what it says.
"""

import logging
import re

import requests

from .config import USER_AGENT, load_config

log = logging.getLogger("artwork_fetcher")

# Production API host.
DEFAULT_HOST = "https://api.streamersonglist.com"

_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# Shown verbatim in the dashboard when a read is rejected with no credential at
# all, instead of a dead overlay and a silent log line.
AUTH_MESSAGE = ("StreamerSonglist has upgraded its API and now requires a "
                "token. Add your API token in Settings → StreamerSonglist — "
                "either a User token from your SSL profile → API Access, or a "
                "Streamer token from your channel's Settings → Access.")

# Authorization schemes v2 accepts, spelled exactly as they must appear in the
# header. Verified live 2026-08-31 against production:
#   User      profile → API Access; reaches every channel the account admins
#   Streamer  channel Settings → Access; one channel, full access to that one
#   Bearer    an OAuth2 access token
# The prefix is case-sensitive — a lowercase "user" is rejected with "invalid
# prefix" before the token is even looked at.
TOKEN_TYPES = ("User", "Streamer", "Bearer")
DEFAULT_TOKEN_TYPE = "User"

# SSL's error bodies (ErrorModel.json) name the real failure in `detail`. This
# module used to throw that away and answer every 401 with AUTH_MESSAGE, so a
# user who had *already* pasted a token was told to add a token — which is
# exactly what a wrong token type looks like. Details verified live 2026-08-31.
_WRONG_TYPE_HINT = (
    "Check it was pasted in full, and that the token type matches where you "
    "created it — User for your SSL profile → API Access, Streamer for your "
    "channel's Settings → Access."
)
_AUTH_DETAIL_MESSAGES = {
    "missing authorization header": AUTH_MESSAGE,
    "invalid prefix": (
        "StreamerSonglist did not recognise the token type. Choose User, "
        "Streamer or Bearer in Settings → StreamerSonglist."
    ),
    "invalid access token": "StreamerSonglist rejected this token. " + _WRONG_TYPE_HINT,
    "invalid token": "StreamerSonglist rejected this token. " + _WRONG_TYPE_HINT,
}

# A Streamer token is bound to the one channel that created it, so a typo in
# the username — or an SSL account name that differs from the platform one —
# comes back 403 rather than 401. Before this existed the user got requests'
# raw "403 Client Error: Forbidden for url: …" and no idea what to change.
FORBIDDEN_STREAMER_MESSAGE = (
    "This token is not authorised for that channel. A Streamer token only "
    "works for the one channel it was created in — check the username, or use "
    "a User token from your SSL profile → API Access."
)

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


class Forbidden(requests.RequestException):
    """
    HTTP 403 — the credential is valid but may not read that streamer.

    Subclasses RequestException for the same reason AuthRequired does: every
    caller already treats one as "fetch failed, keep the last known state", so
    a token pointed at the wrong channel can't read as an empty queue.
    """


class RateLimited(requests.RequestException):
    """HTTP 429. v2 documents it but publishes no numbers; callers back off."""


class StreamerNotFound(requests.RequestException):
    """
    HTTP 404 "streamer not found": no channel by that name on that platform.

    A settings mistake like AuthRequired, so callers put it in front of the
    user rather than retrying quietly. Before this existed it surfaced as
    requests' raw "404 Client Error: Not Found for url: …".
    """


_PLATFORM_NAMES = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick"}


def _not_found_message(username: str, platform: str) -> str:
    where = _PLATFORM_NAMES.get(platform)
    return (
        f"StreamerSonglist has no {where + ' ' if where else ''}channel called "
        f"'{username}'. Check the spelling"
        f"{', and that Platform is where you stream' if where else ''} — or "
        f"paste your songlist's URL instead."
    )


def normalize_token_type(value: str) -> str:
    """
    Canonicalise a configured token type to one the API will accept.

    Anything unrecognised becomes "User": passing an arbitrary string straight
    into the header earns an "invalid prefix" 401 that looks identical to a bad
    token, sending the user off hunting the wrong problem.
    """
    wanted = (value or "").strip().lower()
    for name in TOKEN_TYPES:
        if name.lower() == wanted:
            return name
    return DEFAULT_TOKEN_TYPE


def _error_detail(resp) -> str:
    """The reason string out of an SSL error body, or "" if there isn't one."""
    try:
        data = resp.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    return (data.get("detail") or "").strip()


def _auth_error(resp):
    """Map a 401/403 onto a typed exception carrying SSL's own reason."""
    detail = _error_detail(resp)
    if resp.status_code == 403:
        if "not authorized for the requested streamer" in detail.lower():
            return Forbidden(FORBIDDEN_STREAMER_MESSAGE, response=resp)
        return Forbidden(
            detail or "StreamerSonglist refused the request (403).", response=resp
        )
    message = _AUTH_DETAIL_MESSAGES.get(detail.lower())
    if not message:
        # An unrecognised reason still beats the generic text — pass it on
        # rather than telling a user with a token to go and add one.
        message = f"{AUTH_MESSAGE} (StreamerSonglist said: {detail})" if detail \
            else AUTH_MESSAGE
    return AuthRequired(message, response=resp)


def extract_username(text: str) -> str:
    """
    Accept either a bare username or a pasted URL like
    https://www.streamersonglist.com/t/alanthompson_music/songs and return the
    username, as typed — the API resolves any case.
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
    Request headers, with the credential attached when one is configured.
    """
    headers = dict(_HEADERS)
    token = (cfg.get("api_token") or "").strip()
    if token:
        token_type = normalize_token_type(cfg.get("api_token_type"))
        headers["Authorization"] = f"{token_type} {token}"
    return headers


def _get(url: str, params: dict, cfg: dict, timeout: int = 15) -> dict:
    """GET + JSON, mapping 401/403/429 onto the typed exceptions above."""
    resp = requests.get(url, params=params, headers=_headers(cfg), timeout=timeout)
    if resp.status_code in (401, 403):
        raise _auth_error(resp)
    if resp.status_code == 429:
        retry = resp.headers.get("Retry-After", "?")
        raise RateLimited(
            f"StreamerSonglist rate limit hit (retry after {retry}s)",
            response=resp,
        )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Streamer resolution
# ---------------------------------------------------------------------------

def resolve_streamer(username: str, cfg: dict = None) -> dict:
    """
    Resolve a username to a normalised streamer record:
        {"id", "name", "avatar", "platform", "raw"}

    The id is resolved at runtime and never persisted — production and staging
    hand out different ids for the same channel, and a stale id doesn't error,
    it silently syncs someone else's catalogue.

    Raises requests.RequestException on failure: AuthRequired / Forbidden for
    the credential, StreamerNotFound for a username SSL doesn't know.
    """
    cfg = _cfg(cfg)
    username = extract_username(username)
    host = _host(cfg)
    platform = _platform(cfg)

    try:
        data = _get(f"{host}/streamers",
                    {"streamer_name": username, "platform": platform}, cfg)
    except requests.HTTPError as e:
        # Only SSL's own "streamer not found" gets the friendly message: a 404
        # from anything else (a mistyped api_base, say) is not about the name.
        if (e.response is not None and e.response.status_code == 404
                and _error_detail(e.response).lower() == "streamer not found"):
            raise StreamerNotFound(_not_found_message(username, platform),
                                   response=e.response) from None
        raise
    # The top-level `name` comes back None. The display name and avatar live
    # under platforms.<platform>, where the username is properly cased.
    platforms = data.get("platforms")
    entry = platforms.get(platform) if isinstance(platforms, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    return {
        "id": data.get("id"),
        "name": (entry.get("username") or data.get("name") or username),
        "avatar": entry.get("profileImageUrl") or None,
        "platform": platform,
        "raw": data,
    }


def resolve_streamer_with_token_type(username: str, cfg: dict = None):
    """
    Resolve a streamer, retrying with the other token prefixes if the
    configured one is rejected. Returns ``(streamer, token_type)`` so the
    caller can offer the type that actually worked.

    This exists because the two token types are **indistinguishable by
    inspection** — a User token and a Streamer token are both 32-character
    opaque strings (checked against one of each, 2026-08-31) — so a user who
    picked the wrong one cannot tell from the token, and neither can the app.
    Without this, the only route out is guessing.

    For the settings "Test connection" button only, never the runtime: it costs
    an extra request per wrong guess, and on a live stream the configured type
    is either right or the user needs to be told, not silently worked around.

    Only AuthRequired moves on to the next type. A Forbidden means the
    credential was accepted and pointed at the wrong channel — trying other
    prefixes would replace that precise message with a vaguer one.
    """
    cfg = _cfg(cfg)
    configured = normalize_token_type(cfg.get("api_token_type"))
    if not (cfg.get("api_token") or "").strip():
        # No token to vary — one plain attempt, whatever it reports.
        return resolve_streamer(username, cfg=cfg), configured

    order = [configured] + [t for t in TOKEN_TYPES if t != configured]
    first_error = None
    for token_type in order:
        attempt = dict(cfg)
        attempt["api_token_type"] = token_type
        try:
            streamer = resolve_streamer(username, cfg=attempt)
        except AuthRequired as e:
            first_error = first_error or e
            continue
        if token_type != configured:
            log.info("Token accepted as %s, not the configured %s",
                     token_type, configured)
        return streamer, token_type
    raise first_error


def fetch_requests_active(username: str, cfg: dict = None):
    """
    Whether the streamer is currently accepting requests, or None when the
    API does not say so (the field going away).

    v2 carries this as ``requestsActive`` on the streamer record. Verified
    flipping on 2026-08-20: it read False with requests closed and True after
    opening them, with nothing else in the payload moving. The neighbouring
    ``canUserRequest`` / ``canAnonymousRequest`` / ``requestMode`` fields are
    standing permissions and are *not* the live switch.

    Raises requests.RequestException (incl. AuthRequired) on failure, so a
    caller can tell "closed" from "could not ask".
    """
    raw = resolve_streamer(username, cfg=cfg).get("raw") or {}
    value = raw.get("requestsActive")
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# Songs
# ---------------------------------------------------------------------------
#
# /songs is active songs only (185 here), matching what v1 always returned.
# /songs/all returns 193 — it includes deactivated songs, which would pull 8
# dead entries into the library and start proposing artwork for them. Stay on
# /songs.

def _songs_page(cfg: dict, streamer_id, limit: int = 100, after=None) -> dict:
    params = {"streamer_id": streamer_id, "limit": min(int(limit), 100)}
    if after:
        params["after"] = after
    return _get(f"{_host(cfg)}/songs", params, cfg)


def fetch_song_count(streamer_id, cfg: dict = None) -> int:
    """Total songs in the songlist (active only)."""
    cfg = _cfg(cfg)
    data = _songs_page(cfg, streamer_id, limit=1)
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
    separate fields.
    """
    cfg = _cfg(cfg)

    # Cursor pagination. Stop when the cursor is absent, the page is empty, or
    # the cursor stops advancing (a stuck token would otherwise spin here).
    items = []
    after = None
    while True:
        data = _songs_page(cfg, streamer_id, limit=size, after=after)
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

    Raises requests.RequestException so callers can tell a transient failure
    apart from a genuinely empty queue.
    """
    cfg = _cfg(cfg)
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

    The API types nonlistSong as `string | null`; the dict shape the old v1
    API was never pinned down on is still handled, defensively.
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
    API numbers the now-playing song 0 and restarts the upcoming queue at 1,
    which is not how the overlay counts.

    ``requests`` is a list of request rows — multiple requesters are joined.

    Reading the requester needs both fields. The API leaves ``name`` "" for
    platform requests (``source: "twitch"``) and puts the identity in
    ``user.username`` — verified live 2026-08-17, which is why the overlays
    showed no "requested by" line after the v2 cutover. ``name`` still comes
    first: manual adds carry free text there that ``user`` cannot reproduce
    ("<viewer> (needs dusting)").
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

    `playing` is optional in the response, so an empty slot falls back to the
    head of the upcoming queue.
    """
    if isinstance(playing, dict):
        parsed = _queue_item_song(playing)
        if parsed is not None:
            return parsed
    if upcoming:
        return _queue_item_song(upcoming[0])
    return None
