"""
Shared artwork orchestration used by BOTH the watcher and the dashboard: turning
a chosen candidate into a stored library image, and rendering a library image to
the OBS output file. Keeping this in one place means live grabs and dashboard
confirmations produce byte-identical library entries.
"""

import logging

from . import imaging, sources
from .library import (
    STATUS_CONFIRMED, STATUS_PROPOSED, STATUS_REJECTED_ALL,
)

log = logging.getLogger("artwork_fetcher")

# Order the dashboard reject-cycle pulls fresh candidates from.
SOURCE_ORDER = ("itunes", "lastfm", "musicbrainz")


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


def propose_entry(lib, cfg: dict, entry: dict) -> bool:
    """
    Bulk-propose step for one song: search iTunes only (fast, throttled by the
    caller), store the first downloadable result as ``proposed`` and keep the
    rest of the candidates for the reject-cycle. Returns True if art was stored.
    Sets ``proposed`` either way so the song surfaces in the review flow.
    """
    title, artist = entry["title"], entry["artist"]
    cands = sources.search_itunes(artist, title, cfg.get("itunes_country", "GB"))
    entry["candidates"] = cands
    entry["searched_sources"] = ["itunes"]
    entry["candidate_index"] = -1

    for idx, cand in enumerate(cands):
        raw = try_download(cand)
        if raw is not None:
            store_candidate(lib, title, artist, cand, STATUS_PROPOSED, raw_bytes=raw)
            entry["candidate_index"] = idx
            lib.save()
            return True

    # iTunes found nothing downloadable — still mark proposed (no image) so the
    # review card shows it and the user can pull deeper sources on demand.
    entry["file"] = None
    lib.set_status(entry, STATUS_PROPOSED)
    lib.save()
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


def confirm_entry(lib, entry: dict) -> None:
    lib.set_status(entry, STATUS_CONFIRMED)
    lib.save()


def store_upload(lib, entry: dict, raw_bytes: bytes) -> None:
    """Store a user-uploaded image (any format → PNG) as a confirmed manual entry."""
    png = imaging.to_png_bytes(raw_bytes)
    fname = lib.save_image_bytes(entry["title"], entry["artist"], png)
    entry["file"] = fname
    entry["source"] = "manual"
    lib.set_status(entry, STATUS_CONFIRMED)
    lib.save()
