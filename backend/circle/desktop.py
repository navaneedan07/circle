"""Desktop entry point for the packaged Circle app.

A frozen build has no source tree, so it cannot rely on the working directory
or on relative paths: data goes next to the executable (or into the user's
Documents folder if that is not writable, which is the normal case inside
Program Files), and the bundled frontend is read from beside the binary.

Everything runs on one thread with the browser pointed at it. The user gets a
window and a tray-free, single-purpose app; there is no server to configure.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

APP_NAME = "Circle"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def bundle_dir() -> Path:
    """Where the bundled frontend lives.

    A one-file build unpacks itself to a temporary directory (sys._MEIPASS)
    and runs the executable from somewhere else entirely, so the bundled
    assets are next to sys._MEIPASS, not next to the .exe.
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def app_data_dir() -> Path:
    """A writable folder for the archive, media and logs.

    Program Files is not writable by a normal user, so the packaged app asks
    for Documents. Everything Circle owns lives in one folder: that is what
    makes "back up Circle" and "delete Circle" single, obvious operations.
    """
    override = os.environ.get("CIRCLE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        docs = Path.home() / "Documents" / APP_NAME
        try:
            docs.mkdir(parents=True, exist_ok=True)
            probe = docs / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return docs
        except OSError:
            pass
    return Path(sys.executable).resolve().parent / "circle-data" \
        if is_frozen() else bundle_dir() / "circle-data"


def prepare_environment() -> Path:
    """Point the app at a writable data folder before settings load."""
    data = app_data_dir()
    data.mkdir(parents=True, exist_ok=True)
    # IMPORT_ROOT is the folder whose exports get ingested. It defaults to a
    # Circle-managed layout inside the data folder, so a fresh install works
    # with no configuration at all.
    os.environ.setdefault("IMPORT_ROOT", str(data / "imports"))
    os.environ.setdefault("SQLITE_FILE", str(data / "circle.db"))
    os.environ.setdefault("STORAGE_BACKEND", "sqlite")
    os.environ.setdefault("ACCESS_KEY", "")     # loopback only
    os.environ.setdefault("WATCHER_ENABLED", "true")
    # Bundled builds carry no source tree, so the tests and tools are skipped.
    return data


def find_free_port(preferred: int = 8477) -> int:
    """Use the fixed port when it is free, otherwise let the OS choose.

    A stable port means a bookmark keeps working; falling back means two
    copies of Circle can both run without the second one dying at startup.
    """
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    return preferred


def open_when_ready(url: str, timeout: float = 90.0) -> None:
    """Open the browser once the API answers, not when the process starts.

    The first run loads models and scans folders, which takes a while; opening
    the window immediately would show a connection error the user cannot act on.
    """
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/api/health", timeout=3) as r:
                if r.status == 200:
                    webbrowser.open(url)
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1.0)
    webbrowser.open(url)


def main() -> int:
    data = prepare_environment()

    # The bundled frontend sits beside the executable; tell the app where,
    # because inside a frozen build the source-relative default is wrong.
    dist = bundle_dir() / "frontend"
    if dist.is_dir():
        os.environ["CIRCLE_FRONTEND_DIST"] = str(dist)

    from circle.config import get_settings
    settings = get_settings()
    port = find_free_port(settings.api_port)
    host = "127.0.0.1"
    url = f"http://{host}:{port}"

    print(f"{APP_NAME} is starting.")
    print(f"  data:     {data}")
    print(f"  address:  {url}")
    print("  Your data never leaves this machine. Close this window to quit.")

    if os.environ.get("CIRCLE_NO_BROWSER") != "1":
        threading.Thread(target=open_when_ready, args=(url,), daemon=True).start()

    # The app object is imported here, not passed as a string: inside a
    # frozen build uvicorn's import string has no source tree to resolve
    # against, so "circle.main:app" fails with a bare "could not import".
    from circle.main import app

    import uvicorn
    uvicorn.run(app, host=host, port=port,
                log_level=settings.log_level.lower(), access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())