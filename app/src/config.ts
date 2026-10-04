/**
 * Configuration for Circle.
 *
 * Two rules are load-bearing here and come from real bugs in the previous
 * version of this app:
 *
 *  1. There is exactly ONE folder the user chose to read, and Circle creates
 *     nothing inside it. No `whatsapp/`, no `imports/`, no stubs. The previous
 *     build auto-created a dozen empty source folders before anyone was asked,
 *     which synced straight back to Google Drive.
 *
 *  2. Circle's own working files (the SQLite archive, processed/, failed/,
 *     tmp/, the media store) live in a SEPARATE app-data folder. One install
 *     lives in one place, so "back up Circle" and "delete Circle" stay single
 *     operations. Keeping them apart is also what stops the watcher from ever
 *     seeing Circle's own archive as an import.
 */
import os from "node:os";
import path from "node:path";
import fs from "node:fs";

export interface CirclePaths {
  /** Where Circle's own files live. Never inside the user's folder. */
  dataDir: string;
  /** The SQLite archive file. */
  dbPath: string;
  /** Media store (content-addressed copies of attachments). */
  mediaDir: string;
  processedDir: string;
  failedDir: string;
  quarantineDir: string;
  tmpDir: string;
  /** A small JSON file holding the user's chosen settings. */
  settingsFile: string;
}

export interface CircleSettings {
  /** The one folder the user asked Circle to read. Empty until they choose. */
  watchFolder: string;
  /** Whether first-run has been completed. */
  onboarded: boolean;
  /**
   * When the reader accepted the terms of use and privacy notice, and which
   * version of them they accepted.
   *
   * Empty means they have not. The app refuses to index anything until this
   * is set: accepting is a precondition for reading someone's messages, not
   * a formality to wave through after the data is already on disk.
   */
  termsAcceptedAt: string;
  termsVersion: string;
  ollamaUrl: string;
  ollamaModel: string;
  embeddingModel: string;
  llmMaxAnswerTokens: number;
  llmMaxPrepTokens: number;
  ragMaxEvidence: number;
  ragEvidenceChars: number;
  ragTotalEvidenceChars: number;
}

export const DEFAULT_SETTINGS: CircleSettings = {
  watchFolder: "",
  onboarded: false,
  termsAcceptedAt: "",
  termsVersion: "",
  ollamaUrl: "http://localhost:11434",
  ollamaModel: "gemma3:4b",
  embeddingModel: "nomic-embed-text",
  llmMaxAnswerTokens: 320,
  llmMaxPrepTokens: 600,
  ragMaxEvidence: 4,
  ragEvidenceChars: 240,
  ragTotalEvidenceChars: 1000,
};

/**
 * Resolve every path Circle owns.
 *
 * A packaged build must not write into Program Files, so the default is the
 * user's Documents folder. `CIRCLE_DATA_DIR` overrides it (used by tests and
 * by the dev server).
 */
export function resolvePaths(dataDirOverride?: string): CirclePaths {
  const dataDir =
    dataDirOverride ||
    process.env.CIRCLE_DATA_DIR ||
    path.join(os.homedir(), "Documents", "Circle");
  return {
    dataDir,
    dbPath: path.join(dataDir, "circle.db"),
    mediaDir: path.join(dataDir, "media"),
    processedDir: path.join(dataDir, "processed"),
    failedDir: path.join(dataDir, "failed"),
    quarantineDir: path.join(dataDir, "quarantine"),
    tmpDir: path.join(dataDir, "tmp"),
    settingsFile: path.join(dataDir, "settings.json"),
  };
}

/** Create only Circle's own folders. Never the user's watch folder. */
export function ensureOwnLayout(paths: CirclePaths): void {
  for (const dir of [
    paths.dataDir,
    paths.mediaDir,
    paths.processedDir,
    paths.failedDir,
    paths.quarantineDir,
    paths.tmpDir,
  ]) {
    fs.mkdirSync(dir, { recursive: true });
  }
}

export function loadSettings(paths: CirclePaths): CircleSettings {
  try {
    const raw = fs.readFileSync(paths.settingsFile, "utf-8");
    const parsed = JSON.parse(raw) as Partial<CircleSettings>;
    return { ...DEFAULT_SETTINGS, ...parsed };
  } catch {
    return { ...DEFAULT_SETTINGS };
  }
}

/**
 * The version of the terms the reader agreed to.
 *
 * Bumping this is what makes a materially changed document require a fresh
 * agreement: an old acceptance stops counting, and the consent screen returns.
 * Without it, agreeing once would silently cover every future revision.
 */
export const TERMS_VERSION = "2026-10-04";

/** Whether these terms have been accepted for the CURRENT version. */
export function hasAcceptedTerms(settings: CircleSettings): boolean {
  return Boolean(settings.termsAcceptedAt) && settings.termsVersion === TERMS_VERSION;
}

export function saveSettings(paths: CirclePaths, settings: CircleSettings): void {  fs.mkdirSync(path.dirname(paths.settingsFile), { recursive: true });
  const tmp = `${paths.settingsFile}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(settings, null, 2), "utf-8");
  fs.renameSync(tmp, paths.settingsFile);
}

/**
 * True when `child` is inside `parent`.
 *
 * Uses path-segment comparison, not a string prefix: `/x/processed2` must NOT
 * be treated as inside `/x/processed`. The previous watcher used `startsWith`
 * and let Circle ingest its own output folders.
 */
export function isInside(child: string, parent: string): boolean {
  const rel = path.relative(path.resolve(parent), path.resolve(child));
  return rel === "" || (!rel.startsWith("..") && !path.isAbsolute(rel));
}

/**
 * Whether an imported file may be moved into processed/.
 *
 * It may only be moved when it already lives inside Circle's own managed
 * folder. A folder the user pointed us at (Google Drive, Downloads, a USB
 * backup) is read in place and left exactly as it was.
 */
export function shouldArchive(
  filePath: string,
  paths: CirclePaths,
  settings: CircleSettings
): boolean {
  if (!settings.watchFolder) return false;
  const watch = path.resolve(settings.watchFolder);
  // The user's folder is only "managed" if it happens to sit inside our data
  // dir, which it normally never does. This keeps Drive folders read-only.
  if (isInside(watch, paths.dataDir)) {
    return isInside(filePath, watch);
  }
  return false;
}
