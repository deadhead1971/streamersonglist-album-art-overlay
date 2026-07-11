"""
Runtime watcher — headless, launched by a .bat from TouchPortal at stream start.

Watches ``current song.txt`` and, on change, writes ``current_artwork.png`` for
OBS. Lookup order (changed from v2):
  1. Parse ``Song - Artist`` from the song file.
  2. skip_artists -> fallback image (the streamer's own originals).
  3. Library lookup (confirmed, or proposed/unverified as better-than-nothing):
     render library image -> atomic save. Done.
  4. Live cascade iTunes -> Last.fm -> MusicBrainz: on success, save into the
     library as ``unverified`` (so it appears in the dashboard review queue),
     then output it.
  5. Nothing found -> fallback image, and record a ``rejected_all`` stub so the
     dashboard shows the miss.

The watchdog skeleton (debounce, seed-last-content, initial fetch) and the
atomic save are ported from v2 because those details were hard-won.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from PIL import Image

from . import artwork, config, imaging, sources
from .library import (
    STATUS_CONFIRMED, STATUS_PROPOSED, STATUS_REJECTED_ALL, STATUS_UNVERIFIED,
    Library,
)

log = logging.getLogger("artwork_fetcher")

# Library statuses whose stored image the watcher is willing to display, best
# first. rejected_all is excluded (user rejected everything → go live/fallback).
USABLE_STATUSES = (STATUS_CONFIRMED, STATUS_PROPOSED, STATUS_UNVERIFIED)


def setup_logging():
    if log.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(config.LOG_FILE, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ---------------------------------------------------------------------------
# Song file parsing (ported from v2, incl. en/em-dash variants)
# ---------------------------------------------------------------------------

def read_song_file(path: str):
    """Read the song file and parse 'Song - Artist'. Returns (song, artist) or None."""
    if not path:
        log.error("No song_file configured — set it in the dashboard settings")
        return None
    try:
        text = Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        log.warning("Song file not found: %s", path)
        return None
    except Exception as e:
        log.error("Error reading song file: %s", e)
        return None

    if not text:
        log.info("Song file is empty")
        return None

    for sep in (" - ", " – ", " — "):  # hyphen, en-dash, em-dash
        if sep in text:
            song, artist = text.split(sep, maxsplit=1)
            return song.strip(), artist.strip()
    return text, ""


# ---------------------------------------------------------------------------
# Fallback output
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


# ---------------------------------------------------------------------------
# Main fetch
# ---------------------------------------------------------------------------

def fetch_artwork(cfg: dict = None, lib: Library = None) -> bool:
    """Read the song file, resolve artwork through the lookup order, write output."""
    if cfg is None:
        cfg = config.load_config()
    if lib is None:
        lib = Library()

    result = read_song_file(cfg.get("song_file"))
    if result is None:
        log.info("No song data — applying fallback")
        apply_fallback(cfg)
        return False

    song, artist = result
    log.info("Current song: '%s' by '%s'", song, artist)

    # 2. Own songs → fallback, no search.
    skip = [a.strip().lower() for a in cfg.get("skip_artists", [])]
    if artist.strip().lower() in skip:
        log.info("Own song (artist '%s') — applying fallback", artist)
        apply_fallback(cfg)
        return False

    # 3. Library lookup.
    entry = lib.get(song, artist)
    if entry and entry.get("status") in USABLE_STATUSES and lib.has_image(entry):
        log.info("Library hit (%s) — rendering stored art", entry.get("status"))
        if artwork.render_entry_to_output(lib, cfg, entry):
            return True

    # 4. Live cascade.
    log.info("No library art — running live cascade")
    for candidate in sources.search_cascade(artist, song, cfg):
        raw = artwork.try_download(candidate)
        if raw is None:
            continue
        try:
            entry = artwork.store_candidate(
                lib, song, artist, candidate, STATUS_UNVERIFIED, raw_bytes=raw
            )
        except ValueError:
            continue
        log.info("Live grab from %s ('%s') — flagged unverified for review",
                 candidate.get("source"), candidate.get("album"))
        artwork.render_entry_to_output(lib, cfg, entry)
        return True

    # 5. Nothing found → fallback + record the miss.
    log.warning("No artwork found for '%s' - '%s'", song, artist)
    miss = lib.ensure_entry(song, artist)
    if not lib.has_image(miss):
        miss["file"] = None
        lib.set_status(miss, STATUS_REJECTED_ALL)
        lib.save()
    apply_fallback(cfg)
    return False


# ---------------------------------------------------------------------------
# Watch mode (ported skeleton from v2)
# ---------------------------------------------------------------------------

def watch_mode(cfg: dict):
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        log.error("Install watchdog for watch mode: pip install watchdog")
        sys.exit(1)

    song_file = cfg.get("song_file")
    if not song_file:
        log.error("No song_file configured — set it in the dashboard settings")
        sys.exit(1)

    song_path = Path(song_file)
    lib = Library()

    class SongFileHandler(FileSystemEventHandler):
        def __init__(self):
            self.last_content = None
            self.debounce_time = 0.0

        def on_modified(self, event):
            try:
                if Path(event.src_path).resolve() != song_path.resolve():
                    return
            except OSError:
                return

            now = time.time()
            if now - self.debounce_time < 1.0:
                return
            self.debounce_time = now

            try:
                content = song_path.read_text(encoding="utf-8").strip()
            except Exception:
                return

            if content == self.last_content:
                return
            self.last_content = content

            log.info("Song file changed — fetching artwork...")
            fetch_artwork(cfg, lib)

    handler = SongFileHandler()
    observer = Observer()
    observer.schedule(handler, str(song_path.parent), recursive=False)
    observer.start()

    log.info("Watching for changes to: %s", song_file)
    log.info("Press Ctrl+C to stop")

    # Seed last_content so the startup FS event for the already-present file
    # doesn't trigger a duplicate fetch on top of the initial one below.
    try:
        handler.last_content = song_path.read_text(encoding="utf-8").strip()
    except Exception:
        pass

    fetch_artwork(cfg, lib)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        log.info("Stopped watching")
    observer.join()


def main():
    setup_logging()
    parser = argparse.ArgumentParser(
        description="Fetch album artwork for the current song"
    )
    parser.add_argument("--watch", "-w", action="store_true",
                        help="Watch the song file for changes (continuous mode)")
    args = parser.parse_args()

    cfg = config.load_config()
    if args.watch:
        watch_mode(cfg)
    else:
        fetch_artwork(cfg)


if __name__ == "__main__":
    main()
