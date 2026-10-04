/** Local Gemma via Ollama: status, warmup and asynchronous generation. */
import { curlJson, fetchJson } from "./embeddings.js";

export interface LlmStatus {
  available: boolean;
  model: string;
  detail: string;
  installed_models?: string[];
}

export class LLMProvider {
  lastMetrics: Record<string, unknown> = {};

  constructor(
    private readonly baseUrl: string,
    private readonly model: string,
    private readonly maxAnswerTokens: number,
    private readonly numCtx = 4096,
    private readonly keepAlive = "30m"
  ) {}

  private listModels(): string[] {
    // Short timeout: this is a status probe, not a model call.
    const out = curlJson(`${this.baseUrl}/api/tags`, "GET", undefined, 5) as {
      models?: { name?: string }[];
    };
    return (out.models ?? []).map((m) => m.name ?? "").filter(Boolean);
  }

  status(): LlmStatus {
    try {
      const names = this.listModels();
      const available = names.some((n) => n === this.model || n.startsWith(`${this.model}:`));
      return {
        available,
        model: this.model,
        detail: available ? "ready" : `model ${this.model} is not installed (ollama pull ${this.model})`,
        installed_models: names,
      };
    } catch (err) {
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
   */
  async generate(prompt: string, options: { maxTokens?: number; system?: string } = {}): Promise<string> {
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
