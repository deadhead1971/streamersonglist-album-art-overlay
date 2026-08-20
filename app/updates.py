"""
Update check against the project's GitHub Releases.

Users install this by cloning or downloading a zip, so nothing tells them a new
version exists unless they go and look. This asks GitHub's release API once per
dashboard launch, in a background thread, and the dashboard shows a banner if
the latest release is newer than ``__version__``.

Deliberately small and self-contained:

* It never runs on a request path and never inside the runtime tick — that tick
  is stream-critical and gets no GitHub calls.
* It never writes config.json. The check timestamp lives in memory only, so a
  background check can't race a settings save into a read-modify-write clobber.
  Only the user dismissing a version writes, and that happens on a request
  thread.
* Failure is silent. A firewalled or offline user should not get an error bar
  on every page for something they never asked for. The Settings page's
  "Check now" is the one path that reports errors, because there the user asked.
"""

import logging
import re
import threading

import requests

from . import __version__, config

log = logging.getLogger("artwork_fetcher")

REPO = "deadhead1971/streamersonglist-album-art-overlay"
# /releases/latest excludes drafts and prereleases, which is exactly the
# semantics we want — a prerelease should not nag everybody to upgrade.
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPO}/releases/latest"

# Unauthenticated GitHub allows 60 requests/hour per IP. One check per launch
# plus the occasional manual click is nowhere near it.
_TIMEOUT = 5

_LOCK = threading.Lock()
_STATE = {
    "checked": False,   # has a check completed (successfully or not) yet?
    "latest": "",       # canonicalised release tag, e.g. "v1.4.0"
    "name": "",         # release title
    "url": RELEASE_PAGE,
    "published_at": "",
    "error": "",
}

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


def parse_version(text: str):
    """
    "v1.10.0" -> (1, 10, 0). None if it doesn't look like a version at all.

    Tuples, not strings: "1.10.0" sorts *below* "1.9.0" as text, and that bug
    would sit silent until the day the minor version reaches double digits.
    """
    match = _VERSION_RE.match((text or "").strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def check() -> dict:
    """
    Ask GitHub for the latest release and cache the answer. Returns the raw
    cache dict (not the resolved view — see ``state``). Never raises.
    """
    headers = {
        "User-Agent": config.USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        resp = requests.get(RELEASES_API, headers=headers, timeout=_TIMEOUT)
    except requests.RequestException as exc:
        # The raw urllib3 text is a wall of connection-pool detail — log it,
        # show the user a sentence.
        log.info("Update check failed: %s", exc)
        return _store(error="couldn't reach github.com")

    if resp.status_code != 200:
        # 403 here is almost always the 60-requests/hour anonymous rate limit,
        # which is worth telling apart from a real outage.
        limited = (resp.status_code in (403, 429)
                   and resp.headers.get("X-RateLimit-Remaining") == "0")
        message = ("GitHub rate limit reached — try again later" if limited
                   else f"GitHub returned {resp.status_code}")
        log.info("Update check failed: %s", message)
        return _store(error=message)

    try:
        data = resp.json()
    except ValueError as exc:  # non-JSON body
        log.info("Update check returned junk: %s", exc)
        return _store(error="unexpected response from GitHub")

    tag = str(data.get("tag_name") or "")
    parsed = parse_version(tag)
    if parsed is None:
        # A tag we can't read is not evidence of an update.
        log.info("Update check: unrecognised tag %r", tag)
        return _store(error="unrecognised release tag")

    # Store one canonical form ("v1.4.0") however the tag was written, so every
    # place that shows it can print it as-is. Nothing builds a URL from this —
    # the release link comes from html_url — so normalising is safe.
    latest = "v%d.%d.%d" % parsed
    log.info("Update check: latest release %s (running %s)", latest, __version__)
    return _store(
        latest=latest,
        name=_headline(str(data.get("name") or ""), tag, latest),
        url=str(data.get("html_url") or RELEASE_PAGE),
        published_at=str(data.get("published_at") or ""),
        error="",
    )


def _headline(name: str, *tags: str) -> str:
    """
    The release title with any leading version stripped.

    Release names here are written as 'v1.3.0 - "Requested by" is back', and
    the banner already says the version — without this it reads
    'v1.3.0 available — v1.3.0 - "Requested by" is back'.
    """
    text = name.strip()
    for tag in tags:
        if tag and text.lower().startswith(tag.lower()):
            text = text[len(tag):]
            break
    return text.lstrip(" -–—:·").strip()


def _store(**fields) -> dict:
    """
    Update the cache. The one place _STATE is written.

    Only the named fields change, so the error paths (which pass just ``error``)
    keep the last known release rather than wiping it — a "Check now" that fails
    should not make a banner the user was already shown disappear.
    """
    with _LOCK:
        _STATE.update(checked=True, **fields)
        return dict(_STATE)


def state(cfg: dict) -> dict:
    """
    The cached answer, resolved against the running version and the version the
    user dismissed. Safe to call from a request handler — pure cache read.
    """
    with _LOCK:
        snapshot = dict(_STATE)

    current = parse_version(__version__)
    latest = parse_version(snapshot["latest"])
    newer = bool(current and latest and latest > current)

    skipped = (cfg.get("updates", {}).get("skipped_version") or "").strip()
    snapshot.update(
        current=__version__,
        newer=newer,
        # Local ahead of remote (a dev machine mid-cycle) reads as no update,
        # which falls out of the > comparison above.
        update_available=newer and snapshot["latest"] != skipped,
        skipped=skipped,
    )
    return snapshot


def check_enabled(cfg: dict) -> bool:
    return bool(cfg.get("updates", {}).get("check_enabled", True))


def start_background_check(cfg: dict) -> None:
    """Kick the launch-time check off a daemon thread so startup never waits."""
    if not check_enabled(cfg):
        log.info("Update check disabled in config")
        return
    threading.Thread(target=check, name="update-check", daemon=True).start()
