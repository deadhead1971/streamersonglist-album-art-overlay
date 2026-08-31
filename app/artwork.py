"""
Shared artwork orchestration used by BOTH the runtime service and the dashboard
request handlers: resolving the current song to an output PNG, turning a chosen
candidate into a stored library image, and rendering a library image to the OBS
output file. Keeping this in one place means live grabs and dashboard
confirmations produce byte-identical library entries.
"""

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PIL import Image

from . import config, imaging, sources
from .library import (
    STATUS_CONFIRMED, STATUS_PROPOSED, STATUS_REJECTED_ALL, STATUS_UNVERIFIED,
)

log = logging.getLogger("artwork_fetcher")

# Order the dashboard reject-cycle pulls fresh candidates from.
SOURCE_ORDER = ("itunes", "lastfm", "musicbrainz")

# Library statuses whose stored image the runtime is willing to display, best
# first. rejected_all is excluded (user rejected everything → go live/fallback).
USABLE_STATUSES = (STATUS_CONFIRMED, STATUS_PROPOSED, STATUS_UNVERIFIED)


# Endpoints something polls on a timer: both overlays (every 5s each, per OBS
# browser source *and* per preview iframe), the dashboard's status pill (every
# 5s per open tab), and one /img request per thumbnail on the songs and review
# pages. Werkzeug logs a line per request, and measured on 2026-08-31 these
# were 79% of a 10MB log — ~2,200 lines an hour saying nothing happened, which
# pushes real history out of the rotation window.
_POLLED_PATHS = ("/overlay/queue.json", "/overlay/current.json",
                 "/api/runtime/status", "/img?")

# The status code in a werkzeug access line: '"GET /x HTTP/1.1" 200 -'.
_ACCESS_STATUS_RE = re.compile(r'"\s+(\d{3})\s')


class _DropPollingRequests(logging.Filter):
    """
    Drop the *successful* access-log lines for those endpoints.

    Only the successful ones: a 404 or a 500 on a polling endpoint is the
    interesting case (a browser source pointed at the wrong URL, a handler
    blowing up mid-stream), and dropping those would trade one blind spot for
    a worse one. Everything else — page loads, POSTs, errors, and every line
    the app itself logs — is untouched.
    """

    def filter(self, record) -> bool:
        if record.levelno > logging.INFO:
            return True
        message = record.getMessage()
        if not any(path in message for path in _POLLED_PATHS):
            return True
        match = _ACCESS_STATUS_RE.search(message)
        return bool(match) and int(match.group(1)) >= 400


