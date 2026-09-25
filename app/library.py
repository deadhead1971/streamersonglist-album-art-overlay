"""
Artwork library: the source of truth.

- Images are stored in ``library/`` at their source resolution (up to ~1000px),
  named human-readably as ``{artist} - {title}.png`` so bad images are easy to
  find and delete/replace by hand.
- ``library/manifest.json`` maps a NORMALISED key -> entry. The normalisation
  function is the joint that makes dashboard writes and live-runtime lookups
  line up, so both sides must call ``normalize_key`` and nothing else.

The manifest tolerates a manually deleted image file: an entry whose ``file`` is
gone is treated as needing art again (``has_image`` returns False). It does
NOT tolerate an unreadable manifest by treating it as empty — see "Manifest
persistence" below.
"""

import hashlib
import json
import logging
import random
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config, fileio, imaging

log = logging.getLogger("artwork_fetcher")

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
            bits = dhash(im)
    except (OSError, ValueError):
        return None

    with _HASH_LOCK:
        _PHASH_MEMO[key] = (stamp[0], stamp[1], bits)
    return bits


def dhash(image) -> int:
    """
    Difference hash of an open PIL image (see perceptual_hash). Two images are
    the same cover when their hashes differ in only a few of the 64 bits.
    """
    from PIL import Image
    small = image.convert("L").resize(
        (_PHASH_SIDE + 1, _PHASH_SIDE), Image.Resampling.LANCZOS
    )
    pixels = list(small.getdata())

    bits = 0
    for row in range(_PHASH_SIDE):
        offset = row * (_PHASH_SIDE + 1)
        for col in range(_PHASH_SIDE):
            brighter = pixels[offset + col] > pixels[offset + col + 1]
            bits = (bits << 1) | (1 if brighter else 0)
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


WALL_ORDERS = ("shuffle", "artist", "lightness", "hue")


def order_tiles(lib, tiles: list, order: str) -> list:
    """
    Arrange the art wall's tiles. Returns a new list, leaving the input alone.

    Only the sequence is decided here. The client takes a random contiguous
    slice of it, so a sorted wall still opens on a different stretch of the
    library every time a scene starts without the ordering itself ever
    breaking — and its swaps stay inside a window around each tile's place in
    this list, so a gradient does not speckle as covers cycle.
    """
    if order == "artist":
        return sorted(tiles, key=lambda t: (normalize_text(t.get("artist", "")),
                                            normalize_text(t.get("title", ""))))

    if order in ("lightness", "hue"):
        paths = lib.paths_by_hash()
        blank = {"hue": 0, "chroma": 0.0, "light": 0.0}
        profiled = []
        for tile in tiles:
            path = paths.get(tile["hash"])
            profile = imaging.colour_profile(path, tile["hash"]) if path else blank
            profiled.append((tile, profile))

        if order == "lightness":
            profiled.sort(key=lambda tp: tp[1]["light"])
        else:
            # Hue is a circle, so a plain 0-360 sweep runs red → orange →
            # yellow → green → cyan → blue → violet and back toward red, which
            # is the warm-to-cool gradient. Covers with no usable hue are not
            # scattered at whatever their noise peaked at: they form their own
            # band, ordered by lightness, ahead of the colour sweep.
            profiled.sort(key=lambda tp: (
                tp[1]["chroma"] > imaging.CHROMA_FLOOR,
                tp[1]["hue"] if tp[1]["chroma"] > imaging.CHROMA_FLOOR
                else tp[1]["light"],
            ))
        return [tile for tile, _ in profiled]

    shuffled = list(tiles)
    random.shuffle(shuffled)
    return shuffled


def image_filename(title: str, artist: str) -> str:
    """Human-readable ``{artist} - {title}.png`` filename (sanitised)."""
    artist_part = sanitize_filename(artist) if artist else "Unknown Artist"
    title_part = sanitize_filename(title) if title else "Unknown Title"
    return f"{artist_part} - {title_part}.png"


# ---------------------------------------------------------------------------
# Manifest persistence
# ---------------------------------------------------------------------------
#
# The manifest is the one file in the library that cannot be rebuilt: it holds
# every review decision. It used to be rewritten in place, and to load as an
# empty library whenever it failed to parse — a torn write, or a hand edit with
# a stray comma — without a log line. The next save then overwrote the
# original, and the next sync or live song re-downloaded artwork on top of the
# image files already on disk, uploads included. So now:
#   * every save is atomic, and first copies the version it is replacing to
#     manifest.json.bak (only ever a version that parses);
#   * an unreadable or missing manifest is replaced by that backup, with the
#     unreadable copy kept beside it;
#   * with no usable backup, loading raises ManifestUnreadable, so nothing
#     that writes to the library can run until the file is fixed.

