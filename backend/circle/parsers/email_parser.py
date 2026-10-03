"""Email importer: .eml, .mbox, .csv, .json (local files only -- never sends
or accesses mailboxes without an explicit connector)."""
from __future__ import annotations

import csv
import email
import email.header
import email.utils
import io
import json
import mailbox
import re
from pathlib import Path
from typing import Any, Optional

from circle.domain.models import DataOrigin, Email, SourceType
from circle.parsers.common import ParseResult, clean_text, deterministic_id, parse_iso


def _decode_header(value: Any) -> str:
    if not value:
        return ""
    parts = []
    for chunk, enc in email.header.decode_header(str(value)):
        if isinstance(chunk, bytes):
            parts.append(chunk.decode(enc or "utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return clean_text("".join(parts))


def _body_from_message(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
                    part.get_content_disposition() != "attachment":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        return payload.decode(charset, errors="replace")
                    except LookupError:
                        return payload.decode("utf-8", errors="replace")
        # fall back to html
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    return re.sub(r"<[^>]+>", " ", payload.decode("utf-8", errors="replace"))
        return ""
    payload = msg.get_payload(decode=True)
    if payload is None:
        raw = msg.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _attachments(msg: email.message.Message) -> list[dict]:
    out = []
    for part in msg.walk():
        fn = part.get_filename()
        if fn:
            out.append({"kind": "attachment", "label": _decode_header(fn),
                        "size": len(part.get_payload(decode=True) or b"")})
    return out


def _dt(value: Any):
    if isinstance(value, email.message.Message):
        return None
    try:
        dt = email.utils.parsedate_to_datetime(str(value))
        if dt.tzinfo is None:
            from datetime import timezone
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return parse_iso(value)


def _add(result: ParseResult, *, sender: str, sender_addr: str, to: list[str],
         subject: str, when, body: str, attachments: list, key_hint: str) -> None:
    body = clean_text(body) if len(body) < 20000 else clean_text(body[:20000])
    if not (body or subject or attachments):
        return
    ext_id = "eml-" + deterministic_id(sender_addr, subject, str(when), body[:200])
    result.emails.append(Email(
        id=ext_id,
        from_label=sender,
        from_address=sender_addr,
        to=to,
        subject=clean_text(subject),
        body=body,
        sent_at=when,
        attachments=attachments,
        source=SourceType.EMAIL,
        origin=DataOrigin.IMPORTED,
        external_id=ext_id,
    ))


def parse_email(path: Path) -> ParseResult:
    ext = path.suffix.lower()
    result = ParseResult()
    if ext == ".eml":
        _parse_eml(path, result)
    elif ext == ".mbox":
        _parse_mbox(path, result)
    elif ext == ".csv":
        _parse_email_csv(path, result)
    elif ext == ".json":
        _parse_email_json(path, result)
    else:
        raise ValueError(f"unsupported email file type: {ext}")
    if result.is_empty():
        raise ValueError("no emails extracted")
    return result


def _parse_eml(path: Path, result: ParseResult) -> None:
    msg = email.message_from_bytes(path.read_bytes())
    sender = _decode_header(msg.get("From", ""))
    addr = email.utils.parseaddr(msg.get("From", ""))[1].lower()
    to = [_decode_header(x) for x in msg.get_all("To", []) if x]
    to = [a for t in to for a in re.split(r"[,;]", t) if a.strip()]
    _add(result, sender=sender, sender_addr=addr, to=to,
         subject=_decode_header(msg.get("Subject", "")),
         when=_dt(msg.get("Date")), body=_body_from_message(msg),
         attachments=_attachments(msg), key_hint=path.name)


def _parse_mbox(path: Path, result: ParseResult) -> None:
    mbox = mailbox.mbox(str(path))
    for msg in mbox:
        sender = _decode_header(msg.get("From", ""))
        addr = email.utils.parseaddr(msg.get("From", ""))[1].lower()
        to = [_decode_header(x) for x in msg.get_all("To", []) if x]
        to = [a for t in to for a in re.split(r"[,;]", t) if a.strip()]
        _add(result, sender=sender, sender_addr=addr, to=to,
             subject=_decode_header(msg.get("Subject", "")),
             when=_dt(msg.get("Date")), body=_body_from_message(msg),
             attachments=_attachments(msg), key_hint="mbox")


def _rows(path: Path) -> list[dict]:
    text = path.read_bytes().decode("utf-8", errors="replace")
    if path.suffix.lower() == ".csv":
        return [dict(r) for r in csv.DictReader(io.StringIO(text))]
    data = json.loads(text)
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _pick(row: dict, names: tuple[str, ...]) -> str:
    lowered = {k.lower().strip(): v for k, v in row.items()}
    for n in names:
        if n in lowered and lowered[n]:
            return str(lowered[n])
    return ""


def _parse_email_csv(path: Path, result: ParseResult) -> None:
    for row in _rows(path):
        sender = _pick(row, ("from", "sender", "from_address", "sender_email"))
        addr = email.utils.parseaddr(sender)[1].lower() or sender.lower()
        to_raw = _pick(row, ("to", "recipients", "recipient", "cc"))
        to = [t.strip() for t in re.split(r"[,;]", to_raw) if t.strip()]
        body = _pick(row, ("body", "text", "content", "message", "snippet", "html"))
        if path.suffix.lower() == ".html" and body.startswith("<"):
            body = re.sub(r"<[^>]+>", " ", body)
        _add(result, sender=sender or addr, sender_addr=addr, to=to,
             subject=_pick(row, ("subject", "title")),
             when=parse_iso(_pick(row, ("date", "timestamp", "sent_at", "time"))),
             body=body, attachments=[], key_hint=path.name)


def _parse_email_json(path: Path, result: ParseResult) -> None:
    _parse_email_csv(path, result)   # same row logic