def setup_logging():
    if log.handlers:
        return
    # A Windows console is cp1252, and these messages are full of → and —.
    # Without this, logging raises UnicodeEncodeError inside emit() and prints
    # a 30-line traceback in place of the line — so the one message that tells
    # a user their token is wrong is the one the console can't show them.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            # Rotated, not open-ended: a fault that logs on every overlay poll
            # (a rejected token used to) writes thousands of identical lines an
            # hour, and this file had reached 10 MB before anyone looked at it.
            # Four files, so a session's history survives without unbounded
            # growth.
            RotatingFileHandler(config.LOG_FILE, maxBytes=2_000_000,
                                backupCount=3, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    # On the logger, not the handlers: Logger.handle() applies its own filters
    # before propagating, so this keeps the polling lines out of the file and
    # the console with one attachment.
    logging.getLogger("werkzeug").addFilter(_DropPollingRequests())


# ---------------------------------------------------------------------------
# Song file parsing (ported from v2, incl. en/em-dash variants)
# ---------------------------------------------------------------------------

def read_song_file(path: str, quiet: bool = False):
    """
    Read the song file and parse 'Song - Artist'. Returns (song, artist) or
    None. ``quiet`` suppresses problem logging — the runtime service reads the
    file every poll tick and logs misses on transition instead.
    """
    if not path:
        if not quiet:
            log.error("No song_file configured — set it in the dashboard settings")
        return None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        if not quiet:
            log.warning("Song file not found: %s", path)
        return None
    except Exception as e:
        if not quiet:
            log.error("Error reading song file: %s", e)
        return None

    if not text:
        if not quiet:
            log.info("Song file is empty")
        return None

    for sep in (" - ", " – ", " — "):  # hyphen, en-dash, em-dash
        if sep in text:
            song, artist = text.split(sep, maxsplit=1)
            return song.strip(), artist.strip()
    return text, ""


# ---------------------------------------------------------------------------
# Runtime resolve: current song → output PNG
# ---------------------------------------------------------------------------

def apply_fallback(cfg: dict):
    """Render the fallback image (or a blank placeholder) to the output path."""
    output = cfg.get("output_image")
    if not output:
        log.error("No output_image configured — cannot write fallback")
        return
    fallback = cfg.get("fallback_image")
    size = int(cfg.get("image_size", 640))

    if fallback and Path(fallback).exists():
        img = Image.open(fallback).convert("RGBA")
        if img.size != (size, size):
            img = img.resize((size, size), Image.Resampling.LANCZOS)
        log.info("Applied fallback image")
    else:
        img = Image.new("RGBA", (size, size), (30, 30, 30, 255))
        log.info("Applied blank placeholder (no fallback image found)")

    reflection = cfg.get("reflection", {})
    if reflection.get("enabled", True):
        img = imaging.add_reflection(img, reflection)
    imaging.atomic_save_image(img, output)


def resolve_artwork_for_song(cfg: dict, lib, song: str, artist: str) -> bool:
    """
    Given a resolved (song, artist) — from whatever source — run the lookup
    order and write the output image. Returns True if artwork was displayed.

    Lookup order:
      1. skip_artists → fallback image (the streamer's own originals).
      2. Library lookup (confirmed, or proposed/unverified as
         better-than-nothing): render library image → atomic save. Done.
      3. Live cascade iTunes → Last.fm → MusicBrainz: on success, save into the
         library as ``unverified`` (so it appears in the dashboard review
         queue), then output it.
      4. Nothing found → fallback image, and record a ``rejected_all`` stub so
         the dashboard shows the miss.
    """
    log.info("Current song: '%s' by '%s'", song, artist)

    # 1. Own songs → fallback, no search.
    skip = [a.strip().lower() for a in cfg.get("skip_artists", [])]
    if artist.strip().lower() in skip:
        log.info("Own song (artist '%s') — applying fallback", artist)
        apply_fallback(cfg)
        return False

    # 2. Library lookup.
    entry = lib.get(song, artist)
    if entry and entry.get("status") in USABLE_STATUSES and lib.has_image(entry):
        log.info("Library hit (%s) — rendering stored art", entry.get("status"))
        if render_entry_to_output(lib, cfg, entry):
            return True

    # 3. Live cascade.
    log.info("No library art — running live cascade")
    for candidate in sources.search_cascade(artist, song, cfg):
        raw = try_download(candidate)
        if raw is None:
            continue
        try:
            entry = store_candidate(
                lib, song, artist, candidate, STATUS_UNVERIFIED, raw_bytes=raw
            )
        except ValueError:
            continue
        log.info("Live grab from %s ('%s') — flagged unverified for review",
                 candidate.get("source"), candidate.get("album"))
        render_entry_to_output(lib, cfg, entry)
        return True

    # 4. Nothing found → fallback + record the miss.
    log.warning("No artwork found for '%s' - '%s'", song, artist)
    miss = lib.ensure_entry(song, artist)
    if not lib.has_image(miss):
        miss["file"] = None
        lib.set_status(miss, STATUS_REJECTED_ALL)
        lib.save()
    apply_fallback(cfg)
    return False


def try_download(candidate: dict):
    """Download a candidate's image, returning raw bytes or None on failure."""
    url = candidate.get("url")
    if not url:
        return None
    try:
        return imaging.download_image_bytes(url)
    except Exception as e:  # network / 404 (CAA misses) / decode
        log.info("Candidate download failed (%s): %s", url, e)
        return None


def store_candidate(lib, title: str, artist: str, candidate: dict,
                    status: str, raw_bytes: bytes = None) -> dict:
    """
    Store a candidate's image into the library at source resolution and update
    its manifest entry (file, source, album, status, candidates_tried). Saves the
    manifest. Returns the entry. ``raw_bytes`` may be passed to avoid a second
    download when the caller already fetched it.
    """
    if raw_bytes is None:
        raw_bytes = try_download(candidate)
    if raw_bytes is None:
        raise ValueError("could not download candidate image")

    png = imaging.to_png_bytes(raw_bytes)
    entry = lib.ensure_entry(title, artist)
    fname = lib.save_image_bytes(title, artist, png)

    entry["file"] = fname
    entry["source"] = candidate.get("source")
    entry["album"] = candidate.get("album")
    lib.set_status(entry, status)

    url = candidate.get("url")
    if url and url not in entry.setdefault("candidates_tried", []):
        entry["candidates_tried"].append(url)

    lib.save()
    return entry


def render_entry_to_output(lib, cfg: dict, entry: dict) -> bool:
    """Render an entry's library image to the configured OBS output path."""
    path = lib.image_path(entry)
    if not path or not path.exists():
        return False
    output = cfg.get("output_image")
    if not output:
        log.error("No output_image configured")
        return False
    img = imaging.render_output_from_path(str(path), cfg)
    imaging.atomic_save_image(img, output)
    log.info("Output written from library: %s", path.name)
    return True


# ---------------------------------------------------------------------------
# Dashboard candidate management (bulk propose + reject-cycle + upload)
# ---------------------------------------------------------------------------

def _search_source(cfg: dict, artist: str, title: str, source: str) -> list:
    if source == "itunes":
        return sources.search_itunes(artist, title, cfg.get("itunes_country", "GB"))
    if source == "lastfm":
        return sources.search_lastfm(artist, title, cfg.get("lastfm_api_key", ""))
    if source == "musicbrainz":
        return sources.search_musicbrainz(artist, title)
    return []


def _add_next_source(entry: dict, cfg: dict) -> bool:
    """
    Search the next not-yet-tried source and append its candidates (deduped by
    URL) to the entry's pool. Returns True if a source was tried (even if it
    yielded nothing), False when every source is exhausted.
    """
    searched = entry.setdefault("searched_sources", [])
    for src in SOURCE_ORDER:
        if src in searched:
            continue
        searched.append(src)
        found = _search_source(cfg, entry["artist"], entry["title"], src)
        existing = {c.get("url") for c in entry.get("candidates", [])}
        entry.setdefault("candidates", []).extend(
            c for c in found if c.get("url") not in existing
        )
        return True
    return False


def _adopt_first_candidate(lib, entry: dict) -> bool:
    """
    Store the first downloadable candidate in the pool as the entry's
    ``proposed`` image. Returns False when none of them can be downloaded.
    """
    title, artist = entry["title"], entry["artist"]
    for idx, cand in enumerate(entry.get("candidates", [])):
        raw = try_download(cand)
        if raw is not None:
            store_candidate(lib, title, artist, cand, STATUS_PROPOSED, raw_bytes=raw)
            entry["candidate_index"] = idx
            lib.save()
            return True
    return False


def _mark_proposed_without_image(lib, entry: dict) -> None:
    """
    Nothing downloadable — still mark proposed (no image) so the review card
    shows it and the user can pull deeper sources on demand.
    """
    entry["file"] = None
    lib.set_status(entry, STATUS_PROPOSED)
    lib.save()


def propose_entry(lib, cfg: dict, entry: dict) -> bool:
    """
    Bulk-propose step for one song: search iTunes only (fast, throttled by the
    caller), store the first downloadable result as ``proposed`` and keep the
    rest of the candidates for the reject-cycle. Returns True if art was stored.
    Sets ``proposed`` either way so the song surfaces in the review flow.
    """
    entry["candidates"] = sources.search_itunes(
        entry["artist"], entry["title"], cfg.get("itunes_country", "GB")
    )
    entry["searched_sources"] = ["itunes"]
    entry["candidate_index"] = -1

    if _adopt_first_candidate(lib, entry):
        return True
    _mark_proposed_without_image(lib, entry)
    return False


def ensure_proposal(lib, cfg: dict, entry: dict) -> bool:
    """
    Bring a song that has never been through the propose flow up to a reviewable
    state, and return True if it now has an image.

    A sync creates entries empty (``status`` None, no candidates) and only the
    propose flow fills them in, so a song opened straight from the songs list
    would otherwise show an empty review card. This is the one call that closes
    that gap: search if nothing has been searched yet, otherwise adopt the best
    candidate already in the pool. It is deliberately a no-op once the entry has
    a status or an image, so re-opening a card never re-searches and never
    overwrites a decision the user already made.
    """
    if entry.get("status") or lib.has_image(entry):
        return lib.has_image(entry)
    if not entry.get("candidates"):
        return propose_entry(lib, cfg, entry)
    if _adopt_first_candidate(lib, entry):
        return True
    _mark_proposed_without_image(lib, entry)
    return False


def advance_candidate(lib, cfg: dict, entry: dict):
    """
    Reject → next candidate: move to the next downloadable candidate, pulling
    fresh sources (Last.fm, then MusicBrainz) on demand when the pool runs out.
    Stores the new candidate as ``proposed`` and returns it, or returns None and
    marks ``rejected_all`` when every source is exhausted.
    """
    title, artist = entry["title"], entry["artist"]
    i = entry.get("candidate_index", -1) + 1

    while True:
        while i >= len(entry.get("candidates", [])):
            if not _add_next_source(entry, cfg):
                entry["candidate_index"] = i
                entry["file"] = None
                lib.set_status(entry, STATUS_REJECTED_ALL)
                lib.save()
                return None
        cand = entry["candidates"][i]
        raw = try_download(cand)
        if raw is not None:
            store_candidate(lib, title, artist, cand, STATUS_PROPOSED, raw_bytes=raw)
            entry["candidate_index"] = i
            lib.save()
            return cand
        i += 1  # undownloadable (e.g. CAA 404) — skip to the next


def select_candidate(lib, entry: dict, index: int) -> dict:
    """
    Pick a specific already-discovered candidate by index: download it and store
    it as the entry's (proposed) image. Lets the user jump to any option in the
    gallery, forward or back, instead of only cycling. Raises IndexError for a
    bad index or ValueError if the image can't be downloaded.
    """
    candidates = entry.get("candidates", [])
    if not (0 <= index < len(candidates)):
        raise IndexError("candidate index out of range")
    cand = candidates[index]
    raw = try_download(cand)
    if raw is None:
        raise ValueError("could not download candidate image")
    store_candidate(lib, entry["title"], entry["artist"], cand,
                    STATUS_PROPOSED, raw_bytes=raw)
    entry["candidate_index"] = index
    lib.save()
    return cand


def find_more_candidates(lib, cfg: dict, entry: dict) -> int:
    """
    Pull the next not-yet-tried source(s) into the entry's candidate pool until
    at least one new candidate is found or every source is exhausted. Returns the
    number of candidates added (0 = nothing more found).
    """
    before = len(entry.get("candidates", []))
    while len(entry.get("candidates", [])) == before:
        if not _add_next_source(entry, cfg):
            break
    added = len(entry.get("candidates", [])) - before
    # Nothing has ever been picked for this song (a never-proposed entry, or an
    # iTunes miss): adopt the best option now, so the card shows art instead of
    # only a gallery the user has to click to see anything at all. A candidate
    # index of 0 or more means the reject-cycle has been here — leave it alone.
    if added and entry.get("candidate_index", -1) < 0 and not lib.has_image(entry):
        _adopt_first_candidate(lib, entry)
    lib.save()
    return added


def confirm_entry(lib, entry: dict) -> None:
    lib.set_status(entry, STATUS_CONFIRMED)
    lib.save()


def store_upload(lib, entry: dict, raw_bytes: bytes) -> None:
    """
    Store a user-uploaded image (any format → PNG) as the entry's manual artwork.

    An upload picks the image; it does not accept it. Confirming stays the
    user's explicit act (the review card repaints with the upload and waits on
    Confirm), because marking it confirmed here dropped the song straight out of
    the review queue — the user never saw what they had just uploaded, and the
    song then read as "done" everywhere while nobody had looked at it.

    An unverified live grab keeps that status so an upload never shuffles a song
    between the two review lists; everything else lands on proposed.
    """
    png = imaging.to_png_bytes(raw_bytes)
    fname = lib.save_image_bytes(entry["title"], entry["artist"], png)
    entry["file"] = fname
    entry["source"] = "manual"
    entry["candidate_index"] = -1  # a manual upload isn't one of the candidates
    status = (STATUS_UNVERIFIED if entry.get("status") == STATUS_UNVERIFIED
              else STATUS_PROPOSED)
    lib.set_status(entry, status)
    lib.save()
