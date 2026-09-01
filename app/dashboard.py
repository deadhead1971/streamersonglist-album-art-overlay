"""
Flask dashboard: settings -> songlist sync -> bulk propose -> review -> review queue.

Local tool only — binds 127.0.0.1. The bulk-propose run is long (185+ throttled
iTunes calls) so it runs in a background thread with a polled progress endpoint;
request handlers never block on it.
"""

import io
import json
import logging
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import requests
from flask import (
    Flask, jsonify, redirect, render_template, request, send_file, url_for,
)
from PIL import Image

from . import (
    __version__, artwork, config, imaging, library, runtime, songlist, updates,
)
from .library import (
    Library, STATUS_PROPOSED, STATUS_UNVERIFIED,
    normalize_key,
)

log = logging.getLogger("artwork_fetcher")

app = Flask(__name__)


@app.context_processor
def inject_version():
    """Every page footer/banner can show which version is running."""
    return {"app_version": __version__}


# Snapshot of the last fetched songlist (for the songs table). Lives beside the
# manifest so a removed-from-SSL song can still be shown/filtered.
SONGLIST_SNAPSHOT = config.LIBRARY_DIR / "songlist.json"

# iTunes rate limit is ~20/min; throttle the bulk run to ~3s/call.
PROPOSE_DELAY = 3.0

# Platforms the StreamerSonglist API accepts for username resolution.
_PLATFORMS = ("twitch", "youtube", "kick", "none")

# The token field is never pre-filled — not with the token, and not with a
# placeholder value standing in for it (see save_settings for why the old
# bullet mask had to go). `token_set` alone tells the page one is stored.


# ---------------------------------------------------------------------------
# Songlist sync
# ---------------------------------------------------------------------------

def merge_songlist(lib: Library, songs: list) -> list:
    """
    Ensure a manifest entry per fetched song and flag which songs are currently
    in the songlist (removed songs keep their entry with in_songlist=False).
    Returns the keys of the brand-new entries created, so the caller can send
    just those to the propose job.
    """
    present_keys = set()
    for song in songs:
        title = song.get("title", "")
        artist = song.get("artist", "")
        present_keys.add(normalize_key(title, artist))

    added = []
    for key, entry in lib.all_entries().items():
        entry["in_songlist"] = key in present_keys

    for song in songs:
        title = song.get("title", "")
        artist = song.get("artist", "")
        key = normalize_key(title, artist)
        existed = key in lib.all_entries()
        entry = lib.ensure_entry(title, artist)
        entry["in_songlist"] = True
        entry["active"] = song.get("active", True)
        if not existed:
            added.append(key)
        elif entry["title"] != title or entry["artist"] != artist:
            # Same normalised key but the SSL spelling changed (case, accents,
            # punctuation, bracketed parts). Adopt the new spelling — searches
            # are built from these fields — and, unless artwork is already
            # confirmed, reset the search state so a re-propose uses it.
            entry["title"] = title
            entry["artist"] = artist
            if entry.get("status") != "confirmed":
                entry["candidates"] = []
                entry["candidate_index"] = -1
                entry["candidates_tried"] = []
                entry["searched_sources"] = []
                if entry.get("status") in ("proposed", "rejected_all"):
                    entry["status"] = None
            lib.touch(entry)
        elif (
            entry.get("status") == "proposed"
            and not entry.get("candidates")
            and not lib.has_image(entry)
        ):
            # Stuck entry from an earlier sync that reset the candidate pool
            # without resetting the status: nothing to review, and bulk propose
            # skips non-empty statuses. Hand it back to the propose flow.
            entry["candidate_index"] = -1
            entry["candidates_tried"] = []
            entry["searched_sources"] = []
            entry["status"] = None
            lib.touch(entry)

    lib.save()
    SONGLIST_SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SONGLIST_SNAPSHOT.write_text(
        json.dumps(songs, ensure_ascii=False), encoding="utf-8"
    )
    return added


# ---------------------------------------------------------------------------
# Bulk propose background job
# ---------------------------------------------------------------------------

class ProposeJob:
    def __init__(self):
        self.thread = None
        self.lock = threading.Lock()
        self.stop_flag = False
        self.running = False
        self.total = 0
        self.done = 0
        self.found = 0
        self.current = ""

    def status(self) -> dict:
        with self.lock:
            return {
                "running": self.running,
                "total": self.total,
                "done": self.done,
                "found": self.found,
                "current": self.current,
            }

    def start(self, cfg: dict, keys: list = None) -> bool:
        """
        Start a run. ``keys`` limits it to those manifest keys (a sync passes
        the songs it just added); None means every un-proposed song in the
        songlist, which is what the Find artwork button asks for.
        """
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.stop_flag = False
            self.done = 0
            self.found = 0
            self.current = ""
        self.thread = threading.Thread(target=self._run, args=(cfg, keys),
                                       daemon=True)
        self.thread.start()
        return True

    def stop(self):
        with self.lock:
            self.stop_flag = True

    def _run(self, cfg: dict, keys: list = None):
        # Fresh Library instance for the worker thread.
        lib = Library()
        candidates = (
            lib.all_entries().items() if keys is None
            else [(k, lib.get_by_key(k)) for k in keys]
        )
        targets = [
            key for key, e in candidates
            if e and e.get("in_songlist") and not e.get("status")
        ]
        with self.lock:
            self.total = len(targets)

        try:
            for i, key in enumerate(targets):
                with self.lock:
                    if self.stop_flag:
                        break
                entry = lib.get_by_key(key)
                if entry is None:
                    continue
                with self.lock:
                    self.current = f"{entry['artist']} - {entry['title']}"
                try:
                    got = artwork.propose_entry(lib, cfg, entry)
                except Exception as e:  # never let one bad song kill the run
                    log.error("Propose failed for %s: %s", key, e)
                    got = False
                with self.lock:
                    self.done = i + 1
                    if got:
                        self.found += 1
                # Throttle iTunes — but not after the final item.
                if i < len(targets) - 1:
                    time.sleep(PROPOSE_DELAY)
        finally:
            with self.lock:
                self.running = False
                self.current = ""


