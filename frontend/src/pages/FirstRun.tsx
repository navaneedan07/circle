import { useState } from "react";
import { api } from "../api";
import { usePoll } from "../hooks";

/**
 * First run: the user names ONE folder for Circle to read, then sees a health
 * check. Circle creates nothing inside that folder, and it is the only folder
 * watched -- there are no default or idle folders.
 */
export default function FirstRun({ onDone }: { onDone: () => void }) {
  const [folder, setFolder] = useState("");
  const [step, setStep] = useState<"folder" | "health">("folder");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const { data: health } = usePoll(() => api.health(), 3000);

  const desktop = typeof window !== "undefined" ? window.circleDesktop : undefined;

  const browse = async () => {
    if (!desktop?.chooseFolder) return;
    const chosen = await desktop.chooseFolder();
    if (chosen) setFolder(chosen);
  };

  const start = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.setBootstrap({ import_root: folder.trim(), mark_done: true });
      setStep("health");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const h = health;
  const rows: [string, boolean, string][] = [
    ["AI status", !!h?.health.llm.available, h?.health.llm.available ? "Local, Gemma via Ollama" : "Not available"],
    ["Database", !!h?.health.database.connected, "SQLite, one local file"],
    ["Watcher", !!h?.health.watcher.running, h?.health.watcher.running ? "Reading your folder" : "Waiting for a folder"],
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
              {step === "folder" ? "1 of 2 / Folder" : "2 of 2 / Health"}
            </div>
          </div>
        </header>

        <section className="mt-6 border border-line">
          <h2 className="eyebrow-ink border-b border-line px-5 py-3">
            {step === "folder" ? "Where Circle reads from" : "System check"}
          </h2>

          <div className="px-5 py-5">
            {step === "folder" && (
              <div className="space-y-5">
                <p className="text-sm leading-6 text-muted">
                  Choose the one folder that holds your chat and email exports.
                  Put the exports in it however you like &mdash; WhatsApp,
                  Instagram, Telegram and the rest can all be subfolders inside
                  it. Circle reads the files where they are and never moves,
                  renames or deletes them, and it creates nothing inside.
                </p>

                <div>
                  <label className="eyebrow block">Folder to read</label>
                  <div className="mt-2 flex gap-2">
                    <input
                      value={folder}
                      onChange={(e) => setFolder(e.target.value)}
                      onKeyDown={(e) => e.key === "Enter" && folder.trim() && start()}
                      placeholder="C:\Users\you\Documents\ChatBackups"
                      className="mono min-w-0 flex-1 border border-line bg-paper px-3.5 py-2.5 text-sm text-ink outline-none placeholder:text-muted focus:border-line-strong"
                    />
                    {desktop?.chooseFolder && (
                      <button
                        onClick={browse}
                        className="shrink-0 border border-ink px-4 py-2.5 text-xs font-medium uppercase tracking-[0.16em] text-ink hover:bg-ink hover:text-paper"
                      >
                        Browse
                      </button>
                    )}
                  </div>
                  <p className="mt-2 text-xs leading-5 text-muted">
                    This is the only folder Circle watches. There are no default
                    folders, and nothing is created inside it.
                  </p>
                </div>

                {error && (
                  <div className="border border-accent px-3.5 py-2.5 text-sm text-accent">{error}</div>
                )}

                <button
                  onClick={start}
                  disabled={!folder.trim() || saving}
                  className="w-full bg-ink py-3 text-xs font-medium uppercase tracking-[0.16em] text-paper hover:bg-accent disabled:opacity-40"
                >
                  {saving ? "Preparing" : "Start Circle"}
                </button>
              </div>
            )}

            {step === "health" && (
              <div className="space-y-5">
                <div>
                  {rows.map(([label, ok, detail]) => (
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
