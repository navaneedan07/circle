"""Open Circle in the browser once the server is actually answering.

Called in the background by serve.sh and serve.bat. Waiting matters: opening
the browser immediately means the user's first impression is a connection
error, which looks like the app is broken.

Kept in Python rather than in each shell so both launchers share one
implementation. Uses only the standard library, and never raises: a machine
with no desktop browser must still start Circle.
"""
from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
import webbrowser

DEFAULT_TIMEOUT = 90


def wait_for(url: str, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Poll the health endpoint until it answers, or the timeout runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                return True
            # unreachable anyway
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    return False


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: wait_and_open.py <url>", file=sys.stderr)
        return 2
    url = argv[1].rstrip("/")
    ready = wait_for(f"{url}/api/health")
    # Open on the root regardless: the health check is just a readiness probe.
    try:
        webbrowser.open(url)
    except Exception as exc:  # pragma: no cover - depends on the desktop
        print(f"could not open a browser: {exc}", file=sys.stderr)
        return 0
    if not ready:
        print(f"opened {url} before the server confirmed it was ready",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))