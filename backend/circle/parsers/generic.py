"""Generic chat importer for TXT / JSON / CSV / HTML.

Two modes:
  - auto:      detect structure and parse without a mapping
  - mapped:    caller supplies {timestamp|sender|content|conversation: column}

Field detection powers the mapping UI:
    Detected fields: timestamp | sender | content
"""
from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup

from circle.domain.models import DataOrigin, Message, SourceType
from circle.parsers.common import (
    ParseResult, clean_text, message_id, parse_epoch, parse_iso,
)

# Common aliases so mapping can be guessed
ALIASES = {
    "timestamp": ("timestamp", "time", "date", "datetime", "created_at",
                  "createdAt", "sent_at", "sentAt", "ts", "when"),
    "sender": ("sender", "from", "author", "user", "username", "name",
               "sender_name", "senderName", "contact", "phone_number"),
    "content": ("content", "message", "text", "body", "msg", "value",
                "message_text", "comment"),
    "conversation": ("conversation", "chat", "chat_id", "thread", "room",
                     "dialog", "channel", "title"),
}

# "31/12/23, 23:59 - Alice: hi" | "2023-12-31 23:59 <Alice> hi"
_DT = r"\d{1,4}[\/\-.]\d{1,2}[\/\-.]\d{1,4}[, T]\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM|am|pm)?"
_LINE_PATTERNS = [
    # timestamp + <sender> body
    re.compile(rf"^(?P<dt>{_DT})\s*(?:[-–>]\s*)?<(?P<b1>[^>]+)>\s*(?P<body>.*)$"),
    # timestamp + [sender] body
    re.compile(rf"^(?P<dt>{_DT})\s*(?:[-–>]\s*)?\[(?P<b2>[^\]]+)\]\s*(?P<body>.*)$"),
    # timestamp - Sender: body
    re.compile(rf"^(?P<dt>{_DT})\s*[-–]\s*(?P<b3>[^<>\[\]]{{1,60}}?):\s*(?P<body>.*)$"),
    # Sender: body (no timestamp)
    re.compile(r"^(?P<b4>[A-Za-z][A-Za-z .\'-]{0,40}):\s+(?P<body>\S.*)$"),
]


def detect_fields(path: Path) -> list[str]:
    """Best-effort detection of available columns/keys for the mapping UI."""
    text = _read(path)
    ext = path.suffix.lower()
    if ext == ".json":
        data = _safe_json(text)
        rows = _json_rows(data)
        if rows:
            keys: list[str] = []
            for row in rows[:20]:
                for k in row.keys():
                    if k not in keys:
                        keys.append(k)
            return keys
        return []
    if ext == ".csv":
        try:
            reader = csv.reader(io.StringIO(text))
            header = next(reader)
            return [h.strip() for h in header]
        except StopIteration:
            return []
    if ext in (".html", ".htm"):
        soup = BeautifulSoup(text, "lxml")
        if soup.table:
            headers = [th.get_text(strip=True) for th in soup.table.find_all("th")]
            if headers:
                return headers
            first_row = soup.table.find("tr")
            if first_row:
                return [c.get_text(strip=True) for c in first_row.find_all(["td", "th"])]
        return []
    return []  # TXT: no discrete fields; line-pattern parsing is used


