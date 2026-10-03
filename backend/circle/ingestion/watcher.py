"""Local import folder watcher (spec §6, §43).

Watches any number of folders at once, so a Google Drive sync folder, a
Downloads folder and a USB backup can all feed the same index.

Windows-aware:
  - waits until a file's size is stable before processing (partially copied files)
  - materializes cloud placeholders before reading (Drive/OneDrive streaming)
  - retries reads briefly on sharing violations (file locks)
  - de-duplicates bursty/repeated filesystem events
  - handles renames and deletions gracefully
  - survives restarts: scans existing folders on startup, resumes queued jobs

Never raises out of a callback: one broken file must not stop ingestion.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from circle.config import Settings, get_settings
from circle.integration import cloudfolder

log = logging.getLogger("circle.watcher")

# Files a browser/OS creates while a download or write is still in progress.
# These must never be ingested (or counted as failures) — the final file that
# replaces them will be picked up by its own event.
IN_PROGRESS_SUFFIXES = (
    ".crdownload",   # Chrome/Edge
    ".part",         # Firefox / wget
    ".partial",      # Internet Explorer / Edge legacy
    ".download",     # Safari
    ".tmp", ".temp", ".filepart",
    ".swp", ".swx",  # editors
)
IN_PROGRESS_NAMES = (".ds_store", "thumbs.db", "desktop.ini", "icon\r")

def is_in_progress(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(IN_PROGRESS_SUFFIXES):
        return True
    if name in IN_PROGRESS_NAMES or name.startswith(("~$", ".~", ".#")):
        return True
    return False


class _Handler(FileSystemEventHandler):
    def __init__(self, emit: Callable[[str, Path], None]):
        self._emit = emit

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("created", Path(event.src_path))

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("modified", Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit("moved", Path(event.dest_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        pass  # nothing to do; state stays consistent


class FolderWatcher:
    def __init__(self, settings: Optional[Settings] = None,
                 process_fn: Optional[Callable[[Path], object]] = None,
                 roots: Optional[list[Path]] = None):
        self.settings = settings or get_settings()
        self.process_fn = process_fn
        # roots[0] is the primary folder; `root` stays the back-compat alias.
        self.roots: list[Path] = self._normalize(roots) or [self.settings.root_dir()]
        self._queue: "queue.Queue[Path]" = queue.Queue()
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._observer: Optional[Observer] = None
        self._scheduled: dict[str, Observer] = {}
        self._worker: Optional[threading.Thread] = None
        self._scan_thread: Optional[threading.Thread] = None
        self.stats = {"processed": 0, "failed": 0, "skipped": 0,
                      "last_event_at": None, "running": False}

    @staticmethod
    def _normalize(roots: Optional[list[Path]]) -> list[Path]:
        out: list[Path] = []
        seen: set[str] = set()
        for r in (roots or []):
            try:
                resolved = Path(r).expanduser().resolve()
            except OSError:
                continue
            key = str(resolved).lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(resolved)
        return out

    @property
    def root(self) -> Path:
        """Primary watch folder (kept for callers that only care about one)."""
        return self.roots[0]

    def set_roots(self, roots: list[Path], rescan: bool = True) -> list[Path]:
        """Replace the watched folders at runtime, without a restart.

        A running observer keeps watching folders that are still present, so
        swapping a Google Drive path does not lose events mid-import.
        """
        new_roots = self._normalize(roots) or [self.settings.root_dir()]
        previous = {str(p).lower() for p in self.roots}
        added = [p for p in new_roots if str(p).lower() not in previous]
        self.roots = new_roots
        if self._observer is not None:
            for path in added:
                self._watch(self._observer, path)
            dropped = {str(p).lower() for p in new_roots}
            for key in list(self._scheduled):
                if key not in dropped:
                    self._unschedule(key)
            if self.settings.watcher_enabled and rescan:
                threading.Thread(target=self._scan_roots, daemon=True,
                                 name="circle-scan").start()
        return self.roots

    # ------------------------------------------------------------------
    def start(self, rescan: bool = True) -> None:
        self._ensure_layout()
        self._observer = Observer()
        self._observer.daemon = True
        for path in self.roots:
            self._watch(self._observer, path)
        self._observer.start()
        self._worker = threading.Thread(target=self._work_loop, daemon=True,
                                        name="circle-pipeline")
        self._worker.start()
        self.stats["running"] = True
        if rescan:
            self._scan_thread = threading.Thread(target=self._scan_roots,
                                                 daemon=True,
                                                 name="circle-scan")
            self._scan_thread.start()
        log.info("watcher started on %d folder(s): %s", len(self.roots),
                 ", ".join(str(p) for p in self.roots))

    def _watch(self, observer: Observer, path: Path) -> None:
        """Schedule one folder, creating it when it does not exist yet."""
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("cannot create watch folder %s: %s", path, e)
            return
        key = str(path).lower()
        if key in self._scheduled:
            return
        try:
            observer.schedule(_Handler(self._on_event), str(path), recursive=True)
            self._scheduled[key] = observer
            log.info("watching folder: %s", path)
        except OSError as e:
            log.warning("cannot watch %s: %s", path, e)

    def _unschedule(self, key: str) -> None:
        """Stop watching a folder that was removed."""
        watch = self._scheduled.pop(key, None)
        if watch is None:
            return
        try:
            watch.unschedule_all()   # type: ignore[attr-defined]
        except Exception:
            pass
        log.info("stopped watching folder: %s", key)

    def stop(self) -> None:
        self._stop.set()
        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
        if self._worker:
            self._worker.join(timeout=5)
        self.stats["running"] = False

    def rescan(self) -> int:
        n = 0
        for path in self._iter_all():
            self.enqueue(path)
            n += 1
        return n

    def _iter_all(self):
        for root in self.roots:
            yield from self._iter_files(root)

    def enqueue(self, path: Path) -> None:
        key = str(path)
        now = time.time()
        with self._lock:
            last = self._seen.get(key)
            if last and now - last < 1.0:
                return   # duplicate filesystem event
            self._seen[key] = now
        self._queue.put(path)

    # ------------------------------------------------------------------
    def _on_event(self, kind: str, path: Path) -> None:
        try:
            self.stats["last_event_at"] = time.time()
            self.enqueue(path)
        except Exception as e:   # pragma: no cover
            log.warning("event handling failed: %s", e)

    def _iter_files(self, root: Path):
        if not root.exists():
            return
        try:
            for p in sorted(root.rglob("*")):
                if not p.is_file() or is_in_progress(p):
                    continue
                if p.name.startswith("."):
                    continue
                yield p
        except OSError as e:
            log.warning("scan error on %s: %s", root, e)

    def _scan_roots(self) -> None:
        count = 0
        for path in self._iter_all():
            if self._stop.is_set():
                return
            self.enqueue(path)
            count += 1
        log.info("initial scan queued %d file(s) across %d folder(s)",
                 count, len(self.roots))

    # ------------------------------------------------------------------
    def _wait_stable(self, path: Path, timeout: float = 30.0) -> bool:
        """Wait until the file is complete and fully readable.

        Two different problems meet here. A local file is still being copied,
        so its size keeps changing. A cloud placeholder reports the full size
        from remote metadata while having no local bytes at all, so size
        stability says nothing and reading is what triggers the download.
        Placeholders are therefore materialized first, with a much longer
        budget, because a truncated read would hash to the wrong checksum.
        """
        settle = max(0.3, self.settings.watcher_stability_seconds)
        if cloudfolder.is_placeholder(path):
            budget = self.settings.cloud_materialize_timeout
            log.info("materializing cloud file: %s", path.name)
            if not cloudfolder.materialize(path, timeout=budget):
                log.warning("cloud file did not download in time: %s", path.name)
                return False
            # Once local, fall through to the normal stability check.
            timeout = max(timeout, 5.0)
        deadline = time.time() + timeout
        last_size = -1
        stable_since = 0.0
        stable_polls = 0
        while time.time() < deadline and not self._stop.is_set():
            try:
                st = path.stat()
                size = st.st_size
            except OSError:
                return False   # deleted or not ready
            if size == last_size and size >= 0:
                stable_polls += 1
                # A file last modified before the settle window, whose size has
                # not moved across consecutive polls, is already complete. On a
                # large folder (a synced Drive backup is thousands of files)
                # paying the full window per file is pure sleeping.
                if stable_polls >= 2 and (time.time() - st.st_mtime) >= settle:
                    return True
                if time.time() - stable_since >= settle:
                    return True
            else:
                last_size = size
                stable_since = time.time()
                stable_polls = 0
            time.sleep(0.25)
        return False

    def _read_with_retry(self, path: Path) -> bool:
        """Return True when the file can be opened exclusively (unlocked)."""
        for _ in range(5):
            try:
                with path.open("rb") as f:
                    f.read(1)
                return True
            except OSError:
                time.sleep(0.5)
        return False

    def owns(self, path: Path) -> bool:
        """True when the path lives inside one of the watched folders."""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        for root in self.roots:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            try:
                path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._handle(path)
            except Exception as e:   # never die
                log.error("watcher item failed: %s", e, exc_info=True)
            finally:
                self._queue.task_done()

    def _handle(self, path: Path) -> None:
        if self.process_fn is None or not path.exists():
            return
        if is_in_progress(path):
            # Mid-download/partial write: ignore silently (not a failure).
            # If a real file never replaces it, it simply stays untouched.
            return
        try:
            if path.stat().st_size == 0:
                # Nothing written yet (browser still streaming the download).
                # Skip quietly: the write event that adds content re-triggers us.
                log.debug("empty file parked until content arrives: %s", path.name)
                return
        except OSError:
            return
        # Ignore files inside output folders
        try:
            resolved = path.resolve()
            for out in (self.settings.processed_dir(),
                        self.settings.failed_dir(),
                        self.settings.quarantine_dir()):
                if str(resolved).startswith(str(out)):
                    return
        except OSError:
            return

        if not self._wait_stable(path):
            if path.exists():
                log.warning("file never became stable: %s", path.name)
                self.stats["failed"] += 1
            return
        if not self._read_with_retry(path):
            log.warning("file locked, skipping for now: %s", path.name)
            return
        result = self.process_fn(path)
        status = getattr(result, "status", "UNKNOWN")
        if status == "COMPLETED":
            self.stats["processed"] += 1
        elif status in ("FAILED", "QUARANTINED"):
            self.stats["failed"] += 1
        elif status == "SKIPPED":
            self.stats["skipped"] += 1

    # ------------------------------------------------------------------
    def _ensure_layout(self) -> None:
        root = self.settings.root_dir()
        # Source subfolders are only created in Circle's own managed folder.
        # A Google Drive folder belongs to the user: creating a dozen empty
        # subfolders inside their synced backup would be rude and would then
        # sync back to Drive.
        if not self.settings.root_is_managed():
            return
        for sub in self.settings.SOURCE_SUBFOLDERS:
            (root / sub).mkdir(parents=True, exist_ok=True)
        for d in (root, self.settings.processed_dir(),
                  self.settings.failed_dir(), self.settings.quarantine_dir(),
                  self.settings.temp_dir()):
            d.mkdir(parents=True, exist_ok=True)
