/** Local Gemma via Ollama: status, warmup and asynchronous generation. */
import { fetchJson } from "./embeddings.js";
import { withOllama } from "./gate.js";

export interface LlmStatus {
  available: boolean;
  model: string;
  detail: string;
  installed_models?: string[];
  /** True when the last probe failed only because Ollama was busy. */
  busy?: boolean;
}

export class LLMProvider {
  lastMetrics: Record<string, unknown> = {};
  /**
   * Last time we positively confirmed the model was present.
   *
   * A probe that times out does NOT mean the model is missing -- it usually
   * means Ollama is busy serving an import. Reporting "unavailable" there
   * showed the reader a model that was installed and working as though it
   * were not, which is worse than saying nothing changed.
   */
  private lastConfirmedAt = 0;
  private lastConfirmedAvailable = false;

  constructor(
    private readonly baseUrl: string,
    private readonly model: string,
    private readonly maxAnswerTokens: number,
    private readonly numCtx = 4096,
    private readonly keepAlive = "30m"
  ) {}

  private async listModels(): Promise<string[]> {
    // Generous enough to survive a busy Ollama queueing behind an import,
    // short enough that an actually-dead Ollama is noticed quickly.
    const out = (await fetchJson(`${this.baseUrl}/api/tags`, "GET", undefined, 20_000)) as {
      models?: { name?: string }[];
    };
    return (out.models ?? []).map((m) => m.name ?? "").filter(Boolean);
  }

  /** Async probe: never block the main process behind an unresponsive Ollama. */
  async statusAsync(): Promise<LlmStatus> {
    try {
      const names = await this.listModels();
      const available = names.some((n) => n === this.model || n.startsWith(`${this.model}:`));
      this.lastConfirmedAt = Date.now();
      this.lastConfirmedAvailable = available;
      return {
        available,
        model: this.model,
        detail: available ? "ready" : `model ${this.model} is not installed (ollama pull ${this.model})`,
        installed_models: names,
      };
    } catch (err) {
      // Keep the last good answer rather than flipping to "missing" every time
      // the probe loses a race with an import.
      if (this.lastConfirmedAt > 0) {
        return {
          available: this.lastConfirmedAvailable,
          model: this.model,
          detail: `${this.lastConfirmedAvailable ? "ready" : "not installed"} (Ollama busy; last confirmed recently)`,
          installed_models: [],
          busy: true,
        };
      }
      return {
        available: false,
        model: this.model,
        detail: err instanceof Error ? err.message : "Ollama is not reachable",
      };
    }
  }

  /** Preload the model so the first question is not the slowest. */
  async warmup(): Promise<Record<string, unknown>> {
    const started = Date.now();
    try {
      await this.generate("hi", { maxTokens: 1 });
      return { ok: true, ms: Date.now() - started };
    } catch (err) {
      return { ok: false, error: err instanceof Error ? err.message : String(err) };
    }
  }

  /**
   * Generate an answer.
   *
   * Async because this runs on Electron's main process: a synchronous call
   * would freeze the window for the entire generation, which on a 4B model is
   * tens of seconds.
   *
   * Taken as an INTERACTIVE slot: an import embedding in the background will
   * pause between batches rather than making the reader wait for the import.
   */
  async generate(prompt: string, options: { maxTokens?: number; system?: string } = {}): Promise<string> {
    return withOllama("interactive", () => this.generateUngated(prompt, options));
  }

  private async generateUngated(prompt: string, options: { maxTokens?: number; system?: string }): Promise<string> {
    const payload: Record<string, unknown> = {
      model: this.model,
      prompt,
      stream: false,
      keep_alive: this.keepAlive,
      options: {
        num_ctx: this.numCtx,
        num_predict: options.maxTokens ?? this.maxAnswerTokens,
      },
    };
    if (options.system) payload.system = options.system;
    const started = Date.now();
    const out = (await fetchJson(`${this.baseUrl}/api/generate`, "POST", JSON.stringify(payload))) as {
      response?: string;
      prompt_eval_count?: number;
      eval_count?: number;
      load_duration?: number;
    };
    this.lastMetrics = {
      load_ms: out.load_duration ? Math.round(out.load_duration / 1e6) : 0,
      prompt_tokens: out.prompt_eval_count ?? 0,
      answer_tokens: out.eval_count ?? 0,
      total_ms: Date.now() - started,
    };
    return out.response ?? "";
  }
}

export function getLlm(
  baseUrl: string,
  model: string,
  maxAnswerTokens: number
): LLMProvider {
  return new LLMProvider(baseUrl, model, maxAnswerTokens);
}