propose_job = ProposeJob()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def entry_view(lib: Library, key: str, entry: dict) -> dict:
    """JSON-friendly view of a manifest entry for the UI."""
    candidates = entry.get("candidates", [])
    idx = entry.get("candidate_index", -1)
    current = candidates[idx] if 0 <= idx < len(candidates) else None
    searched = entry.get("searched_sources", [])
    # The gallery shows every discovered candidate by its remote image URL, so
    # the user can pick any of them (not just cycle forward).
    candidate_views = [
        {
            "index": i,
            "url": c.get("url"),
            "source": c.get("source"),
            "album": c.get("album"),
            "similarity": c.get("similarity"),
            "blocklisted": c.get("blocklisted", False),
        }
        for i, c in enumerate(candidates)
    ]
    return {
        "key": key,
        "title": entry.get("title"),
        "artist": entry.get("artist"),
        "status": entry.get("status"),
        "source": entry.get("source"),
        "album": entry.get("album"),
        "in_songlist": entry.get("in_songlist", False),
        "has_image": lib.has_image(entry),
        "updated_at": entry.get("updated_at"),
        "candidate_index": idx,
        "candidate_count": len(candidates),
        "candidate_album": current.get("album") if current else entry.get("album"),
        "candidate_source": current.get("source") if current else entry.get("source"),
        "similarity": current.get("similarity") if current else None,
        "candidates": candidate_views,
        "sources_exhausted": all(s in searched for s in artwork.SOURCE_ORDER),
        "image_url": url_for("serve_image", key=key,
                             v=entry.get("updated_at", "")) if lib.has_image(entry) else None,
    }


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    cfg = config.load_config()
    if not cfg.get("streamersonglist_username"):
        return redirect(url_for("settings_page"))
    return redirect(url_for("songs_page"))


@app.route("/settings")
def settings_page():
    cfg = config.load_config()
    # Fingerprint of the stored token — type plus its last four characters.
    # The field itself is never pre-filled, so without this there is nothing on
    # the page that distinguishes a working credential from one silently
    # replaced by something the browser filled in.
    stored_token = (cfg.get("api_token") or "").strip()
    return render_template("settings.html", cfg=cfg,
                           platforms=_PLATFORMS,
                           token_set=bool(stored_token),
                           token_hint=stored_token[-4:] if len(stored_token) >= 8 else "",
                           token_types=songlist.TOKEN_TYPES,
                           token_type=songlist.normalize_token_type(
                               cfg.get("api_token_type")),
                           update=updates.state(cfg),
                           needs_setup=not cfg.get("streamersonglist_username"))


def _verify_saved_credential(cfg: dict, previous_token: str) -> dict:
    """
    Check the token/type pair the user just submitted, before the save can be
    reported as a success. Returns query args for the settings redirect, and
    may correct ``api_token_type`` or put back the previous token.

    This exists because on 2026-08-31 a User token was saved carrying the
    Streamer prefix. SSL rejects that pair on every endpoint it has, so the
    runtime service was rejected 300ms after this handler redirected — while
    the page said "Settings saved." and both overlays quietly went empty for
    five hours. Every fact needed to catch it was available here, at save
    time, and nothing looked.

    Outcomes:
      * the pair works                → nothing to say
      * a different prefix works      → correct the type and say so
      * SSL rejects the token         → keep the credential that was already
        stored and report SSL's own reason. A value the user never typed (a
        password manager filling the box, which is what this form's
        username+password shape invites) must not be able to replace a
        working token.
      * the check itself fails        → not a verdict: save, and say it could
        not be checked
    """
    token = (cfg.get("api_token") or "").strip()
    username = (cfg.get("streamersonglist_username") or "").strip()
    if not token or not username:
        return {}

    asked_for = songlist.normalize_token_type(cfg.get("api_token_type"))
    try:
        _, token_type = songlist.resolve_streamer_with_token_type(username, cfg=cfg)
    except (songlist.AuthRequired, songlist.Forbidden) as e:
        if previous_token and previous_token != token:
            cfg["api_token"] = previous_token
            return {"token_error": f"{e} The token you just entered was not "
                                   f"saved — the one that was already stored "
                                   f"is still in use."}
        return {"token_error": str(e)}
    except requests.RequestException as e:
        return {"token_warning": f"Saved, but the token could not be checked "
                                 f"just now: {e}"}

    if token_type != asked_for:
        cfg["api_token_type"] = token_type
        return {"token_fixed": token_type}
    return {}


@app.route("/settings", methods=["POST"])
def save_settings():
    cfg = config.load_config()
    form = request.form
    # Kept for _verify_saved_credential: a rejected new token must not be able
    # to displace a working one.
    previous_token = (cfg.get("api_token") or "").strip()

    cfg["streamersonglist_username"] = songlist.extract_username(
        form.get("streamersonglist_username", "").strip()
    )
    # API token: the field always renders empty, so a blank submit means "leave
    # it alone" rather than "clear it" — otherwise every settings save would
    # wipe the credential. Clearing is explicit.
    #
    # The field used to be pre-filled with a row of bullets and anything
    # starting with one was discarded as "that's just the mask". Pasting a new
    # token after the existing text — which is what clicking into a filled
    # password box and pasting does — therefore threw the token away and still
    # reported "Settings saved.". An empty box cannot be appended to.
    #
    # The leading-bullet strip is for pages served before this change: a tab
    # left open on the old settings form still submits the mask, and under the
    # new "anything non-blank is the token" rule that would overwrite a working
    # credential with bullets. All bullets means "keep"; bullets followed by
    # anything else is the old paste-after-the-mask case, and the real token is
    # what comes after. No SSL token starts with "•".
    token = form.get("api_token", "").strip().lstrip("•").strip()
    if form.get("api_token_clear") == "on":
        cfg["api_token"] = ""
    elif token:
        cfg["api_token"] = token
    cfg["api_token_type"] = songlist.normalize_token_type(form.get("api_token_type"))
    platform = form.get("platform", "twitch").strip().lower()
    cfg["platform"] = platform if platform in _PLATFORMS else "twitch"
    source = form.get("song_source", "streamersonglist").strip()
    cfg["song_source"] = source if source in ("streamersonglist", "file") else "streamersonglist"
    try:
        cfg["poll_interval"] = max(2, int(form.get("poll_interval", 10)))
    except (ValueError, TypeError):
        cfg["poll_interval"] = 10
    cfg["runtime_service"] = form.get("runtime_service") == "on"
    cfg["websocket_events"] = form.get("websocket_events") == "on"
    cfg["song_file"] = form.get("song_file", "").strip()
    cfg["output_image"] = form.get("output_image", "").strip()
    cfg["fallback_image"] = form.get("fallback_image", "").strip()
    cfg["itunes_country"] = form.get("itunes_country", "GB").strip() or "GB"
    cfg["lastfm_api_key"] = form.get("lastfm_api_key", "").strip()
    cfg["skip_artists"] = [
        a.strip() for a in form.get("skip_artists", "").split(",") if a.strip()
    ]
    try:
        cfg["image_size"] = int(form.get("image_size", 640))
    except ValueError:
        cfg["image_size"] = 640

    ref = cfg.setdefault("reflection", {})
    ref["enabled"] = form.get("reflection_enabled") == "on"
    for fld, cast in (("height", float), ("opacity", float), ("gap", int),
                      ("perspective", float), ("fade_power", float)):
        try:
            ref[fld] = cast(form.get(f"reflection_{fld}", ref.get(fld)))
        except (ValueError, TypeError):
            pass

    cfg.setdefault("updates", {})["check_enabled"] = (
        form.get("updates_check_enabled") == "on"
    )

    # Host/token/platform may have changed — re-probe which API is live. This
    # comes before the check below so it can't answer from a cached verdict.
    songlist.reset_backend()
    notice = _verify_saved_credential(cfg, previous_token)

    config.save_config(cfg)
    # Pick up the new username/source/interval without an app restart.
    if _runtime_wanted(cfg):
        runtime.service.restart(cfg)
    else:
        runtime.service.stop()
    return redirect(url_for("settings_page", saved=1, **notice))


