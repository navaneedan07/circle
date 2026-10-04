/** Contacts parser: `.csv` and `.vcf`. Primary identity-resolution source. */
import fs from "node:fs";
import { emptyParseResult, type ParseResult } from "../domain.js";

interface Contact {
  name: string;
  emails: string[];
  phones: string[];
  aliases?: string[];
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

function parseVcf(raw: string): Contact[] {
  const contacts: Contact[] = [];
  let current: { fn?: string; n?: string; emails: string[]; phones: string[] } | null = null;
  for (const rawLine of raw.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (line.toUpperCase() === "BEGIN:VCARD") current = { emails: [], phones: [] };
    else if (line.toUpperCase() === "END:VCARD") {
      if (current) {
        const name = current.fn || current.n?.replace(/;/g, " ").trim() || "";
        if (name) contacts.push({ name, emails: current.emails, phones: current.phones });
      }
      current = null;
    } else if (current) {
      const idx = line.indexOf(":");
      if (idx === -1) continue;
      const key = line.slice(0, idx).split(";")[0]!.toUpperCase();
      const value = line.slice(idx + 1).trim();
      if (key === "FN") current.fn = value;
      else if (key === "N") current.n = value;
      else if (key === "EMAIL" && value) current.emails.push(value.toLowerCase());
      else if (key === "TEL" && value) current.phones.push(value.replace(/[^\d+]/g, ""));
    }
  }
  return contacts;
}

function parseContactsCsv(raw: string): Contact[] {
  const lines = raw.split(/\r?\n/).filter((l) => l.trim());
  if (lines.length < 2) return [];
  const header = splitCsvLine(lines[0]!).map((h) => h.trim().toLowerCase());
  const find = (names: string[]) => header.findIndex((h) => names.includes(h));
  const nameIdx = find(["name", "display_name", "full_name", "fn"]);
  const firstIdx = find(["first name", "firstname", "given name"]);
  const lastIdx = find(["last name", "lastname", "family name"]);
  const emailIdx = find(["email", "e-mail", "email address", "emails"]);
  const phoneIdx = find(["phone", "phone number", "tel", "mobile", "phones"]);

  const out: Contact[] = [];
  for (let i = 1; i < lines.length; i++) {
    const cols = splitCsvLine(lines[i]!);
    let name = nameIdx >= 0 ? (cols[nameIdx] ?? "").trim() : "";
    if (!name && (firstIdx >= 0 || lastIdx >= 0)) {
      name = `${firstIdx >= 0 ? cols[firstIdx] ?? "" : ""} ${lastIdx >= 0 ? cols[lastIdx] ?? "" : ""}`.trim();
    }
    if (!name) continue;
    const emails = emailIdx >= 0 ? (cols[emailIdx] ?? "").split(/[;|]/).map((e) => e.trim().toLowerCase()).filter(Boolean) : [];
    const phones = phoneIdx >= 0 ? (cols[phoneIdx] ?? "").split(/[;|]/).map((p) => p.replace(/[^\d+]/g, "")).filter(Boolean) : [];
    out.push({ name, emails, phones });
  }
  return out;
}

export function parseContacts(filePath: string): ParseResult {
  const result = emptyParseResult();
  const raw = fs.readFileSync(filePath, "utf-8");
  const ext = filePath.toLowerCase().split(".").pop();
  const contacts = ext === "vcf" ? parseVcf(raw) : ext === "csv" ? parseContactsCsv(raw) : [];
  result.contacts.push(...contacts);
  return result;
}
