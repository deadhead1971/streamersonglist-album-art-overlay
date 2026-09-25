"""
The desktop launcher: the app as a system-tray program, with no console window.

The installed app's shortcut runs exactly this, with the bundled embeddable
Python: ``python\pythonw.exe -m app.desktop`` (packaging/build.ps1). From
source it is ``python -m app.desktop``, or ``pythonw -m app.desktop`` for the
real no-console experience, so the tray can be tried without building.
``python -m app.dashboard`` is unchanged and still the developer's console run.

Why a tray and not a console: a black window is exactly what a non-technical
user closes to tidy up, taking the overlays down mid-stream; and on the classic
Windows console a stray click starts a text selection that blocks every thread
writing to it — the runtime logs each song change before it writes the PNG, so
one click froze the artwork.

Flags:
  --background       don't open the browser (for start-at-login, later)
  --no-tray          serve without a tray icon (CI, smoke tests)
  --self-check       check that bundled optional pieces actually import
  --data-dir PATH    use PATH as the data folder (sets ALBUMART_DATA_DIR)
"""

import argparse
import os
import sys
import threading
import webbrowser

HOST = "127.0.0.1"
PORT = 5050


def _message_box(text: str, title: str = "Album Art Overlay", error=True):
    """A Windows message box — the only way a windowed app can say anything
    before its dashboard is up. Falls back to stderr elsewhere."""
    if sys.platform == "win32":
        import ctypes
        flags = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000)
    else:
        print(f"{title}: {text}", file=sys.stderr)


def _parse(argv):
    parser = argparse.ArgumentParser(prog="AlbumArtOverlay")
    parser.add_argument("--background", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--data-dir")
    parser.add_argument("--port", type=int, default=PORT, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# --self-check
# ---------------------------------------------------------------------------

def self_check() -> int:
    """
    Import everything an installed copy can be missing without anything
    failing loudly, and report. Exit status is the number of failures, so the
    build fails on any.

    Each of these degrades silently when missing: events.py swallows the
    centrifuge ImportError by design (instant updates quietly become polling);
    websockets resolves ``connect`` lazily, so a broken install only shows on
    the first connect; tkinter is not part of the embeddable Python at all
    and is added by the build, and only the Browse… child process uses it;
    WebP is a Pillow plugin the thumbnails need; certifi's CA file is a data
    file, not code.
    """
    results = []

    def check(name, fn):
        try:
            detail = fn()
            results.append((True, name, detail or ""))
        except Exception as e:  # noqa: BLE001 — reporting, not handling
            results.append((False, name, f"{type(e).__name__}: {e}"))

    def centrifuge():
        from . import events
        if not events.AVAILABLE:
            raise ImportError(events.IMPORT_ERROR)
        import centrifuge.protocol.client_pb2  # noqa: F401 — protobuf codec

    def ws():
        import websockets
        websockets.connect  # lazy attribute: forces the real import
        import websockets.asyncio.client  # noqa: F401
        return websockets.__version__

    def tk():
        import tkinter
        import tkinter.filedialog  # noqa: F401
        # Importing proves only the .pyd; a dialog also needs the Tcl and Tk
        # script libraries on disk. Tcl() fails without init.tcl, and no
        # window is created for either check.
        from pathlib import Path
        library = Path(tkinter.Tcl().eval("info library"))
        tk_script = library.parent / f"tk{tkinter.TkVersion}" / "tk.tcl"
        if not tk_script.is_file():
            raise FileNotFoundError(tk_script)
        return f"Tk {tkinter.TkVersion}"

    def webp():
        from PIL import features
        if not features.check("webp"):
            raise RuntimeError("Pillow was built without WebP")

    def certs():
        import certifi
        path = certifi.where()
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        return path

    def tray():
        import pystray  # noqa: F401
        return pystray.Icon.__module__

    def templates():
        from . import config
        from .dashboard import app
        for rel in ("templates/base.html", "static/style.css"):
            if not os.path.isfile(os.path.join(app.root_path, rel)):
                raise FileNotFoundError(rel)
        for name in ("overlay_queue.html", "overlay_current.html",
                     "overlay_wall.html"):
            if not (config.BUNDLED_OBS_DIR / name).is_file():
                raise FileNotFoundError(f"obs/{name}")
        if not config.EXAMPLE_PATH.is_file():
            raise FileNotFoundError(config.EXAMPLE_PATH.name)

    for name, fn in (("realtime (centrifuge)", centrifuge), ("websockets", ws),
                     ("tkinter", tk), ("Pillow WebP", webp),
                     ("certifi CA bundle", certs), ("pystray", tray),
                     ("templates, static, loaders", templates)):
        check(name, fn)

    failures = sum(1 for ok, _, _ in results if not ok)
    report = "\n".join(f"{'ok  ' if ok else 'FAIL'}  {name}  {detail}"
                       for ok, name, detail in results)
    report += f"\n{failures} failure(s)"
    if sys.stdout is not None:
        print(report)
    else:
        # A windowed build has no stdout; leave the report where CI can read it.
        from . import config
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        (config.DATA_DIR / "self-check.txt").write_text(report, encoding="utf-8")
    return failures


# ---------------------------------------------------------------------------
# Tray
# ---------------------------------------------------------------------------

def _tray_image():
    """The tray icon: the packaged .ico when there is one, else a drawn record."""
    from PIL import Image, ImageDraw

    from . import config
    for candidate in (config.RESOURCE_DIR / "packaging" / "icon.ico",
                      config.RESOURCE_DIR / "icon.ico"):
        if candidate.is_file():
            try:
                return Image.open(candidate)
            except OSError:
                pass
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 2, size - 3, size - 3), fill=(24, 24, 28, 255),
                 outline=(91, 140, 255, 255), width=3)
    draw.ellipse((22, 22, size - 23, size - 23), fill=(91, 140, 255, 255))
    draw.ellipse((29, 29, size - 30, size - 30), fill=(24, 24, 28, 255))
    return img


