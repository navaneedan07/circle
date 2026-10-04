/**
 * Retrieval-augmented answering.
 *
 * Two paths, deliberately:
 *
 *  1. Countable questions ("who do I talk to most", "how many messages last
 *     week") are answered by SQL aggregates over the archive. A 4B model asked
 *     to count 100k records returns a confident wrong number copied out of
 *     whichever message it retrieved.
 *
 *  2. Everything else is hybrid retrieval (vector + FTS5 keyword) fused with
 *     reciprocal rank, handed to local Gemma with citations, and the citations
 *     are validated against the evidence actually supplied.
 */
import { contentTerms } from "../parsers/common.js";
import { topicTotals } from "../relationship.js";
import type { Memory, SourceType } from "../domain.js";
import type { Store } from "../store.js";
import type { EmbeddingProvider } from "./embeddings.js";
import type { LLMProvider } from "./llm.js";

export interface RagDeps {
  store: Store;
  embedder: EmbeddingProvider;
  llm: LLMProvider;
  maxEvidence: number;
  evidenceChars: number;
  totalEvidenceChars: number;
  maxAnswerTokens: number;
}

export interface AnswerSource {
  label: string;
  memory_id: string | null;
  person_id: string | null;
  person_name: string | null;
  source: string;
  kind: string;
  origin: string;
  occurred_at: string | null;
  citation: string;
  snippet: string;
}

export interface AskResult {
  answer: string;
  sources: AnswerSource[];
  person_id: string | null;
  person_name: string | null;
  insufficient: boolean;
  evidence_count: number;
  intent: string;
  latency_ms: Record<string, number>;
}

export type Intent =
  | "top_people"
  | "people_count"
  | "activity_count"
  | "recent_people"
  | "last_interaction"
  | "top_topics"
  | "ask";

export function understand(question: string): Intent {
  const q = question.toLowerCase();
  if (/(who do i talk to (the )?most|most (talked|messaged)|top people|talk to most)/.test(q)) return "top_people";
  if (/(how many (people|persons|contacts)|number of people)/.test(q)) return "people_count";
  if (/(how many (messages|texts|emails|interactions)|message count)/.test(q)) return "activity_count";
  if (/(who did i talk to|who have i (talked|spoken) to).*(last|past|week|month|days)/.test(q)) return "recent_people";
  if (/(when did i last (talk|speak|message)|last (talk|spoke|messaged))/.test(q)) return "last_interaction";
  if (/(top|main|most).*(topics|subjects|things we)/.test(q) || /what are my top topics/.test(q)) return "top_topics";
  return "ask";
}

function windowDays(question: string): number | null {
  const q = question.toLowerCase();
  const m = q.match(/(\d+)\s*(day|week|month|year)/);
  if (!m) {
    if (q.includes("last week") || q.includes("past week")) return 7;
    if (q.includes("last month") || q.includes("past month")) return 30;
    if (q.includes("last year")) return 365;
    return null;
  }
  const n = Number(m[1]);
  const unit = m[2]!;
  if (unit.startsWith("day")) return n;
  if (unit.startsWith("week")) return n * 7;
  if (unit.startsWith("month")) return n * 30;
  return n * 365;
}

function sinceIso(days: number | null): string | null {
  if (days === null) return null;
  return new Date(Date.now() - days * 86_400_000).toISOString();
}

