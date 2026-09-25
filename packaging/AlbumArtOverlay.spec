# PyInstaller spec for the Windows build: one-folder, windowed (no console).
# Build with packaging/build.ps1, or directly:
#   pyinstaller packaging/AlbumArtOverlay.spec --distpath dist --workpath build
import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo,
    VarStruct, VSVersionInfo,
)

REPO = Path(SPECPATH).parent

# The Windows version resource (Properties -> Details) comes from
# app/__init__.py, the same __version__ tools/release.py bumps and tags.
VERSION = re.search(r'__version__\s*=\s*"([^"]+)"',
                    (REPO / "app" / "__init__.py").read_text()).group(1)
_parts = tuple(int(p) for p in VERSION.split(".")) + (0,) * 4
VERSION_INFO = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_parts[:4], prodvers=_parts[:4]),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "Alan Thompson"),
            StringStruct("FileDescription", "Album Art Overlay for StreamerSonglist"),
            StringStruct("FileVersion", VERSION),
            StringStruct("InternalName", "AlbumArtOverlay"),
            StringStruct("LegalCopyright", "Copyright (c) 2026 Alan Thompson. MIT License."),
            StringStruct("OriginalFilename", "AlbumArtOverlay.exe"),
            StringStruct("ProductName", "Album Art Overlay"),
            StringStruct("ProductVersion", VERSION),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

a = Analysis(
    [str(REPO / "packaging" / "entry.py")],
    pathex=[str(REPO)],
    datas=[
        (str(REPO / "app" / "templates"), "app/templates"),
        (str(REPO / "app" / "static"), "app/static"),
        # RESOURCE_DIR (sys._MEIPASS) is the bundle root: config.py reads
        # these from there and copies the loaders out to the data folder.
        (str(REPO / "obs"), "obs"),
        (str(REPO / "config.example.json"), "."),
        (str(REPO / "LICENSE"), "."),
        # The tray icon and the dashboard favicon (config.RESOURCE_DIR/icon.ico).
        (str(REPO / "packaging" / "icon.ico"), "."),
    ],
    hiddenimports=[
        # Imported only by the Browse... child process, via desktop.main().
        "app.filedialog",
        # websockets resolves `connect` lazily; the scan can't see it.
        "websockets.asyncio.client",
    ],
    # pystray is LGPL-3.0: ship it as plain .py files so it stays replaceable.
    module_collection_mode={"pystray": "py"},
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AlbumArtOverlay",
    console=False,
    # UPX-packed executables are a well-known antivirus trigger.
    upx=False,
    debug=False,
    strip=False,
    icon=str(REPO / "packaging" / "icon.ico"),
    version=VERSION_INFO,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="AlbumArtOverlay",
)
