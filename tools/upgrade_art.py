"""
Re-fetch the library's iTunes covers at the size the app now asks for.

iTunes covers used to be downloaded at 600x600 — smaller than the 640px image
the app writes for OBS, so nearly every cover on stream was an enlargement.
New searches fetch sources.ITUNES_ART_SIZE. This brings the covers already in
the library up to match without re-reviewing anything: each entry records the
exact iTunes URL its image came from, and Apple serves that artwork at any size.

    python -m tools.upgrade_art            # dry run: check every cover, change nothing
    python -m tools.upgrade_art --apply    # replace them

Safe by construction:
  * only images that came from iTunes, and only ones smaller than the new size;
  * the new download must look like the stored image (the difference hash the
    art wall uses to spot duplicate covers). The URL could now serve different
    artwork, so anything that doesn't match is left alone and listed;
  * every original is copied to library/pre-upgrade/ before it is replaced, so
    undoing it is copying them back;
  * covers hidden from the art wall stay hidden — the wall keys them by the
    file's contents, which change, so their keys are carried over;
  * it will not run while the dashboard is open, because both would be
    writing the library.
"""

import argparse
import shutil
import socket
import sys
import time
from io import BytesIO
from pathlib import Path

# Run from the repo root so `python -m tools.upgrade_art` finds the app package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image  # noqa: E402

from app import config, fileio, imaging, library, sources  # noqa: E402

# Most differing bits (of 64) still counted as the same cover. A re-encode or a
# resize of the same artwork moves a few bits at most; different artwork moves
# dozens.
MAX_DISTANCE = 6

# Politeness gap between downloads from Apple's image server.
DOWNLOAD_GAP = 0.3


def _dashboard_running(port: int = 5050) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _source_url(entry: dict):
    """The iTunes URL the entry's image was downloaded from, or None."""
    cands = entry.get("candidates") or []
    idx = entry.get("candidate_index", -1)
    if 0 <= idx < len(cands) and cands[idx].get("source") == "itunes":
        return cands[idx].get("url")
    # A live grab records no candidate index — only the URL it downloaded.
    tried = entry.get("candidates_tried") or []
    return tried[-1] if tried else None


def _carry_hidden_covers(lib, hidden: list, remap: dict) -> list:
    """
    The art wall's hidden list after the upgrade. Each hidden key gains the
    keys of the files that replaced it, and keeps its own only while some file
    in the library still has it.
    """
    still_present = lib.paths_by_hash()
    carried = []
    for digest in hidden:
        for new in sorted(remap.get(digest, ())):
            if new not in carried:
                carried.append(new)
        if (digest not in remap or digest in still_present) and digest not in carried:
            carried.append(digest)
    return carried


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-fetch iTunes covers at the current size.")
    ap.add_argument("--apply", action="store_true",
                    help="replace the images (without it, nothing is changed)")
    args = ap.parse_args()

    if _dashboard_running():
        print("The dashboard is running. Close it first — both would be "
              "writing to the library.")
        return 1

    target = int(sources.ITUNES_ART_SIZE.split("x", 1)[0])
    lib = library.Library()
    cfg = config.load_config()
    hidden = list(cfg.get("wall", {}).get("exclude") or [])
    backup_dir = lib.dir / "pre-upgrade"

    entries = [e for e in lib.all_entries().values()
               if isinstance(e, dict) and e.get("source") == "itunes"
               and lib.has_image(e)]
    done, big_enough, nothing_larger = [], 0, 0
    different, failed, no_url = [], [], []
    remap = {}  # hidden content hash -> hashes of the files that replaced it
    bytes_before = bytes_after = 0
    distances = []

    print(f"{len(entries)} covers came from iTunes; upgrading any under {target}px"
          f"{'' if args.apply else ' (dry run — nothing will be changed)'}.\n")
    try:
        for entry in entries:
            name = f"{entry.get('artist')} - {entry.get('title')}"
            path = lib.image_path(entry)
            with Image.open(path) as im:
                # The longer side: a non-square original comes back 1000x979.
                if max(im.size) >= target:
                    big_enough += 1
                    continue
                old_size = max(im.size)
                old_hash = library.dhash(im)
            url = _source_url(entry)
            if not url or "mzstatic.com" not in url:
                no_url.append(name)
                continue
            try:
                raw = imaging.download_image_bytes(sources.itunes_art_url(url))
                with Image.open(BytesIO(raw)) as im:
                    im.load()
                    new_size = max(im.size)
                    distance = bin(library.dhash(im) ^ old_hash).count("1")
                png = imaging.to_png_bytes(raw)
            except Exception as e:  # noqa: BLE001 - network, 404, undecodable
                failed.append(f"{name} ({e})")
                time.sleep(DOWNLOAD_GAP)
                continue
            if new_size <= old_size:
                # Apple only scales down: when the label uploaded a 600px
                # cover, 600px is all there is. Nothing to gain by rewriting.
                nothing_larger += 1
                time.sleep(DOWNLOAD_GAP)
                continue
            distances.append(distance)
            if distance > MAX_DISTANCE:
                different.append(f"{name} ({distance} of 64 bits differ)")
                time.sleep(DOWNLOAD_GAP)
                continue

            bytes_before += path.stat().st_size
            bytes_after += len(png)
            if args.apply:
                old_md5 = library.content_hash(path)
                backup_dir.mkdir(parents=True, exist_ok=True)
                backup = backup_dir / path.name
                if not backup.exists():  # a second run must not replace the original
                    shutil.copy2(path, backup)
                fileio.write_atomic(path, png)
                lib.touch(entry)
                if old_md5 in hidden:
                    remap.setdefault(old_md5, set()).add(library.content_hash(path))
            done.append(name)
            if len(done) % 20 == 0:
                print(f"  {len(done)} {'upgraded' if args.apply else 'checked'}…")
            time.sleep(DOWNLOAD_GAP)
    finally:
        # Also on Ctrl+C: whatever was replaced gets its cache-busting stamp and
        # keeps its hidden-from-the-wall status.
        if args.apply and done:
            lib.save()
            if remap:
                cfg.setdefault("wall", {})["exclude"] = _carry_hidden_covers(lib, hidden, remap)
                config.save_config(cfg)

    verb = "Upgraded" if args.apply else "Would upgrade"
    print(f"\n{verb} {len(done)} covers to {target}px"
          f"{f'; {big_enough} already big enough' if big_enough else ''}"
          f"{f'; {nothing_larger} where iTunes has nothing larger' if nothing_larger else ''}.")
    if done:
        print(f"Library size for those: {bytes_before / 1e6:.0f} MB -> "
              f"{bytes_after / 1e6:.0f} MB.")
    if distances:
        spread = ", ".join(f"{d} bits: {distances.count(d)}"
                           for d in sorted(set(distances)))
        print(f"How far each download is from the stored image (of 64 bits; "
              f"over {MAX_DISTANCE} is left alone) — {spread}.")
    if args.apply and done:
        print(f"Originals are in {backup_dir} — delete that folder once you're happy.")
        if remap:
            print(f"{len(remap)} covers hidden from the art wall stay hidden.")
    for title, rows in (("Left alone — the URL now serves different artwork:", different),
                        ("Download failed:", failed),
                        ("No iTunes URL recorded:", no_url)):
        if rows:
            print(f"\n{title}")
            for row in rows:
                print(f"  {row}")
    if not args.apply and done:
        print("\nRun again with --apply to make the change.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
