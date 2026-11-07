"""
One running copy of the app per Windows session, enforced with a named mutex.

The port probe (dashboard._port_owner) alone has a race: two launches a second
apart both find 5050 free, and because Werkzeug sets SO_REUSEADDR, both then
bind it — the 2026-08-17 trap, and double-clicking twice is exactly what an
impatient user does. Two processes mean two runtime services writing one
output PNG and two manifest writers, which library.MANIFEST_LOCK cannot
serialise because it only works within one process.

A named mutex has no such window: creating it is atomic, and Windows removes it
when the holder exits, however it exits, so a crash never leaves a stale lock.
The installer checks the same name (Inno Setup's AppMutex) before upgrading.
"""

import sys

# Shared with packaging/installer.iss (AppMutex). Changing it lets an old and a
# new version run side by side, and the installer stop noticing a running copy.
MUTEX_NAME = "AlbumArtOverlay.SingleInstance"

_ERROR_ALREADY_EXISTS = 183

# Kept for the life of the process: closing the last handle deletes the mutex.
_handle = None


def acquire() -> bool:
    """
    Claim the instance lock. True if this process now holds it (or already
    did); False if another copy does. Always True off Windows, which has no
    such lock here and never had a packaged build.
    """
    global _handle
    if _handle is not None or sys.platform != "win32":
        return True

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL,
                                      wintypes.LPCWSTR)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    error = ctypes.get_last_error()
    if not handle:
        # ERROR_ACCESS_DENIED: the mutex exists but was made by a process we
        # can't open, such as a copy started "as administrator" — the very
        # instance the 2026-08-17 trap was about. It is still another copy.
        return False
    if error == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False
    _handle = handle
    return True