export async function hybridRetrieve(
  deps: RagDeps,
  question: string,
  options: { personId?: string | null; k?: number } = {}
): Promise<Memory[]> {
  const k = options.k ?? deps.maxEvidence;
  const personId = options.personId ?? undefined;

  // Keyword retrieval first (cheap, and always available).
  const terms = contentTerms(question);
  const keyword = deps.store.keywordSearch(terms, k * 3, personId);

  // Vector retrieval over stored embeddings.
  const vector: { memory: Memory; score: number }[] = [];
  try {
    const index = deps.store.embeddingIndex();
    const queryVec = index ? await deps.embedder.embedOne(question).catch(() => null) : null;
    if (index && queryVec && queryVec.length === index.dim) {
      // Score the flat matrix in a single pass, keeping only the best rows.
      //
      // The obvious version allocates one object per row and then sorts the
      // lot. On a real archive that is ~110k short-lived objects plus a sort,
      // every single question. Tracking the top rows in place avoids both,
      // and only the winners are ever read back from the database.
      const want = Math.min(k * 5, index.ids.length);
      const bestIdx = new Int32Array(want);
      const bestScore = new Float32Array(want);
      bestIdx.fill(-1);
      bestScore.fill(Number.NEGATIVE_INFINITY);
      let filled = 0;

      for (let i = 0; i < index.ids.length; i++) {
        const score = cosineRow(queryVec, index.vectors, i * index.dim, index.dim);
        if (filled < want) {
          bestIdx[filled] = i;
          bestScore[filled] = score;
          filled++;
          if (filled === want) bestScore.sort(); // keep the cut-off ascending
          continue;
        }
        if (score <= bestScore[0]!) continue;
        // Replace the weakest and re-establish the cut-off.
        let worst = 0;
        for (let j = 1; j < want; j++) if (bestScore[j]! < bestScore[worst]!) worst = j;
        bestIdx[worst] = i;
        bestScore[worst] = score;
        bestScore.sort();
      }

      const order = Array.from(bestIdx.slice(0, filled), (_, n) => n).sort(
        (a, b) => bestScore[b]! - bestScore[a]!
      );
      for (const n of order) {
        const memory = deps.store.getMemory(index.ids[bestIdx[n]!]!);
        if (!memory) continue;
        if (personId && memory.person_id !== personId) continue;
        vector.push({ memory, score: bestScore[n]! });
      }
    }
  } catch {
    /* vector path optional */
  }

  // Reciprocal-rank fusion of the two ranked lists.
  const fused = new Map<string, { memory: Memory; score: number }>();
  const K_RRF = 60;
  keyword.forEach((hit, i) => {
    const prev = fused.get(hit.memory.id);
    const add = 1 / (K_RRF + i + 1);
    if (prev) prev.score += add;
    else fused.set(hit.memory.id, { memory: hit.memory, score: add });
  });
  vector.forEach((hit, i) => {
    const prev = fused.get(hit.memory.id);
    const add = 1 / (K_RRF + i + 1);
    if (prev) prev.score += add;
    else fused.set(hit.memory.id, { memory: hit.memory, score: add });
  });

  const ranked = [...fused.values()].sort((a, b) => b.score - a.score).map((h) => h.memory);

  // Budget by total characters as well as record count: prompt size dominates
  // CPU latency, and fewer tighter records are also more focused.
  const out: Memory[] = [];
  let budget = deps.totalEvidenceChars;
  for (const memory of ranked) {
    if (out.length >= deps.maxEvidence) break;
    const trimmed = trimAtWord(memory.text, deps.evidenceChars);
    if (trimmed.length > budget) continue;
    budget -= trimmed.length;
    out.push({ ...memory, text: trimmed });
  }
  return out;
}

function trimAtWord(text: string, limit: number): string {
  if (text.length <= limit) return text;
  const cut = text.slice(0, limit);
  const space = cut.lastIndexOf(" ");
  return `${space > limit * 0.6 ? cut.slice(0, space) : cut}…`;
}

/** Cosine between a query vector and row `offset` of a flat matrix. */
function cosineRow(query: number[], matrix: Float32Array, offset: number, dim: number): number {
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < dim; i++) {
    const q = query[i]!;
    const v = matrix[offset + i]!;
    dot += q * v;
    na += q * q;
    nb += v * v;
  }
  const denom = Math.sqrt(na) * Math.sqrt(nb);
  return denom === 0 ? 0 : dot / denom;
}

const SYSTEM_PROMPT =
  "You are Circle, a private assistant answering questions about the user's own imported " +
  "messages and notes. Answer only from the numbered evidence provided. Cite the evidence " +
  "you used as [S1], [S2] and so on. If the evidence does not answer the question, say you " +
  "could not find enough evidence in the imported data. Never infer emotional or " +
  "mental-health states about people. Be concise.";

