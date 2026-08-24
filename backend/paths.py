"""Resolves the two directories the rest of the app needs, correctly both
when running from source and when frozen into a standalone PyInstaller exe:

- BUNDLE_DIR: where read-only bundled resources live (the frontend static
  files). PyInstaller extracts --add-data content to sys._MEIPASS at
  runtime; that's a temp directory that goes away when the exe exits, so
  nothing writable belongs there.
- APP_DIR: a real, persistent directory next to wherever the app is
  actually running from -- the source repo root in dev, or the folder
  containing the .exe itself when frozen. The sqlite database (and
  anything else that needs to survive between runs) lives under this one.
"""

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    BUNDLE_DIR = Path(sys._MEIPASS)
else:
    APP_DIR = Path(__file__).resolve().parent.parent
    BUNDLE_DIR = APP_DIR
