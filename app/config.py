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

from . import __version__

# Repo root = parent of the app package directory.
ROOT = Path(__file__).resolve().parent.parent

CONFIG_PATH = ROOT / "config.json"
EXAMPLE_PATH = ROOT / "config.example.json"

LIBRARY_DIR = ROOT / "library"
MANIFEST_PATH = LIBRARY_DIR / "manifest.json"

LOG_FILE = ROOT / "artwork_fetcher.log"

# MusicBrainz asks that the User-Agent identify the app + a contact URL. Identify
# the tool and its repo, not the streamer running it. Built from __version__ so
# the two can't drift — they did between 1.0 and 1.1.0.
USER_AGENT = (
    f"StreamerSonglistAlbumArtOverlay/{__version__} "
    "(+https://github.com/deadhead1971/streamersonglist-album-art-overlay)"
)

DEFAULT_CONFIG = {
    "streamersonglist_username": "",
    # StreamerSonglist API. v1 (today's production) needs no credentials; the
    # v2 rewrite requires auth on every read. The client detects which is live
    # and only sends the token when one is set, so these can stay blank until
    # SSL flips.
    #   api_base       blank = production; set to the staging host to test
    #   api_token      the SSL access token — NEVER commit this
    #   api_token_type "User" (profile → API Access) or "Streamer"
    #   platform       twitch | youtube | kick | none
    "api_base": "",
    "api_token": "",
    "api_token_type": "User",
    "platform": "twitch",
    # Where the runtime service gets the current song:
    #   "streamersonglist" (default) — poll the live queue, top slot = now playing
    #   "file"                        — read a text file another tool writes
    "song_source": "streamersonglist",
    # Seconds between runtime service ticks (queue poll / song-file check).
    # With realtime events connected this stretches to a slow safety net —
    # changes arrive over the socket instead.
    "poll_interval": 10,
    # Subscribe to SSL's realtime event stream (v2 only) so queue changes apply
    # immediately instead of on the next poll. Falls back to polling by itself
    # if the socket is unavailable; turn off to force polling.
    "websocket_events": True,
    # Blank = production events host; set it to test against staging.
    "events_url": "",
    "song_file": "",
    "output_image": "",
    "fallback_image": "",
    "skip_artists": [],
    "itunes_country": "GB",
    "lastfm_api_key": "",
    "image_size": 640,
    # Start the in-process runtime service (queue poller + PNG writer) when the
    # dashboard launches. Disable only if you never use the live outputs.
    "runtime_service": True,
    # Update check. Once per dashboard launch the app asks GitHub for the
    # latest release and shows a banner if it is newer than __version__. It is
    # an anonymous GET to api.github.com that sends nothing but the User-Agent.
    #   skipped_version  a release the user dismissed; a newer one still shows
    "updates": {
        "check_enabled": True,
        "skipped_version": "",
    },
    # Queue overlay (OBS browser source at /overlay/queue).
    "overlay": {
        "max_songs": 5,
        "include_current": True,
        "show_artwork": True,
        "show_title": True,
        "show_artist": True,
        "show_requester": True,
        "show_position": True,
        # Show the empty-queue promo card (content in the "promo" section)
        # when this overlay has nothing else to render.
        "show_promo": False,
        "preset": "dark",        # dark | light | minimal | glass
        "font_size": 20,         # px, base text size
        "art_size": 56,          # px, artwork thumbnail square
        "row_gap": 10,           # px between rows
        "accent": "#4da3ff",     # position number / highlight colour
        "animation": "slide",    # slide | fade | none
        "anim_speed": "normal",  # normal | fast
    },
    # Now-playing card (OBS browser source at /overlay/current).
    "overlay_current": {
        "layout": "horizontal",  # horizontal (art left) | vertical (art top)
        "show_artwork": True,
        "show_title": True,
        "show_artist": True,
        "show_requester": True,
        "show_label": True,      # the small caption above the title
        "label_text": "Now playing",
        "preset": "dark",        # dark | light | minimal | glass
        "font_size": 26,         # px, title base size
        "art_size": 160,         # px, artwork square
        "accent": "#4da3ff",     # label / highlight colour
        "animation": "fade",     # slide | fade | none — on song change
        "anim_speed": "normal",  # normal | fast
        "hide_when_empty": True, # hide the card when the queue is empty
        "show_promo": False,     # ...unless the promo card is on, which wins
    },
    # Empty-queue promo: a card inviting viewers to request something, shown by
    # either overlay (see their "show_promo" flags) once the queue has been
    # empty for delay_seconds. The content is shared so the same message does
    # not have to be maintained in two places.
    "promo": {
        "text": "Requests are open!",
        "subtext": "Type !songlist to see what I can play",
        # Blank = the streamer's SSL avatar, falling back to fallback_image.
        "image": "",
        # Seconds the queue must stay empty first. Stops the card flashing in
        # the gap between one song ending and the next being promoted.
        "delay_seconds": 8,
        # Shown instead when the streamer is not accepting requests (SSL's
        # requestsActive switch). Blank text = show nothing while closed;
        # blank image = reuse the image above.
        "closed_text": "",
        "closed_subtext": "",
        "closed_image": "",
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