export async function ask(deps: RagDeps, question: string, personId: string | null = null): Promise<AskResult> {
  const started = Date.now();
  const intent = understand(question);
  const latency: Record<string, number> = {};

  // ---- deterministic, database-answered intents ----
  const deterministic = answerFromArchive(deps, question, intent, personId);
  if (deterministic) {
    latency.total_ms = Date.now() - started;
    return { ...deterministic, latency_ms: latency };
  }

  // ---- retrieval + local model ----
  const retrieveStart = Date.now();
  const evidence = await hybridRetrieve(deps, question, { personId });
  latency.retrieval_ms = Date.now() - retrieveStart;

  if (evidence.length === 0) {
    latency.total_ms = Date.now() - started;
    return {
      answer: "I couldn't find enough evidence in your imported data.",
      sources: [],
      person_id: personId,
      person_name: personId ? deps.store.getPerson(personId)?.display_name ?? null : null,
      insufficient: true,
      evidence_count: 0,
      intent,
      latency_ms: latency,
    };
  }

  const blocks = evidence.map((m, i) => `[S${i + 1}] ${m.citation}\n${m.text}`).join("\n\n");
  const prompt = `Evidence:\n${blocks}\n\nQuestion: ${question}\n\nAnswer with citations:`;
  const genStart = Date.now();
  let answer = "";
  try {
    answer = await deps.llm.generate(prompt, { system: SYSTEM_PROMPT, maxTokens: deps.maxAnswerTokens });
  } catch (err) {
    answer = `The local model could not be reached (${err instanceof Error ? err.message : String(err)}).`;
  }
  latency.generation_ms = Date.now() - genStart;

  const { text: validated, citations } = validateCitations(answer, evidence.length);
  const sources: AnswerSource[] = citations.map((idx) => toSource(deps, evidence[idx]!));
  const insufficient = citations.length === 0;

  latency.total_ms = Date.now() - started;
  return {
    answer: insufficient
      ? `${validated}\n\n(No valid citations were produced, so this answer is treated as insufficient.)`
      : validated,
    sources,
    person_id: personId,
    person_name: personId ? deps.store.getPerson(personId)?.display_name ?? null : null,
    insufficient,
    evidence_count: evidence.length,
    intent,
    latency_ms: latency,
  };
}

