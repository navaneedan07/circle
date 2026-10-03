"""Safe file handling: checksums, filename sanitization, type/size validation
and ZIP-slip-proof extraction. Untrusted input lives here."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import zipfile
from pathlib import Path

MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024   # hard cap for archives
MAX_ZIP_ENTRIES = 5000

ALLOWED_EXTENSIONS = {
    ".txt", ".json", ".csv", ".html", ".htm", ".pdf", ".docx",
    ".wav", ".mp3", ".m4a", ".ogg", ".webm",
    ".zip", ".eml", ".mbox", ".ics", ".vcf",
}

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg", ".webm"}

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class FileSecurityError(Exception):
    """Raised when a file violates a security policy."""


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sanitize_filename(name: str) -> str:
    """Strip path components, control chars, traversal and reserved names."""
    name = (name or "").replace("\x00", "")
    name = name.replace("\\", "/").split("/")[-1]         # drop any path
    name = re.sub(r"[\r\n\t]", "", name)
    name = re.sub(r'[<>:"|?*]', "_", name).strip().lstrip(".")
    if not name:
        name = "unnamed"
    stem = Path(name).stem
    if stem.upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name[:180]


def validate_extension(path: Path) -> str:
    ext = path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise FileSecurityError(f"unsupported file type: {ext or '(none)'}")
    return ext


def validate_size(path: Path, max_bytes: int) -> int:
    size = path.stat().st_size
    if size == 0:
        raise FileSecurityError("empty file")
    if size > max_bytes:
        raise FileSecurityError(f"file too large ({size} > {max_bytes} bytes)")
    return size


def is_probably_text(path: Path, sample: int = 4096) -> bool:
    """Reject binary blobs pretending to be text."""
    with path.open("rb") as f:
        head = f.read(sample)
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError:
        return True   # likely latin-1 / other text; parsers handle decoding
    return True


def safe_extract_zip(zip_path: Path, dest_dir: Path) -> list[Path]:
    """Extract a ZIP preventing ZIP-slip/path traversal and zip bombs.

    Returns extracted file paths. Raises FileSecurityError on any violation.
    """
    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as e:
        raise FileSecurityError(f"corrupt zip: {e}") from e

    extracted: list[Path] = []
    total = 0
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise FileSecurityError("too many entries in archive")
        for info in infos:
            if info.is_dir():
                continue
            total += info.file_size
            if total > MAX_UNCOMPRESSED_BYTES:
                raise FileSecurityError("archive uncompressed size exceeds limit")
            # Normalize member name and forbid traversal
            name = info.filename.replace("\\", "/")
            if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
                raise FileSecurityError(f"absolute path in archive: {info.filename}")
            parts = [p for p in name.split("/") if p not in ("", ".")]
            if any(p == ".." for p in parts):
                raise FileSecurityError(f"path traversal in archive: {info.filename}")
            if info.file_size > MAX_UNCOMPRESSED_BYTES:
                raise FileSecurityError("entry too large")
            target = (dest_dir / Path(*parts)).resolve()
            if not str(target).startswith(str(dest_dir) + os.sep) and target != dest_dir:
                raise FileSecurityError(f"zip-slip blocked: {info.filename}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                remaining = info.file_size
                while remaining > 0:
                    buf = src.read(min(1 << 20, remaining))
                    if not buf:
                        break
                    out.write(buf)
                    remaining -= len(buf)
            extracted.append(target)
    return extracted


def move_file(src: Path, dest_dir: Path) -> Path:
    """Move src into dest_dir (dedupe name collision), never overwriting."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    counter = 1
    final = dest_dir / sanitize_filename(src.name)
    while final.exists():
        final = dest_dir / f"{src.stem}_{counter}{src.suffix}"
        counter += 1
    shutil.move(str(src), str(final))
    return final
