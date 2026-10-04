/**
 * WhatsApp `.txt` chat export parser.
 *
 * Handles the common dialects:
 *   [12/05/2024, 9:41:02 PM] Alice: hello
 *   12/05/2024, 9:41 PM - Alice: hello
 *   2024-05-12, 21:41 - Alice: hello
 * plus system lines ("Messages and calls are end-to-end encrypted...") and
 * multi-line messages, which are folded into the message they follow.
 */
import fs from "node:fs";
import { cleanText, deterministicId, repairMojibake } from "./common.js";
import { emptyParseResult, type Message, type ParseResult } from "../domain.js";

const PATTERNS: RegExp[] = [
  /^\[(\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?[Mm]\.?)?)\]\s*([^:]+?):\s(.*)$/,
  /^(\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}),\s*(\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?[Mm]\.?)?)\s*[-–]\s*([^:]+?):\s(.*)$/,
  /^(\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}),\s*(\d{1,2}:\d{2})\s*-\s*(.*)$/,
];

function parseTimestamp(datePart: string, timePart: string): string | null {
  const dp = datePart.trim();
  const tp = timePart.trim().replace(/\./g, "").toUpperCase();
  let day: number, month: number, year: number;

  const dateParts = dp.split(/[/.-]/).map((p) => p.trim());
  if (dateParts.length !== 3) return null;
  const [a, b, c] = dateParts as [string, string, string];

  if (a.length === 4) {
    // YYYY-MM-DD
    year = Number(a);
    month = Number(b);
    day = Number(c);
  } else if (c.length === 4 || Number(c) > 31) {
    // DD/MM/YYYY (WhatsApp default) or MM/DD/YYYY if day > 12 disambiguates
    year = Number(c);
    const first = Number(a);
    const second = Number(b);
    if (first > 12) {
      day = first;
      month = second;
    } else if (second > 12) {
      day = second;
      month = first;
    } else {
      day = first;
      month = second; // assume day-first (international default)
    }
  } else {
    // no year: assume current year
    year = new Date().getFullYear();
    day = Number(a);
    month = Number(b);
  }

  const timeMatch = tp.match(/^(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP]M)?$/);
  if (!timeMatch) return null;
  let hour = Number(timeMatch[1]);
  const minute = Number(timeMatch[2]);
  const second = Number(timeMatch[3] || 0);
  const meridiem = timeMatch[4];
  if (meridiem === "PM" && hour < 12) hour += 12;
  if (meridiem === "AM" && hour === 12) hour = 0;

  const d = new Date(Date.UTC(year, month - 1, day, hour, minute, second));
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString();
}

const SYSTEM_HINTS = [
  "Messages and calls are end-to-end encrypted",
  "You changed the group",
  "created group",
  "changed the subject",
  "added you",
  "removed you",
  "changed this group's icon",
  "joined using this group's invite link",
  "changed their phone number",
  "deleted this message",
  "This message was deleted",
  "Waiting for this message",
];

function looksLikeSystem(body: string): boolean {
  return SYSTEM_HINTS.some((h) => body.includes(h));
}

export function parseWhatsApp(filePath: string): ParseResult {
  const result = emptyParseResult();
  const raw = fs.readFileSync(filePath, "utf-8");
  const lines = raw.split(/\r?\n/);

  const chatTitle = filePath.replace(/\\/g, "/").split("/").pop()?.replace(/\.txt$/i, "") ?? "WhatsApp chat";
  const externalKey = `whatsapp:${chatTitle}`;

  let current: { ts: string | null; sender: string; body: string[] } | null = null;
  const senders = new Set<string>();

  const flush = () => {
    if (!current) return;
    const body = cleanText(repairMojibake(current.body.join("\n")));
    if (body && !looksLikeSystem(body)) {
      const id = `msg-${deterministicId(externalKey, current.ts ?? "", current.sender, body)}`;
      const msg: Message = {
        id,
        conversation_id: null,
        person_id: null,
        sender_label: current.sender,
        sent_at: current.ts,
        content: body,
        attachments: extractAttachments(body),
        reply_to: null,
        source: "whatsapp",
        origin: "imported",
        external_id: id,
        media_ids: [],
        imported_at: new Date().toISOString(),
      };
      result.messages.push(msg);
    }
    current = null;
  };

  for (const line of lines) {
    let matched = false;
    for (let i = 0; i < PATTERNS.length; i++) {
      const m = PATTERNS[i]!.exec(line);
      if (!m) continue;
      if (i === 2) {
        // System line without a sender
        flush();
        matched = true;
        break;
      }
      flush();
      const ts = parseTimestamp(m[1]!, m[2]!);
      const sender = repairMojibake(m[3]!.trim());
      senders.add(sender);
      current = { ts, sender, body: [m[4] ?? ""] };
      matched = true;
      break;
    }
    if (!matched) {
      if (current) {
        current.body.push(line);
      }
    }
  }
  flush();

  if (result.messages.length > 0) {
    result.conversations.push({ external_key: externalKey, title: chatTitle, source: "whatsapp" });
  }
  return result;
}

/** Lines like "IMG-2024...jpg (file attached)" or "<attached: 00000012-PHOTO.jpg>". */
export function extractAttachments(body: string): Record<string, unknown>[] {
  const out: Record<string, unknown>[] = [];
  const patterns = [
    /([^\s(<>]+\.(?:jpg|jpeg|png|gif|webp|mp4|opus|m4a|mp3|ogg|pdf|docx?|webp|webm|aac|3gp|sticker))[^\n]*?(?:\(file attached\)|<attached:)/gi,
    /<attached:\s*([^>]+)>/gi,
    /(?:^|\s)([A-Z]{3}-\d{8}-[A-Z]{2}\d{2}\.\w+)/g,
  ];
  const seen = new Set<string>();
  for (const re of patterns) {
    let m: RegExpExecArray | null;
    while ((m = re.exec(body)) !== null) {
      const filename = (m[1] ?? "").trim().replace(/^.*[/\\]/, "");
      if (!filename || seen.has(filename)) continue;
      seen.add(filename);
      out.push({ filename, filename_key: filename.toLowerCase() });
    }
  }
  return out;
}
