"""
Artwork library: the source of truth.

- Images are stored in ``library/`` at their source resolution (up to ~1000px),
  named human-readably as ``{artist} - {title}.png`` so bad images are easy to
  find and delete/replace by hand.
- ``library/manifest.json`` maps a NORMALISED key -> entry. The normalisation
  function is the joint that makes dashboard writes and live-runtime lookups
  line up, so both sides must call ``normalize_key`` and nothing else.

The manifest tolerates a manually deleted image file: an entry whose ``file`` is
gone is treated as needing art again (``has_image`` returns False).
"""

import hashlib
import json
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

# Status values used in manifest entries.
STATUS_PROPOSED = "proposed"        # auto-fetched, awaiting review
STATUS_CONFIRMED = "confirmed"      # user approved
STATUS_UNVERIFIED = "unverified"    # grabbed live during a stream (review queue)
STATUS_REJECTED_ALL = "rejected_all"  # user/runtime exhausted candidates, no art

# Statuses whose stored image is willing to be displayed, best first.
# rejected_all is excluded — but note it can never carry an image anyway: both
# code paths that set it null the ``file`` field first, so has_image() is False
# by construction. ``artwork.USABLE_STATUSES`` aliases this; it lives here so
# library-side helpers (the art wall's tile pool) can use it without importing
# artwork and creating a cycle.
USABLE_STATUSES = (STATUS_CONFIRMED, STATUS_PROPOSED, STATUS_UNVERIFIED)

# Process-wide guard for manifest reads/writes: the dashboard's request threads
# and the background runtime service each hold their own Library instances, so
# serialise the file I/O to prevent torn writes.
MANIFEST_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# Content hashing — the art wall's tile identity
# ---------------------------------------------------------------------------

# 31% of a real library is duplicate artwork (measured 2026-09-01: 188 files,
# 129 distinct images, one EP cover repeated twelve times). A grid of covers
# has to dedupe or the same tile visibly appears more than once.
#
# The hash is the key, not the manifest's ``album`` field: album is null on 15
# of those entries and splits variants like "Gold" / "Gold (UK Version)", so it
# collapses more than the truth. Naming the thumbnail cache by this hash makes
# dedupe, cache size and browser caching one decision instead of three.
#
# Hashing 100MB of PNGs is far too slow to repeat per request, so it is memoised
# per path and invalidated on (mtime, size) — a file replaced by hand re-hashes,
# an untouched one never does. Bounded by file count, so it cannot grow.
_HASH_MEMO = {}          # path str -> (mtime_ns, size, digest)
_HASH_LOCK = threading.Lock()


