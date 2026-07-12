"""
Flask dashboard: settings -> songlist sync -> bulk propose -> review -> review queue.

Local tool only — binds 127.0.0.1. The bulk-propose run is long (185+ throttled
iTunes calls) so it runs in a background thread with a polled progress endpoint;
request handlers never block on it.
"""

import io
import json
import logging
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

from flask import (
    Flask, jsonify, redirect, render_template, request, send_file, url_for,
)
from PIL import Image

from . import artwork, config, runtime, songlist
from .library import (
    Library, STATUS_CONFIRMED, STATUS_PROPOSED, STATUS_UNVERIFIED,
    normalize_key,
)

log = logging.getLogger("artwork_fetcher")

app = Flask(__name__)

# Snapshot of the last fetched songlist (for the songs table). Lives beside the
# manifest so a removed-from-SSL song can still be shown/filtered.
SONGLIST_SNAPSHOT = config.LIBRARY_DIR / "songlist.json"

# iTunes rate limit is ~20/min; throttle the bulk run to ~3s/call.
PROPOSE_DELAY = 3.0


# ---------------------------------------------------------------------------
# Songlist sync
# ---------------------------------------------------------------------------

def merge_songlist(lib: Library, songs: list) -> int:
    """
    Ensure a manifest entry per fetched song and flag which songs are currently
    in the songlist (removed songs keep their entry with in_songlist=False).
    Returns the number of brand-new entries created.
    """
    present_keys = set()
    for song in songs:
        title = song.get("title", "")
        artist = song.get("artist", "")
        present_keys.add(normalize_key(title, artist))

    added = 0
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
            added += 1
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

    def start(self, cfg: dict) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.stop_flag = False
            self.done = 0
            self.found = 0
            self.current = ""
        self.thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self.thread.start()
        return True

    def stop(self):
        with self.lock:
            self.stop_flag = True

    def _run(self, cfg: dict):
        # Fresh Library instance for the worker thread.
        lib = Library()
        targets = [
            key for key, e in lib.all_entries().items()
            if e.get("in_songlist") and not e.get("status")
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
    return render_template("settings.html", cfg=cfg,
                           needs_setup=not cfg.get("streamersonglist_username"))


@app.route("/settings", methods=["POST"])
def save_settings():
    cfg = config.load_config()
    form = request.form

    cfg["streamersonglist_username"] = songlist.extract_username(
        form.get("streamersonglist_username", "").strip()
    )
    source = form.get("song_source", "streamersonglist").strip()
    cfg["song_source"] = source if source in ("streamersonglist", "file") else "streamersonglist"
    try:
        cfg["poll_interval"] = max(2, int(form.get("poll_interval", 10)))
    except (ValueError, TypeError):
        cfg["poll_interval"] = 10
    cfg["runtime_service"] = form.get("runtime_service") == "on"
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

    config.save_config(cfg)
    # Pick up the new username/source/interval without an app restart.
    if cfg.get("runtime_service", True) and cfg.get("streamersonglist_username"):
        runtime.service.restart(cfg)
    else:
        runtime.service.stop()
    return redirect(url_for("settings_page", saved=1))


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
    username = (request.json or {}).get("username", "")
    try:
        streamer = songlist.resolve_streamer(username)
        sid = streamer.get("id")
        first = songlist.fetch_songs_page(sid, size=1, current=0)
        return jsonify({"ok": True, "id": sid,
                        "name": streamer.get("name"),
                        "song_count": first.get("total", 0)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/sync", methods=["POST"])
def api_sync():
    cfg = config.load_config()
    username = cfg.get("streamersonglist_username")
    if not username:
        return jsonify({"ok": False, "error": "No username configured"}), 400
    try:
        streamer = songlist.resolve_streamer(username)
        songs = songlist.fetch_all_songs(streamer["id"])
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    lib = Library()
    added = merge_songlist(lib, songs)
    return jsonify({"ok": True, "total": len(songs), "added": added})


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
# Runtime service + queue overlay
# ---------------------------------------------------------------------------

@app.route("/api/runtime/status")
def api_runtime_status():
    return jsonify(runtime.service.status())


# Statuses whose stored image the overlay will show (mirrors the watcher's
# USABLE_STATUSES — rejected_all means the user turned everything down).
_OVERLAY_STATUSES = (STATUS_CONFIRMED, STATUS_PROPOSED, STATUS_UNVERIFIED)

_OVERLAY_PRESETS = ("dark", "light", "minimal", "glass")

# Query-param overrides so one config can drive multiple differently-styled
# browser sources: bools are 0/1, e.g. /overlay/queue?max=3&current=0&preset=glass
_OVERLAY_BOOL_KEYS = {
    "current": "include_current",
    "art": "show_artwork",
    "title": "show_title",
    "artist": "show_artist",
    "requester": "show_requester",
    "position": "show_position",
}


def overlay_options(cfg: dict, args) -> dict:
    """Effective overlay options: config defaults + query-string overrides."""
    opts = dict(config.DEFAULT_CONFIG["overlay"])
    opts.update(cfg.get("overlay", {}))

    for param, key in _OVERLAY_BOOL_KEYS.items():
        if param in args:
            opts[key] = args.get(param) not in ("0", "false", "no", "")
    for param, key, lo, hi in (("max", "max_songs", 1, 20),
                               ("font_size", "font_size", 8, 72),
                               ("art_size", "art_size", 16, 300),
                               ("row_gap", "row_gap", 0, 100)):
        if param in args:
            try:
                opts[key] = min(hi, max(lo, int(args.get(param))))
            except (TypeError, ValueError):
                pass
    if args.get("preset") in _OVERLAY_PRESETS:
        opts["preset"] = args.get("preset")
    if args.get("accent"):
        opts["accent"] = args.get("accent")
    return opts


@app.route("/overlay/queue.json")
def overlay_queue_json():
    cfg = config.load_config()
    opts = overlay_options(cfg, request.args)

    items = runtime.service.get_queue(cfg)
    if not opts["include_current"]:
        items = items[1:]
    items = items[: opts["max_songs"]]

    lib = Library()
    rows = []
    for pos, item in enumerate(items, start=1 if opts["include_current"] else 2):
        view = songlist.queue_item_view(item, pos)
        if view is None:
            continue
        entry = lib.get(view["title"], view["artist"])
        if (entry and entry.get("status") in _OVERLAY_STATUSES
                and lib.has_image(entry)):
            view["art"] = url_for("serve_image",
                                  key=normalize_key(view["title"], view["artist"]),
                                  v=entry.get("updated_at", ""))
        else:
            view["art"] = url_for("overlay_fallback_image")
        rows.append(view)

    return jsonify({"items": rows, "options": opts})


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


@app.route("/overlay")
def overlay_settings_page():
    cfg = config.load_config()
    if not cfg.get("streamersonglist_username"):
        return redirect(url_for("settings_page"))
    opts = overlay_options(cfg, {})
    overlay_url = url_for("overlay_queue_page", _external=True)
    return render_template("overlay_settings.html", opts=opts, cfg=cfg,
                           overlay_url=overlay_url, presets=_OVERLAY_PRESETS)


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
    opts["accent"] = form.get("accent", "#4da3ff").strip() or "#4da3ff"

    config.save_config(cfg)
    return redirect(url_for("overlay_settings_page", saved=1))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(host="127.0.0.1", port=5050, open_browser=True):
    first_run = config.ensure_config()
    cfg = config.load_config()
    if cfg.get("runtime_service", True) and cfg.get("streamersonglist_username"):
        runtime.service.start(cfg)
    if open_browser:
        target = "settings" if first_run else ""
        threading.Timer(
            1.0, lambda: webbrowser.open(f"http://{host}:{port}/{target}")
        ).start()
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    run()
