"""Cloud storage folder detection.

Circle never talks to a cloud API. The user syncs their own folder with
Google Drive for Desktop (or OneDrive) and points Circle at the local path.
All this module does is answer two questions:

  1. Is a Google Drive install present, and where does it put files?
  2. Is this file a *placeholder* whose bytes are not on disk yet?

(2) matters most. In streaming mode a placeholder reports its full size from
the cloud metadata, but reading it is what triggers the download. Reading a
half-downloaded file yields a wrong checksum and a corrupt import, so the
watcher has to materialize the file before it is measured or hashed.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# Win32 file attributes that mark a cloud/recallable placeholder.
FILE_ATTRIBUTE_OFFLINE = 0x00000010
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_PINNED = 0x00080000
FILE_ATTRIBUTE_UNPINNED = 0x00100000

_PLACEHOLDER_ATTRS = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    | FILE_ATTRIBUTE_REPARSE_POINT
)


def _windows() -> bool:
    return os.name == "nt"


def file_attributes(path: Path) -> int:
    """Win32 attribute bits, or 0 off Windows."""
    if not _windows():
        return 0
    try:
        return int(getattr(os.stat(path), "st_file_attributes", 0) or 0)
    except OSError:
        return 0


def is_placeholder(path: Path) -> bool:
    """True when the bytes for this file are not local yet.

    Off Windows this is always False (no reparse-point concept): macOS and
    Linux mounts behave like ordinary files, which the stability check
    handles on its own.
    """
    attrs = file_attributes(path)
    if not attrs:
        return False
    return bool(attrs & _PLACEHOLDER_ATTRS)


def is_pinned(path: Path) -> Optional[bool]:
    """True/False when the cloud provider pins the file locally, else None."""
    attrs = file_attributes(path)
    if attrs & FILE_ATTRIBUTE_PINNED:
        return True
    if attrs & FILE_ATTRIBUTE_UNPINNED:
        return False
    return None


def materialize(path: Path, timeout: float = 600.0,
                chunk: int = 1024 * 1024) -> bool:
    """Force a placeholder to download by reading it once.

    Returns True when the file was fully readable. Reading is the only
    portable trigger for a cloud provider's recall: there is no API call to
    make. Callers must treat a False result as "not ready, retry later".
    """
    import time
    deadline = time.time() + max(timeout, 5.0)
    while True:
        read = 0
        try:
            with path.open("rb") as f:
                while True:
                    block = f.read(chunk)
                    if not block:
                        break
                    read += len(block)
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(1.0)
            continue
        try:
            expected = path.stat().st_size
        except OSError:
            return False
        if expected == 0 or read >= expected:
            return True
        # Short read: the provider is still streaming the rest.
        if time.time() >= deadline:
            return False
        time.sleep(1.0)


def _drive_candidates() -> list[Path]:
    local = os.environ.get("LOCALAPPDATA", "")
    program_files = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    out = [
        Path(local) / "Google" / "DriveFS" if local else None,
        Path(local) / "Google" / "Drive" if local else None,
    ]
    out += [Path(p) / "Google" / "DriveFS" for p in program_files if p]
    return [p for p in out if p]


def google_drive_install() -> dict:
    """Where Google Drive for Desktop lives on this machine.

    Detection only. Circle does not read the user's Drive credentials and
    does not sign in: the sync client does that, and Circle only watches the
    folder it makes available locally.
    """
    if not _windows():
        return {"installed": False, "platform": os.name, "roots": []}
    roots: list[str] = []
    installed = False
    for cand in _drive_candidates():
        try:
            if cand.exists():
                installed = True
                roots.append(str(cand))
        except OSError:
            continue
    # The default stream locations, in Drive for Desktop's own preference order.
    for letter in ("G", "F", "C"):
        drive = Path(f"{letter}:\\") / "My Drive"
        try:
            if drive.exists():
                roots.append(str(drive))
        except OSError:
            continue
    return {"installed": installed, "platform": os.name, "roots": roots}


def classify_folder(path: Path) -> str:
    """Which provider (if any) serves this folder, by path signature."""
    text = str(path).lower()
    if "my drive" in text or "googledrive" in text or "google drive" in text \
            or "drivefs" in text:
        return "google_drive"
    if "onedrive" in text:
        return "onedrive"
    if "icloud" in text or "com~apple~clouddocs" in text:
        return "icloud"
    if "dropbox" in text:
        return "dropbox"
    return "local"


def provider_installed(kind: str) -> Optional[bool]:
    if kind == "google_drive":
        return google_drive_install()["installed"]
    if kind in ("onedrive", "dropbox", "icloud"):
        return None      # not probed; the folder itself is the signal
    return None


def folder_status(path: Path) -> dict:
    """Everything the Settings screen needs to describe a watch folder."""
    try:
        exists = path.exists()
    except OSError:
        exists = False
    kind = classify_folder(path)
    return {
        "path": str(path),
        "exists": exists,
        "kind": kind,
        "cloud": kind != "local",
        "provider_installed": provider_installed(kind),
        "is_drive_letter": path.drive.upper().endswith(":\\") if _windows() else False,
    }