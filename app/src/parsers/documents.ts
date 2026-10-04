/**
 * Document parser: `.txt`, `.md`, `.html`, `.htm`.
 *
 * PDF and DOCX extraction is intentionally left to a follow-up: both need a
 * dependency (pdf-parse / mammoth) and are not on the critical path for chat
 * archives. Files with those extensions are skipped rather than mis-parsed.
 */
import fs from "node:fs";
import { cleanText } from "./common.js";
import { emptyParseResult, type ParseResult } from "../domain.js";

function htmlToText(html: string): string {
  return cleanText(
    html
      .replace(/<script[\s\S]*?<\/script>/gi, " ")
      .replace(/<style[\s\S]*?<\/style>/gi, " ")
      .replace(/<[^>]+>/g, " ")
      .replace(/&nbsp;/g, " ")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
  );
}

export function documentResult(filePath: string): ParseResult {
  const result = emptyParseResult();
  const ext = filePath.toLowerCase().split(".").pop() ?? "";
  if (!["txt", "md", "html", "htm", "csv", "json"].includes(ext)) return result;
  let text = "";
  try {
    const raw = fs.readFileSync(filePath, "utf-8");
    text = ext === "html" || ext === "htm" ? htmlToText(raw) : cleanText(raw);
  } catch {
    return result;
  }
  if (!text) return result;
  const filename = filePath.replace(/\\/g, "/").split("/").pop() ?? "document";
  const title = filename.replace(/\.[^.]+$/, "");
  result.documents.push({ filename, title, text });
  return result;
}
