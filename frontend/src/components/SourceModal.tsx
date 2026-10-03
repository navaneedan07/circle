import { useEffect, useRef, useState } from "react";
import { api, fmtDate, MediaAttachment } from "../api";
import MediaGallery from "./MediaGallery";

/** Shows the original normalized record behind a cited source. */
export default function SourceModal({
  memoryId,
  onClose,
}: {
  memoryId: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<{
    memory: Record<string, unknown>;
    record: Record<string, unknown>;
    media: MediaAttachment[];
    person_name: string | null;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    api
      .memory(memoryId)
      .then(setData)
      .catch((e: Error) => setError(e.message));
  }, [memoryId]);

  // Escape closes, and focus lands inside the dialog so a keyboard user is
  // not left tabbing through the page behind it.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    closeRef.current?.focus();
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const memory = data?.memory ?? {};
  const facts: [string, string][] = data
    ? [
        ["Kind", String(memory.kind ?? "unknown")],
        ["Source", String(memory.source ?? "unknown")],
        ["Origin", String(memory.origin ?? "unknown")],
        ["When", fmtDate((memory.occurred_at as string) ?? null)],
      ]
    : [];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"
      onClick={onClose}
      role="presentation"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Evidence record"
        className="max-h-[85vh] w-full max-w-2xl overflow-auto border border-line-strong bg-paper"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 flex items-start justify-between gap-4 border-b border-line bg-paper px-5 py-3.5">
          <div>
            <div className="eyebrow-ink">Evidence record</div>
            {data?.person_name && (
              <div className="mt-1 text-xs text-muted">
                Person: {data.person_name}
              </div>
            )}
          </div>
          <button
            ref={closeRef}
            onClick={onClose}
            className="eyebrow text-muted hover:text-ink"
          >
            Close
          </button>
        </div>

        {error && <div className="px-5 py-5 text-sm text-accent">{error}</div>}
        {!data && !error && (
          <div className="px-5 py-10 text-center text-sm text-muted">
            Loading record
          </div>
        )}

        {data && (
          <div>
            <dl className="grid grid-cols-2 gap-px bg-line sm:grid-cols-4">
              {facts.map(([label, value]) => (
                <div key={label} className="bg-paper px-4 py-3">
                  <dt className="eyebrow">{label}</dt>
                  <dd
                    className="mono mt-1.5 truncate text-xs text-ink"
                    title={value}
                  >
                    {value}
                  </dd>
                </div>
              ))}
            </dl>

            <div className="px-5 py-4">
              <div className="whitespace-pre-wrap break-words text-sm leading-6 text-ink">
                {String(memory.text ?? "")}
              </div>
            </div>

            {data.media.length > 0 && (
              <div className="border-t border-line px-5 py-4">
                <div className="eyebrow mb-3">Attached media</div>
                <MediaGallery items={data.media} />
              </div>
            )}

            {Object.keys(data.record || {}).length > 0 && (
              <div className="border-t border-line">
                <details>
                  <summary className="eyebrow cursor-pointer px-5 py-3 hover:text-ink">
                    Full normalized record
                  </summary>
                  <pre className="mono max-h-64 overflow-auto whitespace-pre-wrap break-words border-t border-line px-5 py-3 text-[11px] text-muted">
                    {JSON.stringify(data.record, null, 2)}
                  </pre>
                </details>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
