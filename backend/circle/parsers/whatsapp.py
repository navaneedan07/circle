"""WhatsApp chat-export parser.

Supports the common export variants without assuming one format:

  12/31/23, 11:59 PM - Alice: hello          (US, 12h)
  31/12/2023, 23:59 - Alice: hello           (EU, 24h)
  [31/12/23, 23:59:59] Alice: hello          (bracketed desktop variant)
  31/12/23, 23:59 - Alice: hello             (with seconds)

Multi-line bodies, system messages and <Media omitted> style lines are
handled. Sender is split on the first ": ".
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from circle.domain.models import Message, SourceType, DataOrigin
from circle.parsers.common import (
    ParseResult, clean_text, deterministic_id, message_id,
    parse_export_datetime,
)
from circle.security.media import classify_media, media_filename_key

# Line forms:  DATE, TIME  -  SENDER: BODY
_LINE_RE = re.compile(
    r"^(?P<date>\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})\s*,\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\s*"
    r"(?:-|–)\s*(?P<rest>.*)$"
)
_BRACKET_RE = re.compile(
    r"^\[(?P<date>\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4})\s*,\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?)\]\s*"
    r"(?P<rest>.*)$"
)

_MEDIA_RE = re.compile(
    r"^(?:<)?(?:image|video|audio|voice|sticker|gif|document|contact card)"
    r"(?:\s+omitted|\s+attached|\s+\.?\w+)?(?:>)?$",
    re.IGNORECASE,
)
_OMITTED_RE = re.compile(r"media omitted|image omitted|video omitted|"
                         r"audio omitted|document omitted|gif omitted|"
                         r"sticker omitted", re.IGNORECASE)

# Real attachment references in modern WhatsApp exports:
#   IMG-20240101-WA0001.jpg (file attached)
#   <attached: 00000012-PHOTO-2024-01-01-12-01-01.jpg>
_MEDIA_EXT_ALT = (r"jpg|jpeg|png|webp|heic|gif|bmp|tif|tiff|mp4|3gp|mov|mkv|"
                  r"opus|ogg|oga|amr|m4a|mp3|wav|aac|wma|webm|pdf|doc|docx|"
                  r"xls|xlsx|ppt|pptx|vcf|epub|zip")
_ATTACHED_ANGLE_RE = re.compile(
    r"<?attached:\s*(?P<name>[^>\n]+?)\s*>", re.IGNORECASE)
_FILE_ATTACHED_RE = re.compile(
    r"(?P<name>[^\s<>\"|*?]+?\.(?:" + _MEDIA_EXT_ALT + r"))"
    r"\s*\(\s*file attached\s*\)", re.IGNORECASE)
_MEDIA_FILENAME_RE = re.compile(
    r"(?P<name>(?:IMG|PHOTO|PTT|AUD|VID|GIF|STK|DOC)[-_][^\s<>\"|*?]+?"
    r"\.(?:" + _MEDIA_EXT_ALT + r"))", re.IGNORECASE)
_BIDI_MARKS = ("\u200e", "\u200f", "\u202a", "\u202b", "\u202c", "\u202d",
               "\u202e", "\ufeff")
_NOISE_PREFIXES = (
    "messages and calls are end-to-end encrypted",
    "end-to-end encrypted",
    "this message was deleted",
    "you deleted this message",
    "waiting for this message",
    "tap and hold to unpin",
    "missed voice call",
    "missed video call",
    "changed the group description",
    "changed this group's subject",
    "created group",
    "added you",
    "left",
    "changed the group icon",
    "security code",
    "turned on disappearing messages",
    "turned off disappearing messages",
)


def _is_noise(text: str) -> bool:
    t = text.lower()
    return any(t.startswith(p) for p in _NOISE_PREFIXES)


def _clean_media_name(name: str) -> str:
    name = (name or "").strip().strip("\"'")
    for ch in _BIDI_MARKS:
        name = name.replace(ch, "")
    name = name.replace("\\", "/").split("/")[-1].strip()
    return name


def _attachments_from_body(body: str) -> list[dict]:
    """Extract real media references (filename + kind) from a message body."""
    names: list[str] = []
    # Explicit forms first; the bare-prefix scan is only a fallback so the
    # same filename is not captured twice from one reference.
    for rx in (_ATTACHED_ANGLE_RE, _FILE_ATTACHED_RE):
        for m in rx.finditer(body):
            name = _clean_media_name(m.group("name"))
            if name:
                names.append(name)
    if not names:
        for m in _MEDIA_FILENAME_RE.finditer(body):
            name = _clean_media_name(m.group("name"))
            if name:
                names.append(name)

    out: list[dict] = []
    seen: set[str] = set()
    for name in names:
        key = media_filename_key(name)
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            kind_value = classify_media(name)[0].value
        except Exception:
            kind_value = "media"
        out.append({"kind": kind_value, "filename": name,
                    "filename_key": key, "label": body})

    if not out and (_OMITTED_RE.search(body) or _MEDIA_RE.match(body)):
        # old exports strip the file entirely ("<Media omitted>")
        out.append({"kind": "media", "label": body})
    return out


def _caption_from_body(body: str) -> str:
    """Remove attachment references so only the human caption remains."""
    text = body
    for rx in (_MEDIA_FILENAME_RE, _ATTACHED_ANGLE_RE, _FILE_ATTACHED_RE):
        text = rx.sub(" ", text)
    text = re.sub(r"\(\s*file attached\s*\)", " ", text, flags=re.IGNORECASE)
    for ch in _BIDI_MARKS:
        text = text.replace(ch, " ")
    return clean_text(text)


def parse_whatsapp(path: Path) -> ParseResult:
    try:
        raw = path.read_bytes()
    except OSError as e:
        raise RuntimeError(f"cannot read file: {e}") from e

    text: Optional[str] = None
    for enc in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            text = raw.decode(enc)
            if "\ufffd" not in text[:200]:
                break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    result = ParseResult()
    lines = text.splitlines()

    # Split into (date, time, sender_or_none, body_lines[])
    entries: list[tuple[str, str, Optional[str], list[str]]] = []
    cur: Optional[tuple[str, str, Optional[str], list[str]]] = None

    def flush():
        nonlocal cur
        if cur is not None:
            entries.append(cur)
            cur = None

    for line in lines:
        m = _LINE_RE.match(line) or _BRACKET_RE.match(line)
        if m:
            flush()
            rest = m.group("rest")
            sender, body = _split_sender(rest)
            cur = (m.group("date"), m.group("time"), sender,
                   [body] if body else [])
        elif cur is not None:
            # continuation line
            cur[3].append(line)
    flush()

    if not entries:
        # Not a WhatsApp export we understand
        raise ValueError("no WhatsApp-style message lines found")

    # Conversation identity derives from the PARTICIPANTS, not the filename,
    # so re-exports of the same chat dedupe even when renamed (spec §30).
    sender_names = sorted({s for _, _, s, _ in entries if s})
    conv_key = ("whatsapp:d" + deterministic_id(*[s.lower() for s in sender_names])) \
        if sender_names else f"whatsapp:{path.stem.lower()}"
    result.conversations.append({
        "external_key": conv_key,
        "title": path.stem.replace("_chat", "").replace(".txt", ""),
        "source": SourceType.WHATSAPP,
        "participants": sender_names,
    })

    seen_conversations: set[str] = set()
    for date_s, time_s, sender, body_lines in entries:
        when = parse_export_datetime(date_s, time_s)
        body = "\n".join(body_lines).strip()
        if sender is None:
            # system/notification line -> keep only if meaningful
            if not body or _is_noise(body):
                continue
            continue
        if not body:
            continue
        if _is_noise(body):
            continue

        attachments = _attachments_from_body(body)
        has_files = any(a.get("filename") for a in attachments)
        content = _caption_from_body(body) if has_files else clean_text(body)

        msg = Message(
            id=message_id("whatsapp", conv_key, when, sender, body),
            conversation_id=conv_key,
            sender_label=sender,
            sent_at=when,
            content=content,
            attachments=attachments,
            source=SourceType.WHATSAPP,
            origin=DataOrigin.IMPORTED,
            external_id=conv_key,
        )
        result.messages.append(msg)

    if not result.messages and not entries:
        raise ValueError("empty WhatsApp export")
    return result


_SYSTEM_VERBS = re.compile(
    r"\b(added|removed|left|created|changed|turned on|turned off|deleted|"
    r"joined|pinned|cleared|is a participant|made)\b", re.IGNORECASE)


def _split_sender(rest: str) -> tuple[Optional[str], str]:
    """'Alice: hi' -> ('Alice', 'hi');  'no colon here' -> (None, rest)."""
    if ":" in rest:
        head, tail = rest.split(":", 1)
        head = head.strip()
        # Reject URL-ish heads, pure numbers (12:30) and system lines
        if (head and not re.fullmatch(r"[\d:]+", head) and len(head) < 100
                and "://" not in head and not head.lower().startswith(("www.", "http"))
                and not _SYSTEM_VERBS.search(head)):
            return head, tail.strip()
        return None, rest.strip()
    return None, rest.strip()
