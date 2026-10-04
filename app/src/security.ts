/**
 * File safety helpers: zip-slip/path-traversal guards, size limits, filename
 * sanitization, and content hashing.
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { unzipSync } from "fflate";

export const ALLOWED_EXTENSIONS = new Set([
  ".txt", ".md", ".json", ".js", ".csv", ".html", ".htm", ".eml", ".mbox",
  ".ics", ".vcf", ".zip", ".pdf", ".docx",
  ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic",
  ".mp4", ".mov", ".webm", ".3gp", ".mkv",
  ".opus", ".m4a", ".mp3", ".ogg", ".wav", ".aac", ".flac",
]);

export const MEDIA_EXTENSIONS = new Set([
  ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic",
  ".mp4", ".mov", ".webm", ".3gp", ".mkv",
  ".opus", ".m4a", ".mp3", ".ogg", ".wav", ".aac", ".flac",
]);

export const AUDIO_EXTENSIONS = new Set([".opus", ".m4a", ".mp3", ".ogg", ".wav", ".aac", ".flac"]);

const WINDOWS_RESERVED = new Set([
  "con", "prn", "aux", "nul",
  "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
  "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
]);

export class FileSecurityError extends Error {}

export function sanitizeFilename(name: string): string {
  let base = path.basename(name).replace(/[\u0000-\u001F<>:"/\\|?*]/g, "_").trim();
  base = base.replace(/^\.+/, "").replace(/\.+$/, "");
  if (!base) base = "unnamed";
  const stem = base.split(".")[0]!.toLowerCase();
  if (WINDOWS_RESERVED.has(stem)) base = `_${base}`;
  return base.slice(0, 200);
}

export function validateExtension(filePath: string): string {
  const ext = path.extname(filePath).toLowerCase();
  if (!ALLOWED_EXTENSIONS.has(ext)) {
    throw new FileSecurityError(`unsupported file type: ${ext || "(none)"}`);
  }
  return ext;
}

export function validateSize(filePath: string, maxBytes: number): void {
  const size = fs.statSync(filePath).size;
  if (size > maxBytes) {
    throw new FileSecurityError(`file too large (${Math.round(size / 1e6)} MB > ${Math.round(maxBytes / 1e6)} MB)`);
  }
}

export function sha256File(filePath: string): string {
  const hash = crypto.createHash("sha256");
  const fd = fs.openSync(filePath, "r");
  try {
    const buf = Buffer.allocUnsafe(1 << 20);
    let bytes = 0;
    while ((bytes = fs.readSync(fd, buf, 0, buf.length, null)) > 0) {
      hash.update(buf.subarray(0, bytes));
    }
  } finally {
    fs.closeSync(fd);
  }
  return hash.digest("hex");
}

/**
 * Async checksum.
 *
 * The watch folder is often a cloud-synced drive (Google Drive, OneDrive),
 * where the first read of a file materialises it from the network. Doing that
 * synchronously on the main thread is a visible freeze, so the pipeline hashes
 * asynchronously and stays responsive.
 */
export async function sha256FileAsync(filePath: string): Promise<string> {
  const hash = crypto.createHash("sha256");
  const handle = await fs.promises.open(filePath, "r");
  try {
    const buf = Buffer.allocUnsafe(1 << 20);
    for (;;) {
      const { bytesRead } = await handle.read(buf, 0, buf.length, null);
      if (bytesRead <= 0) break;
      hash.update(buf.subarray(0, bytesRead));
    }
  } finally {
    await handle.close();
  }
  return hash.digest("hex");
}

/** Read only the first `bytes` of a file -- enough to identify its format. */
export function readHead(filePath: string, bytes = 4000): string {
  const fd = fs.openSync(filePath, "r");
  try {
    const buf = Buffer.allocUnsafe(bytes);
    const read = fs.readSync(fd, buf, 0, bytes, 0);
    return buf.subarray(0, read).toString("utf-8");
  } finally {
    fs.closeSync(fd);
  }
}

/**
 * Extract a zip with zip-slip protection.
 *
 * Every entry is resolved and checked to stay inside the destination; absolute
 * paths and drive letters are rejected outright, and entry count plus
 * uncompressed size are capped.
 */
export function safeExtractZip(
  zipPath: string,
  destDir: string,
  options: { maxEntries?: number; maxTotalBytes?: number; maxEntryBytes?: number } = {}
): string[] {
  const maxEntries = options.maxEntries ?? 20_000;
  const maxTotalBytes = options.maxTotalBytes ?? 2 * 1024 * 1024 * 1024;
  const maxEntryBytes = options.maxEntryBytes ?? 200 * 1024 * 1024;

  // Use a real zip reader, not the system `tar`: GNU tar (the one shipped in
  // Git for Windows) cannot read zip files at all, which would fail every
  // WhatsApp archive on Windows.
  let entries: Record<string, Uint8Array>;
  try {
    entries = unzipSync(new Uint8Array(fs.readFileSync(zipPath))) as Record<string, Uint8Array>;
  } catch {
    throw new Error("cannot read archive (is it a valid zip?)");
  }

  const names = Object.keys(entries);
  if (names.length > maxEntries) {
    throw new FileSecurityError(`archive has too many entries (${names.length})`);
  }

  const destResolved = path.resolve(destDir);
  const extracted: string[] = [];
  let total = 0;
  for (const name of names) {
    // Reject absolute paths, drive letters and traversal outright.
    if (name.includes("..") || path.isAbsolute(name) || /^[a-zA-Z]:/.test(name)) {
      throw new FileSecurityError(`unsafe path in archive: ${name}`);
    }
    const target = path.resolve(destResolved, name);
    const rel = path.relative(destResolved, target);
    if (rel.startsWith("..") || path.isAbsolute(rel)) {
      throw new FileSecurityError(`path escapes archive root: ${name}`);
    }

    const data = entries[name]!;
    if (data.length > maxEntryBytes) {
      throw new FileSecurityError(`archive entry too large: ${path.basename(name)}`);
    }
    total += data.length;
    if (total > maxTotalBytes) {
      throw new FileSecurityError("archive uncompressed size exceeds limit");
    }

    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, data);
    extracted.push(target);
  }
  return extracted;
}

export function moveFile(from: string, destDir: string): string {
  fs.mkdirSync(destDir, { recursive: true });
  const target = path.join(destDir, sanitizeFilename(path.basename(from)));
  let final = target;
  let n = 1;
  while (fs.existsSync(final)) {
    const ext = path.extname(target);
    final = path.join(destDir, `${path.basename(target, ext)}_${n}${ext}`);
    n++;
  }
  try {
    fs.renameSync(from, final);
  } catch {
    fs.copyFileSync(from, final);
    fs.unlinkSync(from);
  }
  return final;
}
