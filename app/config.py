"""
Configuration loading/saving.

config.json lives in the repo root and is gitignored. On first run there is no
config.json; ``ensure_config()`` copies ``config.example.json`` into place so the
app can start and send the user to the settings page instead of crashing.

All the hardcoded constants from the v2 script live here as config values.
"""

import json
import shutil
from pathlib import Path

# Repo root = parent of the app package directory.
ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

LIBRARY_DIR = ROOT / "library"
MANIFEST_PATH = LIBRARY_DIR / "manifest.json"

LOG_FILE = ROOT / "artwork_fetcher.log"

# MusicBrainz asks that the User-Agent identify the app + a contact URL. Identify
# the tool and its repo, not the streamer running it.
USER_AGENT = (
    "StreamerSonglistArtFetcher/1.0 "
    "(+https://github.com/deadhead1971/streamersonglist-art-fetcher)"
)

DEFAULT_CONFIG = {
    "streamersonglist_username": "",
    # Where the runtime watcher gets the current song:
    #   "streamersonglist" (default) — poll the live queue, top slot = now playing
    #   "file"                        — watch a text file another tool writes
    "song_source": "streamersonglist",
    # Seconds between SSL queue polls (only used when song_source == streamersonglist).
    "poll_interval": 10,
    "song_file": "",
    "output_image": "",
    "fallback_image": "",
    "skip_artists": [],
    "itunes_country": "GB",
    "lastfm_api_key": "",
    "image_size": 640,
    # Start the in-process runtime service (queue poller + PNG writer) when the
    # dashboard launches. Disable if you only ever run the standalone watcher.
    "runtime_service": True,
    # Queue overlay (OBS browser source at /overlay/queue).
    "overlay": {
        "max_songs": 5,
        "include_current": True,
        "show_artwork": True,
        "show_title": True,
        "show_artist": True,
        "show_requester": True,
        "show_position": True,
        "preset": "dark",        # dark | light | minimal | glass
        "font_size": 20,         # px, base text size
        "art_size": 56,          # px, artwork thumbnail square
        "row_gap": 10,           # px between rows
        "accent": "#4da3ff",     # position number / highlight colour
        "animation": "slide",    # slide | fade | none
        "anim_speed": "normal",  # normal | fast
    },
    "reflection": {
        "enabled": True,
        "height": 0.6,
        "opacity": 1.0,
        "gap": 5,
        "perspective": 0.55,
        "fade_power": 0.5,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    """Return base with override applied, recursing into nested dicts."""
    result = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def config_exists() -> bool:
    return CONFIG_PATH.exists()


def ensure_config() -> bool:
    """
    Make sure config.json exists. If it doesn't, copy config.example.json over
    it (or write the built-in defaults if the example is missing too).

    Returns True if a fresh config was just created (first run), False if one
    already existed.
    """
    if CONFIG_PATH.exists():
        return False

    if EXAMPLE_PATH.exists():
        shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
    else:
        save_config(DEFAULT_CONFIG)
    return True


def load_config() -> dict:
    """
    Load config.json, filling any missing keys from DEFAULT_CONFIG so older or
    partial configs never raise KeyError. Returns a copy of the defaults if no
    config file exists yet.
    """
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)

    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)

    if not isinstance(data, dict):
        return dict(DEFAULT_CONFIG)

    return _deep_merge(DEFAULT_CONFIG, data)


def save_config(cfg: dict) -> None:
    """Write config.json (pretty-printed, stable key order for clean diffs)."""
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )
