/**
 * Instagram JSON/NDJSON export parser.
 *
 * Instagram's export shape drifts between releases, so this tolerates several:
 *   { participants: [...], messages: [{ sender_name, timestamp_ms, content }] }
 *   { messages: [...] } (single thread)
 *   NDJSON, one JSON object per line
 * Also handles the newer `{ label_values: [...] }` and
 * `{ string_map_data: {...} }` wrappers.
 */
import fs from "node:fs";
import path from "node:path";
import { cleanText, deterministicId, repairMojibake } from "./common.js";
import { emptyParseResult, type Message, type ParseResult } from "../domain.js";

function threadName(data: Record<string, unknown>, fallback: string): string {
  // Instagram writes `participants`; older Messenger exports wrote
  // `participate`. Reading the wrong key silently fell back to the file name,
  // which made every thread in the export collide on one conversation.
  const raw = data.participants ?? data.participate;
  if (Array.isArray(raw) && raw.length > 0) {
    const names = raw.map((p) => String((p as Record<string, unknown>).name ?? "")).filter(Boolean);
    if (names.length) return names.join(", ");
  }
  const title = String(data.title ?? "").trim();
  return title || fallback;
}

/**
 * A stable key for the thread this file belongs to.
 *
 * Every file inside a thread folder is named `message_1.json`, `message_2.json`
 * and so on, so the file name says nothing about *who* is in the thread. The
 * folder name (`05esther__12_18005080511581432`) does, and it is stable across
 * re-downloads of the same export.
 */
function threadKey(filePath: string, data: Record<string, unknown>, fallback: string): string {
  const dir = path.basename(path.dirname(filePath));
  const generic = new Set(["inbox", "messages", "message_requests", "archived_threads", ".", ""]);
  if (dir && !generic.has(dir.toLowerCase())) return dir;
  const title = threadName(data, "").trim();
  return title || fallback;
}

function handleThread(
  data: Record<string, unknown>,
  sourceName: string,
  result: ParseResult,
  filePath: string
): number {
  const messages = data.messages as unknown;
  if (!Array.isArray(messages)) return 0;
  const title = threadName(data, sourceName);
  const externalKey = `instagram:${threadKey(filePath, data, sourceName)}`;
  let count = 0;

  for (const raw of messages) {
    const m = raw as Record<string, unknown>;
    const content = cleanText(repairMojibake(String(m.content ?? m.text ?? "")));
    const sender = repairMojibakeName(String(m.sender_name ?? m.sender ?? title));
    const tsRaw = m.timestamp_ms ?? m.timestamp;
    let ts: string | null = null;
    if (typeof tsRaw === "number") ts = new Date(tsRaw).toISOString();
    else if (typeof tsRaw === "string" && tsRaw) {
      const d = new Date(tsRaw);
      ts = Number.isNaN(d.getTime()) ? null : d.toISOString();
    }
    if (!content) continue;
    const id = `msg-${deterministicId(externalKey, ts ?? "", sender, content)}`;
    const msg: Message = {
      id,
      conversation_id: null,
      person_id: null,
      sender_label: sender,
      sent_at: ts,
      content,
      attachments: [],
      reply_to: null,
      source: "instagram",
      origin: "imported",
      external_id: id,
      media_ids: [],
      imported_at: new Date().toISOString(),
    };
    result.messages.push(msg);
    count++;
  }

  if (count > 0) {
    result.conversations.push({ external_key: externalKey, title, source: "instagram" });
  }
  return count;
}

function repairMojibakeName(name: string): string {
  return repairMojibake(name).trim();
}

export function parseInstagram(filePath: string): ParseResult {
  const result = emptyParseResult();
  const raw = fs.readFileSync(filePath, "utf-8");
  const sourceName = filePath.replace(/\\/g, "/").split("/").pop()?.replace(/\.json$/i, "") ?? "instagram";

  const docs: Record<string, unknown>[] = [];
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) docs.push(...(parsed as Record<string, unknown>[]));
    else docs.push(parsed as Record<string, unknown>);
  } catch {
    // NDJSON fallback: one object per line.
    for (const line of raw.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        docs.push(JSON.parse(trimmed) as Record<string, unknown>);
      } catch {
        /* skip malformed line */
      }
    }
  }

  for (const doc of docs) {
    if (handleThread(doc, sourceName, result, filePath) > 0) continue;
    // Some exports nest threads one level down.
    for (const value of Object.values(doc)) {
      if (value && typeof value === "object" && !Array.isArray(value)) {
        handleThread(value as Record<string, unknown>, sourceName, result, filePath);
      }
    }
  }
  return result;
}