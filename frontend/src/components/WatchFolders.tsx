import { useState } from "react";
import { api, CloudDriveInfo, WatchFolder } from "../api";
import { usePoll } from "../hooks";

const PROVIDER_LABEL: Record<string, string> = {
  google_drive: "Google Drive",
  onedrive: "OneDrive",
  icloud: "iCloud",
  dropbox: "Dropbox",
  local: "This machine",
};

/**
 * Where Circle reads from.
 *
 * This is the surface for pointing Circle at a synced folder. It is shown
 * prominently on the Imports page (that is where files come from) and again in
 * Settings. Folders the user owns are read in place: nothing is moved.
 */
export default function WatchFolders({ compact = false }: { compact?: boolean }) {
  const { data, refresh } = usePoll(() => api.watchFolders(), 15000);
  const [entry, setEntry] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const folders = data?.folders ?? [];
  const drive = data?.cloud_drive as CloudDriveInfo | undefined;
  const driveRoots = (drive?.roots ?? []).filter(
    (r) => !folders.some((f) => f.path.toLowerCase() === r.toLowerCase())
  );
  const hasCloud = folders.some((f) => f.cloud);

  const add = async (path: string) => {
    const clean = path.trim().replace(/^["']|["']$/g, "");
    if (!clean) return;
    setBusy(true);
    setNote(null);
    try {
      await api.addWatchFolder(clean);
      setEntry("");
      setNote(`Now watching ${clean}`);
      refresh();
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (folder: WatchFolder) => {
    setBusy(true);
    setNote(null);
    try {
      await api.removeWatchFolder(folder.path);
      refresh();
    } catch (e) {
      setNote((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="border border-line">
      <div className="flex flex-wrap items-baseline justify-between gap-3 border-b border-line px-4 py-2.5">
        <h2 className="eyebrow">Where Circle reads from</h2>
        <span className="eyebrow">
          {folders.length} watched
          {hasCloud ? ", read only" : ""}
        </span>
      </div>

      {!compact && (
        <p className="border-b border-line px-4 py-3 text-xs leading-5 text-muted">
          Drop an export into any of these folders and Circle picks it up on its
          own. A folder you own, such as a Google Drive backup, is read in place:
          nothing is moved, renamed or deleted inside it.
        </p>
      )}

      <ul>
        {folders.length === 0 && (
          <li className="px-4 py-6 text-center text-sm text-muted">
            No folders configured yet.
          </li>
        )}
        {folders.map((f) => (
          <li
            key={f.path}
            className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3 last:border-b-0"
          >
            <div className="min-w-0">
              <div className="mono truncate text-xs text-ink" title={f.path}>
                {f.path}
              </div>
              <div className="eyebrow mt-1.5 flex flex-wrap gap-x-4 gap-y-1">
                <span>{PROVIDER_LABEL[f.kind] ?? f.kind}</span>
                {f.cloud && <span>read only</span>}
                {f.primary && <span>main folder</span>}
                {!f.exists && <span className="text-accent">not found</span>}
              </div>
            </div>
            {!f.primary && (
              <button
                onClick={() => remove(f)}
                disabled={busy}
                className="inline-flex min-h-6 items-center text-[11px] uppercase tracking-[0.16em] text-muted hover:text-accent disabled:opacity-40"
              >
                Stop watching
              </button>
            )}
          </li>
        ))}
      </ul>

      <div className="border-t border-line px-4 py-4">
        <h3 className="eyebrow">Use a Google Drive backup</h3>
        {drive?.installed ? (
          <p className="mt-2 text-xs leading-5 text-muted">
            Google Drive for Desktop is installed. Pick one of your Drive
            folders below, or paste any path. You stay signed in to Google
            there; Circle holds no Google credentials and never signs in for
            you.
          </p>
        ) : (
          <p className="mt-2 text-xs leading-5 text-muted">
            Google Drive for Desktop is not installed on this machine. Install
            it and sign in there, then come back and pick your backup folder.
            Circle reads the folder it syncs locally and holds no Google
            credentials.
          </p>
        )}

        {driveRoots.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {driveRoots.map((r) => (
              <button
                key={r}
                onClick={() => add(r)}
                disabled={busy}
                className="mono max-w-full truncate border border-line px-3 py-1.5 text-left text-xs text-ink hover:border-line-strong disabled:opacity-40"
                title={`Watch ${r}`}
              >
                Watch {r}
              </button>
            ))}
          </div>
        )}

        <a
          href="https://www.google.com/drive/download/"
          target="_blank"
          rel="noreferrer"
          className="mt-3 inline-flex min-h-6 items-center text-xs text-muted underline underline-offset-2 hover:text-ink"
        >
          Get Google Drive for Desktop from Google
        </a>
      </div>

      <div className="border-t border-line px-4 py-4">
        <h3 className="eyebrow">Watch any other folder</h3>
        <p className="mt-2 text-xs leading-5 text-muted">
          A USB drive, a Downloads folder, a backup on another disk. Use the
          full path.
        </p>
        <div className="mt-3 flex gap-2">
          <input
            value={entry}
            onChange={(e) => setEntry(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && add(entry)}
            placeholder="G:\My Drive\Backups, or D:\Archive"
            className="mono min-w-0 flex-1 border border-line bg-paper px-3 py-2 text-xs text-ink outline-none placeholder:text-muted focus:border-line-strong"
          />
          <button
            onClick={() => add(entry)}
            disabled={busy || !entry.trim()}
            className="shrink-0 bg-ink px-4 py-2 text-xs font-medium text-paper hover:bg-accent disabled:opacity-40"
          >
            Watch
          </button>
        </div>
        {note && <div className="mt-2 text-xs text-moss">{note}</div>}
      </div>
    </section>
  );
}