class ManifestUnreadable(RuntimeError):
    """The manifest exists but cannot be used, and there is no backup to use."""


# What went wrong with the manifest, for the dashboard banner. An error clears
# on the next clean load; a restore notice stays until the dashboard restarts,
# because it happened once and the user needs to hear about it once. This is
# also what keeps a failure to one log line: Library() is built per request and
# the overlays poll every few seconds, so logging per load would flood the log.
_PROBLEM_LOCK = threading.Lock()
_problem = {"error": None, "notice": None}


def manifest_problem() -> Optional[dict]:
    """``{"level": "error"|"warning", "message": ...}`` for the banner, or None."""
    with _PROBLEM_LOCK:
        if _problem["error"]:
            return {"level": "error", "message": _problem["error"]}
        if _problem["notice"]:
            return {"level": "warning", "message": _problem["notice"]}
        return None


def _set_problem(kind: str, message: Optional[str]) -> None:
    with _PROBLEM_LOCK:
        changed = message is not None and _problem[kind] != message
        _problem[kind] = message
    if changed:
        (log.error if kind == "error" else log.warning)("%s", message)


def backup_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(manifest_path.name + ".bak")


def _display(path: Path) -> str:
    return f"{path.parent.name}/{path.name}"


def _parse_manifest(raw: bytes) -> dict:
    """Manifest bytes as a dict. ValueError if they are not a manifest."""
    # Parsed from bytes rather than text: json detects the encoding itself and
    # skips the UTF-8 byte-order mark some Windows editors add on save, which
    # decoding as plain UTF-8 turned into a parse failure.
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"expected a JSON object, found {type(data).__name__}")
    return data


def _unreadable(path: Path, reason: str) -> ManifestUnreadable:
    message = (
        f"Your artwork library's index ({_display(path)}) can't be read: "
        f"{reason}. Nothing has been changed, and nothing will be saved to the "
        f"library until it is fixed. Repair the file, or put a backup copy in "
        f"its place; the dashboard picks it up without a restart."
    )
    _set_problem("error", message)
    return ManifestUnreadable(message)


def _load_manifest_file(path: Path) -> dict:
    """
    The manifest at ``path``: {} when there has never been one (first run), the
    backup when it is unreadable or missing, else ManifestUnreadable. Call with
    MANIFEST_LOCK held.
    """
    if path.exists():
        try:
            raw = fileio.read_bytes(path)
        except OSError as e:
            # Not a verdict on the contents — the file may be fine — so it is
            # neither replaced nor mistaken for an empty library.
            raise _unreadable(path, f"it could not be opened ({e})") from e
        try:
            data = _parse_manifest(raw)
        except ValueError as e:
            data = _restore_backup(path, raw, f"it isn't a valid manifest ({e})")
    elif backup_path(path).exists():
        # A manifest does not vanish on its own, so this is far more likely an
        # accident than a deliberate fresh start. Starting over would quietly
        # lose every review decision; bringing the backup back loses nothing,
        # and anyone who really wants a clean slate deletes both.
        data = _restore_backup(path, None, "it was missing")
    else:
        data = {}
    _set_problem("error", None)
    return data


