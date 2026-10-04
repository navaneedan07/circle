/**
 * Embeddings via local Ollama (`nomic-embed-text`, 768-dim by default).
 *
 * Every call that talks to Ollama is ASYNC on purpose. This process is also
 * Electron's main process and hosts the API server, so a synchronous HTTP call
 * freezes the whole app -- window, menu and API -- for as long as Ollama takes.
 * A batch embed over a large import used to lock the UI for minutes at a time.
 *
 * When Ollama is unavailable the calls reject, callers fall back to null
 * vectors, and the archive still works through keyword (FTS5) search.
 */
import { execFileSync } from "node:child_process";
import { withOllama } from "./gate.js";

export interface EmbeddingStatus {
  available: boolean;
  model: string;
  detail: string;
}

export class EmbeddingProvider {
  lastMetrics: Record<string, unknown> = {};
  /** See LLMProvider: a timed-out probe means "busy", not "missing". */
  private lastConfirmedAt = 0;
  private lastConfirmedAvailable = false;

  constructor(
    private readonly baseUrl: string,
    private readonly model: string
  ) {}

  /**
   * Probe the model list.
   *
   * Async on purpose: this feeds the health and setup screens, and a
   * synchronous probe blocked the whole window for as long as an
   * unresponsive Ollama took to answer (up to the timeout).
   */
  async statusAsync(): Promise<EmbeddingStatus> {
    try {
      const names = await this.listModels();
      const available = names.some((n) => n === this.model || n.startsWith(`${this.model}:`));
      this.lastConfirmedAt = Date.now();
      this.lastConfirmedAvailable = available;
      return {
        available,
        model: this.model,
        detail: available ? "ready" : `model ${this.model} is not installed (ollama pull ${this.model})`,
      };
    } catch (err) {
      // A timeout while an import is running must not look like a missing
      // model: the reader would be told to install something they have.
      if (this.lastConfirmedAt > 0) {
        return {
          available: this.lastConfirmedAvailable,
          model: this.model,
          detail: `${this.lastConfirmedAvailable ? "ready" : "not installed"} (Ollama busy; last confirmed recently)`,
        };
      }
      return {
        available: false,
        model: this.model,
        detail: err instanceof Error ? err.message : "Ollama is not reachable",
      };
    }
  }

  async listModels(): Promise<string[]> {
    // Generous enough to survive a busy Ollama queueing behind an import,
    // short enough that an actually-dead Ollama is noticed quickly.
    const out = (await fetchJson(`${this.baseUrl}/api/tags`, "GET", undefined, 20_000)) as {
      models?: { name?: string }[];
    };
    const models = out?.models ?? [];
    return models.map((m) => m.name ?? "").filter(Boolean);
  }

  /**
   * Embed a batch in ONE request where possible.
   *
   * Ollama's `/api/embed` takes an array of inputs and returns an array of
   * vectors, which is dramatically faster than one request per string: an
   * import of a few thousand messages is a few dozen calls instead of a few
   * thousand. Falls back to per-text `/api/embeddings` on older Ollama.
   *
   * The whole batch is taken as ONE background slot, so a question asked
   * mid-import waits for at most a single batch instead of the whole run.
   */
  async embedBatch(texts: string[]): Promise<(number[] | null)[]> {
    return withOllama("background", () => this.embedBatchUngated(texts));
  }

  private async embedBatchUngated(texts: string[]): Promise<(number[] | null)[]> {
    const started = Date.now();
    const nonEmpty = texts.map((t) => t.trim());
    let vectors: (number[] | null)[] = nonEmpty.map(() => null);

    try {
      const body = JSON.stringify({ model: this.model, input: nonEmpty });
      const out = (await fetchJson(`${this.baseUrl}/api/embed`, "POST", body)) as {
        embeddings?: number[][];
      };
      if (Array.isArray(out.embeddings) && out.embeddings.length === nonEmpty.length) {
        vectors = out.embeddings.map((v) => (Array.isArray(v) && v.length > 0 ? v : null));
        this.lastMetrics = { count: texts.length, ms: Date.now() - started, batched: true };
        return vectors;
      }
    } catch {
      /* fall through to the per-text endpoint */
    }

    for (let i = 0; i < nonEmpty.length; i++) {
      // Each text takes its OWN background slot rather than holding the whole
      // batch's worth of time in one go: a single long-held slot is
      // indistinguishable, from the reader's side, from the app having hung.
      vectors[i] = await withOllama("background", () =>
        this.embedOneUngated(nonEmpty[i]!)
      ).catch(() => null);
    }
    this.lastMetrics = { count: texts.length, ms: Date.now() - started, batched: false };
    return vectors;
  }

  /**
   * Embed one string. `priority` decides whether this is a reader waiting for
   * an answer (interactive) or background indexing.
   */
  async embedOne(text: string, priority: "interactive" | "background" = "interactive"): Promise<number[] | null> {
    return withOllama(priority, () => this.embedOneUngated(text));
  }

  private async embedOneUngated(text: string): Promise<number[] | null> {
    if (!text.trim()) return null;
    const body = JSON.stringify({ model: this.model, prompt: text });
    const out = await fetchJson(`${this.baseUrl}/api/embeddings`, "POST", body);
    const embedding = (out as { embedding?: number[] })?.embedding;
    return Array.isArray(embedding) && embedding.length > 0 ? embedding : null;
  }
}

/**
 * Synchronous JSON over HTTP -- reserved for short, local status probes.
 *
 * Never use this on a path that talks to a model: it blocks the event loop,
 * which in this app means freezing the window. `curl` is present on Windows
 * 10+ and every Unix; bodies go via stdin to avoid argument-length limits.
 */
export function curlJson(url: string, method = "GET", body?: string, timeoutSec = 120): unknown {
  const args = ["-s", "-S", "--max-time", String(timeoutSec), "-X", method, url];
  if (body !== undefined) args.push("-H", "Content-Type: application/json", "--data-binary", "@-");
  const result = execFileSync("curl", args, {
    input: body ?? "",
    encoding: "utf-8",
    maxBuffer: 256 * 1024 * 1024,
  });
  return JSON.parse(result);
}

/**
 * Asynchronous JSON over HTTP.
 *
 * Preferred everywhere: it yields to the event loop instead of blocking it, so
 * the window stays responsive and the local API keeps answering while a model
 * is busy. `fetch` ships with Node/Electron, so no client library is needed.
 */
export async function fetchJson(
  url: string,
  method = "GET",
  body?: string,
  timeoutMs = 600_000
): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body ?? undefined,
      signal: controller.signal,
    });
    const text = await res.text();
    if (!res.ok) throw new Error(`HTTP ${res.status} from ${url}: ${text.slice(0, 200)}`);
    try {
      return JSON.parse(text) as unknown;
    } catch {
      throw new Error(`non-JSON response from ${url}`);
    }
  } finally {
    clearTimeout(timer);
  }
}

export function getEmbeddings(baseUrl: string, model: string): EmbeddingProvider {
  return new EmbeddingProvider(baseUrl, model);
}
