import { useState } from "react";
import { api, fmtDate, Health, IdentitySuggestion } from "../api";
import { PRIVACY_URL, TERMS_URL } from "../version";
import WatchFolders from "../components/WatchFolders";
import { usePoll } from "../hooks";

const fmtNum = (n?: number | null) =>
  n == null ? "?" : n.toLocaleString(undefined);

type AiMetrics = {
  llm: Record<string, number>;
  embeddings: Record<string, number>;
  tuning: Record<string, number | string>;
  advice: string[];
};

function PerformancePanel() {
  const { data: metrics, refresh } = usePoll<AiMetrics>(
    () => api.aiMetrics() as unknown as Promise<AiMetrics>,
    15000
  );
  const { data: health } = usePoll(() => api.health(), 15000);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const llm = metrics?.llm ?? {};
  const models = health?.health.llm.installed_models ?? [];
  const currentModel = String(health?.health.llm.model ?? "");

  const switchModel = async (model: string) => {
    setBusy(true);
    try {
      await api.updateSettings({ ollama_model: model });
      setNote(`Model set to ${model}. Warm it up for a fast first answer.`);
      refresh();
    } finally {
      setBusy(false);
    }
  };

  const warm = async () => {
    setBusy(true);
    setNote("Loading models into memory");
    try {
      const out = await api.warmup();
      setNote(`Models warm: ${JSON.stringify(out).slice(0, 120)}`);
      refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="border border-line bg-surface p-4">
      <h2 className="eyebrow">AI performance on this CPU</h2>
      <p className="mt-1 max-w-2xl text-xs text-muted">
        Prompt size and model size dominate latency on CPU. Circle streams
        answers, keeps models resident and budgets the evidence block
        automatically.
      </p>

      <div className="mt-4 grid grid-cols-2 gap-px border border-line bg-line text-xs sm:grid-cols-4">
        {[
          ["Prompt tokens", llm.prompt_tokens],
          [
            "Prompt eval",
            llm.prompt_eval_ms != null ? `${Math.round(llm.prompt_eval_ms)}ms` : "none",
          ],
          [
            "Generation",
            llm.tokens_per_second ? `${llm.tokens_per_second} tok/s` : "none",
          ],
          [
            "Model load",
            llm.load_ms != null ? `${Math.round(llm.load_ms)}ms` : "none",
          ],
        ].map(([label, value]) => (
          <div key={String(label)} className="bg-surface px-3 py-2">
            <div className="eyebrow">{label}</div>
            <div className="mono mt-1 text-xs text-ink">{value ?? "none"}</div>
          </div>
        ))}
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3 text-xs">
        <span className="eyebrow">Reasoning model</span>
        <select
          value={currentModel}
          onChange={(e) => e.target.value && switchModel(e.target.value)}
          className="mono border border-line bg-paper px-2.5 py-1.5 text-xs text-ink outline-none focus:border-line-strong"
        >
          {[currentModel, ...models.filter((m) => m !== currentModel)].map((m) => (
            <option key={m} value={m}>
              {m}
              {m === currentModel ? " (active)" : ""}
            </option>
          ))}
        </select>
        <button
          onClick={warm}
          disabled={busy}
          className="border border-ink px-3 py-1.5 font-medium text-ink hover:bg-ink hover:text-paper disabled:opacity-50"
        >
          {busy ? "Working" : "Warm up models"}
        </button>
      </div>

      {metrics?.advice && metrics.advice.length > 0 && (
        <ul className="mt-3 space-y-1.5 text-xs text-ochre">
          {metrics.advice.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ul>
      )}
      {note && <div className="mt-2 text-xs text-moss">{note}</div>}
    </section>
  );
}

function StatusRow({
  label,
  ok,
  detail,
}: {
  label: string;
  ok: boolean;
  detail?: string;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line py-2.5 last:border-b-0">
      <span className="text-sm text-ink">{label}</span>
      <span className="flex items-baseline gap-3 text-xs">
        {detail && <span className="text-muted">{detail}</span>}
        <span className={ok ? "text-moss" : "text-accent"}>
          {ok ? "ok" : "missing"}
        </span>
      </span>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="eyebrow">{label}</div>
      <div className="mono truncate text-xs text-ink" title={value}>
        {value}
      </div>
    </div>
  );
}

/**
 * One possible match, with the archive evidence on both sides. A conflict you
 * cannot see is a conflict you cannot judge, so the conflicting messages are
 * quoted here rather than summarized.
 */
function SuggestionRow({
  s,
  onResolve,
}: {
  s: IdentitySuggestion;
  onResolve: (id: string, merge: boolean) => void;
}) {
  const ev = s.evidence;
  const existing = ev?.existing;
  const conflicting = ev?.conflicting;

  return (
    <div className="border border-line">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-3 py-2.5">
        <div className="text-sm text-ink">
          <span className="font-medium">{s.label}</span>
          <span className="text-muted">
            {" "}
            / {s.source} / {s.reason}
          </span>
        </div>
        <div className="flex gap-2 text-xs">
          <button
            onClick={() => onResolve(s.id, true)}
            className="bg-ink px-3 py-1.5 font-medium text-paper hover:bg-accent"
          >
            Merge
          </button>
          <button
            onClick={() => onResolve(s.id, false)}
            className="border border-ink px-3 py-1.5 text-ink hover:bg-ink hover:text-paper"
          >
            Keep separate
          </button>
        </div>
      </div>

      {ev && (
        <div className="px-3 py-2.5">
          <div className="grid grid-cols-2 gap-3 border-b border-line pb-2.5">
            <div className="min-w-0">
              <div className="eyebrow">Circle already has</div>
              <div className="truncate text-sm text-ink">
                {existing?.name ?? "(none)"}
              </div>
              <div className="mono mt-1 text-xs text-muted">
                {existing
                  ? `${fmtNum(existing.interactions)} interactions / ${fmtNum(
                      ev.counts.existing_messages
                    )} messages`
                  : "no existing record"}
              </div>
              {!!existing?.aliases?.length && (
                <div className="mono truncate text-xs text-muted">
                  also: {existing.aliases.join(", ")}
                </div>
              )}
            </div>
            <div className="min-w-0">
              <div className="eyebrow">
                {ev.kind === "duplicate" ? "Would be folded in" : "Conflicts with"}
              </div>
              <div className="truncate text-sm text-ink">
                {conflicting?.name ?? s.label}
              </div>
              <div className="mono mt-1 text-xs text-muted">
                {conflicting?.interactions != null
                  ? `${fmtNum(conflicting.interactions)} interactions`
                  : "unlinked sender label"}{" "}
                / {fmtNum(ev.counts.conflicting_messages)} messages
              </div>
              {conflicting?.last_seen && (
                <div className="mono truncate text-xs text-muted">
                  last seen {fmtDate(conflicting.last_seen)}
                </div>
              )}
            </div>
          </div>

          {!!ev.samples.length && (
            <div className="mt-2.5">
              <div className="eyebrow">
                Where they disagree / messages from {conflicting?.name ?? s.label}
              </div>
              <ul className="mt-1.5 space-y-1.5">
                {ev.samples.map((m, i) => (
                  <li key={i} className="border-l-2 border-line-strong pl-2">
                    <p className="line-clamp-2 text-xs text-ink">{m.text}</p>
                    <p className="mono text-xs text-muted">
                      {m.source}
                      {m.conversation ? ` / ${m.conversation}` : ""}
                      {m.sender_label ? ` / ${m.sender_label}` : ""}
                      {m.at ? ` / ${fmtDate(m.at)}` : ""}
                    </p>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function SettingsPage() {
  const { data: health } = usePoll(() => api.health(), 5000);
  const { data: settings, refresh } = usePoll(() => api.settings(), 10000);
  const { data: suggestions, refresh: refreshSuggestions } = usePoll(
    () => api.identitySuggestions(),
    10000
  );
  const [userNames, setUserNames] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [demo, setDemo] = useState(false);

  const h = health as Health | null;
  const sugg = suggestions?.suggestions ?? [];

  const saveNames = async () => {
    if (userNames === null) return;
    const list = userNames
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
    await api.updateSettings({ user_names: list });
    setSaved(true);
    refresh();
    setTimeout(() => setSaved(false), 2500);
  };

  const resolve = async (id: string, merge: boolean) => {
    await api.resolveSuggestion(id, merge);
    refreshSuggestions();
  };

  const runDemo = async () => {
    setDemo(true);
    try {
      await api.generateDemo();
    } finally {
      setDemo(false);
    }
  };

  const currentNames =
    userNames ?? ((settings?.user_names as string[]) ?? []).join(", ");

  const restartSetup = async () => {
    await api.setBootstrap({ mark_done: false });
    window.location.reload();
  };

  return (
    <div className="space-y-8">
      <div>
        <div className="eyebrow">This machine</div>
        <h1 className="serif mt-3 text-3xl leading-none text-ink">Settings</h1>
        <p className="mt-3 max-w-2xl text-sm leading-6 text-muted">
          Everything runs on this machine. Nothing is sent anywhere unless you
          explicitly configure an optional integration.
        </p>
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="border border-line bg-surface p-4">
          <h2 className="eyebrow mb-2">System health</h2>
          <StatusRow
            label="AI"
            ok={!!h?.health.llm.available}
            detail={h?.health.llm.detail}
          />
          <StatusRow
            label="Database"
            ok={!!h?.health.database.connected}
            detail={h?.health.database.engine}
          />
          <StatusRow
            label="Embeddings"
            ok={!!h?.health.embeddings.available}
            detail={h?.health.embeddings.model}
          />
          <StatusRow
            label="Watcher"
            ok={!!h?.health.watcher.running}
            detail={h?.health.watcher.running ? "running" : h?.health.watcher.detail}
          />
          <StatusRow
            label="Voice transcription, local"
            ok={!!h?.health.stt.available}
            detail={h?.health.stt.detail}
          />
          <StatusRow
            label="Voice output, optional ElevenLabs"
            ok={!!h?.health.tts.enabled}
            detail={h?.health.tts.detail}
          />
          <div className="mt-3 border border-line px-3 py-2 text-xs">
            {h?.offline_ready ? (
              <span className="text-moss">
                Offline ready. Import, search, embeddings and Gemma all work
                without internet.
              </span>
            ) : (
              <span className="text-ochre">
                Local model missing. Pull it with: ollama pull gemma3:4b
              </span>
            )}
          </div>
        </section>

        <section className="border border-line bg-surface p-4">
          <h2 className="eyebrow mb-2">Local AI</h2>
          <dl className="space-y-2 text-sm">
            {[
              ["Reasoning model", String(settings?.ollama_model ?? "")],
              ["Embedding model", String(settings?.embedding_model ?? "")],
              ["Ollama URL", String(settings?.ollama_url ?? "")],
              ["Archive", String(settings?.storage_backend ?? "sqlite")],
              ["Import root", String(settings?.import_root ?? "")],
              [
                "Watched folders",
                String((settings?.import_roots as string[])?.length ?? 1),
              ],
            ].map(([k, v]) => (
              <div key={k} className="flex justify-between gap-4">
                <dt className="eyebrow">{k}</dt>
                <dd
                  className="mono truncate text-right text-xs text-ink"
                  title={v}
                >
                  {v}
                </dd>
              </div>
            ))}
          </dl>

          <div className="mt-4 border-t border-line pt-3">
            <label className="text-sm text-muted">
              Your own display names in exports, comma separated. These are
              treated as you, not as a contact.
            </label>
            <div className="mt-2 flex gap-2">
              <input
                value={currentNames}
                onChange={(e) => setUserNames(e.target.value)}
                placeholder="Me, You"
                className="flex-1 border border-line bg-paper px-3 py-2 text-sm text-ink outline-none placeholder:text-muted focus:border-line-strong"
              />
              <button
                onClick={saveNames}
                className="bg-ink px-4 py-2 text-xs font-medium text-paper hover:bg-accent"
              >
                Save
              </button>
            </div>
            {saved && <div className="mt-1 text-xs text-moss">Saved.</div>}
          </div>
        </section>
      </div>

      <WatchFolders />

      <div className="border border-line bg-surface p-4">
        <h2 className="eyebrow">Setup</h2>
        <p className="mt-1 text-xs text-muted">
          Reopen the first-run walkthrough to choose folders and check system
          health from the start.
        </p>
        <button
          onClick={restartSetup}
          className="mt-3 border border-ink px-4 py-2 text-xs font-medium text-ink hover:bg-ink hover:text-paper"
        >
          Run setup again
        </button>
      </div>

      <PerformancePanel />

      <section className="border border-line bg-surface p-4">
        <h2 className="eyebrow">Possible matches</h2>
        <p className="mt-1 text-xs text-muted">
          Circle never merges people automatically. You decide.
        </p>
        <div className="mt-3 space-y-2">
          {sugg.length === 0 && (
            <div className="text-sm text-muted">No pending suggestions.</div>
          )}
          {sugg.map((s: IdentitySuggestion) => (
            <SuggestionRow key={s.id} s={s} onResolve={resolve} />
          ))}
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <div className="border border-line bg-surface p-4">
          <h2 className="eyebrow">Demo data</h2>
          <p className="mt-1 text-xs text-muted">
            Generates clearly synthetic people, chats, calendar events, emails,
            notes and a voice transcript. All fake, all marked as demo.
          </p>
          <button
            onClick={runDemo}
            disabled={demo}
            className="mt-3 bg-ink px-4 py-2 text-xs font-medium text-paper hover:bg-accent disabled:opacity-50"
          >
            {demo ? "Generating" : "Generate demo data"}
          </button>
        </div>
        <div className="border border-line bg-surface p-4 text-xs text-muted">
          <h2 className="eyebrow mb-2">Where data comes from</h2>
          <ul className="space-y-1.5">
            <li>
              <span className="text-ink">api</span>: official connectors. None
              configured, and Circle never asks for social passwords.
            </li>
            <li>
              <span className="text-ink">imported</span>: files you put in the
              watched folders, including exports, recordings and documents.
            </li>
            <li>
              <span className="text-ink">local</span>: records you create inside
              Circle, such as notes.
            </li>
          </ul>
        </div>
        <section className="border border-line bg-surface p-4 text-xs text-muted">
          <h2 className="eyebrow mb-2">Terms &amp; privacy</h2>
          <p className="leading-5">
            Circle keeps everything on this machine: no account, no server, no
            analytics. The full documents open in your browser.
          </p>
          <div className="mt-3 flex flex-wrap gap-4">
            <a
              href={TERMS_URL}
              target="_blank"
              rel="noreferrer"
              className="text-ink underline underline-offset-2"
            >
              Terms of use
            </a>
            <a
              href={PRIVACY_URL}
              target="_blank"
              rel="noreferrer"
              className="text-ink underline underline-offset-2"
            >
              Privacy notice
            </a>
          </div>
        </section>
      </section>
    </div>
  );
}
