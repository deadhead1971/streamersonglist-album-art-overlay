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

# Guard rail between two requests open/closed checks. Deliberately below the
# tick interval: the tick is what paces this, not the floor — the floor only
# stops a burst of ticks turning into a burst of REST calls.
REQUESTS_CHECK_INTERVAL = 5.0

# How long a failed streamer-id resolve is remembered before another is tried.
# Two jobs, both learned the hard way on 2026-08-31, when a token saved with
# the wrong Authorization prefix 401'd for five hours:
#   * the tick keeps retrying instead of the service dying, so a corrected
#     token (or an SSL outage ending) recovers on its own within this window;
#   * every overlay poll calls _resolve_id too, and without a floor a dead
#     credential meant one doomed request per poll — ~4/s across two browser
#     sources, 192 error lines, and a 10 MB log.
RESOLVE_RETRY_INTERVAL = 30.0

# Error classes surfaced to the dashboard. "auth" is a settings mistake only
# the user can fix, so it earns a banner; "transient" is a blip the next poll
# probably clears, and must not flash red every time a request times out.
ERROR_AUTH = "auth"
ERROR_TRANSIENT = "transient"


def promo_enabled(cfg: dict) -> bool:
    """Whether either overlay is set to show the empty-queue card."""
    return bool(cfg.get("overlay", {}).get("show_promo")
                or cfg.get("overlay_current", {}).get("show_promo"))


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
        self._empty_since = None  # monotonic time the queue went empty, or None
        self._requests_active = None  # True/False, or None = never read
        self._requests_at = 0.0   # monotonic time of last requests-open check
        self._current = None      # (title, artist) playing right now, or None
        self._error = None        # last poll error message, or None
        self._error_kind = None   # ERROR_AUTH / ERROR_TRANSIENT, or None
        self._resolve_failed_at = 0.0  # monotonic time of the last failed resolve

    # -- public API -----------------------------------------------------------

    def start(self, cfg: dict) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._error = None
            self._error_kind = None
            # A restart is the user acting on the problem (it is what a
            # settings save does), so the last failure must not keep the next
            # resolve waiting out its retry window.
            self._resolve_failed_at = 0.0
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
                # Lets the dashboard tell "your token is wrong" (act on it)
                # from "that poll timed out" (ignore it) — see ERROR_AUTH.
                "error_kind": self._error_kind,
                "queue_age": (time.monotonic() - self._queue_at)
                              if self._queue_at else None,
                "events": self._events.state(),
            }

    def error(self):
        """``(message, kind)`` of the last failure, or ``(None, None)``."""
        with self._lock:
            return self._error, self._error_kind

    def empty_for(self):
        """
        Seconds the queue has been continuously empty (nothing playing and
        nothing upcoming), or None if it is not empty — or if no poll has
        landed yet, since "we have not looked" is not "there is nothing there".
        Drives the empty-queue promo card's delay.
        """
        with self._lock:
            if self._empty_since is None:
                return None
            return time.monotonic() - self._empty_since

    def requests_active(self):
        """
        True/False from the last requests open/closed check, or None if one has
        never succeeded (v1 backend, no username, or every attempt failed).
        None means "assume open" to callers — see dashboard._promo_item.
        """
        with self._lock:
            return self._requests_active

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
                # Same rule as a failed fetch below: hand back the last known
                # queue rather than an empty one, so a rejected token or a
                # dropped connection can't read as "the songlist is empty".
                with self._lock:
                    return self._playing, list(self._queue)
        try:
            playing, upcoming = songlist.fetch_queue(streamer_id, cfg=cfg)
        except (songlist.AuthRequired, songlist.Forbidden) as e:
            # Park the credential so the next overlay poll costs nothing.
            self._credential_rejected(str(e))
            log.error("Overlay queue fetch rejected: %s (retrying in %.0fs)",
                      e, RESOLVE_RETRY_INTERVAL)
            with self._lock:
                return self._playing, list(self._queue)
        except requests.RequestException as e:
            log.warning("Overlay queue fetch failed: %s", e)
            with self._lock:
                return self._playing, list(self._queue)
        self._store_queue(playing, upcoming)
        return playing, list(upcoming)

    # -- internals ------------------------------------------------------------

    def _set_error(self, message: str, kind: str):
        with self._lock:
            self._error = message
            self._error_kind = kind

    def _clear_error(self):
        with self._lock:
            self._error = None
            self._error_kind = None

    def _credential_rejected(self, message: str):
        """
        Park the streamer id after a 401/403.

        Both the tick and every overlay poll fetch with the cached id, so a
        token that dies mid-session would otherwise fire one doomed request
        from each, forever. Dropping the id routes both back through
        ``_resolve_id``, which is floored to one attempt per
        ``RESOLVE_RETRY_INTERVAL`` and picks the service back up by itself once
        the credential works again.
        """
        self._set_error(message, ERROR_AUTH)
        with self._lock:
            self._streamer_id = None
            self._resolve_failed_at = time.monotonic()

    def _store_queue(self, playing, upcoming):
        """
        Cache a freshly fetched queue. The single write path for it, so the
        empty-since clock can never disagree with what the overlays are being
        shown — the tick and get_queue's inline fetch both land here.
        """
        with self._lock:
            self._playing = playing
            self._queue = upcoming
            self._queue_at = time.monotonic()
            if playing is not None or upcoming:
                self._empty_since = None
            elif self._empty_since is None:
                self._empty_since = time.monotonic()

    def _refresh_requests_active(self, cfg: dict):
        """
        Re-read SSL's requests open/closed switch, rate-floored.

        A failed read keeps the last answer rather than clearing it: a blip
        should not flip the overlay between its open and closed cards, and an
        answer we have never had is what "assume open" is for.
        """
        with self._lock:
            if self._requests_at and (time.monotonic() - self._requests_at
                                      < REQUESTS_CHECK_INTERVAL):
                return
        try:
            value = songlist.fetch_requests_active(
                cfg.get("streamersonglist_username"), cfg=cfg)
        except requests.RequestException as e:
            log.debug("Requests open/closed check failed (%s) — keeping the "
                      "last answer", e)
            return
        with self._lock:
            if value != self._requests_active:
                log.info("StreamerSonglist requests are now %s",
                         {True: "open", False: "closed"}.get(value, "unknown"))
            self._requests_active = value
            self._requests_at = time.monotonic()

    def _resolve_id(self, cfg: dict):
        """
        Resolve and cache the streamer id, or return None.

        Called by the tick *and* by every overlay poll through ``get_queue``,
        so a failure is remembered for ``RESOLVE_RETRY_INTERVAL`` and callers
        arriving inside that window get None without touching the network. A
        rejected token used to mean one doomed request — and one ERROR line —
        per overlay poll, for as long as it took the user to notice.
        """
        username = cfg.get("streamersonglist_username")
        if not username:
            return None
        with self._lock:
            failed_at = self._resolve_failed_at
        if failed_at and (time.monotonic() - failed_at) < RESOLVE_RETRY_INTERVAL:
            return None
        try:
            streamer = songlist.resolve_streamer(username, cfg=cfg)
        except (songlist.AuthRequired, songlist.Forbidden) as e:
            # Cutover day: say what to do rather than logging a bare 401. A 403
            # joins it because a Streamer token aimed at the wrong channel is a
            # settings mistake, not the transient blip the generic branch below
            # assumes — it needs the loud message and the actionable text.
            self._set_error(str(e), ERROR_AUTH)
            with self._lock:
                self._resolve_failed_at = time.monotonic()
            log.error("Runtime service: %s (retrying in %.0fs)",
                      str(e), RESOLVE_RETRY_INTERVAL)
            return None
        except requests.RequestException as e:
            self._set_error(f"Could not resolve username: {e}", ERROR_TRANSIENT)
            with self._lock:
                self._resolve_failed_at = time.monotonic()
            log.error("Runtime service: could not resolve username: %s "
                      "(retrying in %.0fs)", e, RESOLVE_RETRY_INTERVAL)
            return None
        with self._lock:
            self._streamer_id = streamer.get("id")
            self._streamer_name = streamer.get("name")
            self._streamer_avatar = streamer.get("avatar")
            self._resolve_failed_at = 0.0
            self._error = None
            self._error_kind = None
        return streamer.get("id")

    def _start_events(self, cfg: dict, streamer_id):
        """
        Realtime doorbell. v1 is excluded deliberately: the events service keys
        on v2 streamer ids, and v1 hands out different ids for the same
        channel, so subscribing would quietly follow someone else's queue.

        Split out of _run because the id can now arrive late — a service that
        started with a rejected token subscribes when the resolve finally
        works, instead of polling blind for the rest of the session.
        """
        if streamer_id is None or not cfg.get("websocket_events", True):
            return
        try:
            if songlist.detect_backend(cfg) == "v2":
                self._events.start(streamer_id, cfg)
        except requests.RequestException as e:
            log.info("Realtime events not started (%s) — polling only", e)

    def _run(self, cfg: dict):
        file_mode = cfg.get("song_source", "streamersonglist") == "file"
        try:
            interval = max(2, int(cfg.get("poll_interval", 10)))
        except (TypeError, ValueError):
            interval = 10

        username = (cfg.get("streamersonglist_username") or "").strip()
        streamer_id = self._resolve_id(cfg)
        # Nothing configured to watch at all — the one case that is genuinely
        # not worth a thread. A *failed* resolve is not this case: the username
        # is set and the fault (bad token, SSL down) is fixable while we run,
        # so the loop stays up and retries rather than dying and leaving every
        # overlay empty until the next settings save.
        if streamer_id is None and not file_mode and not username:
            with self._lock:
                self._running = False
            return

        if streamer_id is not None:
            log.info("Runtime service watching SSL queue for %s (poll interval "
                     "%ss; current song from %s)", self._streamer_name, interval,
                     "song file" if file_mode else "now-playing slot")
        elif not username:
            log.info("Runtime service watching song file every %ss (no SSL "
                     "username — queue overlay disabled)", interval)
        else:
            log.warning("Runtime service could not reach StreamerSonglist for "
                        "%s — retrying every %.0fs; overlays keep their last "
                        "state until it succeeds", username,
                        RESOLVE_RETRY_INTERVAL)

        self._start_events(cfg, streamer_id)

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
                # An empty queue with the promo on is the one idle state that
                # still needs a brisk tick: the requests open/closed switch is
                # read on this tick, and closing the songlist should reach the
                # overlay in seconds rather than the best part of a minute.
                # Nothing else is happening then, so the polling costs little.
                watching_requests = (promo_enabled(cfg)
                                     and self.empty_for() is not None)
                relaxed = (self._events.is_connected() and not file_mode
                           and not watching_requests)
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

            # The id can be missing because the first resolve was rejected or
            # the API was down. Try again (rate-floored inside _resolve_id) so
            # a token fixed in Settings — or SSL coming back — recovers without
            # a restart.
            if streamer_id is None and username:
                streamer_id = self._resolve_id(cfg)
                if streamer_id is not None:
                    log.info("Runtime service reached StreamerSonglist — now "
                             "watching the queue for %s", self._streamer_name)
                    self._start_events(cfg, streamer_id)
                elif not file_mode:
                    # Nothing to poll and nothing to read: leave the output PNG
                    # and the cached queue exactly as they are.
                    continue

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
                    self._set_error(str(e), ERROR_TRANSIENT)
                    log.warning("SSL rate limited (%s) — next poll in %.0fs",
                                e, interval + backoff)
                except (songlist.AuthRequired, songlist.Forbidden) as e:
                    # A credential that worked and stopped working: park the id
                    # so this doesn't repeat every tick, and let the resolve
                    # retry pick it up when the user fixes it.
                    self._credential_rejected(str(e))
                    streamer_id = None
                    log.error("SSL queue fetch rejected: %s (retrying in %.0fs)",
                              e, RESOLVE_RETRY_INTERVAL)
                except requests.RequestException as e:
                    # Transient — keep the current frame and cached queue.
                    self._set_error(str(e), ERROR_TRANSIENT)
                    log.warning("SSL queue fetch failed (%s) — retrying", e)

                if not ok and not file_mode:
                    continue
                if ok:
                    backoff = 0.0
                    # The now-playing slot is the current song; the top of the
                    # upcoming queue is only a fallback for when it's empty.
                    result = songlist.current_from_queue(playing, upcoming)
                    self._store_queue(playing, upcoming)
                    self._clear_error()

                # Which card the promo shows depends on the requests switch,
                # and that only matters while there is nothing queued — so it
                # is read here and nowhere else.
                if promo_enabled(cfg) and self.empty_for() is not None:
                    self._refresh_requests_active(cfg)

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
