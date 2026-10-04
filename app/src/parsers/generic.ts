/**
 * Generic fallback parser for unknown chat exports.
 *
 * Detects the sender/time/text fields from JSON or CSV rather than failing.
 * This is what lets a format Circle has never seen still become searchable.
 */
import fs from "node:fs";
import { cleanText, deterministicId } from "./common.js";
import { emptyParseResult, type Message, type ParseResult } from "../domain.js";

const SENDER_KEYS = ["sender", "from", "author", "user", "name", "sender_name", "who"];
const TEXT_KEYS = ["text", "content", "message", "body", "msg"];
const TIME_KEYS = ["timestamp", "time", "date", "sent_at", "datetime", "created_at", "timestamp_ms"];

function pick(obj: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const found = Object.keys(obj).find((k) => k.toLowerCase() === key);
    if (found && obj[found] != null) return String(obj[found]);
  }
  return "";
}

function toIso(value: string): string | null {
  if (!value) return null;
  const asNum = Number(value);
  if (!Number.isNaN(asNum) && value.length >= 10) {
    const d = new Date(asNum > 1e12 ? asNum : asNum * 1000);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
  }
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export function parseGeneric(filePath: string): ParseResult {
  const result = emptyParseResult();
  const raw = fs.readFileSync(filePath, "utf-8");
  const ext = filePath.toLowerCase().split(".").pop();
  const sourceName = filePath.replace(/\\/g, "/").split("/").pop() ?? "import";
  const externalKey = `chat:${sourceName}`;

  let rows: Record<string, unknown>[] = [];
  if (ext === "json") {
    try {
      const data = JSON.parse(raw);
      if (Array.isArray(data)) rows = data as Record<string, unknown>[];
      else {
        const obj = data as Record<string, unknown>;
        const arr = Object.values(obj).find((v) => Array.isArray(v));
        if (Array.isArray(arr)) rows = arr as Record<string, unknown>[];
      }
    } catch {
      return result;
    }
  } else if (ext === "csv") {
    const lines = raw.split(/\r?\n/).filter((l) => l.trim());
    if (lines.length < 2) return result;
    const header = lines[0]!.split(",").map((h) => h.trim().toLowerCase());
    for (let i = 1; i < lines.length; i++) {
      const cols = lines[i]!.split(",");
      const row: Record<string, unknown> = {};
      header.forEach((h, idx) => (row[h] = cols[idx] ?? ""));
      rows.push(row);
    }
  } else {
    return result;
  }

  let count = 0;
  for (const row of rows) {
    const text = cleanText(pick(row, TEXT_KEYS));
    if (!text) continue;
    const sender = pick(row, SENDER_KEYS) || sourceName;
    const ts = toIso(pick(row, TIME_KEYS));
    const id = `msg-${deterministicId(externalKey, ts ?? "", sender, text)}`;
    const msg: Message = {
      id,
      conversation_id: null,
      person_id: null,
      sender_label: sender,
      sent_at: ts,
      content: text,
      attachments: [],
      reply_to: null,
      source: "chat",
      origin: "imported",
      external_id: id,
      media_ids: [],
      imported_at: new Date().toISOString(),
    };
    result.messages.push(msg);
    count++;
  }

  if (count > 0) {
    result.conversations.push({ external_key: externalKey, title: sourceName, source: "chat" });
  }
  return result;
}
