/**
 * Email parser: `.eml`, `.mbox`, `.csv`, `.json`.
 *
 * Only sender/subject/body/date and attachment metadata are kept; the raw
 * message is not stored. Uses a small hand-rolled RFC822 header splitter so
 * there is no heavyweight mail dependency.
 */
import fs from "node:fs";
import { cleanText, deterministicId } from "./common.js";
import { emptyParseResult, type Email, type ParseResult } from "../domain.js";

function parseHeaders(block: string): { headers: Record<string, string>; body: string } {
  const sepIndex = block.search(/\r?\n\r?\n/);
  const headText = sepIndex === -1 ? block : block.slice(0, sepIndex);
  const body = sepIndex === -1 ? "" : block.slice(sepIndex).replace(/^\r?\n\r?\n/, "");

  const headers: Record<string, string> = {};
  const unfolded = headText.replace(/\r?\n[ \t]+/g, " ");
  for (const line of unfolded.split(/\r?\n/)) {
    const idx = line.indexOf(":");
    if (idx === -1) continue;
    const key = line.slice(0, idx).trim().toLowerCase();
    const value = line.slice(idx + 1).trim();
    if (headers[key]) headers[key] += ` ${value}`;
    else headers[key] = value;
  }
  return { headers, body };
}

function decodeQuotedPrintable(text: string): string {
  return text
    .replace(/=\r?\n/g, "")
    .replace(/=([0-9A-Fa-f]{2})/g, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)));
}

function extractAddress(value: string): string {
  const m = value.match(/<([^>]+)>/);
  return (m ? m[1]! : value).trim();
}

function extractName(value: string): string {
  const m = value.match(/^"?([^"<]+)"?\s*</);
  return m ? m[1]!.trim() : "";
}

function toIso(value: string | undefined): string | null {
  if (!value) return null;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d.toISOString();
}

function buildEmail(headers: Record<string, string>, body: string, fallbackKey: string): Email | null {
  const subject = cleanText(headers.subject ?? "");
  const from = headers.from ?? "";
  const fromAddress = extractAddress(from).toLowerCase();
  const fromLabel = extractName(from) || fromAddress;
  const content = cleanText(decodeQuotedPrintable(body));
  if (!fromAddress && !subject && !content) return null;
  const ts = toIso(headers.date);
  const externalId = `eml-${deterministicId(fallbackKey, fromAddress, subject, ts ?? "", content.slice(0, 200))}`;
  return {
    id: externalId,
    person_id: null,
    direction: "inbound",
    from_label: fromLabel,
    from_address: fromAddress,
    to: (headers.to ?? "").split(",").map((s) => extractAddress(s.trim())).filter(Boolean),
    subject,
    body: content,
    sent_at: ts,
    attachments: [],
    source: "email",
    origin: "imported",
    external_id: externalId,
    imported_at: new Date().toISOString(),
  };
}

function parseEml(raw: string, key: string, result: ParseResult): void {
  const { headers, body } = parseHeaders(raw);
  const email = buildEmail(headers, body, key);
  if (email) result.emails.push(email);
}

function parseMbox(raw: string, key: string, result: ParseResult): void {
  const parts = raw.split(/^From .*$/m).filter((p) => p.trim());
  for (const part of parts) parseEml(part, key, result);
}

function parseCsv(raw: string, key: string, result: ParseResult): void {
  const lines = raw.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length < 2) return;
  const header = splitCsvLine(lines[0]!).map((h) => h.trim().toLowerCase());
  const idx = (names: string[]) => header.findIndex((h) => names.includes(h));
  const fromIdx = idx(["from", "from_address", "sender"]);
  const subjIdx = idx(["subject", "title"]);
  const bodyIdx = idx(["body", "content", "text", "message"]);
  const dateIdx = idx(["date", "sent_at", "timestamp"]);

  for (let i = 1; i < lines.length; i++) {
    const cols = splitCsvLine(lines[i]!);
    const from = fromIdx >= 0 ? (cols[fromIdx] ?? "") : "";
    const subject = subjIdx >= 0 ? (cols[subjIdx] ?? "") : "";
    const body = bodyIdx >= 0 ? (cols[bodyIdx] ?? "") : "";
    const date = dateIdx >= 0 ? (cols[dateIdx] ?? "") : "";
    const email = buildEmail({ from, subject, date }, body, `${key}:${i}`);
    if (email) result.emails.push(email);
  }
}

function splitCsvLine(line: string): string[] {
  const out: string[] = [];
  let cur = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]!;
    if (ch === '"') {
      if (inQuotes && line[i + 1] === '"') {
        cur += '"';
        i++;
      } else inQuotes = !inQuotes;
    } else if (ch === "," && !inQuotes) {
      out.push(cur);
      cur = "";
    } else cur += ch;
  }
  out.push(cur);
  return out;
}

function parseJson(raw: string, key: string, result: ParseResult): void {
  let data: unknown;
  try {
    data = JSON.parse(raw);
  } catch {
    return;
  }
  const arr = Array.isArray(data) ? data : ((data as Record<string, unknown>)?.emails as unknown);
  if (!Array.isArray(arr)) return;
  for (const item of arr as Record<string, unknown>[]) {
    const email = buildEmail(
      {
        from: String(item.from ?? item.from_address ?? ""),
        subject: String(item.subject ?? ""),
        date: String(item.date ?? item.sent_at ?? ""),
      },
      String(item.body ?? item.content ?? item.text ?? ""),
      `${key}:${item.id ?? ""}`
    );
    if (email) result.emails.push(email);
  }
}

export function parseEmail(filePath: string): ParseResult {
  const result = emptyParseResult();
  const raw = fs.readFileSync(filePath, "utf-8");
  const key = filePath.replace(/\\/g, "/").split("/").pop() ?? "email";
  const ext = filePath.toLowerCase().split(".").pop();
  if (ext === "eml") parseEml(raw, key, result);
  else if (ext === "mbox") parseMbox(raw, key, result);
  else if (ext === "csv") parseCsv(raw, key, result);
  else if (ext === "json") parseJson(raw, key, result);
  return result;
}