@app.route("/songs")
def songs_page():
    cfg = config.load_config()
    if not cfg.get("streamersonglist_username"):
        return redirect(url_for("settings_page"))
    return render_template("songs.html", cfg=cfg)


@app.route("/review")
def review_page():
    if not config.load_config().get("streamersonglist_username"):
        return redirect(url_for("settings_page"))
    return render_template("review.html", mode="review",
                           title="Review proposed artwork")


@app.route("/queue")
def queue_page():
    if not config.load_config().get("streamersonglist_username"):
        return redirect(url_for("settings_page"))
    return render_template("review.html", mode="queue",
                           title="Review queue (live grabs)")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

# Native file dialogs: the dashboard only ever runs on the streamer's own
# machine (Flask binds 127.0.0.1), so the server can pop a Windows file picker
# and hand the chosen path back to the page. Tkinter must own the main thread
# of its interpreter, which Flask request threads are not — so each dialog runs
# in a short-lived subprocess. A lock stops double-clicks stacking dialogs.
_BROWSE_LOCK = threading.Lock()

_BROWSE_SCRIPT = """
import sys, tkinter, tkinter.filedialog as fd
root = tkinter.Tk(); root.withdraw(); root.attributes("-topmost", True)
kind, initial, filetypes = sys.argv[1], sys.argv[2], sys.argv[3]
types = [tuple(t.split(":", 1)) for t in filetypes.split(";")] if filetypes else []
opts = {"parent": root, "initialfile": initial, "filetypes": types or [("All files", "*.*")]}
if kind == "save":
    path = fd.asksaveasfilename(defaultextension=".png", **opts)
else:
    path = fd.askopenfilename(**opts)
print(path or "")
"""


@app.route("/api/browse", methods=["POST"])
def api_browse():
    data = request.json or {}
    kind = "save" if data.get("kind") == "save" else "open"
    initial = str(data.get("initialfile", ""))
    filetypes = str(data.get("filetypes", ""))
    if not _BROWSE_LOCK.acquire(blocking=False):
        return jsonify({"ok": False, "error": "A file dialog is already open"}), 409
    try:
        result = subprocess.run(
            [sys.executable, "-c", _BROWSE_SCRIPT, kind, initial, filetypes],
            capture_output=True, text=True, timeout=300,
        )
        path = result.stdout.strip()
        if result.returncode != 0:
            return jsonify({"ok": False, "error": "File dialog failed"}), 500
        return jsonify({"ok": True, "path": path})
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "File dialog timed out"}), 500
    finally:
        _BROWSE_LOCK.release()


@app.route("/api/test-connection", methods=["POST"])
def api_test_connection():
    """
    Test the API credentials/username as currently typed into the settings
    form — the token doesn't have to be saved first, which is what makes this
    usable for "did I paste it right?".
    """
    body = request.json or {}
    cfg = config.load_config()
    # Field values from the unsaved form win; a blank token falls back to the
    # stored one (the form never renders the stored token, masked or otherwise).
    if body.get("token"):
        cfg["api_token"] = body["token"].strip()
    if body.get("platform"):
        cfg["platform"] = body["platform"].strip()
    if body.get("token_type"):
        cfg["api_token_type"] = songlist.normalize_token_type(body["token_type"])
    asked_for = songlist.normalize_token_type(cfg.get("api_token_type"))

    try:
        # Retries the other prefixes if this one is rejected, because nothing
        # about a token says which kind it is. The type that worked goes back
        # to the page so the select can correct itself before the user saves —
        # the test still only reports, it never writes config.json.
        streamer, token_type = songlist.resolve_streamer_with_token_type(
            body.get("username", ""), cfg=cfg)
        cfg["api_token_type"] = token_type
        sid = streamer.get("id")
        return jsonify({"ok": True, "id": sid,
                        "name": streamer.get("name"),
                        "avatar": streamer.get("avatar"),
                        "backend": streamer.get("backend"),
                        "token_type": token_type,
                        "token_type_changed": token_type != asked_for,
                        "song_count": songlist.fetch_song_count(sid, cfg=cfg)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/sync", methods=["POST"])
def api_sync():
    cfg = config.load_config()
    username = cfg.get("streamersonglist_username")
    if not username:
        return jsonify({"ok": False, "error": "No username configured"}), 400
    try:
        streamer = songlist.resolve_streamer(username, cfg=cfg)
        songs = songlist.fetch_all_songs(streamer["id"], cfg=cfg)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    # An empty songlist is never a legitimate sync: merge_songlist clears
    # in_songlist on every entry before re-adding, so a 200 carrying zero songs
    # (wrong streamer, or an API shim answering for an unknown channel) would
    # silently flip the whole library out of the songlist.
    if not songs:
        return jsonify({
            "ok": False,
            "error": ("StreamerSonglist returned an empty songlist — refusing "
                      "to sync. Check the username in Settings and try again."),
        }), 400

    lib = Library()
    added = merge_songlist(lib, songs)
    # A newly added song is created empty and only the propose flow fills it in,
    # so without this it would sit invisible to the review queue until someone
    # thought to press Find artwork. Scoped to the new keys: a sync never
    # re-sweeps the whole library behind the user's back.
    proposing = bool(added) and propose_job.start(cfg, keys=added)
    return jsonify({"ok": True, "total": len(songs), "added": len(added),
                    "proposing": proposing})


@app.route("/api/entries")
def api_entries():
    """List entries for the songs table or a review filter."""
    lib = Library()
    flt = request.args.get("filter", "songlist")
    rows = []
    for key, entry in lib.all_entries().items():
        status = entry.get("status")
        if flt == "songlist" and not entry.get("in_songlist"):
            continue
        if flt == "review" and status not in (STATUS_PROPOSED, STATUS_UNVERIFIED):
            continue
        if (flt == "review" and status == STATUS_PROPOSED
                and entry.get("in_songlist") is False):
            # Proposed entries only exist via sync; in_songlist False means the
            # song was renamed/removed in SSL — don't resurface it for review.
            continue
        if flt == "queue" and status != STATUS_UNVERIFIED:
            continue
        rows.append(entry_view(lib, key, entry))
    # Stable, human-friendly order.
    rows.sort(key=lambda r: ((r["artist"] or "").lower(),
                             (r["title"] or "").lower()))
    return jsonify({"entries": rows})


@app.route("/api/entry")
def api_entry():
    lib = Library()
    key = request.args.get("key", "")
    entry = lib.get_by_key(key)
    if entry is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify(entry_view(lib, key, entry))


@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    lib = Library()
    key = (request.json or {}).get("key", "")
    entry = lib.get_by_key(key)
    if entry is None or not lib.has_image(entry):
        return jsonify({"ok": False, "error": "no image to confirm"}), 400
    artwork.confirm_entry(lib, entry)
    return jsonify({"ok": True, **entry_view(lib, key, entry)})


@app.route("/api/select", methods=["POST"])
def api_select():
    """Pick a specific candidate from the gallery (by index) as the artwork."""
    lib = Library()
    body = request.json or {}
    key = body.get("key", "")
    entry = lib.get_by_key(key)
    if entry is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    try:
        index = int(body.get("index"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "bad index"}), 400
    try:
        artwork.select_candidate(lib, entry, index)
    except IndexError:
        return jsonify({"ok": False, "error": "index out of range"}), 400
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, **entry_view(lib, key, entry)})


@app.route("/api/more", methods=["POST"])
def api_more():
    """Pull the next source(s) of candidates into the gallery on demand."""
    lib = Library()
    cfg = config.load_config()
    key = (request.json or {}).get("key", "")
    entry = lib.get_by_key(key)
    if entry is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    added = artwork.find_more_candidates(lib, cfg, entry)
    return jsonify({"ok": True, "added": added, **entry_view(lib, key, entry)})


@app.route("/api/propose/one", methods=["POST"])
def api_propose_one():
    """
    Search + adopt artwork for one song that has never been through the propose
    flow. The review card calls this on arrival, so a song opened straight from
    the songs list (or one the sync's propose run hasn't reached yet) shows art
    instead of an empty card. A no-op for an entry that already has a status or
    an image, so it is safe to call on every card.
    """
    lib = Library()
    cfg = config.load_config()
    key = (request.json or {}).get("key", "")
    entry = lib.get_by_key(key)
    if entry is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    artwork.ensure_proposal(lib, cfg, entry)
    return jsonify({"ok": True, **entry_view(lib, key, entry)})


@app.route("/api/upload", methods=["POST"])
def api_upload():
    lib = Library()
    key = request.form.get("key", "")
    entry = lib.get_by_key(key)
    if entry is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"ok": False, "error": "no file"}), 400
    try:
        artwork.store_upload(lib, entry, file.read())
    except Exception as e:
        return jsonify({"ok": False, "error": f"bad image: {e}"}), 400
    return jsonify({"ok": True, **entry_view(lib, key, entry)})


@app.route("/api/propose/start", methods=["POST"])
def api_propose_start():
    cfg = config.load_config()
    started = propose_job.start(cfg)
    return jsonify({"ok": True, "started": started, **propose_job.status()})


@app.route("/api/propose/status")
def api_propose_status():
    return jsonify(propose_job.status())


@app.route("/api/propose/stop", methods=["POST"])
def api_propose_stop():
    propose_job.stop()
    return jsonify({"ok": True})


@app.route("/img")
def serve_image():
    lib = Library()
    key = request.args.get("key", "")
    entry = lib.get_by_key(key)
    if entry is None:
        return "not found", 404
    path = lib.image_path(entry)
    if not path or not path.exists():
        return "no image", 404
    return send_file(path, mimetype="image/png")


# ---------------------------------------------------------------------------
# Update check
# ---------------------------------------------------------------------------

@app.route("/api/update")
def api_update():
    """Cached answer only — a page load must never wait on GitHub."""
    return jsonify(updates.state(config.load_config()))


@app.route("/api/update/check", methods=["POST"])
def api_update_check():
    """
    Force a fresh check ("Check now" on Settings). This is the one path that
    surfaces errors, because here the user asked for the answer.
    """
    updates.check()
    return jsonify(updates.state(config.load_config()))


@app.route("/api/update/skip", methods=["POST"])
def api_update_skip():
    """
    Dismiss the current release. Persisted, so it stays dismissed across
    restarts — but only for this version, so the next release still shows.
    Posting with no version clears the dismissal instead.
    """
    cfg = config.load_config()
    version = (request.json or {}).get("version", "") if request.is_json else ""
    cfg.setdefault("updates", {})["skipped_version"] = str(version).strip()
    config.save_config(cfg)
    return jsonify(updates.state(cfg))


# ---------------------------------------------------------------------------
# Runtime service + queue overlay
# ---------------------------------------------------------------------------

@app.route("/api/runtime/status")
def api_runtime_status():
    return jsonify(runtime.service.status())


# Statuses whose stored image the overlay will show (rejected_all means the
# user turned everything down).
_OVERLAY_STATUSES = artwork.USABLE_STATUSES

_OVERLAY_PRESETS = ("dark", "light", "minimal", "glass")
_OVERLAY_ANIMATIONS = ("slide", "fade", "none")
_OVERLAY_SPEEDS = ("normal", "fast")

# Query-param overrides so one config can drive multiple differently-styled
# browser sources: bools are 0/1, e.g. /overlay/queue?max=3&current=0&preset=glass
_OVERLAY_BOOL_KEYS = {
    "current": "include_current",
    "art": "show_artwork",
    "title": "show_title",
    "artist": "show_artist",
    "requester": "show_requester",
    "position": "show_position",
    "promo": "show_promo",
}


