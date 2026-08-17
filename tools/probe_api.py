"""
Probe the StreamerSonglist API with the app's own client.

Run this on the day production flips (or any time the overlay goes quiet) to
see, in seconds, which API is answering and whether the credentials work:

    python -m tools.probe_api                  # use config.json as-is
    python -m tools.probe_api --username foo   # override the username
    python -m tools.probe_api --base https://api.staging.streamersonglist.com

Reads the token from config.json and never prints it — only whether one is set.
Read-only: every call is a GET.
"""

import argparse
import sys

# Run from the repo root so `python -m tools.probe_api` finds the app package.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import requests  # noqa: E402

from app import config, songlist  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe the StreamerSonglist API.")
    ap.add_argument("--username", default="", help="override the configured username")
    ap.add_argument("--base", default="", help="override api_base (e.g. the staging host)")
    ap.add_argument("--platform", default="", help="override the platform")
    args = ap.parse_args()

    cfg = config.load_config()
    if args.base:
        cfg["api_base"] = args.base
    if args.platform:
        cfg["platform"] = args.platform
    username = args.username or cfg.get("streamersonglist_username") or ""

    host = songlist._host(cfg)
    token_set = bool((cfg.get("api_token") or "").strip())
    print(f"host      : {host}")
    print(f"platform  : {songlist._platform(cfg)}")
    print(f"username  : {username or '(none configured)'}")
    print(f"token     : {'set (' + (cfg.get('api_token_type') or 'User') + ')' if token_set else 'NOT set'}")

    if not username:
        print("\nNo username — pass --username to probe further.")
        return 2

    print()
    try:
        backend = songlist.detect_backend(cfg, username)
        print(f"backend   : {backend}")
    except songlist.AuthRequired as e:
        print("backend   : v2 (auth required)")
        print(f"\nFAIL: {e}")
        return 1
    except requests.RequestException as e:
        print(f"\nFAIL: probe request failed: {e}")
        return 1

    try:
        streamer = songlist.resolve_streamer(username, cfg=cfg)
        print(f"streamer  : id={streamer['id']} name={streamer['name']!r}")
        print(f"avatar    : {streamer['avatar'] or '(none)'}")

        count = songlist.fetch_song_count(streamer["id"], cfg=cfg)
        print(f"songs     : {count}")

        playing, upcoming = songlist.fetch_queue(streamer["id"], cfg=cfg)
        now = songlist.current_from_queue(playing, upcoming)
        # Print the now-playing requester too, not just the upcoming rows —
        # the now-playing card is a separate v2 object (NowPlayingDetails), so
        # a requester that resolves in the queue can still be missing here.
        now_view = songlist.queue_item_view(playing, 0) if playing else None
        print(f"playing   : {now[0] + ' — ' + now[1] if now else '(nothing)'}"
              f"{'  (' + now_view['requester'] + ')' if now_view and now_view['requester'] else ''}"
              f"{'' if playing else '   [slot empty; fell back to queue top]'}")
        print(f"upcoming  : {len(upcoming)}")
        for i, item in enumerate(upcoming[:5], start=1):
            view = songlist.queue_item_view(item, i)
            if view:
                print(f"   {i}. {view['title']} — {view['artist']}"
                      f"{'  (' + view['requester'] + ')' if view['requester'] else ''}")
    except songlist.AuthRequired as e:
        print(f"\nFAIL: {e}")
        return 1
    except requests.RequestException as e:
        print(f"\nFAIL: {e}")
        return 1

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
