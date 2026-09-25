"""
Crash-safe file writes for everything the app cannot regenerate: the library
manifest, the library's images, and config.json.

Each write goes to a temporary file beside the target, is flushed to disk, and
is then renamed over the target. A reader — or a crash, or the console window
being closed mid-save — only ever sees the old file or the new one, never half
of each. Before this, the manifest was rewritten in place, and a torn write
read back as an empty library.

The output PNG OBS displays does not use this: imaging.atomic_save_image has
its own, deliberately different rules (it falls back to writing in place,
because OBS must get *an* image). Here a blocked rename is an error instead —
a half-written manifest is exactly what this module exists to prevent.
"""

import os
import threading
import time
from pathlib import Path

# On Windows a rename over a file fails while another handle has it open —
# Python opens files without FILE_SHARE_DELETE, so a request thread serving an
# image, a virus scanner or a sync client can each block it for a moment.
# Those handles are short-lived, so the rename is retried for about a second.
_REPLACE_ATTEMPTS = 20
_REPLACE_DELAY = 0.05


def write_atomic(path, data: bytes) -> None:
    """
    Replace ``path`` with ``data`` atomically. Raises OSError if the file
    cannot be written or the rename stays blocked; the target is untouched in
    that case.
    """
    path = Path(path)
    # Unique per thread: two request threads saving at once must not share,
    # and then fight over, one temp file.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            # Without this a power cut can leave the renamed file empty: the
            # rename is journalled, the data it points at may not be on disk.
            os.fsync(fh.fileno())
        for _ in range(_REPLACE_ATTEMPTS - 1):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                time.sleep(_REPLACE_DELAY)
        os.replace(tmp, path)  # the last try's error propagates
    finally:
        # Only still there if something above failed.
        try:
            tmp.unlink()
        except OSError:
            pass


def read_bytes(path) -> bytes:
    """
    Read a whole file, riding out the same brief Windows sharing violations
    ``write_atomic`` does — a scanner holding the file must not read as the
    file being broken.
    """
    path = Path(path)
    for _ in range(_REPLACE_ATTEMPTS - 1):
        try:
            return path.read_bytes()
        except PermissionError:
            time.sleep(_REPLACE_DELAY)
    return path.read_bytes()  # the last try's error propagates