_CURRENT_LAYOUTS = ("horizontal", "vertical")

_CURRENT_BOOL_KEYS = {
    "art": "show_artwork",
    "title": "show_title",
    "artist": "show_artist",
    "requester": "show_requester",
    "label": "show_label",
    "hide_empty": "hide_when_empty",
    "promo": "show_promo",
}


def _merge_overlay_options(cfg: dict, args, section: str,
                           bool_keys: dict, int_specs) -> dict:
    """Effective options for one overlay: defaults + config + query overrides."""
    opts = dict(config.DEFAULT_CONFIG[section])
    opts.update(cfg.get(section, {}))

    for param, key in bool_keys.items():
        if param in args:
            opts[key] = args.get(param) not in ("0", "false", "no", "")
    for param, key, lo, hi in int_specs:
        if param in args:
            try:
                opts[key] = min(hi, max(lo, int(args.get(param))))
            except (TypeError, ValueError):
                pass
    if args.get("preset") in _OVERLAY_PRESETS:
        opts["preset"] = args.get("preset")
    if args.get("anim") in _OVERLAY_ANIMATIONS:
        opts["animation"] = args.get("anim")
    if args.get("speed") in _OVERLAY_SPEEDS:
        opts["anim_speed"] = args.get("speed")
    if args.get("accent"):
        opts["accent"] = args.get("accent")
    return opts


def overlay_options(cfg: dict, args) -> dict:
    """Effective queue overlay options: config defaults + query overrides."""
    return _merge_overlay_options(
        cfg, args, "overlay", _OVERLAY_BOOL_KEYS,
        (("max", "max_songs", 1, 20),
         ("font_size", "font_size", 8, 72),
         ("art_size", "art_size", 16, 300),
         ("row_gap", "row_gap", 0, 100)))


def overlay_current_options(cfg: dict, args) -> dict:
    """Effective now-playing card options: config defaults + query overrides."""
    opts = _merge_overlay_options(
        cfg, args, "overlay_current", _CURRENT_BOOL_KEYS,
        (("font_size", "font_size", 8, 96),
         ("art_size", "art_size", 32, 600)))
    if args.get("layout") in _CURRENT_LAYOUTS:
        opts["layout"] = args.get("layout")
    if args.get("label_text"):
        opts["label_text"] = args.get("label_text")
    return opts


_WALL_FILTERS = ("songlist", "all")
_WALL_ORDERS = library.WALL_ORDERS
_WALL_AXES = ("row", "column")

_WALL_BOOL_KEYS = {
    "dedupe": "dedupe",
    "drift": "drift",
    "assemble": "assemble",
    "breathe": "breathe",
}

# (query param, config key, min, max). ``columns`` is capped at 12 because past
# that a normal library has over half of itself on screen at once, which leaves
# the swap nothing to draw on.
_WALL_INT_SPECS = (
    ("cols", "columns", 2, 12),
    ("gap", "gap", 0, 64),
    ("radius", "radius", 0, 64),
    ("swap", "swap_interval", 0, 3600),
    ("fade", "swap_fade", 0, 10000),
    ("hero", "hero_interval", 0, 3600),
)


def wall_options(cfg: dict, args) -> dict:
    """
    Effective art wall options: defaults + config + query overrides.

    Deliberately not routed through _merge_overlay_options: the wall has no
    preset, animation mode or accent colour, and that helper would graft those
    keys onto the section where nothing reads them.
    """
    opts = dict(config.DEFAULT_CONFIG["wall"])
    opts.update(cfg.get("wall", {}))

    for param, key in _WALL_BOOL_KEYS.items():
        if param in args:
            opts[key] = args.get(param) not in ("0", "false", "no", "")
    for param, key, lo, hi in _WALL_INT_SPECS:
        if param in args:
            try:
                opts[key] = min(hi, max(lo, int(args.get(param))))
            except (TypeError, ValueError):
                pass
    if args.get("filter") in _WALL_FILTERS:
        opts["filter"] = args.get("filter")
    if args.get("order") in _WALL_ORDERS:
        opts["order"] = args.get("order")
    if args.get("axis") in _WALL_AXES:
        opts["sort_axis"] = args.get("axis")
    # An unknown order from a hand-edited config would otherwise fall through
    # to "no sorting at all", which looks like the setting being ignored.
    if opts.get("order") not in _WALL_ORDERS:
        opts["order"] = "shuffle"
    if opts.get("sort_axis") not in _WALL_AXES:
        opts["sort_axis"] = "row"
    # ``source`` is reserved for a future live-queue mode. Coerce anything else
    # back rather than letting a stale config or a typo render an empty wall.
    if opts.get("source") != "library":
        opts["source"] = "library"
    return opts


def wall_pool(cfg: dict, opts: dict, lib=None) -> list:
    """The eligible tiles for these options, in no particular order."""
    return (lib or Library()).tile_pool(
        songlist_only=(opts.get("filter") == "songlist"),
        exclude=opts.get("exclude") or (),
        dedupe=bool(opts.get("dedupe", True)),
    )


def wall_tiles(cfg: dict, opts: dict) -> list:
    """The tile pool arranged into the configured order."""
    lib = Library()
    return library.order_tiles(lib, wall_pool(cfg, opts, lib),
                               opts.get("order", "shuffle"))


def _resolve_art(lib: Library, title: str, artist: str) -> str:
    """URL of the stored artwork for a song, or the overlay fallback image."""
    entry = lib.get(title, artist)
    if (entry and entry.get("status") in _OVERLAY_STATUSES
            and lib.has_image(entry)):
        return url_for("serve_image", key=normalize_key(title, artist),
                       v=entry.get("updated_at", ""))
    return url_for("overlay_fallback_image")


# Avatar of the configured streamer, resolved at most once per username so the
# promo image cannot turn into an API call per overlay render.
_AVATAR_CACHE = {"key": None, "url": None}


def _streamer_avatar(cfg: dict):
    """
    The streamer's avatar URL (v2 API only — v1 hands out no avatar), from the
    runtime service if it is up, else resolved and cached here.
    """
    url = runtime.service.status().get("avatar")
    if url:
        return url
    username = (cfg.get("streamersonglist_username") or "").strip()
    if not username:
        return None
    if _AVATAR_CACHE["key"] == username:
        return _AVATAR_CACHE["url"]
    try:
        streamer = songlist.resolve_streamer(username, cfg=cfg)
    except requests.RequestException:
        # Covers AuthRequired too, and none of it is fatal here — the promo
        # card just falls back to the fallback image.
        return None
    _AVATAR_CACHE.update(key=username, url=streamer.get("avatar"))
    return _AVATAR_CACHE["url"]


