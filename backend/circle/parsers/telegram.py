"""Telegram JSON export parser (official desktop export format).

Handles:
  {"name": ..., "type": "personal_chat"/"private_group", "id": ..., "messages": [...]}
  and newer desktop exports wrapping chats in {"chats": {"list": [...]}}.

Message text may be a plain string or a list of {'type': 'text'|'link'|...,
'text': ...} segments. Media references and reply ids are preserved.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from circle.domain.models import DataOrigin, Message, SourceType
from circle.parsers.common import (
    ParseResult, clean_text, message_id, parse_iso,
)


def _load_json(path: Path) -> Any:
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError("not valid JSON")


def _flatten_text(text: Any) -> str:
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        parts = []
        for seg in text:
            if isinstance(seg, dict):
                parts.append(str(seg.get("text", "")))
            else:
                parts.append(str(seg))
        return "".join(parts)
    return str(text or "")


def _iter_chats(data: Any) -> list[dict]:
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict) and "messages" in c]
    if isinstance(data, dict):
        if "messages" in data and ("name" in data or "title" in data or "id" in data):
            return [data]
        # {"chats": {"list": [...]}}
        chats = data.get("chats")
        if isinstance(chats, dict) and isinstance(chats.get("list"), list):
            return [c for c in chats["list"] if isinstance(c, dict)]
        for key in ("list", "conversations", "chats"):
            if isinstance(data.get(key), list):
                return [c for c in data[key] if isinstance(c, dict)]
    return []


_MEDIA_KEYS = ("photo", "file", "thumbnail", "sticker_emoji", "path",
               "media_type", "voice", "video_file")


def parse_telegram(path: Path) -> ParseResult:
    data = _load_json(path)
    chats = _iter_chats(data)
    if not chats:
        raise ValueError("no Telegram chats found in JSON")

    result = ParseResult()
    for chat in chats:
        title = clean_text(chat.get("name") or chat.get("title") or "conversation")
        chat_type = str(chat.get("type") or "")
        participants = [clean_text(p) for p in (chat.get("participants") or [])
                        if isinstance(p, dict)]
        if not participants:
            participants = [clean_text(chat.get("name") or "")]
        conv_key = f"telegram:{re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-') or 'chat'}"
        result.conversations.append({
            "external_key": conv_key,
            "title": title,
            "source": SourceType.TELEGRAM,
            "participants": participants,
            "kind": chat_type,
        })

        for m in chat.get("messages") or []:
            if not isinstance(m, dict):
                continue
            if m.get("type") not in (None, "message"):
                continue  # service messages
            body = _flatten_text(m.get("text"))
            sender = clean_text(m.get("from") or m.get("actor") or "")
            when = parse_iso(m.get("date"))
            if not body and not m.get("photo") and not m.get("file"):
                continue

            attachments = []
            for k in _MEDIA_KEYS:
                if m.get(k):
                    attachments.append({"kind": k, "label": str(m.get(k))})
            if not sender:
                sender = "system"
                continue  # skip service events without sender

            msg = Message(
                id=message_id("telegram", conv_key, when, sender, body),
                conversation_id=conv_key,
                sender_label=sender,
                sent_at=when,
                content=clean_text(body),
                attachments=attachments,
                reply_to=str(m.get("reply_to_message_id") or "") or None,
                source=SourceType.TELEGRAM,
                origin=DataOrigin.IMPORTED,
                external_id=conv_key,
            )
            result.messages.append(msg)

    if not result.messages:
        raise ValueError("Telegram export contained no messages")
    return result