def _restore_backup(path: Path, raw: Optional[bytes], reason: str) -> dict:
    """Put manifest.json.bak in place of an unreadable or missing manifest."""
    backup = backup_path(path)
    try:
        backup_raw = fileio.read_bytes(backup)
        data = _parse_manifest(backup_raw)
    except (OSError, ValueError) as e:
        if raw is None:
            # No manifest, and a backup that is no use either: nothing is left
            # to protect, so this is a fresh start after all.
            log.warning("%s is missing and %s can't be read (%s) — starting "
                        "an empty library", _display(path), backup.name, e)
            return {}
        if isinstance(e, FileNotFoundError):
            raise _unreadable(path, f"{reason}, and there is no backup to "
                                    f"restore") from e
        raise _unreadable(path, f"{reason}, and the backup {backup.name} "
                                f"can't be used either ({e})") from e

    kept = ""
    try:
        if raw is not None:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            keep = path.with_name(f"{path.stem}.unreadable-{stamp}{path.suffix}")
            fileio.write_atomic(keep, raw)
            kept = f" The unreadable version was kept as {keep.name}."
        fileio.write_atomic(path, backup_raw)
    except OSError as e:
        raise _unreadable(path, f"{reason}, and putting the backup back "
                                f"failed ({e})") from e

    _set_problem("notice", (
        f"Your artwork library's index ({_display(path)}) couldn't be read "
        f"because {reason}, so its previous version ({backup.name}) was put "
        f"back in its place. Your most recent change to the library may need "
        f"redoing.{kept}"
    ))
    return data


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
        """Raises ManifestUnreadable rather than ever returning a false {}."""
        with MANIFEST_LOCK:
            return _load_manifest_file(self.manifest_path)

    def save(self) -> None:
        with MANIFEST_LOCK:
            data = json.dumps(self._manifest, indent=2, ensure_ascii=False)
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            self._keep_backup()
            fileio.write_atomic(self.manifest_path, data.encode("utf-8"))

    def _keep_backup(self) -> None:
        """
        Copy the manifest that is about to be replaced to manifest.json.bak.
        Only a version that parses is copied, so a damaged file can never
        displace the last good backup.
        """
        try:
            raw = fileio.read_bytes(self.manifest_path)
            _parse_manifest(raw)
        except (OSError, ValueError):
            return  # nothing usable to keep — including before the first save
        try:
            fileio.write_atomic(backup_path(self.manifest_path), raw)
        except OSError as e:
            # The previous backup is still intact; failing the user's save
            # over its safety net would be the wrong way round.
            log.warning("Could not update %s (%s) — the previous backup "
                        "is kept", backup_path(self.manifest_path).name, e)

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
        groups = {}
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

            title = entry.get("title") or ""
            artist = entry.get("artist") or ""
            name = " — ".join(p for p in (artist, title) if p) or "Untitled"

            if dedupe:
                # Group on what the cover LOOKS like, not on its bytes: the
                # same artwork fetched twice comes back re-encoded and would
                # otherwise appear on the wall more than once. Falls back to
                # the byte hash if the image cannot be decoded.
                group = perceptual_hash(path)
                if group is None:
                    group = digest
                first = groups.get(group)
                if first is not None:
                    # Merged away, but still recorded: a cover shared by six
                    # songs that names only one of them looks like the wall has
                    # picked the wrong picture. ``songs`` lets the tooltip say
                    # how many it stands for.
                    first["songs"].append(name)
                    continue

            tile = {
                "hash": digest,
                "title": title,
                "artist": artist,
                "songs": [name],
            }
            if dedupe:
                groups[group] = tile
            tiles.append(tile)
        return tiles

    def paths_by_hash(self) -> dict:
        """
        ``{content hash: path}`` for every image in the library, in one pass.

        Ordering needs the file behind every tile at once, and calling
        ``path_for_hash`` per tile would rescan the manifest each time.
        """
        index = {}
        for entry in self._manifest.values():
            if not isinstance(entry, dict):
                continue
            path = self.image_path(entry)
            if not path or not path.exists():
                continue
            digest = content_hash(path)
            if digest:
                index.setdefault(digest, path)
        return index

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

    def save_image_bytes(self, entry: dict, data: bytes) -> str:
        """
        Write an image for ``entry`` at source resolution and return its
        filename. Callers set the entry's ``file`` to it and save the manifest.

        An image the user chose is never overwritten. When a confirmed or
        uploaded image is being replaced, its file stays and the new one is
        saved beside it as ``Artist - Title (2).png``. The same goes for any
        file this entry doesn't own: another song whose name sanitises to the
        same filename, or an image left on disk by a manifest that was lost.
        That last case is how a sync after a lost manifest used to re-download
        artwork over every upload.

        Only this entry's own machine-picked art (proposed, unverified) is
        replaced in place, so cycling through candidates leaves no trail of
        files behind it.
        """
        fname = self._own_disposable_file(entry) or self._free_filename(entry)
        fileio.write_atomic(self.dir / fname, data)
        return fname

    def _own_disposable_file(self, entry: dict) -> Optional[str]:
        """The entry's current file, if it may be overwritten — else None."""
        current = entry.get("file")
        if not current:
            return None
        if entry.get("status") == STATUS_CONFIRMED or entry.get("source") == "manual":
            return None
        for other in self._manifest.values():
            if other is not entry and isinstance(other, dict) \
                    and other.get("file") == current:
                return None
        return current

    def _free_filename(self, entry: dict) -> str:
        """``Artist - Title.png``, or the first free ``(2)``, ``(3)``… after it."""
        wanted = Path(image_filename(entry.get("title") or "",
                                     entry.get("artist") or ""))
        name, n = wanted.name, 2
        while (self.dir / name).exists():
            name = f"{wanted.stem} ({n}){wanted.suffix}"
            n += 1
        return name
