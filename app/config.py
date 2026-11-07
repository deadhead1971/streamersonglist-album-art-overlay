"""
Configuration loading/saving.

config.json lives in the data folder (the repo root when run from source, where
it is gitignored; see DATA_DIR below). On first run there is no
config.json; ``ensure_config()`` copies ``config.example.json`` into place so the
app can start and send the user to the settings page instead of crashing.

All the hardcoded constants from the v2 script live here as config values.
"""

import json
import os
import shutil
from pathlib import Path

from . import __version__, fileio

# Two roots, because an installed copy separates them. RESOURCE_DIR holds what
# ships with the app and is only ever read (config.example.json, the OBS
# loaders); DATA_DIR holds everything the app writes. From source both are the
# repo root, exactly as before. Installed, the resources sit in the program
# folder, which an upgrade replaces wholesale — user data written there would
# be wiped by the next install — so data goes to LOCALAPPDATA: per-user, no
# admin rights, never synced by OneDrive (the output PNG is rewritten on every
# song change), and not guarded by Controlled Folder Access.
#
# The installed app is plain source run by a bundled python.org embeddable
# Python (packaging/build.ps1), so it can't be told apart from a source run by
# the interpreter; the build drops this marker file into the program folder.
APP_DIR_NAME = "AlbumArtOverlay"
RESOURCE_DIR = Path(__file__).resolve().parent.parent
INSTALLED = (RESOURCE_DIR / "installed.txt").is_file()


