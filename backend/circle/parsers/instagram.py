"""Instagram data-export parser (user-provided export only -- no scraping).

Tolerant of the known export shapes:
  1. {"participants": [...], "title": ..., "messages": [...]}   (message_*.json)
  2. {"chats"|"conversations": [ <shape 1>, ... ]}
  3. NDJSON: one message/chat object per line
Message fields seen in the wild: sender_name, timestamp_ms, content,
type ('Generic'|'Share'|'Action:...'), photos/videos/audio attachments.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from circle.domain.models import DataOrigin, Message, SourceType
from circle.parsers.common import (
    ParseResult, clean_text, message_id, parse_epoch,
)
from circle.security.media import media_filename_key


def _load(path: Path) -> Any:
    raw = path.read_bytes()
    text: Optional[str] = None
    for enc in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    text = text.strip()
    if not text:
        raise ValueError("empty Instagram export")

    # Whole-document JSON first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # NDJSON fallback
    rows, ok = [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
            ok += 1
        except json.JSONDecodeError:
            continue
    if ok:
        return rows
    raise ValueError("not parseable as JSON/NDJSON")


def _iter_message_objs(data: Any):
    """Yield (chat_context, message_dict) pairs from any known shape."""
    if isinstance(data, dict):
        if "messages" in data:
            ctx = {"participants": data.get("participants") or [],
                   "title": data.get("title") or data.get("name") or ""}
            for m in data.get("messages") or []:
                if isinstance(m, dict):
                    yield ctx, m
            return
        for key in ("chats", "conversations", "inbox"):
            if isinstance(data.get(key), list):
                for chat in data[key]:
                    yield from _iter_message_objs(chat)
                return
        # single bare message
        if "sender_name" in data or "timestamp_ms" in data:
            yield {}, data
        return
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and (
                    "messages" in item or "sender_name" in item
                    or "participants" in item):
                yield from _iter_message_objs(item)


def _clean_sender(name: Any) -> str:
    s = clean_text(repair_mojibake(str(name or "")))
    # Instagram exports sometimes pad names with stray whitespace
    return re.sub(r"\s+", " ", s).strip()


def repair_mojibake(text: str) -> str:
    """Undo Meta's double-encoded UTF-8 in a data export.

    Instagram writes parts of an export as UTF-8 bytes that were then decoded
    as Latin-1, so an emoji arrives as mojibake: "\U0001f31d\U0001f31d" is stored
    as "ð\x9f\x8c\x9dð\x9f\x8c\x9d". Left alone this silently breaks search for
    every message that contains one.

    The round trip only succeeds on a byte sequence that is genuinely valid
    UTF-8, so ordinary text (including real Latin-1 names like "Søren") is
    returned unchanged.
    """
    if not text or text.isascii():
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    return fixed if fixed and "\ufffd" not in fixed else text


_ATTACHMENT_FIELDS = (
    ("photos", "image"), ("videos", "video"), ("audio", "audio"),
    ("gifs", "image"), ("audio_files", "audio"), ("files", "file"),
)


def _attachments(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Attachment descriptors keyed by filename, so media can be linked.

    Regression: these were collected with only a label and no filename_key, so
    the media linker never matched them and Instagram photos sat in the export
    unlinked. The uri is a path inside the export; the basename is what the
    file in photos/ is actually called.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field, kind in _ATTACHMENT_FIELDS:
        for item in m.get(field) or []:
            uri = item.get("uri", "") if isinstance(item, dict) else str(item)
            if not uri:
                continue
            name = uri.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if not name:
                continue
            key = media_filename_key(name)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            entry: dict[str, Any] = {"kind": kind, "label": name}
            if key:
                entry["filename"] = name
                entry["filename_key"] = key
            out.append(entry)
    return out


def parse_instagram(path: Path) -> ParseResult:
    data = _load(path)
    result = ParseResult()

    conv_cache: set[str] = set()
    count = 0
    for ctx, m in _iter_message_objs(data):
        sender = _clean_sender(m.get("sender_name"))
        if not sender:
            continue
        body = clean_text(repair_mojibake(m.get("content") or ""))
        when = parse_epoch(m.get("timestamp_ms") or m.get("timestamp"))
        mtype = str(m.get("type") or "Generic")

        participants = [_clean_sender(p) if isinstance(p, dict) else clean_text(p)
                        for p in (ctx.get("participants") or [])]
        title = clean_text(ctx.get("title") or "")
        if not participants:
            participants = [sender]
        base = title or "/".join(sorted(p for p in participants if p))
        conv_key = "instagram:" + re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")

        if conv_key not in conv_cache:
            conv_cache.add(conv_key)
            result.conversations.append({
                "external_key": conv_key,
                "title": title or base,
                "source": SourceType.INSTAGRAM,
                "participants": participants,
            })

        attachments = _attachments(m)
        if m.get("share") and not body:
            body = clean_text(str(m.get("share", {}).get("text", ""))) or "Shared a post"
        if mtype.startswith("Action:"):
            body = body or mtype.replace("Action:", "Action").replace("_", " ")

        if not body and not attachments:
            continue

        msg = Message(
            id=message_id("instagram", conv_key, when, sender,
                          body + "|" + "|".join(
                              a.get("filename", "") for a in attachments)),
            conversation_id=conv_key,
            sender_label=sender,
            sent_at=when,
            content=body,
            attachments=attachments,
            source=SourceType.INSTAGRAM,
            origin=DataOrigin.IMPORTED,
            external_id=conv_key,
        )
        result.messages.append(msg)
        count += 1

    if count == 0:
        raise ValueError("Instagram export contained no messages")
    return result
