"""
Artwork search cascade + result validation.

Sources, in the order the cascade tries them:
  1. iTunes Search API  (primary; no key, no auth)
  2. Last.fm            (only if the user supplies their own API key in config)
  3. MusicBrainz + Cover Art Archive (no key; ranked at release-GROUP level)

Every returned result is validated: fuzzy artist-similarity check (drop clear
mismatches) and a compilation blocklist (deprioritise, don't hard-reject).

Each source returns a list of "candidate" dicts:
    {
      "url":     "<image url>",
      "artist":  "<returned artist name>",
      "title":   "<returned track name>",
      "album":   "<collection/album name>",
      "source":  "itunes" | "lastfm" | "musicbrainz",
      "similarity": <float 0..1 vs requested artist>,
      "blocklisted": <bool>,
    }
"""

import logging
import time
from difflib import SequenceMatcher

import requests

from .config import USER_AGENT
from .library import clean_title_for_search, normalize_text

log = logging.getLogger("artwork_fetcher")

# Artist similarity threshold — below this we treat the result as a wrong artist.
SIMILARITY_THRESHOLD = 0.6

# Album/collection names containing these are deprioritised (not rejected).
BLOCKLIST_TERMS = [
    "greatest hits", "best of", "collection", "anthology", "essential",
    "live", "deluxe", "remaster", "compilation", "karaoke", "tribute",
    "cover", "covers",
]

# Last.fm ships a grey placeholder star for tracks with no real art. Its URL
# always contains this hash — reject it outright.
LASTFM_PLACEHOLDER_HASH = "2a96cbd8b46e442fc41c2b86b821562f"

# iTunes artwork comes back as 100x100; string-replace to a larger size.
ITUNES_ART_SIZE = "600x600bb.jpg"

MUSICBRAINZ_API = "https://musicbrainz.org/ws/2"
COVERART_API = "https://coverartarchive.org"
MB_REQUEST_DELAY = 1.1  # MusicBrainz asks for <= 1 req/sec
_last_mb_request = 0.0


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def artist_similarity(requested: str, found: str) -> float:
    """Fuzzy 0..1 similarity of two artist names (normalised)."""
    if not requested:
        return 1.0  # nothing to compare against — accept
    if not found:
        return 0.0
    a = normalize_text(requested)
    b = normalize_text(found)
    if not a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def is_blocklisted(album: str) -> bool:
    """True if the album/collection name looks like a compilation/live/etc."""
    if not album:
        return False
    low = album.lower()
    return any(term in low for term in BLOCKLIST_TERMS)


def rank_candidates(candidates: list, requested_artist: str) -> list:
    """
    Drop candidates whose artist clearly doesn't match, then sort clean-titled
    (non-blocklisted) results ahead of blocklisted ones, higher artist
    similarity first. Stable within equal keys.
    """
    kept = []
    for c in candidates:
        sim = artist_similarity(requested_artist, c.get("artist", ""))
        c["similarity"] = sim
        c["blocklisted"] = is_blocklisted(c.get("album", ""))
        if sim >= SIMILARITY_THRESHOLD:
            kept.append(c)

    kept.sort(key=lambda c: (c["blocklisted"], -c["similarity"]))
    return kept


# ---------------------------------------------------------------------------
# iTunes (primary)
# ---------------------------------------------------------------------------

