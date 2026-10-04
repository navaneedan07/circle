/**
 * Folder watcher.
 *
 * Watches exactly ONE folder -- the one the user chose -- recursively. It
 * never creates anything inside that folder, and it can never ingest Circle's
 * own files.
 *
 * The previous version's bug: its only guard against re-ingesting Circle's own
 * output was a string prefix check (`startsWith`), which missed the archive
 * database, the media store and anything else Circle owns, and was also wrong
 * on paths like `/x/processed2` matching `/x/processed`. Here every exclusion
 * is a resolved-path containment test, and the archive/media/working folders
 * are all excluded explicitly.
 */
import fs from "node:fs";
import path from "node:path";
import { EventEmitter } from "node:events";
import { watch, type FSWatcher } from "chokidar";
import { isInside, type CirclePaths } from "./config.js";

const IN_PROGRESS_SUFFIXES = [
  ".crdownload", ".part", ".partial", ".download", ".tmp", ".temp",
  ".filepart", ".swp", ".swx",
];
const IN_PROGRESS_NAMES = [".ds_store", "thumbs.db", "desktop.ini"];

export function isInProgress(filePath: string): boolean {
  const name = path.basename(filePath).toLowerCase();
  if (IN_PROGRESS_SUFFIXES.some((s) => name.endsWith(s))) return true;
  if (IN_PROGRESS_NAMES.includes(name)) return true;
  if (name.startsWith("~$") || name.startsWith(".~") || name.startsWith(".#")) return true;
  return false;
}

export interface WatcherStats {
  processed: number;
  failed: number;
  skipped: number;
  lastEventAt: number | null;
  running: boolean;
}

export class FolderWatcher extends EventEmitter {
  private watcher: FSWatcher | null = null;
  private queue: string[] = [];
  private queued = new Set<string>();
  private draining = false;
  private stopped = false;
  private recent = new Map<string, number>();
  readonly stats: WatcherStats = {
    processed: 0,
    failed: 0,
    skipped: 0,
    lastEventAt: null,
    running: false,
  };

  constructor(
    private paths: CirclePaths,
    private getWatchFolder: () => string,
    private processFn: (filePath: string) => Promise<{ status: string }>
  ) {
    super();
  }

  /** True when `filePath` is one of Circle's own files and must be ignored. */
  private isOurs(filePath: string): boolean {
    const resolved = path.resolve(filePath);
    for (const own of [
      this.paths.dataDir,
      this.paths.dbPath,
      this.paths.mediaDir,
      this.paths.processedDir,
      this.paths.failedDir,
      this.paths.quarantineDir,
      this.paths.tmpDir,
    ]) {
      if (isInside(resolved, own) || resolved === path.resolve(own)) return true;
    }
    // The DB's WAL/SHM sidecars.
    if (resolved.startsWith(path.resolve(this.paths.dbPath))) return true;
    return false;
  }

  start(): void {
    const root = this.getWatchFolder();
    if (!root || !fs.existsSync(root)) {
      this.stats.running = false;
      return;
    }
    this.stopped = false;
    // The watcher IS active from the moment `watch` returns. Its "ready"
    // event only means the initial scan finished, and on a large synced drive
    // that can take minutes -- during which the Settings screen happily
    // reported a working watcher as "missing".
    this.stats.running = true;
    this.watcher = watch(root, {
      persistent: true,
      ignoreInitial: false,
      depth: 12,
      awaitWriteFinish: { stabilityThreshold: 1500, pollInterval: 200 },
      ignored: (p: string) => isInProgress(p) || this.isOurs(p),
    });
    this.watcher.on("add", (p: string) => this.enqueue(p));
    this.watcher.on("change", (p: string) => this.enqueue(p));
    this.watcher.on("error", (err: unknown) => this.emit("error", err));
    this.watcher.on("ready", () => {
      this.stats.running = true;
      this.emit("ready");
    });
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.stats.running = false;
    if (this.watcher) {
      await this.watcher.close();
      this.watcher = null;
    }
  }

  /** Re-point the watcher at a new folder (takes effect immediately). */
  async restart(): Promise<void> {
    await this.stop();
    this.start();
  }

  private enqueue(filePath: string): void {
    if (this.stopped) return;
    const resolved = path.resolve(filePath);
    if (this.isOurs(resolved) || isInProgress(resolved)) return;
    const now = Date.now();
    const last = this.recent.get(resolved);
    if (last && now - last < 800) return;
    this.recent.set(resolved, now);
    if (this.queued.has(resolved)) return;
    this.queued.add(resolved);
    this.queue.push(resolved);
    this.stats.lastEventAt = now;
    void this.drain();
  }

  private async drain(): Promise<void> {
    if (this.draining) return;
    this.draining = true;
    try {
      while (this.queue.length > 0 && !this.stopped) {
        const filePath = this.queue.shift()!;
        this.queued.delete(filePath);
        try {
          const stat = fs.existsSync(filePath) ? fs.statSync(filePath) : null;
          if (!stat || !stat.isFile() || stat.size === 0) continue;
          const result = await this.processFn(filePath);
          if (result.status === "COMPLETED") this.stats.processed++;
          else if (result.status === "FAILED" || result.status === "QUARANTINED") this.stats.failed++;
          else if (result.status === "SKIPPED") this.stats.skipped++;
          this.emit("job", result);
        } catch (err) {
          this.stats.failed++;
          this.emit("error", err);
        }
      }
    } finally {
      this.draining = false;
    }
  }

  /** Queue every existing file under the watch folder. */
  rescan(): number {
    const root = this.getWatchFolder();
    if (!root || !fs.existsSync(root)) return 0;
    let count = 0;
    const walk = (dir: string) => {
      let entries: fs.Dirent[];
      try {
        entries = fs.readdirSync(dir, { withFileTypes: true });
      } catch {
        return;
      }
      for (const entry of entries) {
        const full = path.join(dir, entry.name);
        if (this.isOurs(full)) continue;
        if (entry.isDirectory()) walk(full);
        else if (entry.isFile() && !isInProgress(full)) {
          this.enqueue(full);
          count++;
        }
      }
    };
    walk(root);
    return count;
  }

  queueSize(): number {
    return this.queue.length;
  }
}
