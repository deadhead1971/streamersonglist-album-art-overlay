"""
StreamerSonglist realtime events — a *doorbell* for the runtime service.

SSL's new API publishes queue/now-playing events over Centrifugo
(https://dev.streamersonglist.com/docs/events). This module subscribes to them
and, on anything relevant, rings a bell: it calls a zero-argument callback and
nothing else. It deliberately does **not** parse event payloads or maintain a
client-side copy of the queue.

Why a doorbell and not a data feed:

  * Subscriptions are forward-only — there is no state snapshot on subscribe,
    so REST is the source of truth no matter what.
  * The envelope is ``{"type": ..., "data": ...}`` and ``data`` is documented as
    nullable, explicitly meaning "refetch via REST".
  * ``queue_reorder`` is not published at all for queues over 1000 entries.

Parsing payloads would therefore buy a second, drift-prone model of state that
``songlist.fetch_queue`` already normalises across v1/v2 — and would still need
the REST refetch for the cases above. Ringing a bell and refetching is both
less code and strictly more correct: a missed, malformed, or null event costs
one redundant fetch instead of a wrong overlay.

Connection facts, verified live against production 2026-08-16:

  * wss://events.streamersonglist.com/connection/websocket (Centrifugo 6.9.1)
  * ``streamer:{id}``, ``streamer:{id}-queue`` and ``streamer:{id}-song`` all
    accept an **anonymous** subscribe — no token, even though every v2 REST
    read needs one. We connect anonymously on purpose: the token buys nothing
    on public channels, so there is no reason to hand it to a second host.
  * Channels report ``recoverable=True positioned=True``, so centrifuge
    recovers missed publications across a short drop by itself.

The SDK (``centrifuge-python``) is asyncio-only, so this owns a private event
loop on a daemon thread. It is an accelerator and never load-bearing: if the
import is missing, the socket never connects, or it dies mid-stream, the
runtime service just keeps polling on its own timer exactly as before.
"""

import asyncio
import logging
import threading
import time

log = logging.getLogger("artwork_fetcher")

DEFAULT_EVENTS_URL = "wss://events.streamersonglist.com/connection/websocket"

# Event types worth a refetch. Everything else on the channel (settings edits,
# action log, token balance, ...) can't change the overlay or the artwork.
#
# One caveat since the empty-queue promo card: SSL's requests open/closed
# switch *is* a settings edit, and it does change what that card says. It is
# not added here because the runtime service reads it over REST only while the
# queue is empty — the card is the one thing on screen then, and a 30s lag on
# it is not worth parsing settings payloads for. If that ever needs to be
# instant, the event name has to be learned by logging the channel unfiltered.
RELEVANT_PREFIXES = ("queue_", "now_playing_")

try:  # Optional dependency — absent means "keep polling", not "crash".
    from centrifuge import (Client, ClientEventHandler, SubscriptionEventHandler)
    AVAILABLE = True
    IMPORT_ERROR = None
except ImportError as e:  # pragma: no cover - depends on the install
    Client = None
    ClientEventHandler = object
    SubscriptionEventHandler = object
    AVAILABLE = False
    IMPORT_ERROR = str(e)


def events_url(cfg: dict) -> str:
    """Events host. Blank config = production; set it to test against staging."""
    return (cfg.get("events_url") or "").strip() or DEFAULT_EVENTS_URL


