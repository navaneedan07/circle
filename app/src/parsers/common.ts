/** Shared parser utilities: deterministic ids and text cleanup. */
import crypto from "node:crypto";

export function sha256Text(text: string): string {
  return crypto.createHash("sha256").update(text, "utf-8").digest("hex");
}

export function sha256Bytes(buf: Buffer | Uint8Array): string {
  return crypto.createHash("sha256").update(buf).digest("hex");
}

/** Stable id from a few string parts, so a re-import dedupes exactly. */
export function deterministicId(...parts: string[]): string {
  return sha256Text(parts.join("\u0000")).slice(0, 32);
}

/** Collapse whitespace and strip control characters. */
export function cleanText(text: string): string {
  if (!text) return "";
  return text
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Repair common mojibake from chat exports (UTF-8 read as Latin-1). */
export function repairMojibake(text: string): string {
  if (!text) return "";
  if (!/Ã|Â|â|ð/.test(text)) return text;
  try {
    const repaired = Buffer.from(text, "latin1").toString("utf-8");
    // Only keep the repair when it removed the tell-tale sequences.
    return /Ã|Â/.test(repaired) ? text : repaired;
  } catch {
    return text;
  }
}

/** Normalize a name for comparison: mojibake fixed, punctuation/emoji removed. */
export function normalizeName(name: string): string {
  return repairMojibake(name)
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, "")
    .replace(/\s+/g, " ")
    .trim();
}

const STOPWORDS = new Set(
  ("a an the and or but if then else for of to in on at by with from as is are was were be been being " +
    "it its this that these those i you he she we they me him her us them my your his our their " +
    "do does did done have has had will would can could should may might must not no yes ok okay " +
    "just so very really about into over under again more most some any all what when where who how why " +
    "im ive dont doesnt didnt cant wont thats theres youre theyre whats hi hey hello yeah yep nope " +
    "pls plz please thanks thank thank you").split(/\s+/)
);

export function contentTerms(text: string): string[] {
  const words = normalizeName(text).split(/\s+/).filter(Boolean);
  const out: string[] = [];
  const seen = new Set<string>();
  for (const w of words) {
    if (w.length < 3 || STOPWORDS.has(w) || seen.has(w)) continue;
    seen.add(w);
    out.push(w);
  }
  return out;
}

export function extractTopics(text: string, limit = 8): string[] {
  const counts = new Map<string, number>();
  for (const term of contentTerms(text)) {
    if (term.length < 4) continue;
    counts.set(term, (counts.get(term) || 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, limit)
    .map(([t]) => t);
}