def _promo_image_path(cfg: dict, state: str) -> str:
    """
    The configured promo image for one card state. The closed card falls back
    to the open card's image, so a second graphic is optional.
    """
    promo = cfg.get("promo", {})
    path = (promo.get("closed_image") or "").strip() if state == "closed" else ""
    return path or (promo.get("image") or "").strip()


def _promo_version(cfg: dict, state: str = "open") -> str:
    """Cache-buster for the promo image, so swapping the file shows up in OBS."""
    path = _promo_image_path(cfg, state)
    if not path:
        return "avatar"
    try:
        return str(int(Path(path).stat().st_mtime))
    except OSError:
        return "0"


def _promo_item(cfg: dict, opts: dict, args):
    """
    The synthetic queue item for the empty-queue promo card, or None when the
    promo is off for this overlay, the queue has not been empty long enough, or
    requests are closed and no closed-card message is set.

    The fixed ``id`` is load-bearing: both overlays key their animations on it,
    so a promo that stays put across polls must keep one identity, and a real
    request landing must read as a different one — which is exactly what makes
    the swap animate like any other queue change. The two card states share it
    deliberately: open→closed restyles in place rather than animating out and
    back in.

    ``?promo_demo=1`` skips the empty-for delay; ``=open`` / ``=closed`` also
    pin the card state, for styling both without touching StreamerSonglist.
    """
    if not opts.get("show_promo"):
        return None
    promo = cfg.get("promo", {})

    demo = (args.get("promo_demo") or "").strip().lower()
    if demo in ("0", "false", "no"):
        demo = ""
    if not demo:
        try:
            delay = min(300, max(0, int(promo.get("delay_seconds", 8))))
        except (TypeError, ValueError):
            delay = 8
        # None means "not empty", or "no poll has landed yet" — and not having
        # looked is not the same as there being nothing there.
        empty_for = runtime.service.empty_for()
        if empty_for is None or empty_for < delay:
            return None

    # Only a definite "closed" picks the closed card. Unknown — v1, no username,
    # or a check that has never succeeded — reads as open: losing the card on a
    # failed API read is worse than showing it a minute after requests shut.
    if demo in ("open", "closed"):
        active = demo == "open"
    else:
        active = runtime.service.requests_active() is not False

    state = "open" if active else "closed"
    text = (promo.get("text" if active else "closed_text") or "").strip()
    subtext = (promo.get("subtext" if active else "closed_subtext") or "").strip()
    # A blank closed message is how you say "show nothing while requests are
    # shut" — there is no separate checkbox for it.
    if not active and not text:
        return None

    return {
        "id": "promo",
        "promo": True,
        "position": None,
        "title": text,
        "artist": subtext,
        "requester": None,
        "art": url_for("overlay_promo_image", state=state,
                       v=_promo_version(cfg, state)),
    }


@app.route("/overlay/promo.png")
def overlay_promo_image():
    """
    Art for the promo card: the configured image, else the streamer's avatar,
    else whatever the fallback image would be.

    This is a route rather than a path handed to the page because the overlays
    are served over http, and a browser source will not load a file:// image
    from an http page.
    """
    cfg = config.load_config()
    path = _promo_image_path(cfg, request.args.get("state", "open"))
    if path and Path(path).exists():
        return send_file(path)
    avatar = _streamer_avatar(cfg)
    if avatar:
        # Redirect rather than proxy: the avatar URL is public and OBS follows
        # it, so there is nothing to gain from streaming the bytes twice.
        return redirect(avatar)
    return overlay_fallback_image()


@app.route("/overlay/queue.json")
def overlay_queue_json():
    cfg = config.load_config()
    opts = overlay_options(cfg, request.args)

    # `upcoming` excludes the now-playing song on both backends, so the
    # now-playing card is prepended rather than the list being sliced.
    playing, upcoming = runtime.service.get_queue(cfg)
    items = list(upcoming)
    if opts["include_current"] and playing is not None:
        items.insert(0, playing)
    items = items[: opts["max_songs"]]

    lib = Library()
    rows = []
    # Display numbering, not the API's: v2 numbers the now-playing song 0 and
    # restarts the upcoming queue at 1. Excluding the current song still starts
    # the list at 2, exactly as it reads today.
    for pos, item in enumerate(items, start=1 if opts["include_current"] else 2):
        view = songlist.queue_item_view(item, pos)
        if view is None:
            continue
        view["art"] = _resolve_art(lib, view["title"], view["artist"])
        rows.append(view)

    # Nothing to show: offer the promo card instead of an empty overlay. Note
    # this is "this overlay has no rows", so with the current song excluded a
    # last song playing out with nothing behind it counts as empty here — the
    # now-playing card beside it still shows the real song.
    if not rows:
        promo = _promo_item(cfg, opts, request.args)
        if promo is not None:
            rows.append(promo)

    # The overlay page ignores this (an OBS source must never render an error
    # on stream) — it is here so opening the URL says why the list is empty.
    error, _kind = runtime.service.error()
    return jsonify({"items": rows, "options": opts, "error": error})


@app.route("/overlay/fallback.png")
def overlay_fallback_image():
    """The configured fallback image, or a plain dark placeholder square."""
    cfg = config.load_config()
    fallback = cfg.get("fallback_image")
    if fallback and Path(fallback).exists():
        return send_file(fallback)
    buf = io.BytesIO()
    Image.new("RGBA", (300, 300), (30, 30, 30, 255)).save(buf, "PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png")


@app.route("/overlay/queue")
def overlay_queue_page():
    cfg = config.load_config()
    opts = overlay_options(cfg, request.args)
    return render_template("overlay.html", opts=opts,
                           qs=request.query_string.decode("utf-8"))


@app.route("/overlay/current.json")
def overlay_current_json():
    cfg = config.load_config()
    opts = overlay_current_options(cfg, request.args)
    playing, upcoming = runtime.service.get_queue(cfg)
    # The now-playing slot, falling back to the head of the upcoming queue when
    # nothing has been promoted into it.
    item = playing if playing is not None else (upcoming[0] if upcoming else None)
    view = songlist.queue_item_view(item, 1) if item is not None else None
    if view is not None:
        view["art"] = _resolve_art(Library(), view["title"], view["artist"])
    else:
        # Promo card wins over hide_when_empty: showing something is the point.
        view = _promo_item(cfg, opts, request.args)
    error, _kind = runtime.service.error()
    return jsonify({"item": view, "options": opts, "error": error})


