"""PyInstaller entry point: the packaged app is the desktop launcher."""
from app.desktop import main

raise SystemExit(main())
