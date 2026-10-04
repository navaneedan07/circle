/** Calendar parser: `.ics` and `.csv`. Participants feed identity resolution. */
import fs from "node:fs";
import { cleanText, deterministicId } from "./common.js";
import { emptyParseResult, type CalendarEvent, type ParseResult } from "../domain.js";

function unfoldIcs(raw: string): string[] {
  // RFC5545 line folding: a line starting with space/tab continues the previous.
  const out: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    if (/^[ \t]/.test(line) && out.length > 0) out[out.length - 1] += line.slice(1);
    else out.push(line);
  }
  return out;
}

function icsDate(value: string): string | null {
  const v = value.trim();
  const m = v.match(/^(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})(Z)?)?/);
  if (!m) return null;
  const [, y, mo, d, hh = "00", mm = "00", ss = "00", z] = m;
  const iso = `${y}-${mo}-${d}T${hh}:${mm}:${ss}${z ? "Z" : ""}`;
  const date = new Date(z ? iso : `${y}-${mo}-${d}T${hh}:${mm}:${ss}`);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

export function parseCalendar(filePath: string): ParseResult {
  const result = emptyParseResult();
  const raw = fs.readFileSync(filePath, "utf-8");
  const ext = filePath.toLowerCase().split(".").pop();
  if (ext === "csv") {
    parseCalendarCsv(raw, result);
    return result;
  }

  const lines = unfoldIcs(raw);
  let current: Record<string, string[]> | null = null;
  const events: Record<string, string[]>[] = [];

  for (const line of lines) {
    if (line.startsWith("BEGIN:VEVENT")) current = {};
    else if (line.startsWith("END:VEVENT")) {
      if (current) events.push(current);
      current = null;
    } else if (current) {
      const idx = line.indexOf(":");
      if (idx === -1) continue;
      const key = line.slice(0, idx).split(";")[0]!.toUpperCase();
      const value = line.slice(idx + 1);
      (current[key] ??= []).push(value);
    }
  }

  for (const ev of events) {
    const title = cleanText((ev.SUMMARY ?? [""])[0] ?? "");
    const startsAt = icsDate((ev.DTSTART ?? [""])[0] ?? "");
    const endsAt = icsDate((ev.DTEND ?? [""])[0] ?? "");
    const participants: string[] = [];
    for (const key of ["ATTENDEE", "ORGANIZER"]) {
      for (const raw of ev[key] ?? []) {
        const m = raw.match(/CN=([^;:]+)/i) || raw.match(/mailto:([^;:>]+)/i);
        if (m && m[1]) participants.push(m[1].trim().replace(/^"|"$/g, ""));
      }
    }
    if (!title && !startsAt) continue;
    const externalId = `ics-${deterministicId(title, startsAt ?? "", (ev.UID ?? [""])[0] ?? "")}`;
    const event: CalendarEvent = {
      id: externalId,
      title,
      starts_at: startsAt,
      ends_at: endsAt,
      location: cleanText((ev.LOCATION ?? [""])[0] ?? ""),
      description: cleanText((ev.DESCRIPTION ?? [""])[0] ?? ""),
      participants: [...new Set(participants)],
      person_ids: [],
      source: "calendar",
      origin: "imported",
      external_id: externalId,
      imported_at: new Date().toISOString(),
    };
    result.events.push(event);
  }
  return result;
}

function parseCalendarCsv(raw: string, result: ParseResult): void {
  const lines = raw.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length < 2) return;
  const header = lines[0]!.split(",").map((h) => h.trim().toLowerCase().replace(/^"|"$/g, ""));
  const col = (names: string[]) => header.findIndex((h) => names.includes(h));
  const titleIdx = col(["title", "subject", "summary"]);
  const startIdx = col(["start", "starts_at", "date", "begin"]);
  const endIdx = col(["end", "ends_at"]);
  const locIdx = col(["location", "place"]);
  const partIdx = col(["participants", "attendees", "with"]);

  for (let i = 1; i < lines.length; i++) {
    const cols = lines[i]!.split(",").map((c) => c.trim().replace(/^"|"$/g, ""));
    const title = titleIdx >= 0 ? cols[titleIdx] ?? "" : "";
    const start = startIdx >= 0 ? cols[startIdx] ?? "" : "";
    if (!title && !start) continue;
    const startsAt = start ? new Date(start) : null;
    const participants = partIdx >= 0 ? (cols[partIdx] ?? "").split(/[;|]/).map((s) => s.trim()).filter(Boolean) : [];
    const externalId = `ics-${deterministicId(title, start, String(i))}`;
    result.events.push({
      id: externalId,
      title,
      starts_at: startsAt && !Number.isNaN(startsAt.getTime()) ? startsAt.toISOString() : null,
      ends_at: null,
      location: locIdx >= 0 ? cols[locIdx] ?? "" : "",
      description: "",
      participants,
      person_ids: [],
      source: "calendar",
      origin: "imported",
      external_id: externalId,
      imported_at: new Date().toISOString(),
    });
  }
}