@app.route("/overlay/current")
def overlay_current_page():
    cfg = config.load_config()
    opts = overlay_current_options(cfg, request.args)
    return render_template("overlay_current.html", opts=opts,
                           qs=request.query_string.decode("utf-8"))


# ---------------------------------------------------------------------------
# Art wall
# ---------------------------------------------------------------------------

@app.route("/overlay/wall")
def overlay_wall_page():
    cfg = config.load_config()
    opts = wall_options(cfg, request.args)
    return render_template("overlay_wall.html", opts=opts,
                           pick=request.args.get("pick") == "1",
                           qs=request.query_string.decode("utf-8"))


@app.route("/overlay/wall.json")
def overlay_wall_json():
    """
    The tile pool, fetched once on page load and never polled — the wall reads
    library data, not the queue, so there is nothing to poll for. That makes it
    the cheapest overlay on the server and the most expensive in the browser,
    the exact inverse of the queue and now-playing cards.
    """
    cfg = config.load_config()
    opts = wall_options(cfg, request.args)
    return jsonify({"tiles": wall_tiles(cfg, opts), "options": opts})


@app.route("/overlay/wall/tile")
def overlay_wall_tile():
    """
    One thumbnail, addressed by image content hash rather than by library key,
    so identical artwork shares a single URL and therefore a single browser
    cache entry. Cached immutably: the hash IS the version, so changed artwork
    is a different URL and a stale image cannot be served.
    """
    digest = (request.args.get("h") or "").strip().lower()
    if len(digest) != 32 or not all(c in "0123456789abcdef" for c in digest):
        return "bad hash", 400
    try:
        requested = int(request.args.get("s", 512))
    except (TypeError, ValueError):
        requested = 512
    size = imaging.snap_thumb_size(max(1, requested))

    thumb = imaging.thumbnail_path(digest, size)
    if not thumb.exists():
        source = Library().path_for_hash(digest)
        if source is None:
            return "not found", 404
        thumb = imaging.ensure_thumbnail(source, digest, size)
        if thumb is None:
            return "unreadable", 404

    resp = send_file(thumb, mimetype="image/webp")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/overlay/wall/exclude", methods=["POST"])
def overlay_wall_exclude():
    """
    Hide or restore one cover. Clicking a tile in the settings preview is the
    whole curation story — an exclude list rather than a picker, because the
    real case is "that one cover is wrong", not "let me choose all 128".
    """
    data = request.json or {}
    action = data.get("action")
    cfg = config.load_config()
    opts = cfg.setdefault("wall", {})

    if action == "show_all":
        opts["exclude"] = []
        config.save_config(cfg)
        return jsonify({"ok": True, "excluded": 0})

    digest = str(data.get("hash", "")).strip().lower()
    if len(digest) != 32 or not all(c in "0123456789abcdef" for c in digest):
        return jsonify({"ok": False, "error": "bad hash"}), 400

    excluded = [h for h in (opts.get("exclude") or []) if h != digest]
    if action != "show":
        excluded.append(digest)
    opts["exclude"] = excluded
    config.save_config(cfg)
    return jsonify({"ok": True, "excluded": len(excluded)})


@app.route("/overlay")
def overlay_settings_page():
    cfg = config.load_config()
    if not cfg.get("streamersonglist_username"):
        return redirect(url_for("settings_page"))
    opts = overlay_options(cfg, {})
    cur_opts = overlay_current_options(cfg, {})
    overlay_url = url_for("overlay_queue_page", _external=True)
    current_url = url_for("overlay_current_page", _external=True)
    loader_dir = Path(__file__).resolve().parent.parent / "obs"

    # The wall's tile count drives the headroom advice in its panel. Hashing the
    # library the first time costs a second or so; it is memoised per file after
    # that, and this is a page load, never a request the overlays wait on.
    wall_opts = wall_options(cfg, {})
    # Only the count is needed here, so skip the ordering pass — colour sorts
    # profile every cover, which is wasted work for a number.
    wall_count = len(wall_pool(cfg, wall_opts))

    # Hidden covers are shown back as thumbnails so they can be restored one at
    # a time. They are excluded from the pool by definition, so look them up
    # against the unfiltered library rather than the pool.
    wall_hidden = []
    excluded = wall_opts.get("exclude") or []
    if excluded:
        everything = {
            tile["hash"]: tile
            for tile in Library().tile_pool(songlist_only=False, dedupe=False)
        }
        for digest in excluded:
            found = everything.get(digest)
            wall_hidden.append({
                "hash": digest,
                "name": found["songs"][0] if found else "No longer in your library",
                "known": found is not None,
            })

    return render_template("overlay_settings.html", opts=opts, cfg=cfg,
                           queue_loader=str(loader_dir / "overlay_queue.html"),
                           current_loader=str(loader_dir / "overlay_current.html"),
                           cur_opts=cur_opts, current_url=current_url,
                           promo=cfg.get("promo", {}),
                           has_avatar=bool(_streamer_avatar(cfg)),
                           layouts=_CURRENT_LAYOUTS,
                           wall_opts=wall_opts, wall_pool=wall_count,
                           wall_hidden=wall_hidden,
                           wall_url=url_for("overlay_wall_page", _external=True),
                           wall_loader=str(loader_dir / "overlay_wall.html"),
                           wall_filters=_WALL_FILTERS, wall_orders=_WALL_ORDERS,
                           wall_axes=_WALL_AXES,
                           overlay_url=overlay_url, presets=_OVERLAY_PRESETS,
                           animations=_OVERLAY_ANIMATIONS, speeds=_OVERLAY_SPEEDS)


@app.route("/overlay", methods=["POST"])
def save_overlay_settings():
    cfg = config.load_config()
    form = request.form
    opts = cfg.setdefault("overlay", {})

    for key in _OVERLAY_BOOL_KEYS.values():
        opts[key] = form.get(key) == "on"
    try:
        opts["max_songs"] = min(20, max(1, int(form.get("max_songs", 5))))
    except (TypeError, ValueError):
        opts["max_songs"] = 5
    for key, default, lo, hi in (("font_size", 20, 8, 72),
                                 ("art_size", 56, 16, 300),
                                 ("row_gap", 10, 0, 100)):
        try:
            opts[key] = min(hi, max(lo, int(form.get(key, default))))
        except (TypeError, ValueError):
            opts[key] = default
    preset = form.get("preset", "dark")
    opts["preset"] = preset if preset in _OVERLAY_PRESETS else "dark"
    animation = form.get("animation", "slide")
    opts["animation"] = animation if animation in _OVERLAY_ANIMATIONS else "slide"
    speed = form.get("anim_speed", "normal")
    opts["anim_speed"] = speed if speed in _OVERLAY_SPEEDS else "normal"
    opts["accent"] = form.get("accent", "#4da3ff").strip() or "#4da3ff"

    config.save_config(cfg)
    return redirect(url_for("overlay_settings_page", saved=1))


