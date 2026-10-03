"""In-import media staging (spec: media stays local).

`MediaIngest` copies real attachment files (photos, voice notes, video,
documents) into the content-addressed local media store as they are found in
an export, and indexes them by filename so the parser's attachment references
can be linked to the messages that mentioned them — regardless of whether the
media appeared before or after the chat file.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from circle.config import Settings
from circle.domain.models import DataOrigin, MediaAttachment, SourceType
from circle.security.files import FileSecurityError, sanitize_filename
from circle.security.media import (
    classify_media, media_filename_key, media_root, store_media,
)

log = logging.getLogger("circle.media")


class MediaIngest:
    def __init__(self, store, settings: Settings, stt=None):
        self.store = store
        self.settings = settings
        self.stt = stt
        self.root = media_root(settings.processed_dir())
        self._index: dict[str, MediaAttachment] = {}
        self.staged = 0

    def add(self, path: Path, source: SourceType = SourceType.CHAT,
            occurred_at=None) -> Optional[MediaAttachment]:
        """Store one media file, deduped by content; never raises."""
        path = Path(path)
        try:
            kind, mime = classify_media(str(path))
            stored, checksum, size = store_media(path, self.root, path.name)
        except (FileSecurityError, OSError) as e:
            log.warning("media rejected: %s (%s)", path.name, e)
            return None

        media_id = "media-" + checksum[:24]
        att = self.store.get_media(media_id)
        if att is None:
            att = MediaAttachment(
                id=media_id, filename=sanitize_filename(path.name),
                filename_key=media_filename_key(path.name),
                kind=kind, mime_type=mime, size_bytes=size, checksum=checksum,
                stored_path=str(stored), source=source,
                origin=DataOrigin.IMPORTED, occurred_at=occurred_at,
                status="STORED",
            )
            self.store.insert_media_if_new(att)
            self.staged += 1
        self._index[media_filename_key(att.filename)] = att
        self._index[media_filename_key(path.name)] = att
        return att

    def find(self, name_or_key: str) -> Optional[MediaAttachment]:
        """Look up staged media (falling back to anything already in DB)."""
        if not name_or_key:
            return None
        key = media_filename_key(name_or_key)
        return self._index.get(key) or self.store.find_media_by_filename_key(key)

    def find_by_key(self, key: str) -> Optional[MediaAttachment]:
        if not key:
            return None
        return self._index.get(key) or self.store.find_media_by_filename_key(key)
