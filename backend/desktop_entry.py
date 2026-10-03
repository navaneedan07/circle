"""Entry point for the packaged Circle app.

PyInstaller needs a single module to freeze. Keeping it this small means the
frozen binary has exactly one job, and the real logic stays testable in
circle/desktop.py.
"""
from __future__ import annotations

import multiprocessing
import sys


def main() -> int:
    # Required before anything spawns a process, or a frozen build would try
    # to re-execute itself and duplicate the app.
    multiprocessing.freeze_support()
    from circle.desktop import main as run
    return run()


if __name__ == "__main__":
    sys.exit(main())