@app.route("/overlay/current", methods=["POST"])
def save_overlay_current_settings():
    cfg = config.load_config()
    form = request.form
    opts = cfg.setdefault("overlay_current", {})

    for key in _CURRENT_BOOL_KEYS.values():
        opts[key] = form.get(key) == "on"
    for key, default, lo, hi in (("font_size", 26, 8, 96),
                                 ("art_size", 160, 32, 600)):
        try:
            opts[key] = min(hi, max(lo, int(form.get(key, default))))
        except (TypeError, ValueError):
            opts[key] = default
    layout = form.get("layout", "horizontal")
    opts["layout"] = layout if layout in _CURRENT_LAYOUTS else "horizontal"
    preset = form.get("preset", "dark")
    opts["preset"] = preset if preset in _OVERLAY_PRESETS else "dark"
    animation = form.get("animation", "fade")
    opts["animation"] = animation if animation in _OVERLAY_ANIMATIONS else "fade"
    speed = form.get("anim_speed", "normal")
    opts["anim_speed"] = speed if speed in _OVERLAY_SPEEDS else "normal"
    opts["accent"] = form.get("accent", "#4da3ff").strip() or "#4da3ff"
    opts["label_text"] = form.get("label_text", "").strip() or "Now playing"

    config.save_config(cfg)
    return redirect(url_for("overlay_settings_page", saved="current"))


@app.route("/overlay/promo", methods=["POST"])
def save_overlay_promo_settings():
    """
    The promo card's content. Shared by both overlays — only whether each one
    shows it (``show_promo``) is per-overlay, saved with that overlay's form.
    """
    cfg = config.load_config()
    form = request.form
    opts = cfg.setdefault("promo", {})

    for key in ("text", "subtext", "image",
                "closed_text", "closed_subtext", "closed_image"):
        opts[key] = form.get(key, "").strip()
    try:
        opts["delay_seconds"] = min(300, max(0, int(form.get("delay_seconds", 8))))
    except (TypeError, ValueError):
        opts["delay_seconds"] = 8

    config.save_config(cfg)
    return redirect(url_for("overlay_settings_page", saved="promo") + "#promo")


@app.route("/overlay/wall", methods=["POST"])
def save_overlay_wall_settings():
    cfg = config.load_config()
    form = request.form
    opts = cfg.setdefault("wall", {})
    defaults = config.DEFAULT_CONFIG["wall"]

    for key in _WALL_BOOL_KEYS.values():
        opts[key] = form.get(key) == "on"
    for _param, key, lo, hi in _WALL_INT_SPECS:
        try:
            opts[key] = min(hi, max(lo, int(form.get(key, defaults[key]))))
        except (TypeError, ValueError):
            opts[key] = defaults[key]
    filt = form.get("filter", "songlist")
    opts["filter"] = filt if filt in _WALL_FILTERS else "songlist"
    order = form.get("order", "shuffle")
    opts["order"] = order if order in _WALL_ORDERS else "shuffle"
    axis = form.get("sort_axis", "row")
    opts["sort_axis"] = axis if axis in _WALL_AXES else "row"
    # ``exclude`` is deliberately absent from this form: hiding and restoring
    # happen immediately via /overlay/wall/exclude, so saving these settings
    # must leave the list exactly as it found it.

    config.save_config(cfg)
    return redirect(url_for("overlay_settings_page", saved="wall") + "#wall")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _runtime_wanted(cfg: dict) -> bool:
    """
    Whether the runtime service should run: enabled, and there's a song source
    to drive it (a username for queue mode; file mode works without one — the
    overlay just stays empty).
    """
    if not cfg.get("runtime_service", True):
        return False
    return bool(cfg.get("streamersonglist_username")) or \
        cfg.get("song_source", "streamersonglist") == "file"


def _port_owner(host: str, port: int):
    """
    What is already listening on ``host:port``: ``"dashboard"`` (another
    instance of this app), ``"other"`` (some other server), or None (free).

    Connecting is the only check that works here. A bind test is not enough on
    Windows: Werkzeug sets SO_REUSEADDR, so a second dashboard binds an
    already-bound port *successfully* and the two instances then share it — the
    older one keeps answering with whatever code it started with while the new
    one looks perfectly healthy, and since the log path comes from ``__file__``
    they both write the same log. That cost a real debugging session on
    2026-08-17, with a stale instance serving a fixed overlay's old output.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((host, port)) != 0:
            return None
    try:
        # Ours answers this with JSON; anything else identifies as "other".
        resp = requests.get(f"http://{host}:{port}/api/runtime/status", timeout=2)
        if resp.ok and "running" in resp.json():
            return "dashboard"
    except (requests.RequestException, ValueError):
        pass
    return "other"


def run(host="127.0.0.1", port=5050, open_browser=True):
    artwork.setup_logging()

    # Before the runtime service starts — a second service would poll SSL and
    # fight the first one for the output PNG.
    owner = _port_owner(host, port)
    if owner is not None:
        url = f"http://{host}:{port}/"
        if owner == "dashboard":
            message = (f"The dashboard is already running at {url}\n"
                       f"Open that, or close the other window first if you "
                       f"meant to restart it.")
        else:
            message = (f"Port {port} is already in use by another program, so "
                       f"the dashboard can't start.")
        message += (f"\nTo find what is holding the port:  "
                    f"netstat -ano | findstr :{port}")
        log.error("Refusing to start: %s", message.replace("\n", " "))
        print(f"\n{message}\n")
        raise SystemExit(1)

    first_run = config.ensure_config()
    cfg = config.load_config()
    updates.start_background_check(cfg)
    if _runtime_wanted(cfg):
        runtime.service.start(cfg)
    if open_browser:
        target = "settings" if first_run else ""
        threading.Timer(
            1.0, lambda: webbrowser.open(f"http://{host}:{port}/{target}")
        ).start()
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    run()
