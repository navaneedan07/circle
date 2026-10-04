import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, apiUrl, AskResult, authHeaders, Source } from "../api";
import SourceModal from "./SourceModal";

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
  archive: "Archive",
};

function SourceRow({ source, onClick }: { source: Source; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="w-full border border-line bg-surface px-3 py-2 text-left hover:border-line-strong"
    >
      <span className="eyebrow block truncate">
        {SOURCE_LABELS[source.source] ?? source.source} / {source.label}
      </span>
      <span className="mt-1 block truncate text-xs text-muted">
        {source.snippet}
      </span>
    </button>
  );
}

export default function AskPanel({
  personId,
  placeholder,
  autoFocus = false,
  title,
  seed,
}: {
  personId?: string;
  placeholder?: string;
  autoFocus?: boolean;
  title?: string;
  /** When a new seed arrives, ask it immediately. */
  seed?: { text: string; at: number };
}) {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<AskResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openSource, setOpenSource] = useState<string | null>(null);
  const [lastSeed, setLastSeed] = useState(0);
  const navigate = useNavigate();
  const [streamed, setStreamed] = useState("");
  const [phase, setPhase] = useState("");
  const [elapsed, setElapsed] = useState(0);
  // Answers stay reachable while composing the next question, so a follow-up
  // can refer back to what was just said.
  const [history, setHistory] = useState<{ q: string; intent: string }[]>([]);

  useEffect(() => {
    if (seed && seed.at !== lastSeed) {
      setLastSeed(seed.at);
      void ask(seed.text);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seed?.at]);

  // Example chips elsewhere on the page trigger a question through an event,
  // so the asking state stays inside this component.
  useEffect(() => {
    const onAsk = (e: Event) => {
      const text = (e as CustomEvent<string>).detail;
      if (typeof text === "string" && text.trim()) void ask(text);
    };
    window.addEventListener("circle:ask", onAsk);
    return () => window.removeEventListener("circle:ask", onAsk);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Streams the answer so text appears while the local model generates. */
  const ask = async (q?: string) => {
    const text = (q ?? question).trim();
    if (!text) return;
    setLoading(true);
    setError(null);
    setResult(null);
    setStreamed("");
    setPhase("retrieving evidence");
    setElapsed(0);
    if (q) setQuestion(q);
    setHistory((h) =>
      h.some((x) => x.q === text) ? h : [...h, { q: text, intent: "" }]
    );
    const started = Date.now();
    const timer = window.setInterval(
      () => setElapsed(Math.round((Date.now() - started) / 1000)),
      500
    );
    try {
      const res = await fetch(apiUrl("/api/ask/stream"), {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ question: text, person_id: personId }),
      });
      if (!res.ok || !res.body) throw new Error(`ask failed (${res.status})`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      // The stream is only trustworthy once a terminal `done` frame arrives.
      // Everything below tracks whether that happened so a truncated or
      // protocol-drifted stream can never end as a blank answer.
      let sawDone = false;
      let answerText = "";
      let sources: Source[] | null = null;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() ?? "";
        for (const part of parts) {
          const line = part.replace(/^data:\s*/, "").trim();
          if (!line) continue;
          let ev: Record<string, unknown>;
          try {
            ev = JSON.parse(line);
          } catch {
            continue;
          }
          if (ev.type === "meta") {
            const n = Number(ev.evidence_count ?? 0);
            setPhase(
              Number(ev.latency_ms && (ev.latency_ms as Record<string, number>).generation_ms) != null
                ? `${n} evidence records, generated with local Gemma`
                : `${n === 1 ? "1 archive figure" : `${n} archive figures`}`
            );
          } else if (ev.type === "token") {
            answerText += String(ev.text ?? "");
            setStreamed(answerText);
          } else if (ev.type === "answer") {
            // Legacy frame: the answer arrives whole, before `sources`.
            answerText = String(ev.answer ?? "");
            setStreamed(answerText);
          } else if (ev.type === "sources") {
            sources = (ev.sources as Source[]) ?? [];
          } else if (ev.type === "done") {
            sawDone = true;
            const done = ev as unknown as AskResult;
            setResult(done);
            setStreamed("");
            setHistory((h) => {
              const next = [...h];
              const last = next[next.length - 1];
              // Backfill the intent now that it is known.
              if (last && last.intent === "") next[next.length - 1] = {
                ...last,
                intent: done.intent,
              };
              return next;
            });
          } else if (ev.type === "error") {
            sawDone = true;
            setError(String(ev.error ?? "stream error"));
          }
        }
      }
      // The stream closed without a terminal frame. Recover what we can;
      // otherwise re-ask over the plain endpoint, which returns the whole
      // result in one piece.
      if (!sawDone) {
        if (sources && answerText.trim()) {
          const salvaged: AskResult = {
            answer: answerText,
            sources,
            insufficient: false,
            evidence_count: sources.length,
            intent: "",
            latency_ms: {},
          };
          setResult(salvaged);
        } else {
          setResult(await api.ask(text, personId));
        }
      }
    } catch {
      // Fall back to the non-streaming endpoint if streaming is unavailable
      try {
        setResult(await api.ask(text, personId));
      } catch (e2) {
        setError((e2 as Error).message);
      }
    } finally {
      window.clearInterval(timer);
      setLoading(false);
      setPhase("");
    }
  };

  return (
    <div className="space-y-4">
      <section className="border border-line-strong bg-surface">
        <div className="flex items-baseline justify-between gap-4 border-b border-line px-4 py-2.5">
          <h2 className="eyebrow-ink">{title ?? "Ask your archive"}</h2>
          <span className="eyebrow">Local / private</span>
        </div>
        <div className="flex items-stretch">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              // Enter submits; Shift+Enter is not meaningful in a single-line
              // input, so Ctrl/Cmd+Enter is offered as an explicit alternative
              // for muscle memory from other tools.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void ask();
              }
            }}
            placeholder={placeholder ?? "Ask anything about your imported data"}
            autoFocus={autoFocus}
            aria-label={title ?? "Ask your archive"}
            className="min-w-0 flex-1 bg-transparent px-4 py-3.5 text-sm text-ink outline-none placeholder:text-muted"
          />
          <button
            onClick={() => ask()}
            disabled={loading || !question.trim()}
            className="shrink-0 border-l border-line bg-ink px-6 text-xs font-medium uppercase tracking-[0.14em] text-paper hover:bg-accent disabled:opacity-40"
          >
            {loading ? "Thinking" : "Ask"}
          </button>
        </div>
      </section>

      {error && (
        <div className="border border-accent px-3 py-2 text-sm text-accent">
          {error}
        </div>
      )}

      {loading && (
        <div className="border border-line bg-surface p-4">
          <div className="eyebrow flex items-baseline justify-between">
            <span>{phase || "working"}</span>
            <span className="tabular-nums">{elapsed}s</span>
          </div>
          <div
            aria-live="polite"
            className="mt-2 whitespace-pre-wrap text-sm leading-6 text-ink"
          >
            {streamed || (
              <span className="text-muted">
                The model is reading your evidence locally. Counting questions
                skip this step and answer straight from the database.
              </span>
            )}
          </div>
          {elapsed >= 20 && (
            <p className="mt-3 border-t border-line pt-2 text-xs text-muted">
              Still generating. On CPU a long answer can take a minute; counts
              and rankings come back instantly because they never reach the
              model.
            </p>
          )}
        </div>
      )}

      {result && (
        <div className="space-y-4">
          <div className="border border-line bg-surface p-4">
            <div className="eyebrow mb-3 flex items-baseline justify-between gap-3 border-b border-line pb-2.5">
              <span>Answer</span>
              <button
                onClick={() => {
                  void navigator.clipboard
                    ?.writeText(result.answer)
                    .catch(() => undefined);
                }}
                className="eyebrow text-muted hover:text-ink"
              >
                Copy
              </button>
            </div>
            <div
              aria-live="polite"
              className="whitespace-pre-wrap break-words text-sm leading-6 text-ink"
            >
              {result.answer}
            </div>
            <div className="eyebrow mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line pt-3">
              <span>
                {result.latency_ms.generation_ms != null
                  ? `${result.evidence_count} evidence records`
                  : result.evidence_count === 1
                    ? "1 archive figure"
                    : `${result.evidence_count} archive figures`}
              </span>
              {result.latency_ms.retrieval_ms != null && (
                <span>retrieval {result.latency_ms.retrieval_ms}ms</span>
              )}
              {result.latency_ms.generation_ms != null && (
                <span>generation {result.latency_ms.generation_ms}ms</span>
              )}
              <span>
                {result.latency_ms.generation_ms != null
                  ? "local Gemma"
                  : "counted from your archive"}
              </span>
              {result.insufficient && (
                <span className="text-accent">low evidence</span>
              )}
            </div>
          </div>

          {result.sources.length > 0 && (
            <div className="space-y-2">
              <div className="eyebrow">Sources</div>
              <div className="grid gap-2 sm:grid-cols-2">
                {result.sources.map((s) => (
                  <SourceRow
                    key={s.label}
                    source={s}
                    onClick={() => {
                      if (s.memory_id) setOpenSource(s.memory_id);
                      else if (s.person_id) navigate(`/person/${s.person_id}`);
                    }}
                  />
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {history.length > 0 && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-2">
          <span className="eyebrow">Asked</span>
          {history.map((h) => (
            <button
              key={h.q}
              onClick={() => setQuestion(h.q)}
              title={h.intent ? `${h.q} (${h.intent})` : h.q}
              className="max-w-[22rem] truncate border border-line px-2.5 py-1 text-xs text-muted hover:border-line-strong hover:text-ink"
            >
              {h.q}
            </button>
          ))}
          <button
            onClick={() => {
              setHistory([]);
              setResult(null);
              setQuestion("");
            }}
            className="eyebrow min-h-6 px-1 text-muted hover:text-ink"
          >
            Clear
          </button>
        </div>
      )}

      {openSource && (
        <SourceModal memoryId={openSource} onClose={() => setOpenSource(null)} />
      )}
    </div>
  );
}