function answerFromArchive(
  deps: RagDeps,
  question: string,
  intent: Intent,
  personId: string | null
): Omit<AskResult, "latency_ms"> | null {
  if (intent === "top_people") {
    const rows = deps.store.interactionTotals(10, null);
    if (rows.length === 0) return null;
    const lines = rows.map(([id, count], i) => {
      const person = deps.store.getPerson(id);
      return `${i + 1}. ${person?.display_name ?? id} — ${count} interactions`;
    });
    return {
      answer: `Who you talk to most:\n${lines.join("\n")}`,
      sources: rows.map(([id, count]) => personSource(deps, id, count)),
      person_id: null, person_name: null, insufficient: false,
      evidence_count: rows.length, intent,
    };
  }
  if (intent === "people_count") {
    const days = windowDays(question);
    const n = deps.store.distinctPeople(sinceIso(days));
    return {
      answer: days
        ? `You talked to ${n} people in the last ${days} days.`
        : `You have talked to ${n} people.`,
      sources: [], person_id: null, person_name: null, insufficient: false, evidence_count: 0, intent,
    };
  }
  if (intent === "activity_count") {
    const days = windowDays(question) ?? 7;
    const totals = deps.store.activityTotals(sinceIso(days), personId ?? undefined);
    const parts = Object.entries(totals.by_kind).map(([kind, n]) => `${n} ${kind}${n === 1 ? "" : "s"}`);
    return {
      answer: `${totals.total} interactions in the last ${days} days${parts.length ? ` (${parts.join(", ")})` : ""}.`,
      sources: [], person_id: personId, person_name: null, insufficient: false, evidence_count: 0, intent,
    };
  }
  if (intent === "recent_people") {
    const days = windowDays(question) ?? 7;
    const rows = deps.store.recentPeople(sinceIso(days), 10);
    if (rows.length === 0) {
      return {
        answer: `You did not talk to anyone in the last ${days} days.`,
        sources: [], person_id: null, person_name: null, insufficient: false, evidence_count: 0, intent,
      };
    }
    const lines = rows.map(([id, count], i) => {
      const person = deps.store.getPerson(id);
      return `${i + 1}. ${person?.display_name ?? id} — ${count} interactions`;
    });
    return {
      answer: `People you talked to in the last ${days} days:\n${lines.join("\n")}`,
      sources: rows.map(([id, count]) => personSource(deps, id, count)),
      person_id: null, person_name: null, insufficient: false, evidence_count: rows.length, intent,
    };
  }
  if (intent === "last_interaction") {
    const last = deps.store.lastInteraction(personId ?? undefined);
    if (!last) return null;
    const name = personId ? deps.store.getPerson(personId)?.display_name : "you";
    return {
      answer: `The last recorded interaction${personId ? ` with ${name}` : ""} was on ${new Date(last).toLocaleString()}.`,
      sources: [], person_id: personId, person_name: name ?? null, insufficient: false, evidence_count: 0, intent,
    };
  }
  if (intent === "top_topics") {
    const topics = topicTotals(deps.store, 10);
    if (topics.length === 0) return null;
    const lines = topics.map(([topic, count], i) => `${i + 1}. ${topic} — ${count}`);
    return {
      answer: `Your top topics:\n${lines.join("\n")}`,
      sources: [], person_id: null, person_name: null, insufficient: false, evidence_count: topics.length, intent,
    };
  }
  return null;
}

function personSource(deps: RagDeps, personId: string, count: number): AnswerSource {
  const person = deps.store.getPerson(personId);
  return {
    label: person?.display_name ?? personId,
    memory_id: null,
    person_id: personId,
    person_name: person?.display_name ?? personId,
    source: "archive",
    kind: "aggregate",
    origin: "imported",
    occurred_at: null,
    citation: `${count} interactions`,
    snippet: person?.display_name ?? personId,
  };
}

function toSource(deps: RagDeps, memory: Memory): AnswerSource {
  const person = memory.person_id ? deps.store.getPerson(memory.person_id) : null;
  return {
    label: memory.citation || memory.kind,
    memory_id: memory.id,
    person_id: memory.person_id,
    person_name: person?.display_name ?? null,
    source: memory.source,
    kind: memory.kind,
    origin: memory.origin,
    occurred_at: memory.occurred_at,
    citation: memory.citation,
    snippet: memory.text.slice(0, 300),
  };
}

/**
 * Keep only citations that point at evidence actually supplied.
 *
 * A model that invents [S9] when five records were given has its citation
 * stripped; an answer with no valid citation is treated as insufficient rather
 * than presented as fact.
 */
export function validateCitations(answer: string, evidenceCount: number): { text: string; citations: number[] } {
  const citations: number[] = [];
  const text = answer.replace(/\[S(\d+)\]/gi, (match, num: string) => {
    const idx = Number(num) - 1;
    if (idx >= 0 && idx < evidenceCount) {
      if (!citations.includes(idx)) citations.push(idx);
      return match;
    }
    return "";
  });
  return { text: text.replace(/\s{2,}/g, " ").trim(), citations };
}

export async function prepareBrief(
  deps: RagDeps,
  personId: string,
  meetingLine = ""
): Promise<AskResult> {
  const person = deps.store.getPerson(personId);
  if (!person) {
    return {
      answer: "That person is not in your archive.", sources: [], person_id: personId,
      person_name: null, insufficient: true, evidence_count: 0, intent: "prepare", latency_ms: {},
    };
  }
  const question = `Summarize what I should remember before meeting ${person.display_name}. ${meetingLine}`.trim();
  const result = await ask(deps, question, personId);
  return { ...result, intent: "prepare" };
}

export function sourceOf(memory: Memory): SourceType {
  return memory.source;
}
