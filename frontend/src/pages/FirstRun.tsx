import { useState } from "react";
import { api, CloudDriveInfo, Health } from "../api";
import { usePoll } from "../hooks";

const STEPS = {
  welcome: { n: 1, name: "Welcome", title: "Welcome to Circle" },
  folders: { n: 2, name: "Folders", title: "Where Circle reads from" },
  health: { n: 3, name: "Health", title: "System check" },
} as const;

type Step = keyof typeof STEPS;

/** First-run experience: pick folders to watch, then review system health. */
export default function FirstRun({ onDone }: { onDone: () => void }) {
  const [root, setRoot] = useState("");
  const [extra, setExtra] = useState<string[]>([]);
  const [candidate, setCandidate] = useState("");
  const [step, setStep] = useState<Step>("welcome");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const { data: health } = usePoll(() => api.health(), 3000);
  const { data: boot } = usePoll(() => api.bootstrap(), 60000);

  const start = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.setBootstrap({
        import_root: root.trim(),
        watch_folders: extra,
        mark_done: true,
      });
      setStep("health");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const addCandidate = (path: string) => {
    const clean = path.trim().replace(/^["']|["']$/g, "");
    if (!clean) return;
    if (extra.some((p) => p.toLowerCase() === clean.toLowerCase())) {
      setCandidate("");
      return;
    }
    setExtra([...extra, clean]);
    setCandidate("");
  };

  const h = health as Health | null;
  const drive = boot?.cloud_drive as CloudDriveInfo | undefined;
  const driveRoots = drive?.roots ?? [];
  const meta = STEPS[step];

  const healthRows: [string, boolean, string][] = [
    [
      "AI status",
      !!h?.health.llm.available,
      h?.health.llm.available ? "Local, Gemma via Ollama" : "Not available yet",
    ],
    ["Database", !!h?.health.database.connected, "Connected"],
    ["Watcher", !!h?.health.watcher.running, "Running"],
    ["Model", !!h?.health.llm.available, h?.health.llm.model ?? ""],
    ["Voice STT", !!h?.health.stt.available, h?.health.stt.detail ?? ""],
  ];

  return (
    <div className="flex min-h-screen items-center justify-center bg-paper px-5 py-12">
      <div className="w-full max-w-2xl">
        <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3 border-b border-line pb-4">
          <div>
            <div className="serif text-lg tracking-[0.34em] text-ink">CIRCLE</div>
            <p className="mt-3 text-sm leading-6 text-muted">
              A private record of the people you actually talk to.
            </p>
          </div>
          <div className="text-right">
            <div className="eyebrow">Setup</div>
            <div className="eyebrow-ink mt-1.5">
              {meta.n} of 3 / {meta.name}
            </div>
          </div>
        </header>

        <section className="mt-6 border border-line">
          <h2 className="eyebrow-ink border-b border-line px-5 py-3">
            {meta.title}
          </h2>

          <div className="px-5 py-5">
            {step === "welcome" && (
              <div className="space-y-5">
                <p className="text-sm leading-6 text-muted">
                  Circle keeps everything on this computer. The model runs
                  locally, so no conversation data leaves this machine, and no
                  social account password is ever requested. You provide
                  exports, and Circle reads them.
                </p>
                <div>
                  <label className="eyebrow block">Circle folder</label>
                  <input
                    value={root}
                    onChange={(e) => setRoot(e.target.value)}
                    placeholder="Leave empty to use the default"
                    className="mono mt-2 w-full border border-line bg-paper px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-muted focus:border-line-strong"
                  />
                  <p className="mt-2 text-xs leading-5 text-muted">
                    Circle&apos;s own working folder. Subfolders such as
                    whatsapp/, telegram/, voice/ and calendar/ are created here
                    automatically.
                  </p>
                </div>
                <button
                  onClick={() => setStep("folders")}
                  className="w-full bg-ink py-3 text-xs font-medium uppercase tracking-[0.16em] text-paper hover:bg-accent"
                >
                  Choose what to watch
                </button>
              </div>
            )}

            {step === "folders" && (
              <div className="space-y-6">
                <p className="text-sm leading-6 text-muted">
                  If your WhatsApp and Instagram backups already live in Google
                  Drive, sync that folder with Google Drive for Desktop and give
                  Circle the local path. Circle does not sign in to Drive and
                  holds no Google account credentials.
                </p>

                <div>
                  <div className="flex items-baseline justify-between gap-4 border-y border-line py-2.5">
                    <span className="text-sm text-ink">
                      Google Drive for Desktop
                    </span>
                    <span
                      className={`text-[11px] uppercase tracking-[0.16em] ${
                        drive?.installed ? "text-moss" : "text-ochre"
                      }`}
                    >
                      {drive?.installed ? "detected" : "not detected"}
                    </span>
                  </div>
                  {!drive?.installed && (
                    <a
                      href="https://www.google.com/drive/download/"
                      target="_blank"
                      rel="noreferrer"
                      className="mt-2 inline-block text-xs text-muted underline underline-offset-2 hover:text-ink"
                    >
                      Install it from Google, then sign in there, not here.
                    </a>
                  )}
                </div>

                {driveRoots.length > 0 && (
                  <div>
                    <label className="eyebrow block">Detected Drive folders</label>
                    <ul className="mt-2 border border-line">
                      {driveRoots.map((r) => (
                        <li
                          key={r}
                          className="border-b border-line last:border-b-0"
                        >
                          <button
                            onClick={() => addCandidate(r)}
                            disabled={extra.some(
                              (p) => p.toLowerCase() === r.toLowerCase()
                            )}
                            className="mono block w-full truncate px-3.5 py-2.5 text-left text-xs text-ink hover:bg-surface disabled:opacity-40"
                            title={`Watch ${r}`}
                          >
                            {r}
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <div>
                  <label className="eyebrow block">Any other folder</label>
                  <div className="mt-2 flex gap-2">
                    <input
                      value={candidate}
                      onChange={(e) => setCandidate(e.target.value)}
                      onKeyDown={(e) =>
                        e.key === "Enter" && addCandidate(candidate)
                      }
                      placeholder="G:\My Drive\Chat Backups"
                      className="mono min-w-0 flex-1 border border-line bg-paper px-3 py-2 text-xs text-ink outline-none placeholder:text-muted focus:border-line-strong"
                    />
                    <button
                      onClick={() => addCandidate(candidate)}
                      disabled={!candidate.trim()}
                      className="shrink-0 border border-ink px-4 py-2 text-xs font-medium text-ink hover:bg-ink hover:text-paper disabled:opacity-40"
                    >
                      Add
                    </button>
                  </div>
                </div>

                {extra.length > 0 && (
                  <div>
                    <label className="eyebrow block">Watched in addition</label>
                    <ul className="mt-2 border border-line">
                      {extra.map((p) => (
                        <li
                          key={p}
                          className="flex items-center justify-between gap-3 border-b border-line px-3.5 py-2.5 last:border-b-0"
                        >
                          <span className="mono truncate text-xs text-ink">
                            {p}
                          </span>
                          <button
                            onClick={() =>
                              setExtra(extra.filter((x) => x !== p))
                            }
                            className="text-[11px] uppercase tracking-[0.16em] text-muted hover:text-accent"
                          >
                            Remove
                          </button>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <p className="text-xs leading-5 text-muted">
                  Files in these folders are read where they lie. Circle never
                  deletes or reorganizes them, so a backup in Drive stays
                  exactly where you put it.
                </p>

                {error && (
                  <div className="border border-accent px-3.5 py-2.5 text-sm text-accent">
                    {error}
                  </div>
                )}

                <div className="flex gap-2 border-t border-line pt-5">
                  <button
                    onClick={() => setStep("welcome")}
                    className="shrink-0 border border-ink px-4 py-3 text-xs font-medium uppercase tracking-[0.16em] text-ink hover:bg-ink hover:text-paper"
                  >
                    Back
                  </button>
                  <button
                    onClick={start}
                    disabled={saving}
                    className="flex-1 bg-ink py-3 text-xs font-medium uppercase tracking-[0.16em] text-paper hover:bg-accent disabled:opacity-50"
                  >
                    {saving ? "Preparing" : "Start Circle"}
                  </button>
                </div>
              </div>
            )}

            {step === "health" && (
              <div className="space-y-5">
                <div>
                  {healthRows.map(([label, ok, detail]) => (
                    <div
                      key={label}
                      className="flex items-baseline justify-between gap-4 border-b border-line py-2.5 last:border-b-0"
                    >
                      <span className="text-sm text-ink">{label}</span>
                      <span className="flex items-baseline gap-3">
                        <span className="text-xs text-muted">{detail}</span>
                        <span
                          className={`text-[11px] uppercase tracking-[0.16em] ${
                            ok ? "text-moss" : "text-ochre"
                          }`}
                        >
                          {ok ? "ok" : "pending"}
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
                <button
                  onClick={onDone}
                  className="w-full bg-ink py-3 text-xs font-medium uppercase tracking-[0.16em] text-paper hover:bg-accent"
                >
                  Open Circle
                </button>
              </div>
            )}
          </div>
        </section>

        <p className="mt-4 text-center text-xs text-muted">
          Local first, offline capable, exports only. No scraping, no passwords.
        </p>
      </div>
    </div>
  );
}
