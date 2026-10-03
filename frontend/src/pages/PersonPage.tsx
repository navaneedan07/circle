import { ReactNode, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  apiUrl,
  fmtDate,
  getConnection,
  fmtWhen,
  Person,
  Profile,
  STATUS_DOTS,
  TimelineEntry,
} from "../api";
import { usePoll } from "../hooks";
import AskPanel from "../components/AskPanel";
import SourceModal from "../components/SourceModal";
import MediaGallery from "../components/MediaGallery";

const TABS = ["Timeline", "Voice", "Notes"] as const;
type Tab = (typeof TABS)[number];

function memoryIdFor(entry: TimelineEntry): string | null {
  // memory ids mirror record ids: mem-<recordId>
  return entry.id ? `mem-${entry.id}` : null;
}

/** A rail panel: a ruled header over a body, matching the dashboard ledger. */
function RailPanel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border border-line">
      <h2 className="eyebrow border-b border-line px-4 py-2.5">{title}</h2>
      {children}
    </section>
  );
}

export default function PersonPage({ personName = "" }: { personName?: string }) {
  const { id = "" } = useParams();
  const [tab, setTab] = useState<Tab>("Timeline");
  const [entryType, setEntryType] = useState<string>("");
  const [seed, setSeed] = useState<{ text: string; at: number } | undefined>();
  const [showNote, setShowNote] = useState(false);
  const [noteBody, setNoteBody] = useState("");
  const [noteSaved, setNoteSaved] = useState(false);
  const [sourceModal, setSourceModal] = useState<string | null>(null);
  const [preparing, setPreparing] = useState(false);

  const { data } = usePoll(() => api.person(id), 6000, [id]);
  const { data: timeline, refresh: refreshTimeline } = usePoll(
    () => api.timeline(id),
    6000,
    [id]
  );
  const { data: suggestions } = usePoll(() => api.suggestions(id), 30000, [id]);
  const { data: voice } = usePoll(() => api.voice(id), 8000, [id]);

  if (!data) {
    return (
      <div className="py-20 text-center text-sm text-muted">Loading person</div>
    );
  }

  const person: Person = data.person;
  const profile: Profile | null = data.profile;
  const allEntries = timeline?.timeline ?? [];
  const kinds = Array.from(new Set(allEntries.map((e) => e.type))).sort();
  const entries = entryType
    ? allEntries.filter((e) => e.type === entryType)
    : allEntries;
  const dot = STATUS_DOTS[profile?.status ?? ""] ?? "bg-muted";
  const sources = Object.entries(profile?.source_breakdown ?? {});
  const facts: [string, string][] = [
    ["Interactions", String(profile?.interaction_count ?? 0)],
    ["Last 14 days", String(profile?.interactions_14d ?? 0)],
    ["Last seen", fmtWhen(profile?.last_interaction_at)],
    ["Sources", String(sources.length)],
  ];
  const hasRail =
    sources.length > 0 ||
    (!!profile &&
      (profile.topics.length > 0 ||
        profile.active_topics.length > 0 ||
        profile.upcoming_events.length > 0));

  const saveNote = async () => {
    if (!noteBody.trim()) return;
    await api.createNote({ person_id: id, title: "", body: noteBody.trim() });
    setNoteBody("");
    setShowNote(false);
    setNoteSaved(true);
    refreshTimeline();
    setTimeout(() => setNoteSaved(false), 3000);
  };

  const prepare = () => {
    setPreparing(true);
    setSeed({
      text: "Prepare me for my next meeting with this person",
      at: Date.now(),
    });
    setTimeout(() => setPreparing(false), 1200);
  };

  return (
    <div className="space-y-10">
      <div>
        <Link
          to="/"
          className="eyebrow inline-flex min-h-6 items-center border-b border-transparent text-muted hover:border-ink hover:text-ink"
        >
          All people
        </Link>
      </div>

      <header className="flex flex-wrap items-start justify-between gap-x-8 gap-y-5">
        <div className="flex items-start gap-4">
          <div className="serif flex h-16 w-16 shrink-0 items-center justify-center border border-line-strong text-2xl text-ink">
            {person.display_name.slice(0, 1).toUpperCase()}
          </div>
          <div className="min-w-0">
            <div className="eyebrow">Person record</div>
            <h1 className="serif mt-2 break-words text-4xl leading-none text-ink">
              {person.display_name}
            </h1>
            <div className="mt-3 flex flex-wrap items-center gap-2 text-sm text-muted">
              <span className={`h-2 w-2 ${dot}`} aria-hidden="true" />
              <span>{profile?.status ?? "No data yet"}</span>
              {profile?.status_reason && (
                <span className="text-xs">/ {profile.status_reason}</span>
              )}
            </div>
          </div>
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => setShowNote((v) => !v)}
            className="border border-ink px-4 py-2 text-xs font-medium text-ink hover:bg-ink hover:text-paper"
          >
            Add note
          </button>
          <button
            onClick={prepare}
            disabled={preparing}
            className="bg-ink px-4 py-2 text-xs font-medium text-paper hover:bg-accent disabled:opacity-50"
          >
            {preparing ? "Preparing" : "Prepare me"}
          </button>
        </div>
      </header>

      <dl className="grid grid-cols-2 gap-px border border-line bg-line sm:grid-cols-4">
        {facts.map(([label, value]) => (
          <div key={label} className="bg-paper px-4 py-3">
            <dt className="eyebrow">{label}</dt>
            <dd className="mono mt-2 text-lg leading-none tabular-nums text-ink">
              {value}
            </dd>
          </div>
        ))}
      </dl>

      <div
        className={`grid gap-10 ${hasRail ? "lg:grid-cols-[minmax(0,1fr)_20rem]" : ""}`}
      >
        <div className="min-w-0 space-y-8">
          <AskPanel
            personId={id}
            title={`Ask about ${person.display_name}`}
            placeholder="What did we talk about recently?"
            seed={seed}
          />

          {suggestions && suggestions.prompts.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {suggestions.prompts.map((p) => (
                <button
                  key={p}
                  onClick={() => setSeed({ text: p, at: Date.now() })}
                  className="border border-line px-3 py-1.5 text-xs text-muted hover:border-line-strong hover:text-ink"
                >
                  {p}
                </button>
              ))}
            </div>
          )}

          {showNote && (
            <div className="border border-line bg-surface p-4">
              <textarea
                value={noteBody}
                onChange={(e) => setNoteBody(e.target.value)}
                rows={3}
                autoFocus
                placeholder={`Note about ${person.display_name}`}
                className="w-full resize-none border border-line bg-paper p-3 text-sm text-ink outline-none placeholder:text-muted focus:border-line-strong"
              />
              <div className="mt-2 flex justify-end gap-2">
                <button
                  onClick={() => setShowNote(false)}
                  className="border border-line px-3 py-1.5 text-xs text-muted hover:text-ink"
                >
                  Cancel
                </button>
                <button
                  onClick={saveNote}
                  disabled={!noteBody.trim()}
                  className="bg-ink px-4 py-1.5 text-xs font-medium text-paper hover:bg-accent disabled:opacity-40"
                >
                  Save note
                </button>
              </div>
            </div>
          )}
          {noteSaved && (
            <div className="text-xs text-moss">Note saved and indexed.</div>
          )}

          <section>
            <div className="flex gap-6 border-b border-line">
              {TABS.map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`eyebrow inline-flex min-h-7 items-center border-b-2 ${
                    tab === t
                      ? "border-ink text-ink"
                      : "border-transparent text-muted hover:text-ink"
                  }`}
                >
                  {t}
                </button>
              ))}
            </div>

            <div className="pt-6">
              {tab === "Timeline" && (
                <div className="space-y-4">
                  {kinds.length > 1 && (
                    <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
                      <span className="eyebrow">Show</span>
                      <button
                        onClick={() => setEntryType("")}
                        aria-pressed={!entryType}
                        className={`eyebrow inline-flex min-h-6 items-center border-b ${!entryType ? "border-ink text-ink" : "border-transparent text-muted hover:text-ink"}`}
                      >
                        Everything
                      </button>
                      {kinds.map((k) => {
                        const n = allEntries.filter((e) => e.type === k).length;
                        return (
                          <button
                            key={k}
                            onClick={() => setEntryType(entryType === k ? "" : k)}
                            aria-pressed={entryType === k}
                            className={`eyebrow inline-flex min-h-6 items-center gap-1.5 border-b ${entryType === k ? "border-ink text-ink" : "border-transparent text-muted hover:text-ink"}`}
                          >
                            {k}
                            <span className="tabular-nums text-muted">{n}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}

                  <div className="border border-line">
                  {entries.length === 0 && (
                    <div className="px-6 py-14 text-center text-sm text-muted">
                      {allEntries.length === 0
                        ? "No interactions recorded yet. Import data for this person."
                        : `No ${entryType} entries for this person.`}
                    </div>
                  )}
                  {entries.map((e, i) => {
                    const mid = memoryIdFor(e);
                    return (
                      <div
                        key={`${e.id}-${i}`}
                        className="flex flex-col gap-1 border-b border-line px-4 py-4 last:border-b-0 sm:flex-row sm:gap-4"
                      >
                        <div className="eyebrow shrink-0 pt-1 sm:w-20">
                          {e.type}
                        </div>
                        <div className="min-w-0 flex-1 overflow-hidden">
                          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                            <span className="text-sm font-medium text-ink">
                              {e.title || e.type}
                            </span>
                            <span className="eyebrow">{fmtDate(e.at)}</span>
                            <span className="eyebrow">{e.origin}</span>
                            {mid && (
                              <button
                                onClick={() => setSourceModal(mid)}
                                className="eyebrow border-b border-transparent text-accent hover:border-accent"
                              >
                                source
                              </button>
                            )}
                          </div>
                          {e.body && (
                            <div className="mt-1.5 whitespace-pre-wrap break-words text-sm leading-6 text-ink">
                              {e.body}
                            </div>
                          )}
                          <MediaGallery items={e.media} />
                        </div>
                      </div>
                    );
                  })}
                  </div>

                  {/* The API caps the timeline; say so rather than let the
                      list silently look like the whole history. */}
                  {allEntries.length >= 150 && (
                    <p className="text-xs text-muted">
                      Showing the {allEntries.length} most recent entries. Narrow
                      it with the filters above, or search the archive with
                      Ctrl K.
                    </p>
                  )}
                </div>
              )}

              {tab === "Voice" && (
                <div className="grid gap-3 sm:grid-cols-2">
                  {(voice?.recordings ?? []).length === 0 && (
                    <div className="border border-line px-6 py-12 text-center text-sm text-muted sm:col-span-2">
                      No voice memories yet. Drop recordings into{" "}
                      <code className="mono text-ink">imports/voice/</code> and
                      Circle transcribes them locally.
                    </div>
                  )}
                  {(voice?.recordings ?? []).map((r) => {
                    const rec = r as unknown as Record<string, string>;
                    return (
                      <div
                        key={rec.id}
                        className="border border-line bg-surface p-4"
                      >
                        <div className="eyebrow flex items-baseline justify-between gap-3">
                          <span className="truncate">{rec.filename}</span>
                          <span>{rec.processing_status}</span>
                        </div>
                        <div className="mt-2 line-clamp-3 text-sm text-ink">
                          {rec.transcript || rec.error || "No transcript yet"}
                        </div>
                        <div className="eyebrow mt-2">
                          {fmtDate(rec.recorded_at)}
                        </div>
                        <audio
                          controls
                          preload="none"
                          src={
                            apiUrl(`/api/voice/${rec.id}/audio`) +
                            (getConnection().accessKey
                              ? `?key=${encodeURIComponent(
                                  getConnection().accessKey
                                )}`
                              : "")
                          }
                          className="mt-2 w-full"
                        />
                      </div>
                    );
                  })}
                </div>
              )}

              {tab === "Notes" && <NotesTab personId={id} />}
            </div>
          </section>
        </div>

        {hasRail && (
        <aside className="min-w-0 space-y-6 self-start lg:sticky lg:top-8">
          {sources.length > 0 && (
            <RailPanel title="Where it comes from">
              <ul>
                {sources.map(([src, n]) => (
                  <li
                    key={src}
                    className="flex items-baseline justify-between gap-3 border-b border-line px-4 py-2.5 last:border-b-0"
                  >
                    <span className="text-sm text-ink">{src}</span>
                    <span className="mono text-[11px] tabular-nums text-muted">
                      {n}
                    </span>
                  </li>
                ))}
              </ul>
            </RailPanel>
          )}

          {profile && (
            <RailPanel title="Topics">
              <div className="space-y-5 px-4 py-3">
                <div>
                  <div className="eyebrow mb-2">Most discussed</div>
                  <div className="flex flex-wrap gap-2">
                    {profile.topics.length === 0 ? (
                      <span className="text-xs text-muted">
                        No topics extracted yet.
                      </span>
                    ) : (
                      profile.topics.map((t) => (
                        <span
                          key={t.topic}
                          className="border border-line px-2.5 py-1 text-xs text-ink"
                        >
                          {t.topic} / {t.count}
                        </span>
                      ))
                    )}
                  </div>
                </div>
                <div>
                  <div className="eyebrow mb-2">Active, 14 days</div>
                  <div className="flex flex-wrap gap-2">
                    {profile.active_topics.length === 0 ? (
                      <span className="text-xs text-muted">
                        Nothing active in the last 14 days.
                      </span>
                    ) : (
                      profile.active_topics.map((t) => (
                        <span
                          key={t.topic}
                          className="border border-line-strong px-2.5 py-1 text-xs text-ink"
                        >
                          {t.topic}
                        </span>
                      ))
                    )}
                  </div>
                </div>
              </div>
            </RailPanel>
          )}

          {profile && profile.upcoming_events.length > 0 && (
            <RailPanel title="Upcoming">
              <div className="space-y-3 px-4 py-3">
                {profile.upcoming_events.map((ev) => (
                  <div key={ev.id}>
                    <div className="text-sm text-ink">{ev.title}</div>
                    <div className="eyebrow mt-1">
                      {fmtDate(ev.starts_at)}
                      {ev.location ? ` / ${ev.location}` : ""}
                    </div>
                  </div>
                ))}
              </div>
            </RailPanel>
          )}
        </aside>
        )}
      </div>

      {sourceModal && (
        <SourceModal memoryId={sourceModal} onClose={() => setSourceModal(null)} />
      )}
    </div>
  );
}

function NotesTab({ personId }: { personId: string }) {
  const { data } = usePoll(() => api.notes(personId), 8000, [personId]);
  const notes = (data?.notes ?? []) as unknown as Record<string, string>[];
  if (notes.length === 0) {
    return (
      <div className="border border-line px-6 py-12 text-center text-sm text-muted">
        No notes yet. Notes you write here stay local and become searchable
        evidence.
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {notes.map((n) => (
        <div key={n.id} className="border border-line bg-surface p-4">
          <div className="eyebrow flex items-baseline justify-between gap-3">
            <span className="truncate text-ink">{n.title || "Note"}</span>
            <span>{fmtDate(n.noted_at)}</span>
          </div>
          <div className="mt-2 whitespace-pre-wrap text-sm text-ink">{n.body}</div>
        </div>
      ))}
    </div>
  );
}
