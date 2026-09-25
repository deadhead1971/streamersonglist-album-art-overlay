# PyInstaller spec for the Windows build: one-folder, windowed (no console).
# Build with packaging/build.ps1, or directly:
#   pyinstaller packaging/AlbumArtOverlay.spec --distpath dist --workpath build
from pathlib import Path

REPO = Path(SPECPATH).parent

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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    upx=False,
    name="AlbumArtOverlay",
)
