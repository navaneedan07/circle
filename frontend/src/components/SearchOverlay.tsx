/* Global archive search.

   The backend has had a hybrid search endpoint (/api/search) since early on and
   nothing in the UI called it, so 120k messages were reachable only through
   natural-language questions. This is that endpoint, surfaced as a palette:
   Ctrl/Cmd+K anywhere, or "/" when not already typing in a field.
*/
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, fmtDate, SearchResult } from "../api";

const SOURCE_LABELS: Record<string, string> = {
  whatsapp: "WhatsApp",
  telegram: "Telegram",
  instagram: "Instagram",
  x: "X",
  email: "Email",
  calendar: "Calendar",
  notes: "Note",
  voice: "Voice",
  chat: "Chat",
  document: "Document",
  contacts: "Contacts",
};

export default function SearchOverlay({
  open,
  onClose,
  personId,
  personName = "",
}: {
  open: boolean;
  onClose: () => void;
  /** When set, search is scoped to one person. */
  personId?: string;
  personName?: string;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const navigate = useNavigate();

  // Reset per invocation so a previous query never greets the user.
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setResults([]);
    setError(null);
    setActive(0);
    setBusy(false);
    const t = window.setTimeout(() => inputRef.current?.focus(), 30);
    return () => window.clearTimeout(t);
  }, [open]);

  // Debounced so a keystroke does not fire an embedding round-trip per letter.
  useEffect(() => {
    const text = query.trim();
    if (!open || text.length < 2) {
      setResults([]);
      setBusy(false);
      return;
    }
    let cancelled = false;
    setBusy(true);
    const t = window.setTimeout(() => {
      api
        .search(text, {
          ...(personId ? { person_id: personId } : {}),
          limit: 12,
        })
        .then((r) => {
          if (cancelled) return;
          setResults(r.results ?? []);
          setActive(0);
          setError(null);
        })
        .catch((e: Error) => {
          if (!cancelled) setError(e.message);
        })
        .finally(() => {
          if (!cancelled) setBusy(false);
        });
    }, 220);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [query, open, personId]);

  const open_ = useCallback(
    (r: SearchResult) => {
      onClose();
      if (r.person_id) navigate(`/person/${r.person_id}`);
    },
    [navigate, onClose]
  );

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
      return;
    }
    if (e.key === "ArrowDown" || (e.key === "n" && e.ctrlKey)) {
      e.preventDefault();
      setActive((i) => Math.min(i + 1, results.length - 1));
      return;
    }
    if (e.key === "ArrowUp" || (e.key === "p" && e.ctrlKey)) {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
      return;
    }
    if (e.key === "Enter" && results[active]) {
      e.preventDefault();
      open_(results[active]);
    }
  };

  // Keep the highlighted row in view during keyboard navigation.
  useEffect(() => {
    const el = listRef.current?.children[active] as HTMLElement | undefined;
    el?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const hint = useMemo(
    () =>
      personId
        ? `Scoped to ${personName || "this person"}`
        : "Searching every imported message",
    [personId, personName]
  );

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/70 px-3 pt-[8vh] sm:px-4 sm:pt-[12vh]"
      onClick={onClose}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Search the archive"
        className="flex max-h-[70vh] w-full max-w-2xl flex-col border border-line-strong bg-paper"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={onKeyDown}
      >
        <div className="flex items-stretch border-b border-line">
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={
              personId
                ? `Search ${personName || "this person"}'s messages`
                : "Search every message"
            }
            aria-label="Search the archive"
            className="min-w-0 flex-1 bg-transparent px-4 py-3.5 text-sm text-ink outline-none placeholder:text-muted"
          />
          <button
            onClick={onClose}
            className="eyebrow shrink-0 border-l border-line px-4 text-muted hover:text-ink sm:pointer-events-none"
          >
            {busy ? "Searching" : "Close"}
          </button>
        </div>

        <div className="flex items-baseline justify-between gap-4 border-b border-line px-4 py-2">
          <span className="eyebrow min-w-0 truncate">{hint}</span>
          <span className="eyebrow shrink-0 tabular-nums">
            {query.trim().length < 2
              ? "Type 2 or more characters"
              : `${results.length} found`}
          </span>
        </div>

        {error && (
          <div className="px-4 py-3 text-sm text-accent">
            Search failed: {error}
          </div>
        )}

        <ul ref={listRef} className="min-h-0 flex-1 overflow-auto">
          {query.trim().length >= 2 && !busy && !error && results.length === 0 && (
            <li className="px-4 py-10 text-center text-sm text-muted">
              Nothing matched. Try a word that appears in the message itself,
              such as a project name or a place.
            </li>
          )}

          {results.map((r, i) => (
            <li key={r.id} className="border-b border-line last:border-b-0">
              <button
                onMouseEnter={() => setActive(i)}
                onClick={() => open_(r)}
                className={`w-full px-4 py-3 text-left ${
                  i === active ? "bg-surface" : ""
                }`}
              >
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="eyebrow-ink">
                    {SOURCE_LABELS[r.source] ?? r.source}
                  </span>
                  <span className="eyebrow">{r.kind}</span>
                  <span className="eyebrow">{fmtDate(r.occurred_at)}</span>
                  {r.person_id && (
                    <span className="eyebrow text-accent">person</span>
                  )}
                </div>
                <div className="mt-1.5 line-clamp-2 text-sm leading-6 text-ink">
                  {r.snippet || r.text}
                </div>
              </button>
            </li>
          ))}
        </ul>

        <div className="hidden flex-wrap items-baseline justify-between gap-4 border-t border-line px-4 py-2.5 sm:flex">
          <span className="eyebrow">Up and down to move, Enter to open</span>
          <span className="eyebrow">Hybrid keyword and meaning search</span>
        </div>
      </div>
    </div>
  );
}