def content_hash(path: Path) -> Optional[str]:
    """MD5 of an image file's bytes, memoised. None if it cannot be read."""
    try:
        stat = path.stat()
    except OSError:
        return None

    key = str(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _HASH_LOCK:
        cached = _HASH_MEMO.get(key)
        if cached is not None and cached[:2] == stamp:
            return cached[2]

    try:
        digest = hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return None

    with _HASH_LOCK:
        _HASH_MEMO[key] = (stamp[0], stamp[1], digest)
    return digest


# Byte hashing catches only exact duplicates, and that is not enough in
# practice: the same cover downloaded twice at different times comes back
# re-encoded, so it has two MD5s and lands on the wall twice. Measured on this
# library 2026-09-01 — 129 distinct by MD5, 128 by perceptual hash, the miss
# being four Ryan Adams songs carrying two encodings of one Ashes & Fire cover.
#
# A difference hash fixes that: greyscale, squash to 9x8, and record whether
# each pixel is brighter than its right-hand neighbour. Re-encoding does not
# change those comparisons; a genuinely different cover changes many of them.
# Used only for grouping — the tile's identity in URLs stays the content hash,
# so a mistaken grouping can hide a cover but can never serve the wrong file.
_PHASH_MEMO = {}         # path str -> (mtime_ns, size, bits)
_PHASH_SIDE = 8


def perceptual_hash(path: Path) -> Optional[int]:
    """Difference hash of an image, memoised. None if it cannot be read."""
    try:
        stat = path.stat()
    except OSError:
        return None

    key = str(path)
    stamp = (stat.st_mtime_ns, stat.st_size)
    with _HASH_LOCK:
        cached = _PHASH_MEMO.get(key)
        if cached is not None and cached[:2] == stamp:
            return cached[2]

    try:
        from PIL import Image
        with Image.open(path) as im:
            small = im.convert("L").resize(
                (_PHASH_SIDE + 1, _PHASH_SIDE), Image.Resampling.LANCZOS
            )
            pixels = list(small.getdata())
    except (OSError, ValueError):
        return None

    bits = 0
    for row in range(_PHASH_SIDE):
        offset = row * (_PHASH_SIDE + 1)
        for col in range(_PHASH_SIDE):
            brighter = pixels[offset + col] > pixels[offset + col + 1]
            bits = (bits << 1) | (1 if brighter else 0)

    with _HASH_LOCK:
        _PHASH_MEMO[key] = (stamp[0], stamp[1], bits)
    return bits


# ---------------------------------------------------------------------------
# Normalisation — the shared key function (dashboard AND runtime use this)
# ---------------------------------------------------------------------------

_BRACKETS_RE = re.compile(r"[\(\[\{].*?[\)\]\}]")
_DASH_SUFFIX_RE = re.compile(r"\s[-–—]\s.*$")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """
    Normalise a title or artist for use as a lookup key: lowercase, strip
    diacritics, drop parenthetical/bracket groups (e.g. ``(Live)``,
    ``(Acoustic)``), remove punctuation, collapse whitespace.

    Note: this deliberately does NOT strip ``- suffix`` tails. Song and artist
    arrive as separate fields (the song file's `` Song - Artist`` split already
    consumed the separating dash), so a dash inside a title is meaningful — and
    stripping it merges genuinely distinct songlist entries (e.g. ``Alan's
    choice`` vs ``Alan's Choice - Originals``). Dash-suffix stripping lives in
    ``clean_title_for_search`` where it only affects the outbound query.
    """
    if not text:
        return ""
    # Strip diacritics (é -> e) via canonical decomposition.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = _BRACKETS_RE.sub(" ", text)
    text = _NON_WORD_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def normalize_key(title: str, artist: str) -> str:
    """Library/cache key: ``normalized_title|normalized_artist``."""
    return f"{normalize_text(title)}|{normalize_text(artist)}"


def clean_title_for_search(title: str) -> str:
    """
    Lighter cleaning for building a search term: drop parenthetical/bracket
    groups and ``- suffix`` tails but keep the rest readable. The library/cache
    is still keyed on the ORIGINAL title, not this.
    """
    if not title:
        return ""
    title = _BRACKETS_RE.sub(" ", title)
    title = _DASH_SUFFIX_RE.sub("", title)
    return _WS_RE.sub(" ", title).strip()


# ---------------------------------------------------------------------------
# Filename sanitisation
# ---------------------------------------------------------------------------

_ILLEGAL_FS_RE = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(name: str, max_length: int = 120) -> str:
    """
    Make a string safe as a Windows filename component: strip illegal
    characters, collapse whitespace, trim trailing dots/spaces, cap length.
    """
    name = _ILLEGAL_FS_RE.sub("", name)
    name = _WS_RE.sub(" ", name).strip()
    # Windows disallows trailing dots/spaces.
    name = name.rstrip(". ")
    if len(name) > max_length:
        name = name[:max_length].rstrip(". ")
    return name or "untitled"


def image_filename(title: str, artist: str) -> str:
    """Human-readable ``{artist} - {title}.png`` filename (sanitised)."""
    artist_part = sanitize_filename(artist) if artist else "Unknown Artist"
    title_part = sanitize_filename(title) if title else "Unknown Title"
    return f"{artist_part} - {title_part}.png"


# ---------------------------------------------------------------------------
# Library object
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Library:
    """Wraps the library directory + manifest.json."""

    def __init__(self, library_dir: Path = None, manifest_path: Path = None):
        self.dir = Path(library_dir) if library_dir else config.LIBRARY_DIR
        self.manifest_path = (
            Path(manifest_path) if manifest_path else config.MANIFEST_PATH
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        self._manifest = self._load_manifest()

    # -- manifest persistence ------------------------------------------------

    def _load_manifest(self) -> dict:
        with MANIFEST_LOCK:
            if not self.manifest_path.exists():
                return {}
            try:
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except (json.JSONDecodeError, OSError):
                return {}

    def save(self) -> None:
        with MANIFEST_LOCK:
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self.manifest_path.write_text(
                json.dumps(self._manifest, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    # -- entry access --------------------------------------------------------

    def all_entries(self) -> dict:
        return self._manifest

    def get(self, title: str, artist: str) -> Optional[dict]:
        return self._manifest.get(normalize_key(title, artist))

    def get_by_key(self, key: str) -> Optional[dict]:
        return self._manifest.get(key)

    def image_path(self, entry: dict) -> Optional[Path]:
        """Absolute path to an entry's image file, or None if it has none."""
        fname = entry.get("file")
        if not fname:
            return None
        return self.dir / fname

    def has_image(self, entry: dict) -> bool:
        """
        True only if the entry references a file that actually exists on disk.
        Tolerates a manually deleted image (entry stays, art is treated missing).
        """
        path = self.image_path(entry)
        return bool(path and path.exists())

    # -- entry mutation ------------------------------------------------------

    def ensure_entry(self, title: str, artist: str) -> dict:
        """Get or create a bare entry for this song. Does not save."""
        key = normalize_key(title, artist)
        entry = self._manifest.get(key)
        if entry is None:
            entry = {
                "title": title,
                "artist": artist,
                "file": None,
                "status": None,
                "source": None,
                "album": None,
                "candidates": [],
                "candidate_index": -1,
                "candidates_tried": [],
                "updated_at": _utcnow(),
            }
            self._manifest[key] = entry
        return entry

    def set_status(self, entry: dict, status: str) -> None:
        entry["status"] = status
        entry["updated_at"] = _utcnow()

    def touch(self, entry: dict) -> None:
        entry["updated_at"] = _utcnow()

    def entries_by_status(self, status: str) -> dict:
        return {
            k: v for k, v in self._manifest.items() if v.get("status") == status
        }

    # -- art wall tiles ------------------------------------------------------

    def tile_pool(self, songlist_only: bool = True, exclude=(),
                  dedupe: bool = True) -> list:
        """
        Artwork available to the art wall: ``[{hash, title, artist}, ...]``.

        Any entry with a usable status and an image on disk qualifies, not just
        confirmed ones — once a user has run Sync and Find artwork there is art
        to show, and reviewing it is their call rather than a gate. This is the
        same test the other overlays already apply in ``_resolve_art``.

        ``songlist_only`` drops songs no longer in the songlist. It defaults on:
        without it the wall advertises songs that have been removed, and a
        viewer requests something the streamer cannot play.
        """
        excluded = set(exclude or ())
        seen = set()
        tiles = []
        for entry in self._manifest.values():
            if not isinstance(entry, dict):
                continue
            if entry.get("status") not in USABLE_STATUSES:
                continue
            if songlist_only and not entry.get("in_songlist"):
                continue
            path = self.image_path(entry)
            if not path or not path.exists():
                continue
            digest = content_hash(path)
            if digest is None or digest in excluded:
                continue
            if dedupe:
                # Group on what the cover LOOKS like, not on its bytes: the
                # same artwork fetched twice comes back re-encoded and would
                # otherwise appear on the wall more than once. Falls back to
                # the byte hash if the image cannot be decoded.
                group = perceptual_hash(path)
                if group is None:
                    group = digest
                if group in seen:
                    continue
                seen.add(group)
            tiles.append({
                "hash": digest,
                "title": entry.get("title") or "",
                "artist": entry.get("artist") or "",
            })
        return tiles

    def path_for_hash(self, digest: str) -> Optional[Path]:
        """
        The image file behind a tile hash. Used by the thumbnail route, which
        is handed a hash rather than a library key so identical artwork shares
        one URL (and therefore one browser cache entry).
        """
        if not digest:
            return None
        for entry in self._manifest.values():
            if not isinstance(entry, dict):
                continue
            path = self.image_path(entry)
            if path and path.exists() and content_hash(path) == digest:
                return path
        return None

    def save_image_bytes(self, title: str, artist: str, data: bytes) -> str:
        """
        Write raw image bytes to the library at source resolution under the
        human-readable filename and return the filename. Callers set the manifest
        ``file`` field to this and save the manifest.
        """
        fname = image_filename(title, artist)
        (self.dir / fname).write_bytes(data)
        return fname