def _open_folder(path):
    path.mkdir(parents=True, exist_ok=True)
    os.startfile(str(path))  # noqa: S606 — Windows only, a fixed folder


def run_tray(base_url: str, on_quit, first_run: bool, stoppers: list) -> bool:
    """
    Run the tray icon on this (the main) thread until Quit. Returns False,
    without blocking, if the tray can't start — the caller keeps serving,
    and the dashboard's own Quit covers exiting. Appends the icon's stop to
    ``stoppers``, so a quit that starts anywhere else (the dashboard's button)
    also ends this loop instead of leaving the process up behind a dead server.
    """
    import logging
    log = logging.getLogger("artwork_fetcher")
    try:
        import pystray
    except ImportError as e:
        log.error("Tray unavailable (%s); serving without it", e)
        return False

    from . import __version__, config

    def open_page(path=""):
        return lambda: webbrowser.open(base_url + path)

    def quit_app(icon):
        icon.visible = False
        icon.stop()
        on_quit()

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", open_page(), default=True),
        pystray.MenuItem("Overlay setup", open_page("overlay")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open data folder", lambda: _open_folder(config.DATA_DIR)),
        pystray.MenuItem("Open log folder",
                         lambda: _open_folder(config.LOG_FILE.parent)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(f"Version {__version__}", None, enabled=False),
        pystray.MenuItem("Quit", quit_app),
    )
    icon = pystray.Icon("AlbumArtOverlay", _tray_image(),
                        "Album Art Overlay", menu)
    stoppers.append(icon.stop)

    def setup(icon):
        icon.visible = True
        if first_run and icon.HAS_NOTIFICATION:
            icon.notify("Album Art Overlay is running down here. Close the "
                        "browser tab whenever you like; your overlays keep "
                        "working until you choose Quit.", "Album Art Overlay")

    try:
        icon.run(setup=setup)
    except Exception:  # noqa: BLE001 — any tray failure must not stop serving
        log.exception("Tray icon failed; serving without it")
        return False
    return True


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    args = _parse(list(sys.argv[1:] if argv is None else argv))
    # Must be set before app.config is imported — its paths are module-level.
    if args.data_dir:
        os.environ["ALBUMART_DATA_DIR"] = args.data_dir

    if args.self_check:
        return self_check()

    try:
        return _serve(args)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — last resort for a windowed app
        log_file = None
        try:
            import logging

            from . import config
            logging.getLogger("artwork_fetcher").exception("Startup failed")
            log_file = config.LOG_FILE
        except Exception:  # noqa: BLE001
            pass
        _message_box(f"Album Art Overlay couldn't start:\n\n{e}"
                     + (f"\n\nDetails are in the log:\n{log_file}" if log_file else ""))
        return 1


def _serve(args) -> int:
    import logging

    from werkzeug.serving import make_server

    from . import artwork, config, dashboard, instance, runtime

    log = logging.getLogger("artwork_fetcher")
    port = args.port
    base_url = f"http://{HOST}:{port}/"
    artwork.setup_logging()

    # 1. Another copy holds the lock: it is running or starting, so hand the
    #    user its dashboard rather than a second process.
    # 2. Nobody holds it but the port answers: a copy without the lock (an
    #    older version, or a source run) — same answer. Something else on the
    #    port can't be fixed from here, only reported.
    if instance.acquire():
        owner = dashboard._port_owner(HOST, port)
    else:
        owner = dashboard.wait_for_dashboard(HOST, port)
        if owner is None:
            log.warning("Another copy holds the instance lock but isn't "
                        "answering on %s", base_url)
            _message_box("Album Art Overlay is already running, but isn't "
                         "answering yet. Try again in a moment.", error=False)
            return 1
    if owner == "dashboard":
        log.info("Already running; opening %s", base_url)
        if not args.background:
            webbrowser.open(base_url)
        return 0
    if owner == "other":
        log.error("Port %s is in use by another program; not starting", port)
        _message_box(f"Album Art Overlay can't start, because another program "
                     f"is already using port {port} on this computer.\n\n"
                     f"Close that program and start Album Art Overlay again.")
        return 1

    # Any stray relative path lands in the data folder, as run_dashboard.bat's
    # `cd` arranges for a source run.
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(config.DATA_DIR)
    first_run = dashboard.prepare()
    log.info("Album Art Overlay starting (data folder: %s)", config.DATA_DIR)

    # Listening as soon as the constructor returns, so the browser can open
    # straight away rather than after run()'s one-second guess.
    server = make_server(HOST, port, dashboard.app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever,
                                     name="http", daemon=True)
    server_thread.start()
    if not args.background:
        webbrowser.open(base_url + ("settings" if first_run else ""))

    stopped = threading.Event()
    stoppers = []  # the tray's stop, once it is running

    def shutdown():
        if stopped.is_set():
            return
        stopped.set()
        log.info("Quitting")
        runtime.service.stop()
        for stop in stoppers:
            try:
                stop()
            except Exception:  # noqa: BLE001 — already stopping is fine
                pass
        # From another thread: shutdown() waits for serve_forever to return.
        threading.Thread(target=server.shutdown, daemon=True).start()

    dashboard.request_quit = shutdown

    if args.no_tray or not run_tray(base_url, shutdown, first_run, stoppers):
        # No tray: serve until something calls shutdown() (the dashboard's
        # Quit, or the process being ended).
        try:
            while server_thread.is_alive():
                server_thread.join(0.5)
        except KeyboardInterrupt:
            shutdown()
    server_thread.join(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
