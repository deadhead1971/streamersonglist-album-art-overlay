"""
Flask dashboard: settings -> songlist sync -> bulk propose -> review -> review queue.

Local tool only — binds 127.0.0.1. The bulk-propose run is long (185+ throttled
iTunes calls) so it runs in a background thread with a polled progress endpoint;
request handlers never block on it.
"""

import json
import logging
import threading
import time
import webbrowser

from flask import (
    Flask, jsonify, redirect, render_template, request, send_file, url_for,
)

from . import artwork, config, songlist
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
    return render_template("settings.html", cfg=config.load_config())


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
    return redirect(url_for("settings_page", saved=1))


@app.route("/songs")
def songs_page():
    return render_template("songs.html", cfg=config.load_config())


@app.route("/review")
def review_page():
    return render_template("review.html", mode="review",
                           title="Review proposed artwork")


@app.route("/queue")
def queue_page():
    return render_template("review.html", mode="queue",
                           title="Review queue (live grabs)")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

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
# Entry point
# ---------------------------------------------------------------------------

def run(host="127.0.0.1", port=5050, open_browser=True):
    first_run = config.ensure_config()
    if open_browser:
        target = "settings" if first_run else ""
        threading.Timer(
            1.0, lambda: webbrowser.open(f"http://{host}:{port}/{target}")
        ).start()
    app.run(host=host, port=port, debug=False)


if __name__ == "__main__":
    run()
