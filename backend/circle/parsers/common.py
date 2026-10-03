"""Shared parsing utilities: normalized batch type, tolerant date parsing,
deterministic record identity (dedupe) and text cleanup."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from circle.domain.models import (
    CalendarEvent, Email, Message, Note, SourceType,
)

_WS_RE = re.compile(r"\s+")


@dataclass
class ParsedContact:
    """A person identity observed in a contacts export."""
    name: str = ""
    phone: str = ""
    email: str = ""
    organization: str = ""
    aliases: list[str] = field(default_factory=list)
    usernames: list[tuple[str, str]] = field(default_factory=list)  # (kind, value)


@dataclass
class ParseResult:
    """Everything a parser extracted, already in universal-model shapes."""
    messages: list[Message] = field(default_factory=list)
    emails: list[Email] = field(default_factory=list)
    events: list[CalendarEvent] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    documents: list[tuple[str, str, str]] = field(default_factory=list)  # (filename, title, text)
    contacts: list[ParsedContact] = field(default_factory=list)
    conversations: list[dict[str, Any]] = field(default_factory=list)
    detected_fields: list[str] = field(default_factory=list)  # for generic mapping UI
    warnings: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.messages or self.emails or self.events or self.notes
                    or self.documents or self.contacts)


class ParseError(Exception):
    """Parser cannot handle this file (goes to failed/)."""


# --------------------------------------------------------------------------
# Text cleanup
# --------------------------------------------------------------------------
def clean_text(text: Any) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = unicodedata.normalize("NFC", text)
    return _WS_RE.sub(" ", text).strip()


def normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits  # keep national/international as-is; matching happens on suffix


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def normalize_username(value: str) -> str:
    v = (value or "").strip().lower()
    for prefix in ("@", "https://instagram.com/", "https://x.com/", "https://twitter.com/",
                   "instagram.com/", "x.com/", "twitter.com/", "t.me/", "https://t.me/"):
        if v.startswith(prefix):
            v = v[len(prefix):]
    return v.strip("/")


# --------------------------------------------------------------------------
# Deterministic identity (duplicate detection, spec §30)
# --------------------------------------------------------------------------
def deterministic_id(*parts: Any) -> str:
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:32]


def message_id(source: str, conversation_key: str, sent_at: Optional[datetime],
               sender: str, content: str) -> str:
    ts = sent_at.isoformat() if sent_at else "unknown-time"
    return "msg-" + deterministic_id(source, conversation_key, ts, sender, content)


# --------------------------------------------------------------------------
# Date/time parsing (export formats vary wildly)
# --------------------------------------------------------------------------
_DATE_FORMATS = [
    # (format, dayfirst)
    ("%d/%m/%Y", True), ("%d/%m/%y", True),
    ("%m/%d/%Y", False), ("%m/%d/%y", False),
    ("%d-%m-%Y", True), ("%d-%m-%y", True),
    ("%m-%d-%Y", False), ("%m-%d-%y", False),
    ("%Y-%m-%d", False), ("%Y/%m/%d", False),
    ("%d.%m.%Y", True), ("%d.%m.%y", True),
]

_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM|am|pm)?"
)


def parse_export_datetime(date_str: str, time_str: str) -> Optional[datetime]:
    """Parse the varied date+time pairs found in chat exports."""
    date_str = (date_str or "").strip().replace(".", "/")
    time_str = (time_str or "").strip().upper()

    fmt_candidates: list[tuple[str, bool]] = []
    # Decide day-first vs month-first from the raw numbers when possible
    m = re.match(r"(\d{1,2})[\/\-](\d{1,2})[\/\-](\d{2,4})", date_str)
    dayfirst: Optional[bool] = None
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 >= b:
            dayfirst = True
        elif b > 12 >= a:
            dayfirst = False
    for fmt, df in _DATE_FORMATS:
        if dayfirst is not None and df != dayfirst:
            continue
        fmt_candidates.append((fmt, df))

    tm = _TIME_RE.search(time_str)
    hour = minute = second = 0
    ampm = None
    if tm:
        hour, minute = int(tm.group(1)), int(tm.group(2))
        second = int(tm.group(3) or 0)
        ampm = (tm.group(4) or "").upper()

    for fmt, _ in fmt_candidates:
        try:
            d = datetime.strptime(date_str, fmt)
        except ValueError:
            continue
        if ampm == "PM" and hour < 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and minute <= 59 and second <= 59:
            return datetime(d.year, d.month, d.day, hour, minute, second,
                            tzinfo=timezone.utc)
    # ISO fallback (Telegram etc.)
    try:
        dt = datetime.fromisoformat((date_str + " " + time_str).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_iso(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_epoch(value: Any) -> Optional[datetime]:
    """Epoch seconds or milliseconds (Instagram uses ms)."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v > 1e12:      # milliseconds
        v /= 1000.0
    if v <= 0:
        return None
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def guess_conversation_key(source: str, participants: list[str]) -> str:
    return source + ":" + "|".join(sorted(p.lower() for p in participants if p))
