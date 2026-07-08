from __future__ import annotations

import os
import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main() -> int:
    os.chdir(app_dir())
    try:
        from native_app import main as native_main
    except ImportError as error:
        print("OpenStitch native UI requires PySide6.")
        print("Run: python -m pip install -r requirements.txt")
        raise SystemExit(1) from error
    return native_main()


if __name__ == "__main__":
    raise SystemExit(main())
