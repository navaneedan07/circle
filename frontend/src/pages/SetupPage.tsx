import { useEffect, useRef, useState } from "react";
import { api, apiUrl, SetupProgress, SetupStatus } from "../api";

/**
 * Prerequisite gate.
 *
 * Circle cannot answer anything without a local model, so the app is not
 * usable until Ollama and both models are present. This screen checks, then
 * installs on one click: it downloads Ollama, starts it, and pulls
 * `gemma3:4b` and `nomic-embed-text`. Nothing here sends any user data
 * anywhere; it only fetches public installer and model artifacts.
 */
export default function SetupPage({ onReady }: { onReady: () => void }) {
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [progress, setProgress] = useState<SetupProgress | null>(null);
  const [installing, setInstalling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const logRef = useRef<string[]>([]);
  const [, forceRender] = useState(0);

  const refresh = () => {
    api
      .setupStatus()
      .then(setStatus)
      .catch((e) => setError((e as Error).message));
  };

  useEffect(refresh, []);

  useEffect(() => {
    const source = new EventSource(apiUrl("/api/setup/events"));
    source.onmessage = (event) => {
      try {
        const parsed = JSON.parse(event.data) as SetupProgress;
        setProgress(parsed);
        logRef.current = [...logRef.current.slice(-40), parsed.message];
        forceRender((n) => n + 1);
        if (parsed.stage === "done") refresh();
      } catch {
        /* ignore malformed frame */
      }
    };
    source.onerror = () => source.close();
    return () => source.close();
  }, []);

  const install = async () => {
    setInstalling(true);
    setError(null);
    try {
      const result = await api.setupInstall();
      if (result.status) setStatus(result.status);
      if (result.ok) refresh();
      else setError(result.error ?? "setup failed");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setInstalling(false);
    }
  };

  const rows: [string, boolean, string][] = status
    ? [
        ["Ollama installed", status.ollamaInstalled, status.ollamaPath || "not found"],
        ["Ollama running", status.ollamaRunning, status.ollamaRunning ? "reachable" : "not started"],
        [
          "Model " + status.requiredModels[0],
          !status.missingModels.includes(status.requiredModels[0] ?? ""),
          status.missingModels.includes(status.requiredModels[0] ?? "") ? "not pulled" : "ready",
        ],
        [
          "Model " + status.requiredModels[1],
          !status.missingModels.includes(status.requiredModels[1] ?? ""),
          status.missingModels.includes(status.requiredModels[1] ?? "") ? "not pulled" : "ready",
        ],
      ]
    : [];

  return (
    <div className="flex min-h-screen items-center justify-center bg-paper px-5 py-12">
      <div className="w-full max-w-2xl">
        <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3 border-b border-line pb-4">
          <div>
            <div className="serif text-lg tracking-[0.34em] text-ink">CIRCLE</div>
            <p className="mt-3 text-sm leading-6 text-muted">
              Circle answers using a local model. It has to be on this machine
              before the app can work &mdash; nothing is sent to a server.
            </p>
          </div>
          <div className="text-right">
            <div className="eyebrow">Setup</div>
            <div className="eyebrow-ink mt-1.5">Prerequisites</div>
          </div>
        </header>

        <section className="mt-6 border border-line">
          <h2 className="eyebrow-ink border-b border-line px-5 py-3">Local model check</h2>
          <div className="px-5 py-5">
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
                    {ok ? "ok" : "needed"}
                  </span>
                </span>
              </div>
            ))}

            {status && status.ready && (
              <button
                onClick={onReady}
                className="mt-5 w-full bg-ink py-3 text-xs font-medium uppercase tracking-[0.16em] text-paper hover:bg-accent"
              >
                Continue
              </button>
            )}

            {status && !status.ready && (
              <div className="mt-5 space-y-3">
                <button
                  onClick={install}
                  disabled={installing}
                  className="w-full bg-ink py-3 text-xs font-medium uppercase tracking-[0.16em] text-paper hover:bg-accent disabled:opacity-50"
                >
                  {installing ? "Installing" : "Install automatically"}
                </button>
                <p className="text-xs leading-5 text-muted">
                  Downloads Ollama from ollama.com, starts it, and pulls{" "}
                  {status.requiredModels.join(" and ")}. The model download is
                  several gigabytes and happens once. You can also install
                  Ollama yourself and press refresh.
                </p>
              </div>
            )}

            {progress && (
              <div className="mt-4 border border-line bg-surface px-3.5 py-3">
                <div className="text-xs uppercase tracking-[0.16em] text-muted">
                  {progress.stage}
                </div>
                <div className="mono mt-1.5 text-xs text-ink">{progress.message}</div>
              </div>
            )}

            {error && (
              <div className="mt-4 border border-accent px-3.5 py-2.5 text-sm text-accent">
                {error}
              </div>
            )}

            <div className="mt-4 flex gap-2">
              <button
                onClick={refresh}
                className="flex-1 border border-ink px-4 py-2.5 text-xs font-medium uppercase tracking-[0.16em] text-ink hover:bg-ink hover:text-paper"
              >
                Refresh
              </button>
            </div>
          </div>
        </section>

        <p className="mt-4 text-center text-xs text-muted">
          Local first. Your conversations never leave this machine.
        </p>
      </div>
    </div>
  );
}
