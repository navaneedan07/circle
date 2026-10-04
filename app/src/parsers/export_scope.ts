/**
 * Recognising platform data exports, and what inside them is worth reading.
 *
 * Instagram ("Meta") hands you a folder that is mostly *not* conversations:
 * account settings, ad interests, login history, follower lists, and every
 * photo you have ever posted. In a real export that is hundreds of files
 * against a few dozen message threads, and the non-message JSON is actively
 * harmful: fed to a generic parser it invents "people" and junk memories.
 *
 * So inside an export we only read the conversation tree:
 *
 *   <export>/your_instagram_activity/messages/inbox/<thread>/message_1.json
 *   <export>/your_instagram_activity/messages/inbox/<thread>/photos/*.jpg
 *
 * Everything else is skipped, cheaply, before any file is opened.
 */
import path from "node:path";

/** Directory names that only appear in a Meta data export. */
const EXPORT_MARKER_DIRS = new Set([
  "personal_information",
  "your_instagram_activity",
  "connections",
  "ads_information",
  "security_and_login_information",
  "apps_and_websites_off_of_instagram",
  "logged_information",
  "preferences",
  "monetization",
]);

const EXPORT_MARKER_PREFIXES = ["instagram-", "meta-"];

const INBOX_PARTS = ["messages", "inbox"];
const ATTACHMENT_DIRS = new Set(["photos", "videos", "audio", "gifs", "files"]);
const MESSAGE_SUFFIXES = new Set([".json", ".ndjson", ".html", ".htm"]);

function partsOf(filePath: string): string[] {
  return filePath.split(/[\\/]/).map((p) => p.toLowerCase());
}

export function isDataExport(filePath: string): boolean {
  for (const part of partsOf(filePath)) {
    if (EXPORT_MARKER_DIRS.has(part)) return true;
    if (EXPORT_MARKER_PREFIXES.some((prefix) => part.startsWith(prefix))) return true;
  }
  return false;
}

export function inConversationTree(filePath: string): boolean {
  const parts = partsOf(filePath);
  if (!INBOX_PARTS.every((p) => parts.includes(p))) return false;
  const ext = path.extname(filePath).toLowerCase();
  const stem = path.basename(filePath, ext).toLowerCase();
  if (stem.startsWith("message") && MESSAGE_SUFFIXES.has(ext)) return true;
  // Media sitting next to the thread it belongs to.
  return ext !== "" && parts.slice(0, -1).some((part) => ATTACHMENT_DIRS.has(part));
}

/**
 * Why this file should not be ingested, or null if it should be.
 *
 * Only applies inside a recognised data export; anything outside an export is
 * left to the normal routing rules.
 */
export function skipReason(filePath: string): string | null {
  if (!isDataExport(filePath)) return null;
  if (inConversationTree(filePath)) return null;
  const ext = path.extname(filePath).toLowerCase();
  if (ext === "") return "export directory marker";
  return "not a conversation file in a Meta data export";
}