def search_itunes(artist: str, title: str, country: str = "GB",
                  limit: int = 5) -> list:
    """Search the iTunes Search API and return ranked candidates."""
    term = f"{artist} {clean_title_for_search(title)}".strip()
    log.info("iTunes search: %r (country=%s)", term, country)
    try:
        resp = requests.get(
            "https://itunes.apple.com/search",
            params={"term": term, "entity": "song", "limit": limit,
                    "country": country},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        log.error("iTunes search error: %s", e)
        return []

    candidates = []
    for res in data.get("results", []):
        art = res.get("artworkUrl100") or ""
        if not art:
            continue
        # Upscale by string-replacing the size segment (verified working).
        url = art.replace("100x100bb.jpg", ITUNES_ART_SIZE)
        candidates.append({
            "url": url,
            "artist": res.get("artistName", ""),
            "title": res.get("trackName", ""),
            "album": res.get("collectionName", ""),
            "source": "itunes",
        })

    return rank_candidates(candidates, artist)


# ---------------------------------------------------------------------------
# Last.fm (only when the user supplies their own key)
# ---------------------------------------------------------------------------

LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"


def _lastfm_best_image(images: list) -> str:
    """Largest usable image URL from a Last.fm image list, rejecting placeholder."""
    size_map = {img.get("size", ""): (img.get("#text") or "").strip()
                for img in images}
    for size in ("mega", "extralarge", "large", "medium", "small"):
        url = size_map.get(size, "")
        if url and LASTFM_PLACEHOLDER_HASH not in url:
            return url
    return ""


def search_lastfm(artist: str, title: str, api_key: str) -> list:
    """
    Search Last.fm for album art. Skipped entirely (returns []) when no API key
    is configured — we do NOT ship a key.
    """
    if not api_key:
        return []

    candidates = []

    # Strategy 1: track.getInfo (exact match).
    if artist:
        try:
            resp = requests.get(LASTFM_API_URL, params={
                "method": "track.getInfo", "api_key": api_key,
                "artist": artist, "track": clean_title_for_search(title),
                "format": "json",
            }, timeout=10)
            resp.raise_for_status()
            album = resp.json().get("track", {}).get("album", {})
            url = _lastfm_best_image(album.get("image", [])) if album else ""
            if url:
                candidates.append({
                    "url": url, "artist": artist,
                    "title": title, "album": album.get("title", ""),
                    "source": "lastfm",
                })
        except (requests.RequestException, ValueError) as e:
            log.error("Last.fm track.getInfo error: %s", e)

    # Strategy 2: album.search (song title may be the album title).
    query = f"{clean_title_for_search(title)} {artist}".strip()
    try:
        resp = requests.get(LASTFM_API_URL, params={
            "method": "album.search", "api_key": api_key,
            "album": query, "limit": 3, "format": "json",
        }, timeout=10)
        resp.raise_for_status()
        albums = (resp.json().get("results", {})
                  .get("albummatches", {}).get("album", []))
        for album in albums:
            url = _lastfm_best_image(album.get("image", []))
            if url:
                candidates.append({
                    "url": url, "artist": album.get("artist", ""),
                    "title": title, "album": album.get("name", ""),
                    "source": "lastfm",
                })
    except (requests.RequestException, ValueError) as e:
        log.error("Last.fm album.search error: %s", e)

    return rank_candidates(candidates, artist)


# ---------------------------------------------------------------------------
# MusicBrainz + Cover Art Archive (ranked at release-group level)
# ---------------------------------------------------------------------------

def _mb_rate_limit():
    global _last_mb_request
    elapsed = time.time() - _last_mb_request
    if elapsed < MB_REQUEST_DELAY:
        time.sleep(MB_REQUEST_DELAY - elapsed)
    _last_mb_request = time.time()


def _mb_get(endpoint: str, params: dict):
    _mb_rate_limit()
    try:
        resp = requests.get(
            f"{MUSICBRAINZ_API}/{endpoint}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            params=params, timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        log.error("MusicBrainz API error: %s", e)
        return None


def _parse_date(date_str: str) -> str:
    """Normalise an MB date to a sortable YYYY-MM-DD; unknown sorts last."""
    if not date_str:
        return "9999-99-99"
    parts = date_str.split("-")
    year = parts[0] if len(parts) > 0 and parts[0] else "9999"
    month = parts[1] if len(parts) > 1 and parts[1] else "01"
    day = parts[2] if len(parts) > 2 and parts[2] else "01"
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def _lucene_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _artist_credit_name(entity: dict) -> str:
    credits = entity.get("artist-credit") or []
    if credits:
        first = credits[0]
        if isinstance(first, dict):
            return first.get("name") or (first.get("artist") or {}).get("name", "")
    return ""


def _score_release_group(g: dict) -> tuple:
    """Ranking key for a release-group (lower is better)."""
    type_scores = {"album": 0, "ep": 1, "single": 2}
    type_score = type_scores.get(g["primary"], 3)

    penalty = 0
    unwanted = {"compilation", "live", "remix", "dj-mix", "mixtape/street",
                "demo", "soundtrack", "interview", "spokenword"}
    for st in g["secondary"]:
        if st in unwanted:
            penalty += 10

    return (type_score + penalty, g["date"])


def search_musicbrainz(artist: str, title: str, max_groups: int = 6) -> list:
    """
    Search MusicBrainz for recordings of this song, collapse to release-groups,
    rank them (official album > earliest date, avoid compilation/live), and
    return Cover Art Archive front-image candidates for the top groups.

    We rank at release-GROUP level and hit
    ``/release-group/{mbid}/front`` so the archive serves art from whichever
    release in the group has a scan (improvement over v2's per-release probing).
    The URL is returned as-is; the caller validates by attempting the download
    (CAA 404s when a group has no art).
    """
    clean_title = clean_title_for_search(title)
    if artist:
        query = (f'recording:"{_lucene_escape(clean_title)}" '
                 f'AND artist:"{_lucene_escape(artist)}"')
    else:
        query = f'recording:"{_lucene_escape(clean_title)}"'

    log.info("MusicBrainz recording search: %s", query)
    data = _mb_get("recording", {"query": query, "limit": 25, "fmt": "json"})
    if not data:
        return []

    groups: dict[str, dict] = {}
    for rec in data.get("recordings", []):
        rec_artist = _artist_credit_name(rec)
        for rel in rec.get("releases", []):
            rg = rel.get("release-group") or {}
            rgid = rg.get("id")
            if not rgid:
                continue
            date = _parse_date(rel.get("date") or rg.get("first-release-date", ""))
            existing = groups.get(rgid)
            if existing is None:
                groups[rgid] = {
                    "id": rgid,
                    "title": rg.get("title", ""),
                    "primary": (rg.get("primary-type") or "").lower(),
                    "secondary": [s.lower()
                                  for s in (rg.get("secondary-types") or [])],
                    "date": date,
                    "artist": rec_artist or artist,
                }
            elif date < existing["date"]:
                existing["date"] = date

    if not groups:
        log.info("MusicBrainz: no release-groups found")
        return []

    ranked = sorted(groups.values(), key=_score_release_group)[:max_groups]
    candidates = []
    for g in ranked:
        candidates.append({
            "url": f"{COVERART_API}/release-group/{g['id']}/front-500",
            "artist": g["artist"],
            "title": title,
            "album": g["title"],
            "source": "musicbrainz",
        })

    return rank_candidates(candidates, artist)


# ---------------------------------------------------------------------------
# Combined cascade
# ---------------------------------------------------------------------------

def search_cascade(artist: str, title: str, cfg: dict,
                   include_itunes: bool = True):
    """
    Lazily yield candidates across all configured sources in order:
    iTunes -> Last.fm (if key) -> MusicBrainz. This is a generator on purpose:
    the watcher consumes it until the first candidate downloads, so when iTunes
    wins we never pay for the ~1s/req MusicBrainz call. The dashboard reject-cycle
    uses it the same way when iTunes candidates run out.
    """
    if include_itunes:
        yield from search_itunes(artist, title, cfg.get("itunes_country", "GB"))
    yield from search_lastfm(artist, title, cfg.get("lastfm_api_key", ""))
    yield from search_musicbrainz(artist, title)