def _read(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _safe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _json_rows(data: Any) -> list[dict]:
    if isinstance(data, list):
        rows = [r for r in data if isinstance(r, dict)]
        if rows:
            return rows
        # list of containers holding lists of dicts
        out: list[dict] = []
        for item in data:
            if isinstance(item, dict):
                for v in item.values():
                    if isinstance(v, list):
                        out.extend(r for r in v if isinstance(r, dict))
        return out
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                rows = [r for r in v if isinstance(r, dict)]
                if rows:
                    return rows
        # maybe a single message
        if any(k in data for k in ("text", "message", "content")):
            return [data]
    return []


def _guess_mapping(columns: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    lower = {c.lower(): c for c in columns}
    for canon, aliases in ALIASES.items():
        for alias in aliases:
            if alias.lower() in lower:
                mapping[canon] = lower[alias.lower()]
                break
    return mapping


def _to_dt(value: Any) -> Optional[Any]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return parse_epoch(value)
    return parse_iso(value) or parse_iso(str(value))


def parse_generic(path: Path, mapping: Optional[dict[str, str]] = None) -> ParseResult:
    text = _read(path)
    ext = path.suffix.lower()
    result = ParseResult()

    if ext == ".json":
        rows = _json_rows(_safe_json(text))
        if not rows:
            raise ValueError("no message rows found in JSON")
        columns = list(dict.fromkeys(k for r in rows for k in r.keys()))
        result.detected_fields = columns
        mapping = mapping or _guess_mapping(columns)
        _rows_to_messages(rows, mapping, path, result)
    elif ext == ".csv":
        reader = csv.DictReader(io.StringIO(text))
        rows = [dict(r) for r in reader]
        if not rows:
            raise ValueError("empty CSV")
        columns = list(reader.fieldnames or [])
        result.detected_fields = columns
        mapping = mapping or _guess_mapping(columns)
        _rows_to_messages(rows, mapping, path, result)
    elif ext in (".html", ".htm"):
        rows = _html_to_rows(text)
        if not rows:
            raise ValueError("no rows found in HTML")
        columns = list(dict.fromkeys(k for r in rows for k in r.keys()))
        result.detected_fields = columns
        mapping = mapping or _guess_mapping(columns)
        _rows_to_messages(rows, mapping, path, result)
    else:  # TXT line-based
        _txt_to_messages(text, path, result)

    if result.is_empty():
        raise ValueError("no messages could be extracted")
    return result


def _rows_to_messages(rows: list[dict], mapping: dict[str, str],
                      path: Path, result: ParseResult) -> None:
    ts_col = mapping.get("timestamp")
    sender_col = mapping.get("sender")
    content_col = mapping.get("content")
    conv_col = mapping.get("conversation")
    if not content_col:
        # fall back: largest string-ish column heuristic is skipped; require content
        raise ValueError("content column not mapped")

    conv_label = clean_text(path.stem)
    conv_key = "chat:" + re.sub(r"[^a-z0-9]+", "-", conv_label.lower()).strip("-")
    result.conversations.append({"external_key": conv_key, "title": conv_label,
                                 "source": SourceType.CHAT})

    for i, row in enumerate(rows):
        body = clean_text(row.get(content_col))
        if not body:
            continue
        sender = clean_text(row.get(sender_col)) if sender_col else ""
        when = _to_dt(row.get(ts_col)) if ts_col else None
        conv = clean_text(row.get(conv_col)) if conv_col else ""
        key = conv_key
        if conv:
            key = "chat:" + re.sub(r"[^a-z0-9]+", "-", conv.lower()).strip("-")
            if not any(c["external_key"] == key for c in result.conversations):
                result.conversations.append(
                    {"external_key": key, "title": conv, "source": SourceType.CHAT})
        result.messages.append(Message(
            id=message_id("chat", key, when, sender, body),
            conversation_id=key,
            sender_label=sender or "unknown",
            sent_at=when,
            content=body,
            source=SourceType.CHAT,
            origin=DataOrigin.IMPORTED,
            external_id=key,
        ))


def _html_to_rows(text: str) -> list[dict]:
    soup = BeautifulSoup(text, "lxml")
    table = soup.find("table")
    if not table:
        return []
    rows = table.find_all("tr")
    if not rows:
        return []
    header_cells = rows[0].find_all(["th", "td"])
    headers = [c.get_text(strip=True) or f"col{i}"
               for i, c in enumerate(header_cells)]
    if not all(h for h in headers):
        headers = [f"col{i}" for i in range(len(headers))]
    # If the first row is a header (th) keep data from row 2; otherwise row 1 is data
    has_th = rows[0].find("th") is not None
    data_rows = rows[1:] if has_th else rows
    out: list[dict] = []
    for tr in data_rows:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        out.append({headers[i]: c.get_text(" ", strip=True)
                    for i, c in enumerate(cells) if i < len(headers)})
    return out


def _txt_to_messages(text: str, path: Path, result: ParseResult) -> None:
    conv_key = "chat:" + re.sub(r"[^a-z0-9]+", "-", path.stem.lower()).strip("-")
    result.conversations.append({"external_key": conv_key, "title": path.stem,
                                 "source": SourceType.CHAT})
    cur_sender: Optional[str] = None
    cur_when = None
    buf: list[str] = []
    matched_any = False
    orphans: list[str] = []

    def flush():
        if cur_sender and buf:
            body = "\n".join(buf).strip()
            if body:
                result.messages.append(Message(
                    id=message_id("chat", conv_key, cur_when, cur_sender, body),
                    conversation_id=conv_key,
                    sender_label=cur_sender,
                    sent_at=cur_when,
                    content=clean_text(body),
                    source=SourceType.CHAT,
                    origin=DataOrigin.IMPORTED,
                    external_id=conv_key,
                ))

    for line in text.splitlines():
        m = None
        dt = None
        sender = None
        body = None
        for pat in _LINE_PATTERNS:
            m = pat.match(line)
            if m:
                gd = m.groupdict()
                dt = gd.get("dt")
                sender = gd.get("b1") or gd.get("b2") or gd.get("b3") or gd.get("b4")
                body = gd.get("body")
                break
        if m and sender and body is not None:
            matched_any = True
            flush()
            cur_sender = clean_text(sender)
            cur_when = _to_dt(dt) if dt else None
            buf = [body]
        elif cur_sender:
            buf.append(line)
        elif line.strip():
            # Unstructured line: keep it only if the file demonstrably has
            # chat structure somewhere; a pure prose file must NOT become a
            # fake conversation (it falls through to the document parser).
            if matched_any:
                cur_sender = "unknown"
                cur_when = None
                buf = [line]
            else:
                orphans.append(line)
    flush()
