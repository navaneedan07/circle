import { Link, NavLink } from "react-router-dom";
import { ReactNode, useEffect, useState } from "react";
import { useEvents, usePoll, useTheme } from "../hooks";
import { api, fmtWhen, SyncStatus } from "../api";
import SearchOverlay from "./SearchOverlay";

export default function Layout({
  children,
  searchScope,
}: {
  children: ReactNode;
  /** When set, the search palette is limited to this person. */
  searchScope?: { personId: string; personName: string };
}) {
  const [, toggleTheme] = useTheme();
  const [flash, setFlash] = useState<string | null>(null);
  const [lastSeen, setLastSeen] = useState<number>(0);
  const [searching, setSearching] = useState(false);
  const { data: sync, refresh } = usePoll<SyncStatus>(() => api.syncStatus(), 8000);

  // Ctrl/Cmd+K opens search from anywhere; "/" does too, but not while the
  // user is already typing into a field.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setSearching(false);
        return;
      }
      const typing = ["INPUT", "TEXTAREA", "SELECT"].includes(
        (e.target as HTMLElement)?.tagName ?? ""
      );
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSearching(true);
      } else if (e.key === "/" && !typing) {
        e.preventDefault();
        setSearching(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const { connected, lastEvent } = useEvents((type, payload) => {
    if (type === "job") {
      const s = payload.status;
      const f = payload.filename || "";
      setFlash(
        s === "PROCESSING"
          ? `Importing ${f}`
          : s === "COMPLETED"
            ? `Imported ${f}, ${payload.records ?? 0} records`
            : s === "SKIPPED"
              ? `Skipped ${f}, already processed`
              : s === "FAILED"
                ? `Failed to import ${f}`
                : `Quarantined ${f}`
      );
      setTimeout(() => setFlash(null), 4200);
    } else if (type === "person") {
      setFlash(`Updated ${payload.name || "person"}, +${payload.recent14 ?? ""} recent interactions`);
      setTimeout(() => setFlash(null), 4200);
    } else if (
      type === "sync" ||
      type === "contacts" ||
      type === "voice" ||
      type === "demo" ||
      type === "note"
    ) {
      refresh();
    }
  });

  if (lastEvent && lastEvent.at !== lastSeen && lastEvent.type !== "job") {
    setLastSeen(lastEvent.at);
    refresh();
  }

  const active = sync?.watcher_running;
  const processing = (sync?.jobs?.PROCESSING ?? 0) + (sync?.jobs?.QUEUED ?? 0) > 0;
  const navBase = "eyebrow inline-flex min-h-6 items-center";
  const navClass = ({ isActive }: { isActive: boolean }) =>
    `${navBase} ${isActive ? "text-ink" : "text-muted hover:text-ink"}`;

  return (
    <div className="flex min-h-screen flex-col bg-paper">
      {/* First tab stop: lets a keyboard user reach the content directly. */}
      <a
        href="#content"
        className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:border focus:border-ink focus:bg-paper focus:px-4 focus:py-2 focus:text-sm focus:text-ink"
      >
        Skip to content
      </a>
      <header className="border-b border-line">
        <div className="mx-auto flex w-full max-w-[1440px] flex-wrap items-center justify-between gap-x-8 gap-y-3 px-5 py-5 sm:px-8">
          <Link to="/" className="flex items-baseline gap-3">
            <span className="serif text-lg tracking-[0.34em] text-ink">CIRCLE</span>
            <span className="eyebrow hidden pb-0.5 sm:inline">
              a private record
            </span>
          </Link>
          <nav className="flex flex-wrap items-center gap-x-4 gap-y-2 sm:gap-x-8">
            <NavLink to="/" end className={navClass}>
              People
            </NavLink>
            <NavLink to="/imports" className={navClass}>
              Imports
            </NavLink>
            <NavLink to="/settings" className={navClass}>
              Settings
            </NavLink>
            <span className="h-3 w-px bg-line-strong" aria-hidden="true" />
            <button
              onClick={() => setSearching(true)}
              aria-label="Search the archive (Ctrl K)"
              className="eyebrow inline-flex min-h-6 items-center gap-2 border border-line px-2.5 py-1 text-muted hover:border-line-strong hover:text-ink"
            >
              Search
              <kbd className="mono hidden text-[10px] text-muted sm:inline">
                Ctrl K
              </kbd>
            </button>
            <button
              onClick={toggleTheme}
              className="eyebrow inline-flex min-h-6 items-center text-muted hover:text-ink"
            >
              Theme
            </button>
          </nav>
        </div>
      </header>

      {/* Folio line: the running status of the archive, spread across the width. */}
      <div className="border-b border-line bg-surface">
        <div className="mx-auto flex w-full max-w-[1440px] flex-wrap items-baseline justify-between gap-x-8 gap-y-1 px-5 py-2 sm:px-8">
          <span className="eyebrow">
            {processing ? "Syncing" : active ? "Watcher running" : "Watcher stopped"}
          </span>
          <span className="eyebrow min-w-0 break-words">
            {sync
              ? `${sync.counts.people} people / ${sync.counts.messages} messages / ${sync.counts.memories} indexed`
              : "Reading local store"}
          </span>
          <span className="eyebrow min-w-0 break-words">
            {connected ? "Live updates on" : "Live updates reconnecting"}
            {sync?.last_event_at
              ? `, last ${fmtWhen(new Date(sync.last_event_at * 1000).toISOString())}`
              : ""}
          </span>
        </div>
      </div>

      {flash && (
        <div className="mx-auto w-full max-w-[1440px] px-5 pt-5 sm:px-8">
          <div
            role="status"
            aria-live="polite"
            className="border border-line-strong bg-surface px-3 py-2 text-sm text-ink"
          >
            {flash}
          </div>
        </div>
      )}

      <main
        id="content"
        className="mx-auto w-full max-w-[1440px] flex-1 px-5 py-10 sm:px-8"
      >
        {children}
      </main>

      <footer className="border-t border-line">
        <div className="mx-auto flex w-full max-w-[1440px] flex-wrap items-baseline justify-between gap-x-8 gap-y-1 px-5 py-5 sm:px-8">
          <span className="mono text-[11px] leading-5 text-muted">
            Circle runs on this machine. It reads exports you place in the watched
            folder and never signs in to any social account.
          </span>
          <span className="eyebrow">Local only</span>
        </div>
      </footer>

      <SearchOverlay
        open={searching}
        onClose={() => setSearching(false)}
        personId={searchScope?.personId}
        personName={searchScope?.personName}
      />
    </div>
  );
}
