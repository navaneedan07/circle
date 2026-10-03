"""Media handling: classification, size limits and a safe local media store.

Media never leaves the machine. Files are copied into the local media store
(content-addressed by SHA-256, so duplicates are stored once) and served back
through the API by id — never by user-supplied path.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

from circle.domain.models import MediaKind
from circle.security.files import FileSecurityError, sanitize_filename, sha256_file

log = logging.getLogger("circle.media")

# extension -> (kind, mime)
EXTENSION_TYPES: dict[str, tuple[MediaKind, str]] = {
    ".jpg": (MediaKind.IMAGE, "image/jpeg"),
    ".jpeg": (MediaKind.IMAGE, "image/jpeg"),
    ".png": (MediaKind.IMAGE, "image/png"),
    ".gif": (MediaKind.IMAGE, "image/gif"),
    ".webp": (MediaKind.IMAGE, "image/webp"),
    ".heic": (MediaKind.IMAGE, "image/heic"),
    ".bmp": (MediaKind.IMAGE, "image/bmp"),
    ".tif": (MediaKind.IMAGE, "image/tiff"),
    ".tiff": (MediaKind.IMAGE, "image/tiff"),
    ".mp4": (MediaKind.VIDEO, "video/mp4"),
    ".3gp": (MediaKind.VIDEO, "video/3gpp"),
    ".mov": (MediaKind.VIDEO, "video/quicktime"),
    ".mkv": (MediaKind.VIDEO, "video/x-matroska"),
    ".opus": (MediaKind.VOICE, "audio/ogg"),
    ".ogg": (MediaKind.VOICE, "audio/ogg"),
    ".oga": (MediaKind.VOICE, "audio/ogg"),
    ".amr": (MediaKind.VOICE, "audio/amr"),
    ".m4a": (MediaKind.AUDIO, "audio/mp4"),
    ".mp3": (MediaKind.AUDIO, "audio/mpeg"),
    ".wav": (MediaKind.AUDIO, "audio/wav"),
    ".aac": (MediaKind.AUDIO, "audio/aac"),
    ".wma": (MediaKind.AUDIO, "audio/x-ms-wma"),
    ".webm": (MediaKind.AUDIO, "audio/webm"),
    ".pdf": (MediaKind.DOCUMENT, "application/pdf"),
    ".doc": (MediaKind.DOCUMENT, "application/msword"),
    ".docx": (MediaKind.DOCUMENT,
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".xls": (MediaKind.DOCUMENT, "application/vnd.ms-excel"),
    ".xlsx": (MediaKind.DOCUMENT,
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ".ppt": (MediaKind.DOCUMENT, "application/vnd.ms-powerpoint"),
    ".pptx": (MediaKind.DOCUMENT,
              "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ".vcf": (MediaKind.DOCUMENT, "text/vcard"),
    ".epub": (MediaKind.DOCUMENT, "application/epub+zip"),
    ".zip": (MediaKind.DOCUMENT, "application/zip"),
}

MEDIA_EXTENSIONS = set(EXTENSION_TYPES)

# WhatsApp filename prefixes carry the media kind
PREFIX_KINDS: dict[str, MediaKind] = {
    "IMG": MediaKind.IMAGE,
    "PHOTO": MediaKind.IMAGE,
    "PTT": MediaKind.VOICE,      # push-to-talk = voice note
    "AUD": MediaKind.AUDIO,
    "VID": MediaKind.VIDEO,
    "GIF": MediaKind.VIDEO,
    "STK": MediaKind.STICKER,
    "DOC": MediaKind.DOCUMENT,
}

MEDIA_MAX_BYTES = 200 * 1024 * 1024


def classify_media(filename: str,
                   fallback: Optional[MediaKind] = None) -> tuple[MediaKind, str]:
    """Return (kind, mime). Filename prefix hints win for voice vs audio."""
    name = Path(filename).name
    ext = Path(name).suffix.lower()
    base_kind, mime = EXTENSION_TYPES.get(ext, (MediaKind.OTHER, "application/octet-stream"))

    upper = name.upper()
    prefix_kind: Optional[MediaKind] = None
    for prefix, kind in PREFIX_KINDS.items():
        if upper.startswith(prefix + "-") or upper.startswith(prefix + "_"):
            prefix_kind = kind
            break
    if prefix_kind is not None:
        kind = prefix_kind
    elif fallback is not None and fallback != MediaKind.OTHER:
        kind = fallback
    else:
        kind = base_kind

    # An image-named sticker stays a sticker
    if fallback == MediaKind.STICKER:
        kind = MediaKind.STICKER
    return kind, mime


def store_media(src: Path, media_root: Path,
                filename_hint: str = "") -> tuple[Path, str, int]:
    """Copy a media file into the content-addressed local store.

    Returns (stored_path, sha256, size). Deduplicates identical files.
    """
    if not src.exists() or not src.is_file():
        raise FileSecurityError("media file not found")
    size = src.stat().st_size
    if size == 0:
        raise FileSecurityError("empty media file")
    if size > MEDIA_MAX_BYTES:
        raise FileSecurityError(f"media file too large ({size} bytes)")

    checksum = sha256_file(src)
    name = sanitize_filename(filename_hint or src.name)
    dest_dir = media_root / checksum[:2] / checksum[2:4]
    dest = dest_dir / f"{checksum[:12]}-{name}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(src, dest)
    return dest, checksum, size


def media_root(processed_dir: Path) -> Path:
    return processed_dir / "media"


def media_filename_key(filename: str) -> str:
    """Normalized lookup key so a referenced filename matches a stored one.

    WhatsApp exports sometimes wrap names in zero-width marks or change the
    case of the extension, so we compare on a sanitized, lowercased basename.
    """
    name = (filename or "").strip()
    # strip invisible bidi/zero-width marks WhatsApp adds to media lines
    name = name.replace("\u200e", "").replace("\u200f", "")
    name = name.replace("\u202a", "").replace("\u202b", "")
    name = name.replace("\u202c", "").replace("\u202d", "")
    name = name.replace("\u202e", "").replace("\ufeff", "")
    try:
        name = sanitize_filename(name)
    except Exception:
        name = Path(name).name
    return name.lower()


def is_media_extension(ext: str) -> bool:
    return (ext or "").lower() in MEDIA_EXTENSIONS


def resolve_served_path(stored_path: str, media_root_path: Path) -> Path:
    """Guard the media-serving endpoint against path escapes."""
    media_root_path = media_root_path.resolve()
    candidate = Path(stored_path).resolve()
    if not str(candidate).startswith(str(media_root_path)):
        raise FileSecurityError("media path outside the media store")
    if not candidate.exists():
        raise FileSecurityError("media file missing on disk")
    return candidate
