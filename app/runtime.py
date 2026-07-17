"""
Background runtime service — the live loop running inside the dashboard
process. The dashboard is the only process needed during a stream.

One thread ticks every ``poll_interval`` seconds and:
  1. polls the SSL queue and caches it in memory (feeds the
     /overlay/queue.json endpoint — the overlay never triggers extra API
     calls), and
  2. resolves artwork for the current song on change and writes
     ``current_artwork.png``. The current song is the top queue slot, or —
     when ``song_source`` is ``file`` — the contents of the configured song
     file, read on the same tick.

In file mode a missing/unresolvable SSL username is not fatal: the queue
overlay has nothing to show, but the PNG output still works.

Library discipline: a fresh Library is created for each resolve (never held
across polls) and every manifest write in the process goes through
``Library.save`` under ``MANIFEST_LOCK``, so the service and dashboard request
handlers can't tear or clobber each other's saves.
"""

import logging
import threading
import time

import requests

from . import artwork, songlist
from .library import Library

log = logging.getLogger("artwork_fetcher")


class RuntimeService:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        # State (guarded by _lock)
        self._running = False
        self._streamer_id = None
        self._streamer_name = None
        self._queue = []          # raw SSL queue items, top-first
        self._queue_at = 0.0      # monotonic time of last successful poll
        self._current = None      # (title, artist) at top of queue, or None
        self._error = None        # last poll error message, or None

    # -- public API -----------------------------------------------------------

    def start(self, cfg: dict) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._error = None
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(cfg,), daemon=True, name="runtime-service"
        )
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()

    def restart(self, cfg: dict):
        """
        Stop the poller and start it again with a fresh config (used after a
        settings save). The join can take a while if a live artwork cascade is
        mid-flight, so the handover runs in a background thread — never block a
        request handler on it.
        """
        self.stop()
        old_thread = self._thread

        def _handover():
            if old_thread is not None:
                old_thread.join(timeout=60)
            self.start(cfg)

        threading.Thread(target=_handover, daemon=True).start()

    def status(self) -> dict:
        with self._lock:
            current = self._current
            return {
                "running": self._running,
                "streamer": self._streamer_name,
                "queue_length": len(self._queue),
                "current_title": current[0] if current else None,
                "current_artist": current[1] if current else None,
                "error": self._error,
                "queue_age": (time.monotonic() - self._queue_at)
                              if self._queue_at else None,
            }

    def get_queue(self, cfg: dict, max_age: float = 15.0) -> list:
        """
        Return the cached queue if fresh enough; otherwise (service stopped, or
        cache stale) fetch it inline. Never raises — returns the last known
        queue on failure.
        """
        with self._lock:
            fresh = self._queue_at and (time.monotonic() - self._queue_at) < max_age
            if fresh:
                return list(self._queue)
            streamer_id = self._streamer_id

        if streamer_id is None:
            streamer_id = self._resolve_id(cfg)
            if streamer_id is None:
                return []
        try:
            queue = songlist.fetch_queue(streamer_id)
        except requests.RequestException as e:
            log.warning("Overlay queue fetch failed: %s", e)
            with self._lock:
                return list(self._queue)
        with self._lock:
            self._queue = queue
            self._queue_at = time.monotonic()
            return list(queue)

    # -- internals ------------------------------------------------------------

    def _resolve_id(self, cfg: dict):
        username = cfg.get("streamersonglist_username")
        if not username:
            return None
        try:
            streamer = songlist.resolve_streamer(username)
        except requests.RequestException as e:
            with self._lock:
                self._error = f"Could not resolve username: {e}"
            log.error("Runtime service: %s", self._error)
            return None
        with self._lock:
            self._streamer_id = streamer.get("id")
            self._streamer_name = streamer.get("name")
        return streamer.get("id")

    def _run(self, cfg: dict):
        file_mode = cfg.get("song_source", "streamersonglist") == "file"
        try:
            interval = max(2, int(cfg.get("poll_interval", 10)))
        except (TypeError, ValueError):
            interval = 10

        streamer_id = self._resolve_id(cfg)
        if streamer_id is None and not file_mode:
            with self._lock:
                self._running = False
            return

        if streamer_id is None:
            log.info("Runtime service watching song file every %ss (no SSL "
                     "username — queue overlay disabled)", interval)
        else:
            log.info("Runtime service polling SSL queue for %s every %ss "
                     "(current song from %s)", self._streamer_name, interval,
                     "song file" if file_mode else "queue top")

        # Sentinel so the very first tick always writes the output image (even
        # when the queue/file starts out empty → fallback).
        last = object()
        first = True
        while not self._stop.wait(0 if first else interval):
            first = False

            # Queue poll — feeds the overlay, and in queue mode the PNG too.
            result = None
            if streamer_id is not None:
                try:
                    queue = songlist.fetch_queue(streamer_id)
                except requests.RequestException as e:
                    # Transient — keep the current frame and cached queue.
                    with self._lock:
                        self._error = str(e)
                    log.warning("SSL queue fetch failed (%s) — retrying", e)
                    if not file_mode:
                        continue
                    queue = None
                if queue is not None:
                    result = songlist._queue_item_song(queue[0]) if queue else None
                    with self._lock:
                        self._queue = queue
                        self._queue_at = time.monotonic()
                        self._error = None

            if file_mode:
                result = artwork.read_song_file(cfg.get("song_file"), quiet=True)

            with self._lock:
                self._current = result

            if result != last:
                last = result
                if result is None:
                    if file_mode:
                        log.info("Song file empty, missing, or unconfigured "
                                 "(%s) — applying fallback",
                                 cfg.get("song_file") or "no path set")
                    else:
                        log.info("Queue empty — applying fallback")
                    artwork.apply_fallback(cfg)
                else:
                    log.info("Current song changed — fetching artwork...")
                    # Fresh Library per resolve so this thread never holds a
                    # stale manifest across dashboard edits.
                    artwork.resolve_artwork_for_song(cfg, Library(), *result)

        with self._lock:
            self._running = False
        log.info("Runtime service stopped")


service = RuntimeService()