def _data_dir() -> Path:
    # An explicit override wins in every mode: CI and tests point it at a
    # scratch folder, and tools/ can run against an installed copy's data.
    override = os.environ.get("ALBUMART_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if INSTALLED:
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / APP_DIR_NAME
    return RESOURCE_DIR


DATA_DIR = _data_dir()
# True for an installed copy (or a source run with ALBUMART_DATA_DIR set): the
# data folder is not where the app's own files live, so the OBS loaders are
# copied out and first-run defaults point into the data folder.
SEPARATE_DATA_DIR = RESOURCE_DIR != DATA_DIR

CONFIG_PATH = DATA_DIR / "config.json"
EXAMPLE_PATH = RESOURCE_DIR / "config.example.json"

# The OBS browser-source loaders. OBS stores the absolute path, so it has to
# stay put across upgrades: an installed copy serves them from the data folder
# (see dashboard.sync_obs_loaders), a source run straight from the repo.
BUNDLED_OBS_DIR = RESOURCE_DIR / "obs"
OBS_DIR = DATA_DIR / "obs"

# Where a fresh installed copy writes the OBS image source's PNG until the user
# picks somewhere else. Only used as a first-run default (see ensure_config).
DEFAULT_OUTPUT_IMAGE = DATA_DIR / "current_artwork.png"

LIBRARY_DIR = DATA_DIR / "library"
MANIFEST_PATH = LIBRARY_DIR / "manifest.json"

# Art wall thumbnail cache. Named by image content hash, so identical artwork
# (31% of a real library) collapses to one file and one URL. Disposable —
# deleting it just means the next wall load regenerates. Inside library/, which
# is already gitignored.
THUMBS_DIR = LIBRARY_DIR / "thumbs"

LOG_FILE = DATA_DIR / "artwork_fetcher.log"

# MusicBrainz asks that the User-Agent identify the app + a contact URL. Identify
# the tool and its repo, not the streamer running it. Built from __version__ so
# the two can't drift — they did between 1.0 and 1.1.0.
USER_AGENT = (
    f"StreamerSonglistAlbumArtOverlay/{__version__} "
    "(+https://github.com/deadhead1971/streamersonglist-album-art-overlay)"
)

DEFAULT_CONFIG = {
    "streamersonglist_username": "",
    # StreamerSonglist API. Every read needs a token.
    #   api_base       blank = production; set to the staging host to test
    #   api_token      the SSL access token — NEVER commit this
    #   api_token_type which Authorization prefix the token needs. The two
    #                  kinds look identical (both 32 opaque chars), so this
    #                  cannot be sniffed from the token — the user picks it,
    #                  and Test connection finds it for them:
    #                    "User"     profile → API Access; every channel they admin
    #                    "Streamer" channel Settings → Access; that one channel
    #                    "Bearer"   an OAuth2 access token
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
        # Which way the list runs: "column" is the classic top-down list,
        # "row" a horizontal strip of covers across the screen. Horizontal is
        # artwork-only by design — a cover with its title beside it needs
        # ~300px, and six of those overrun a 1920 source, so the text fields
        # are forced off rather than offered and then truncated. It does not
        # wrap: the strip's height stays put whatever the queue does.
        "direction": "column",   # column | row
        "show_artwork": True,
        "show_title": True,
        "show_artist": True,
        "show_requester": True,
        "show_position": True,
        # Show the empty-queue promo card (content in the "promo" section)
        # when this overlay has nothing else to render.
        "show_promo": False,
        "preset": "dark",        # dark | light | minimal | glass
        # Draw the preset's card behind each row. Off leaves the artwork
        # floating on the transparent source — which is what "artwork only"
        # always needed: hiding the text alone still left the cover sitting in
        # a full-width empty box. The empty-queue promo card is exempt, since
        # it shows alone and its message has to stay readable.
        "show_card": True,
        "font_size": 20,         # px, base text size
        "art_size": 56,          # px, artwork thumbnail square
        # px corner radius on the artwork. 0 is square; half of art_size or
        # more is a circle, so the range has to reach half of art_size's cap.
        "art_radius": 6,
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
    # Art wall (OBS browser source at /overlay/wall): a grid of album covers
    # drawn from the local library. Unlike the other overlays this one reads no
    # queue and polls nothing — the tile list is fetched once on load.
    "wall": {
        # The ONLY size control. Tiles are square at 1fr, so the number of rows
        # is whatever fits the browser source's height — one setting covers
        # landscape and portrait, and it survives a resize in OBS. Capped at 12
        # because past that a normal library has over half of itself on screen.
        "columns": 6,
        "gap": 0,                # px between tiles. 0 = gapless mosaic
        "radius": 0,             # px corner radius. 0 = square
        # Reserved. v1 accepts "library" only; a live-queue source can land
        # later without a config migration.
        "source": "library",
        "filter": "songlist",    # songlist = only songs still in the songlist
        "dedupe": True,          # one tile per distinct image (see content_hash)
        "exclude": [],           # content hashes the user has hidden
        # shuffle | artist | lightness | hue. A sorted wall is not a fixed
        # picture: the page opens on a random stretch of the sequence, so it
        # looks different each time a scene starts while the order holds.
        "order": "shuffle",
        # Which way a sorted sequence runs across the grid: "row" fills left to
        # right (a gradient reads as horizontal bands), "column" fills top to
        # bottom (vertical bands). Ignored when order is shuffle.
        "sort_axis": "row",
        # Motion. The swap is what makes the wall feel alive: every
        # swap_interval seconds one tile cross-fades to a cover that is not
        # currently on screen. It needs SURPLUS to work — with exactly as many
        # covers as tiles there is nothing to swap in and the wall is frozen.
        "swap_interval": 4,      # seconds. 0 = static wall
        "swap_fade": 1200,       # ms cross-fade
        "drift": True,           # slow Ken Burns scale/pan, random per tile
        "assemble": True,        # staggered fade-in on load, in random order
        "breathe": False,        # slow opacity idle (compounds with drift)
        "hero_interval": 0,      # seconds between highlight pulses. 0 = off
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

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if EXAMPLE_PATH.exists():
        shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
    else:
        save_config(DEFAULT_CONFIG)

    if SEPARATE_DATA_DIR:
        # An installed copy starts with an output image already chosen, so the
        # live PNG works before the user has visited Settings, and it lands in
        # the data folder rather than Documents or the Desktop, which OneDrive
        # syncs and Controlled Folder Access can block. A source install keeps
        # today's blank field.
        cfg = load_config()
        if not cfg.get("output_image"):
            cfg["output_image"] = str(DEFAULT_OUTPUT_IMAGE)
            save_config(cfg)
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
        # utf-8-sig: a byte-order mark (Windows editors and PowerShell add one)
        # used to make this a parse failure, i.e. silently all-default settings.
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError):
        return dict(DEFAULT_CONFIG)

    if not isinstance(data, dict):
        return dict(DEFAULT_CONFIG)

    return _deep_merge(DEFAULT_CONFIG, data)


def save_config(cfg: dict) -> None:
    """
    Write config.json (pretty-printed, stable key order for clean diffs).

    Atomic: every overlay poll reads this file, and a read landing mid-write
    could see a truncated file and get all-default settings back — which a
    save handler reading at that moment would then have written out.
    """
    data = json.dumps(cfg, indent=2, ensure_ascii=False)
    fileio.write_atomic(CONFIG_PATH, data.encode("utf-8"))
