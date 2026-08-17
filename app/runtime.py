"""
Background runtime service — the live loop running inside the dashboard
process. The dashboard is the only process needed during a stream.

One thread ticks and:
  1. polls the SSL queue and caches it in memory (feeds the
     /overlay/queue.json endpoint — the overlay never triggers extra API
     calls), and
  2. resolves artwork for the current song on change and writes
     ``current_artwork.png``. The current song is the queue's now-playing
     slot (falling back to the top of the upcoming queue when that slot is
     empty), or — when ``song_source`` is ``file`` — the contents of the
     configured song file, read on the same tick.

What drives the tick depends on what's available. On the v2 API an
``events.EventDoorbell`` subscribes to SSL's Centrifugo channel and wakes the
thread the moment the queue changes, so artwork follows a song change in
roughly the time one REST call takes. The ``poll_interval`` timer stays as a
reconciliation net (stretched to ``IDLE_INTERVAL`` while the socket is up), and
takes over again untouched whenever the doorbell is unavailable, disabled, on
v1, or simply dropped mid-stream. The socket is an accelerator; it is never
load-bearing.

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

from . import artwork, events, songlist
from .library import Library

log = logging.getLogger("artwork_fetcher")

# Poll spacing while the realtime doorbell is connected. Long on purpose: it is
# only catching what the socket could have missed.
IDLE_INTERVAL = 60.0

# Floor between two ticks. A queue reorder publishes a burst of events, and
# each one must not turn into its own REST call.
MIN_TICK_GAP = 1.0


class RuntimeService:
    def __init__(self):
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        # Set to run the tick body early — by a realtime event, or by stop().
        self._wake = threading.Event()
        self._events = events.EventDoorbell(self._wake.set)
        # State (guarded by _lock)
        self._running = False
        self._streamer_id = None
        self._streamer_name = None
        self._streamer_avatar = None
        self._playing = None      # raw now-playing item, or None
        self._queue = []          # raw UPCOMING queue items, top-first
        self._queue_at = 0.0      # monotonic time of last successful poll
        self._current = None      # (title, artist) playing right now, or None
        self._error = None        # last poll error message, or None

    # -- public API -----------------------------------------------------------

    def start(self, cfg: dict) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._error = None
        self._stop.clear()
        self._wake.clear()
        self._thread = threading.Thread(
            target=self._run, args=(cfg,), daemon=True, name="runtime-service"
        )
        self._thread.start()
        return True

    def stop(self):
        self._events.stop()
        self._stop.set()
        # The tick thread waits on _wake, so stopping has to ring it too or the
        # join in restart() would sit out a full interval.
        self._wake.set()

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
                "avatar": self._streamer_avatar,
                # The now-playing slot is counted separately: v2's queue
                # `total` covers upcoming songs only.
                "queue_length": len(self._queue) + (1 if self._playing else 0),
                "current_title": current[0] if current else None,
                "current_artist": current[1] if current else None,
                "error": self._error,
                "queue_age": (time.monotonic() - self._queue_at)
                              if self._queue_at else None,
                "events": self._events.state(),
            }

    def get_queue(self, cfg: dict, max_age: float = 15.0):
        """
        Return ``(playing, upcoming)`` from cache if fresh enough; otherwise
        (service stopped, or cache stale) fetch it inline. Never raises —
        returns the last known queue on failure.
        """
        with self._lock:
            fresh = self._queue_at and (time.monotonic() - self._queue_at) < max_age
            if fresh:
                return self._playing, list(self._queue)
            streamer_id = self._streamer_id

        if streamer_id is None:
            streamer_id = self._resolve_id(cfg)
            if streamer_id is None:
                return None, []
        try:
            playing, upcoming = songlist.fetch_queue(streamer_id, cfg=cfg)
        except requests.RequestException as e:
            log.warning("Overlay queue fetch failed: %s", e)
            with self._lock:
                return self._playing, list(self._queue)
        with self._lock:
            self._playing = playing
            self._queue = upcoming
            self._queue_at = time.monotonic()
            return playing, list(upcoming)

    # -- internals ------------------------------------------------------------

    def _resolve_id(self, cfg: dict):
        username = cfg.get("streamersonglist_username")
        if not username:
            return None
        try:
            streamer = songlist.resolve_streamer(username, cfg=cfg)
        except songlist.AuthRequired as e:
            # Cutover day: say what to do rather than logging a bare 401.
            with self._lock:
                self._error = str(e)
            log.error("Runtime service: %s", self._error)
            return None
        except requests.RequestException as e:
            with self._lock:
                self._error = f"Could not resolve username: {e}"
            log.error("Runtime service: %s", self._error)
            return None
        with self._lock:
            self._streamer_id = streamer.get("id")
            self._streamer_name = streamer.get("name")
            self._streamer_avatar = streamer.get("avatar")
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
            log.info("Runtime service watching SSL queue for %s (poll interval "
                     "%ss; current song from %s)", self._streamer_name, interval,
                     "song file" if file_mode else "now-playing slot")

        # Realtime doorbell. v1 is excluded deliberately: the events service
        # keys on v2 streamer ids, and v1 hands out different ids for the same
        # channel, so subscribing would quietly follow someone else's queue.
        if streamer_id is not None and cfg.get("websocket_events", True):
            try:
                if songlist.detect_backend(cfg) == "v2":
                    self._events.start(streamer_id, cfg)
            except requests.RequestException as e:
                log.info("Realtime events not started (%s) — polling only", e)

        # Sentinel so the very first tick always writes the output image (even
        # when the queue/file starts out empty → fallback).
        last = object()
        first = True
        backoff = 0.0  # extra seconds added after a 429
        last_tick = 0.0
        while not self._stop.is_set():
            if not first:
                # Connected: events drive the tick and this is just the net.
                # Dropped: the wait silently returns to plain polling.
                # File mode never stretches — the song file is read on this
                # timer and no queue event announces a change to it.
                relaxed = self._events.is_connected() and not file_mode
                wait_for = (IDLE_INTERVAL if relaxed else interval) + backoff
                self._wake.wait(timeout=wait_for)
                if self._stop.is_set():
                    break
                gap = time.monotonic() - last_tick
                if gap < MIN_TICK_GAP and self._stop.wait(MIN_TICK_GAP - gap):
                    break
            # Cleared *before* the fetch, never after: an event landing while a
            # fetch is in flight has to schedule another tick rather than be
            # swallowed by the one that didn't see its change.
            self._wake.clear()
            last_tick = time.monotonic()
            first = False

            # Queue poll — feeds the overlay, and in queue mode the PNG too.
            result = None
            if streamer_id is not None:
                playing, upcoming, ok = None, [], False
                try:
                    playing, upcoming = songlist.fetch_queue(streamer_id, cfg=cfg)
                    ok = True
                except songlist.RateLimited as e:
                    # No published limits for v2 — back off instead of hammering.
                    backoff = min(max(backoff * 2, interval), 300)
                    with self._lock:
                        self._error = str(e)
                    log.warning("SSL rate limited (%s) — next poll in %.0fs",
                                e, interval + backoff)
                except songlist.AuthRequired as e:
                    with self._lock:
                        self._error = str(e)
                    log.error("SSL queue fetch rejected: %s", e)
                except requests.RequestException as e:
                    # Transient — keep the current frame and cached queue.
                    with self._lock:
                        self._error = str(e)
                    log.warning("SSL queue fetch failed (%s) — retrying", e)

                if not ok and not file_mode:
                    continue
                if ok:
                    backoff = 0.0
                    # The now-playing slot is the current song; the top of the
                    # upcoming queue is only a fallback for when it's empty.
                    result = songlist.current_from_queue(playing, upcoming)
                    with self._lock:
                        self._playing = playing
                        self._queue = upcoming
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

        self._events.stop()
        with self._lock:
            self._running = False
        log.info("Runtime service stopped")


service = RuntimeService()