class EventDoorbell:
    """
    Subscribes to a streamer's public event channel and calls ``on_ring()``
    whenever the queue or now-playing slot changes.

    ``on_ring`` runs on the doorbell's event loop thread, so it must not block:
    the runtime service passes a ``threading.Event.set``.
    """

    def __init__(self, on_ring):
        self._on_ring = on_ring
        self._lock = threading.Lock()
        self._thread = None
        self._loop = None
        self._stop_async = None      # asyncio.Event, created inside the loop
        # State (guarded by _lock)
        self._connected = False
        self._subscribed = False
        self._channel = None
        self._error = None
        self._rings = 0
        self._last_ring = 0.0        # monotonic time of the last relevant event
        self._unknown_types = set()  # logged once each, to learn the catalogue

    # -- public API -----------------------------------------------------------

    def start(self, streamer_id, cfg: dict) -> bool:
        """
        Connect and subscribe in the background. Returns False (having logged
        why) if the doorbell can't run — the caller carries on polling.
        """
        if not AVAILABLE:
            log.info("Realtime events unavailable (%s) — polling only. "
                     "Install with: pip install centrifuge-python", IMPORT_ERROR)
            return False
        if streamer_id is None:
            return False

        # A settings save restarts the runtime service, and the old doorbell
        # thread can still be winding down when the new one asks to start.
        # Without this the second start would be refused and realtime would be
        # silently off until the next dashboard restart.
        old = self._thread
        if old is not None and old.is_alive():
            self.stop()
            old.join(timeout=5)
            if old.is_alive():
                log.warning("Previous realtime events thread still running — "
                            "polling only")
                return False

        with self._lock:
            self._error = None
            self._channel = f"streamer:{streamer_id}"
            channel, url = self._channel, events_url(cfg)

        self._thread = threading.Thread(
            target=self._thread_main, args=(channel, url), daemon=True,
            name="runtime-events",
        )
        self._thread.start()
        return True

    def stop(self):
        """Ask the loop to disconnect. Safe to call from any thread, or twice."""
        loop, stop_async = self._loop, self._stop_async
        if loop is not None and stop_async is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop_async.set)
            except RuntimeError:
                pass  # loop already finished — nothing to stop

    def is_connected(self) -> bool:
        """True only when the socket is up *and* the channel is subscribed."""
        with self._lock:
            return self._connected and self._subscribed

    def state(self) -> dict:
        """Diagnostics for the dashboard status pill."""
        with self._lock:
            return {
                "available": AVAILABLE,
                "connected": self._connected,
                "subscribed": self._subscribed,
                "channel": self._channel,
                "rings": self._rings,
                "last_ring_age": (time.monotonic() - self._last_ring)
                                  if self._last_ring else None,
                "error": self._error,
            }

    # -- internals ------------------------------------------------------------

    def _ring(self, event_type: str):
        with self._lock:
            self._rings += 1
            self._last_ring = time.monotonic()
        log.debug("Realtime event %s — waking runtime tick", event_type)
        try:
            self._on_ring()
        except Exception:  # noqa: BLE001 - a bad callback must not kill the socket
            log.exception("Event doorbell callback failed")

    def _note_unknown(self, event_type: str):
        """Log each unrecognised event type once, so the filter can be tuned."""
        with self._lock:
            if event_type in self._unknown_types:
                return
            self._unknown_types.add(event_type)
        log.debug("Ignoring realtime event type %r (not queue/now-playing)",
                  event_type)

    def _thread_main(self, channel: str, url: str):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._run(channel, url))
        except Exception as e:  # noqa: BLE001 - never take the dashboard down
            with self._lock:
                self._error = str(e)
            log.warning("Realtime events stopped (%s) — polling continues", e)
        finally:
            try:
                # centrifuge leaves a reconnect task pending; cancelling it
                # before closing keeps asyncio's "Task was destroyed but it is
                # pending!" out of the user's log on every settings save.
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
            try:
                loop.close()
            finally:
                self._loop = None
                self._stop_async = None
                with self._lock:
                    self._connected = False
                    self._subscribed = False

    async def _run(self, channel: str, url: str):
        self._stop_async = asyncio.Event()
        doorbell = self

        class _ClientEvents(ClientEventHandler):
            async def on_connected(self, ctx):
                with doorbell._lock:
                    doorbell._connected = True
                    doorbell._error = None
                log.info("Realtime events connected (%s)", url)

            async def on_connecting(self, ctx):
                with doorbell._lock:
                    doorbell._connected = False

            async def on_disconnected(self, ctx):
                with doorbell._lock:
                    doorbell._connected = False
                    doorbell._subscribed = False
                log.info("Realtime events disconnected (%s) — polling covers "
                         "the gap", ctx.reason)

            async def on_error(self, ctx):
                with doorbell._lock:
                    doorbell._error = str(ctx.error)
                log.debug("Realtime events client error: %s", ctx.error)

        class _SubEvents(SubscriptionEventHandler):
            async def on_subscribed(self, ctx):
                with doorbell._lock:
                    doorbell._subscribed = True
                log.info("Realtime events subscribed to %s — queue changes now "
                         "apply immediately", channel)
                # Whatever happened while we were away is unknown; one refetch
                # re-syncs, and it covers the initial subscribe too (channels
                # deliver no state snapshot).
                doorbell._ring("subscribed")

            async def on_subscribing(self, ctx):
                with doorbell._lock:
                    doorbell._subscribed = False

            async def on_unsubscribed(self, ctx):
                with doorbell._lock:
                    doorbell._subscribed = False

            async def on_error(self, ctx):
                with doorbell._lock:
                    doorbell._error = str(ctx.error)
                log.warning("Realtime events subscription error on %s: %s",
                            channel, ctx.error)

            async def on_publication(self, ctx):
                data = ctx.pub.data
                event_type = data.get("type") if isinstance(data, dict) else None
                if not isinstance(event_type, str):
                    # Unrecognised envelope — refetch rather than guess.
                    doorbell._ring("<no type>")
                elif event_type.startswith(RELEVANT_PREFIXES):
                    doorbell._ring(event_type)
                else:
                    doorbell._note_unknown(event_type)

        # Anonymous on purpose — see the module docstring.
        client = Client(url, events=_ClientEvents(),
                        name="streamersonglist-art-fetcher")
        sub = client.new_subscription(channel, events=_SubEvents())
        await client.connect()
        await sub.subscribe()
        try:
            await self._stop_async.wait()
        finally:
            with self._lock:
                self._connected = False
                self._subscribed = False
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
