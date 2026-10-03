"""Calendar importer: ICS (iCalendar) and CSV."""
from __future__ import annotations

import csv
import io
import re
from datetime import timezone
from pathlib import Path
from typing import Any, Optional

from circle.domain.models import CalendarEvent, DataOrigin, SourceType
from circle.parsers.common import ParseResult, clean_text, deterministic_id, parse_iso


def _dt(value: Any):
    """icalendar prop datetime -> aware UTC datetime (all-day kept date-only)."""
    if value is None:
        return None
    dt = getattr(value, "dt", value)
    if hasattr(dt, "tzinfo"):
        if dt.tzinfo is None:
            # date-only (all-day) or naive local: assume UTC
            return dt.replace(tzinfo=timezone.utc) if hasattr(dt, "hour") else \
                dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return parse_iso(str(value))


def parse_calendar(path: Path) -> ParseResult:
    ext = path.suffix.lower()
    result = ParseResult()
    if ext == ".ics":
        _parse_ics(path, result)
    elif ext == ".csv":
        _parse_csv(path, result)
    else:
        raise ValueError(f"unsupported calendar file type: {ext}")
    if result.is_empty():
        raise ValueError("no calendar events extracted")
    return result


def _parse_ics(path: Path, result: ParseResult) -> None:
    from icalendar import Calendar
    raw = path.read_bytes()
    try:
        cal = Calendar.from_ical(raw)
    except Exception as e:
        raise ValueError(f"invalid ICS file: {e}") from e

    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue
        title = clean_text(str(comp.get("SUMMARY", "")))
        when = _dt(comp.get("DTSTART"))
        ends = _dt(comp.get("DTEND"))
        location = clean_text(str(comp.get("LOCATION", "")))
        # Keep the raw description (newlines intact) for participant parsing,
        # then normalize it for storage.
        raw_description = str(comp.get("DESCRIPTION", ""))
        description = clean_text(raw_description)
        participants = []
        atts = comp.get("ATTENDEE")
        if atts is not None and not isinstance(atts, list):
            atts = [atts]
        for att in atts or []:
            params = getattr(att, "params", None) or {}
            cn = str(params.get("CN") or "")
            mailto = str(att).replace("mailto:", "")
            participants.append(clean_text(cn or mailto))
        if not participants:
            # fall back to description mentions like "Participants: a, b"
            # (parsed on the RAW description so the list ends at the line break)
            m = re.search(r"participants?:\s*([^\r\n]+)", raw_description,
                          re.IGNORECASE)
            if m:
                participants = [clean_text(p)
                                for p in re.split(r"[,;]", m.group(1)) if p.strip()]
        ext_id = "cal-" + deterministic_id(title, str(when), location)
        result.events.append(CalendarEvent(
            id=ext_id,
            title=title or "(untitled)",
            starts_at=when,
            ends_at=ends,
            location=location,
            description=description,
            participants=participants,
            source=SourceType.CALENDAR,
            origin=DataOrigin.IMPORTED,
            external_id=ext_id,
        ))


def _pick(row: dict, names: tuple[str, ...]) -> str:
    lowered = {k.lower().strip(): v for k, v in row.items()}
    for n in names:
        if n in lowered and lowered[n]:
            return str(lowered[n])
    return ""


def _parse_csv(path: Path, result: ParseResult) -> None:
    text = path.read_bytes().decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        title = _pick(row, ("title", "subject", "summary", "event", "name"))
        if not title:
            continue
        when = parse_iso(_pick(row, ("start", "starts_at", "dtstart",
                                     "begin", "date", "when")))
        ends = parse_iso(_pick(row, ("end", "ends_at", "dtend", "finish")))
        participants_raw = _pick(row, ("participants", "attendees", "guests",
                                       "people", "invitees"))
        participants = [clean_text(p) for p in re.split(r"[,;]", participants_raw) if p.strip()]
        ext_id = "cal-" + deterministic_id(title, str(when), path.name)
        result.events.append(CalendarEvent(
            id=ext_id,
            title=clean_text(title),
            starts_at=when,
            ends_at=ends,
            location=clean_text(_pick(row, ("location", "place", "room"))),
            description=clean_text(_pick(row, ("description", "details", "notes"))),
            participants=participants,
            source=SourceType.CALENDAR,
            origin=DataOrigin.IMPORTED,
            external_id=ext_id,
        ))
