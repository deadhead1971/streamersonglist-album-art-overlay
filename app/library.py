"""
Artwork library: the source of truth.

- Images are stored in ``library/`` at their source resolution (up to ~1000px),
  named human-readably as ``{artist} - {title}.png`` so bad images are easy to
  find and delete/replace by hand.
- ``library/manifest.json`` maps a NORMALISED key -> entry. The normalisation
  function is the joint that makes dashboard writes and watcher lookups line up,
  so both sides must call ``normalize_key`` and nothing else.

The manifest tolerates a manually deleted image file: an entry whose ``file`` is
gone is treated as needing art again (``has_image`` returns False).
"""

import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config

# Status values used in manifest entries.
STATUS_PROPOSED = "proposed"        # auto-fetched, awaiting review
STATUS_CONFIRMED = "confirmed"      # user approved
STATUS_UNVERIFIED = "unverified"    # grabbed live during a stream (review queue)
STATUS_REJECTED_ALL = "rejected_all"  # user/watcher exhausted candidates, no art


# ---------------------------------------------------------------------------
# Normalisation — the shared key function (dashboard AND watcher use this)
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
        if not self.manifest_path.exists():
            return {}
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def save(self) -> None:
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

    def save_image_bytes(self, title: str, artist: str, data: bytes) -> str:
        """
        Write raw image bytes to the library at source resolution under the
        human-readable filename and return the filename. Callers set the manifest
        ``file`` field to this and save the manifest.
        """
        fname = image_filename(title, artist)
        (self.dir / fname).write_bytes(data)
        return fname
