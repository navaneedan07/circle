/**
 * Local media store.
 *
 * A media file is copied into a content-addressed folder
 * (`media/<aa>/<bb>/<hash>-<name>`), so identical files are stored once and
 * nothing ever leaves the machine. Only metadata is written to the database.
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import type { CirclePaths, CircleSettings } from "./config.js";
import type { MediaAttachment, MediaKind, SourceType } from "./domain.js";
import { nowIso } from "./domain.js";
import type { Store } from "./store.js";
import { AUDIO_EXTENSIONS, MEDIA_EXTENSIONS, sanitizeFilename } from "./security.js";

const MIME: Record<string, string> = {
  ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif",
  ".webp": "image/webp", ".bmp": "image/bmp", ".heic": "image/heic",
  ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".3gp": "video/3gpp",
  ".opus": "audio/opus", ".m4a": "audio/mp4", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
  ".wav": "audio/wav", ".aac": "audio/aac", ".flac": "audio/flac",
};

export function mediaFilenameKey(filename: string): string {
  return path.basename(filename).toLowerCase().trim();
}

function kindFor(ext: string): MediaKind {
  if ([".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic"].includes(ext)) return "image";
  if ([".mp4", ".mov", ".webm", ".3gp", ".mkv"].includes(ext)) return "video";
  if (ext === ".opus" || ext === ".m4a" || ext === ".aac") return "voice";
  if (AUDIO_EXTENSIONS.has(ext)) return "audio";
  return "document";
}

export class MediaIngest {
  constructor(
    private readonly store: Store,
    private readonly paths: CirclePaths,
    private readonly settings: CircleSettings
  ) {}

  /** Copy a media file into the store and record its metadata. */
  add(filePath: string, source: SourceType): MediaAttachment | null {
    const ext = path.extname(filePath).toLowerCase();
    if (!MEDIA_EXTENSIONS.has(ext)) return null;
    let stat: fs.Stats;
    try {
      stat = fs.statSync(filePath);
    } catch {
      return null;
    }
    if (!stat.isFile() || stat.size === 0) return null;

    const checksum = sha256File(filePath);
    const id = `media-${checksum.slice(0, 32)}`;
    const existing = this.store.getMedia(id);
    if (existing) return existing;

    const aa = checksum.slice(0, 2);
    const bb = checksum.slice(2, 4);
    const dir = path.join(this.paths.mediaDir, aa, bb);
    fs.mkdirSync(dir, { recursive: true });
    const safeName = sanitizeFilename(path.basename(filePath));
    const storedPath = path.join(dir, `${checksum.slice(0, 24)}-${safeName}`);
    try {
      if (!fs.existsSync(storedPath)) fs.copyFileSync(filePath, storedPath);
    } catch {
      return null;
    }

    const attachment: MediaAttachment = {
      id,
      filename: path.basename(filePath),
      filename_key: mediaFilenameKey(filePath),
      kind: kindFor(ext),
      mime_type: MIME[ext] ?? "application/octet-stream",
      size_bytes: stat.size,
      checksum,
      stored_path: storedPath,
      source,
      origin: "imported",
      person_id: null,
      message_id: null,
      conversation_id: null,
      occurred_at: null,
      duration_seconds: null,
      language: "",
      transcript: "",
      caption: "",
      status: "STORED",
      error: "",
      imported_at: nowIso(),
    };
    this.store.insertMediaIfNew(attachment);
    return attachment;
  }

  findByKey(key: string): MediaAttachment | null {
    return this.store.findMediaByFilenameKey(key);
  }

  /** Resolve a stored media id to a path, refusing anything outside the store. */
  resolvePath(mediaId: string): string | null {
    const media = this.store.getMedia(mediaId);
    if (!media) return null;
    const resolved = path.resolve(media.stored_path);
    const root = path.resolve(this.paths.mediaDir);
    if (!resolved.startsWith(root)) return null;
    return fs.existsSync(resolved) ? resolved : null;
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
