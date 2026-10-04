/**
 * X / Twitter archive DM parser.
 *
 * The archive ships `data/direct-messages.js` as `window.YTD.direct_messages.part0 = [ ... ]`,
 * so the JS wrapper is stripped before parsing. Newer archives use
 * `direct-messages-group.js`. No scraping and no credentials: this only reads
 * a file the user downloaded.
 */
import fs from "node:fs";
import { cleanText, deterministicId } from "./common.js";
import { emptyParseResult, type Message, type ParseResult } from "../domain.js";

function stripJsWrapper(raw: string): string {
  const trimmed = raw.trim();
  if (trimmed.startsWith("[") || trimmed.startsWith("{")) return trimmed;
  const eq = trimmed.indexOf("=");
  if (eq === -1) return trimmed;
  let body = trimmed.slice(eq + 1).trim();
  if (body.endsWith(";")) body = body.slice(0, -1);
  return body.trim();
}

function toIso(value: unknown): string | null {
  if (typeof value !== "string" || !value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

export function parseX(filePath: string): ParseResult {
  const result = emptyParseResult();
  let data: unknown;
  try {
    data = JSON.parse(stripJsWrapper(fs.readFileSync(filePath, "utf-8")));
  } catch {
    return result;
  }
  if (!Array.isArray(data)) return result;

  const conversationTitle = filePath.replace(/\\/g, "/").split("/").pop() ?? "x-dm";
  const externalKey = `x:${conversationTitle}`;
  let count = 0;

  for (const entry of data as Record<string, unknown>[]) {
    const dm = (entry.dmConversation ?? entry) as Record<string, unknown>;
    const messages = (dm.messages ?? []) as Record<string, unknown>[];
    for (const raw of messages) {
      const m = (raw.messageCreate ?? raw) as Record<string, unknown>;
      const content = cleanText(String(m.text ?? ""));
      if (!content) continue;
      const sender = String(m.senderId ?? m.sender_id ?? "unknown");
      const ts = toIso(m.createdAt ?? m.created_at);
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
        source: "x",
        origin: "imported",
        external_id: id,
        media_ids: [],
        imported_at: new Date().toISOString(),
      };
      result.messages.push(msg);
      count++;
    }
  }

  if (count > 0) {
    result.conversations.push({ external_key: externalKey, title: conversationTitle, source: "x" });
  }
  return result;
}
