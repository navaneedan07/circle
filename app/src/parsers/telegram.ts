/**
 * Telegram official JSON export parser.
 *
 * The export wraps everything in `{ chats: { list: [...] } }`, with each chat
 * holding a `messages` array. Message `text` is either a string or a list of
 * segments (with links/mentions), and `date` is an ISO string or unix seconds.
 */
import fs from "node:fs";
import { cleanText, deterministicId } from "./common.js";
import { emptyParseResult, type Message, type ParseResult } from "../domain.js";

interface TgTextEntity {
  type?: string;
  text?: string;
}
type TgText = string | (string | TgTextEntity)[];

function flattenText(text: TgText | undefined): string {
  if (!text) return "";
  if (typeof text === "string") return text;
  return text
    .map((part) => (typeof part === "string" ? part : part?.text ?? ""))
    .join("");
}

function toIso(date: unknown): string | null {
  if (typeof date === "string") {
    const d = new Date(date);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
  }
  if (typeof date === "number") {
    const d = new Date(date * 1000);
    return Number.isNaN(d.getTime()) ? null : d.toISOString();
  }
  return null;
}

export function parseTelegram(filePath: string): ParseResult {
  const result = emptyParseResult();
  let data: unknown;
  try {
    data = JSON.parse(fs.readFileSync(filePath, "utf-8"));
  } catch {
    return result;
  }

  const root = data as Record<string, unknown>;
  const chats = root?.chats as Record<string, unknown> | undefined;
  const list = (chats?.list ?? root?.messages) as unknown;
  if (!Array.isArray(list)) return result;

  for (const chat of list) {
    const c = chat as Record<string, unknown>;
    const title = String(c.name ?? c.title ?? "Telegram chat");
    const externalKey = `telegram:${c.id ?? title}`;
    const messages = (c.messages ?? []) as Record<string, unknown>[];
    let count = 0;

    for (const m of messages) {
      if (String(m.type ?? "message") !== "message") continue;
      const text = cleanText(flattenText(m.text as TgText));
      const ts = toIso(m.date);
      const sender = String(m.from ?? m.actor ?? title);
      const attachments: Record<string, unknown>[] = [];
      for (const key of ["photo", "file", "media_type", "sticker", "voice_message", "video_file"]) {
        if (m[key]) attachments.push({ kind: key, filename: String(m.file ?? m.photo ?? key) });
      }
      if (!text && attachments.length === 0) continue;
      const id = `msg-${deterministicId(externalKey, ts ?? "", sender, text || String(m.id ?? ""))}`;
      const msg: Message = {
        id,
        conversation_id: null,
        person_id: null,
        sender_label: sender,
        sent_at: ts,
        content: text,
        attachments,
        reply_to: m.reply_to_message_id ? String(m.reply_to_message_id) : null,
        source: "telegram",
        origin: "imported",
        external_id: id,
        media_ids: [],
        imported_at: new Date().toISOString(),
      };
      result.messages.push(msg);
      count++;
    }

    if (count > 0) {
      result.conversations.push({ external_key: externalKey, title, source: "telegram" });
    }
  }
  return result;
}
