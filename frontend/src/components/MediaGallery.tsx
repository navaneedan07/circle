import { useState } from "react";
import { api, fmtWhen, MediaAttachment } from "../api";

/**
 * Renders real media ingested from an export: photos, voice notes, video,
 * stickers and documents. Files are served from the local backend only.
 */
export default function MediaGallery({ items }: { items?: MediaAttachment[] }) {
  if (!items || items.length === 0) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-3">
      {items.map((m) => (
        <MediaItem key={m.id} item={m} />
      ))}
    </div>
  );
}

/** Small ledger link used for the local-vision actions. */
const LEDGER_LINK =
  "text-[11px] uppercase tracking-[0.16em] text-accent disabled:opacity-50";

function MediaItem({ item }: { item: MediaAttachment }) {
  const [describe, setDescribe] = useState(false);
  const [caption, setCaption] = useState(item.caption ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const url = item.url ?? undefined;

  if (item.kind === "image" || item.kind === "sticker") {
    return (
      <figure className="w-44 border border-line bg-surface">
        <a href={url} target="_blank" rel="noreferrer">
          <img
            src={url}
            alt={item.filename}
            loading="lazy"
            className="h-32 w-full object-cover"
          />
        </a>
        <figcaption className="border-t border-line px-2.5 py-2 text-[11px] text-muted">
          <div className="mono truncate" title={item.filename}>
            {item.filename}
          </div>
          {caption && <div className="mt-1.5 text-xs text-ink">{caption}</div>}
          {!caption && !describe && (
            <button
              onClick={() => setDescribe(true)}
              className={`${LEDGER_LINK} mt-1.5 block`}
            >
              Describe locally
            </button>
          )}
          {describe && !caption && (
            <button
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                setError(null);
                try {
                  const r = await api.describeMedia(item.id);
                  setCaption(r.media.caption ?? "");
                } catch (e) {
                  setError((e as Error).message);
                } finally {
                  setBusy(false);
                }
              }}
              className={`${LEDGER_LINK} mt-1.5 block`}
            >
              {busy ? "Describing" : "Run local vision"}
            </button>
          )}
          {error && <div className="mt-1.5 text-accent">{error}</div>}
        </figcaption>
      </figure>
    );
  }

  if (item.kind === "voice" || item.kind === "audio") {
    return (
      <div className="w-full max-w-md border border-line bg-surface">
        <div className="eyebrow flex items-baseline justify-between gap-3 border-b border-line px-3 py-2">
          <span>Voice note</span>
          <span className="tabular-nums">
            {fmtWhen(item.occurred_at)}
            {item.duration_seconds
              ? ` / ${Math.round(item.duration_seconds)}s`
              : ""}
          </span>
        </div>
        <div className="px-3 py-2.5">
          {url && (
            <audio controls preload="none" src={url} className="w-full" />
          )}
          {item.transcript ? (
            <div className="mt-2 whitespace-pre-wrap text-xs leading-5 text-ink">
              {item.transcript}
            </div>
          ) : (
            <div className="mt-2 text-xs text-muted">No transcript yet.</div>
          )}
        </div>
      </div>
    );
  }

  if (item.kind === "video") {
    return (
      <div className="w-full max-w-md border border-line bg-surface">
        {url && <video controls preload="none" src={url} className="w-full" />}
        <div
          className="mono truncate border-t border-line px-3 py-2 text-[11px] text-muted"
          title={item.filename}
        >
          {item.filename}
        </div>
      </div>
    );
  }

  return (
    <a
      href={url}
      target="_blank"
      rel="noreferrer"
      className="flex min-w-0 max-w-full items-center gap-3 border border-line bg-surface px-3 py-2.5 text-xs hover:border-line-strong"
    >
      <span className="eyebrow shrink-0 text-ink">{item.kind}</span>
      <span className="mono truncate text-ink" title={item.filename}>
        {item.filename}
      </span>
    </a>
  );
}
