/* Typed API client for Circle. All calls hit the local backend only. */

export type Person = {
  id: string;
  display_name: string;
  aliases: string[];
  identities: { kind: string; value: string; source: string; label: string }[];
  status?: string | null;
  status_reason?: string;
  interaction_count?: number;
  interactions_14d?: number;
  last_interaction_at?: string | null;
  updated_at?: string;
};

export type TopicStat = { topic: string; count: number; last_seen?: string | null };

export type Profile = {
  person_id: string;
  status: string;
  status_reason: string;
  interaction_count: number;
  interactions_14d: number;
  interactions_60d: number;
  last_interaction_at?: string | null;
  source_breakdown: Record<string, number>;
  topics: TopicStat[];
  active_topics: TopicStat[];
  upcoming_events: {
    id: string;
    title: string;
    starts_at?: string | null;
    location?: string;
  }[];
  summary: string;
  summary_sources: string[];
  updated_at: string;
};

export type MediaAttachment = {
  id: string;
  filename: string;
  kind: "image" | "video" | "voice" | "audio" | "sticker" | "document" | "other";
  mime_type: string;
  size_bytes: number;
  duration_seconds?: number | null;
  transcript?: string;
  caption?: string;
  message_id?: string | null;
  person_id?: string | null;
  occurred_at?: string | null;
  status: string;
  source: string;
  origin: string;
  url: string | null;
};

export type TimelineEntry = {
  type: "message" | "email" | "calendar" | "note" | "voice";
  id: string;
  at?: string | null;
  title: string;
  body: string;
  source: string;
  origin: string;
  media?: MediaAttachment[];
};

export type Source = {
  label: string;
  memory_id?: string | null;
  /** Set for aggregate sources that point at a person rather than a record. */
  person_id?: string | null;
  person_name?: string | null;
  source: string;
  kind: string;
  origin: string;
  occurred_at?: string | null;
  citation: string;
  snippet: string;
};

export type AskResult = {
  answer: string;
  sources: Source[];
  person_id?: string | null;
  person_name?: string | null;
  insufficient: boolean;
  evidence_count: number;
  intent: string;
  latency_ms: Record<string, number>;
};

export type ImportJob = {
  id?: string | null;
  filename: string;
  source?: string | null;
  status: string;
  records_imported: number;
  records_skipped: number;
  error?: string;
  created_at?: string;
  completed_at?: string | null;
};

export type Health = {
  status: string;
  offline_ready: boolean;
  health: {
    database: { connected: boolean; engine?: string; detail?: string };
    llm: { available: boolean; model: string; detail: string; installed_models?: string[] };
    embeddings: { available: boolean; model: string; detail: string };
    stt: { available: boolean; provider: string; detail: string };
    tts: { enabled: boolean; detail: string };
    watcher: { running: boolean; root?: string; detail?: string };
    imports?: Record<string, string>;
  };
};

export type SyncStatus = {
  watcher_running: boolean;
  root: string;
  roots?: string[];
  folder_count?: number;
  files: { processed: number; failed: number; skipped: number };
  jobs: Record<string, number>;
  last_event_at?: number | null;
  queue_size: number;
  counts: { people: number; messages: number; memories: number };
};

/** A folder Circle reads from. Cloud folders belong to the user: read only. */
export type WatchFolder = {
  path: string;
  exists: boolean;
  kind: "local" | "google_drive" | "onedrive" | "icloud" | "dropbox";
  cloud: boolean;
  provider_installed?: boolean | null;
  is_drive_letter: boolean;
  primary: boolean;
  /** True only for Circle's own folder; managed folders get reorganized. */
  managed: boolean;
  watched: boolean;
};

export type SetupStatus = {
  ollamaInstalled: boolean;
  ollamaRunning: boolean;
  ollamaPath: string;
  models: string[];
  requiredModels: string[];
  missingModels: string[];
  ready: boolean;
  detail: string;
  platform: string;
};

export type SetupProgress = {
  stage: "check" | "download" | "install" | "pull" | "start" | "done" | "error";
  message: string;
  percent?: number;
};

export type CloudDriveInfo = {
  installed: boolean;
  platform: string;
  roots: string[];
};

export type TopPerson = {
  id: string;
  display_name: string;
  interaction_count: number;
};

/** One page of people, plus the totals the ledger needs to paginate. */
export type PeoplePage = {
  people: Person[];
  total: number;
  offset: number;
  limit: number;
  sort: string;
  top: TopPerson[];
};

/** One side of a possible match: who Circle already has, or who it would fold in. */
export type EvidenceSide = {
  id?: string | null;
  name: string;
  aliases?: string[];
  interactions?: number | null;
  last_seen?: string | null;
};

/**
 * Where a possible match actually disagrees, quoted from the archive so the
 * conflict can be read instead of asserted.
 */
export type SuggestionEvidence = {
  kind: "duplicate" | "name_match";
  existing: EvidenceSide;
  conflicting: EvidenceSide;
  counts: {
    conflicting_messages: number;
    existing_messages: number;
  };
  samples: {
    text: string;
    at?: string | null;
    source: string;
    conversation: string;
    sender_label: string;
  }[];
};

export type IdentitySuggestion = {
  id: string;
  label: string;
  source?: string;
  reason?: string;
  person_a_id?: string | null;
  person_b_id?: string | null;
  evidence?: SuggestionEvidence;
};

export type SearchResult = {
  id: string;
  kind: string;
  source: string;
  occurred_at?: string | null;
  text: string;
  snippet: string;
  person_id?: string | null;
  score_label: string;
};

/* ---------------------------------------------------------------------------
 * Where the backend lives.

 * Normally the UI is served by the backend itself, so every path is relative
 * and no configuration is needed. The address is only remembered so that a
 * browser which somehow lost track of its own local backend can be pointed
 * back at it.
 * ------------------------------------------------------------------------- */

const CONNECTION_KEY = "circle-connection";
export const KEY_HEADER = "X-Circle-Key";

export type Connection = { baseUrl: string; accessKey: string };

let connection: Connection = { baseUrl: "", accessKey: "" };

export function loadConnection(): Connection {
  try {
    const raw = localStorage.getItem(CONNECTION_KEY);
    if (raw) {
      const p = JSON.parse(raw) as Partial<Connection>;
      connection = {
        baseUrl: typeof p.baseUrl === "string" ? p.baseUrl.replace(/\/+$/, "") : "",
        accessKey: typeof p.accessKey === "string" ? p.accessKey : "",
      };
    }
  } catch {
    /* private mode or corrupt value: fall back to same-origin */
  }
  return connection;
}

export function saveConnection(next: Partial<Connection>): Connection {
  connection = {
    baseUrl: (next.baseUrl ?? connection.baseUrl).replace(/\/+$/, ""),
    accessKey: next.accessKey ?? connection.accessKey,
  };
  try {
    localStorage.setItem(CONNECTION_KEY, JSON.stringify(connection));
  } catch {
    /* private mode: still usable for this session */
  }
  // Other modules read this directly, so tell them to re-read.
  window.dispatchEvent(new Event("circle:connection"));
  return connection;
}

export function getConnection(): Connection {
  return connection;
}

/** Full URL for an API path, honouring a configured backend address. */
export function apiUrl(path: string): string {
  return `${connection.baseUrl}${path}`;
}

/** Headers every API call must carry. */
export function authHeaders(): Record<string, string> {
  return connection.accessKey
    ? { "X-Circle-Key": connection.accessKey }
    : {};
}

loadConnection();

/** Raised when the backend rejects us, so the UI can show the Connect screen
 *  instead of a generic failure. */
export class AuthError extends Error {
  readonly needsKey: boolean;
  constructor(message: string, needsKey: boolean) {
    super(message);
    this.name = "AuthError";
    this.needsKey = needsKey;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(apiUrl(path), {
      headers: { "Content-Type": "application/json", ...authHeaders() },
      ...init,
    });
  } catch {
    // fetch only rejects on network failure: the app is not running, the
    // address is wrong, or the port is blocked.
    throw new AuthError(
      connection.baseUrl
        ? `Cannot reach Circle at ${connection.baseUrl}. Is the app running?`
        : "Cannot reach the Circle backend.",
      false
    );
  }
  // A static host rewrites unknown paths to index.html, so a wrong or missing
  // address returns 200 with HTML rather than a JSON error. Reading that as
  // JSON would surface a raw parser message; say what is actually wrong.
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("text/html")) {
    throw new AuthError(
      connection.baseUrl
        ? `Circle at ${connection.baseUrl} did not answer. Is that address right?`
        : "This page has no Circle backend behind it. Enter your Circle's address above.",
      false
    );
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch {
      /* ignore */
    }
    if (res.status === 401 || res.status === 403) {
      throw new AuthError(detail || "This Circle needs an access key.", true);
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const api = {
  authStatus: () =>
    request<{
      access_key_required: boolean;
      /** Whether THIS caller's key was accepted. A 200 from this endpoint
       *  does not imply it, because the endpoint itself is public. */
      key_accepted: boolean;
      remote_allowed: boolean;
      llm_available: boolean;
      model: string;
      model_installed: boolean;
      installed_models: string[];
      embedding_model: string;
      embedding_installed: boolean;
      ollama_reachable: boolean;
    }>("/api/auth/status"),
  pullModel: () =>
    request<{ ok: boolean; model: string; detail?: string; error?: string }>(
      "/api/setup/pull-model",
      { method: "POST" }
    ),
  setupStatus: () => request<SetupStatus>("/api/setup/status"),
  terms: () =>
    request<{ accepted: boolean; version: string; accepted_at: string }>(
      "/api/terms"
    ),
  acceptTerms: () =>
    request<{ ok: boolean; version: string; accepted_at: string }>(
      "/api/terms/accept",
      { method: "POST", body: JSON.stringify({ accepted: true }) }
    ),
  setupInstall: () =>
    request<{ ok: boolean; status?: SetupStatus; error?: string }>("/api/setup/install", {
      method: "POST",
    }),
  health: () => request<Health>("/api/health"),
  aiMetrics: () => request<Record<string, unknown>>("/api/ai/metrics"),
  warmup: () => request<Record<string, unknown>>("/api/ai/warmup", { method: "POST" }),
  syncStatus: () => request<SyncStatus>("/api/sync/status"),
  people: (params: {
    q?: string;
    status?: string;
    sort?: string;
    limit?: number;
    offset?: number;
  } = {}) => {
    const qs = new URLSearchParams();
    if (params.q) qs.set("q", params.q);
    if (params.status) qs.set("status", params.status);
    if (params.sort) qs.set("sort", params.sort);
    if (params.limit != null) qs.set("limit", String(params.limit));
    if (params.offset != null) qs.set("offset", String(params.offset));
    const s = qs.toString();
    return request<PeoplePage>(`/api/people${s ? `?${s}` : ""}`);
  },
  person: (id: string) =>
    request<{
      person: Person;
      profile: Profile | null;
      conversations: unknown[];
      counts: Record<string, number>;
    }>(`/api/people/${id}`),
  timeline: (id: string) =>
    request<{ timeline: TimelineEntry[] }>(`/api/people/${id}/timeline?limit=150`),
  relationship: (id: string) =>
    request<{
      profile: Profile;
      person_name: string;
      recent_events: Record<string, unknown>[];
    }>(`/api/relationship/${id}`),
  suggestions: (id: string) =>
    request<{ prompts: string[] }>(`/api/people/${id}/suggestions`),
  ask: (question: string, person_id?: string) =>
    request<AskResult>("/api/ask", {
      method: "POST",
      body: JSON.stringify({ question, person_id }),
    }),
  prepare: (person_id: string, meeting_line = "") =>
    request<AskResult>("/api/people/prepare", {
      method: "POST",
      body: JSON.stringify({ person_id, meeting_line }),
    }),
  search: (
    query: string,
    extra: { person_id?: string; source?: string; kind?: string; limit?: number } = {}
  ) =>
    request<{ results: SearchResult[]; count: number }>("/api/search", {
      method: "POST",
      body: JSON.stringify({ query, ...extra }),
    }),
  memory: (id: string) =>
    request<{
      memory: Record<string, unknown>;
      record: Record<string, unknown>;
      media: MediaAttachment[];
      person_name: string | null;
    }>(`/api/memories/${id}`),
  media: (id: string) =>
    request<{ media: MediaAttachment }>(`/api/media/${id}/info`),
  describeMedia: (id: string, prompt = "") =>
    request<{ ok: boolean; media: MediaAttachment }>(`/api/media/${id}/describe`, {
      method: "POST",
      body: JSON.stringify({ prompt }),
    }),
  imports: () => request<{ jobs: ImportJob[]; counts: Record<string, number> }>("/api/imports"),
  rescan: () => request<{ queued: number; root: string }>("/api/imports/rescan", { method: "POST" }),
  notes: (person_id?: string) =>
    request<{ notes: Record<string, unknown>[] }>(
      "/api/notes" + (person_id ? `?person_id=${person_id}` : "")
    ),
  createNote: (body: { person_id?: string; title: string; body: string }) =>
    request<{ note: Record<string, unknown> }>("/api/notes", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  watchFolders: () =>
    request<{
      folders: WatchFolder[];
      cloud_drive: CloudDriveInfo;
      managed_root: boolean;
      archives_files: boolean;
    }>("/api/watch/folders"),
  addWatchFolder: (path: string) =>
    request<{ folders: WatchFolder[] }>("/api/watch/folders", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  removeWatchFolder: (path: string) =>
    request<{ folders: WatchFolder[] }>(
      `/api/watch/folders?path=${encodeURIComponent(path)}`,
      { method: "DELETE" }
    ),
  settings: () => request<Record<string, unknown>>("/api/settings"),
  updateSettings: (payload: Record<string, unknown>) =>
    request<Record<string, unknown>>("/api/settings", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  bootstrap: () =>
    request<{
      first_run: boolean;
      /** True until the user has named at least one folder to read. */
      needs_folder: boolean;
      layout: string[];
      roots?: string[];
      health: unknown;
      cloud_drive?: CloudDriveInfo;
    }>("/api/bootstrap"),
  setBootstrap: (payload: {
    import_root?: string;
    watch_folders?: string[];
    mark_done?: boolean;
  }) =>
    request<{ ok: boolean; root: string; roots?: string[] }>("/api/bootstrap", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  generateDemo: () =>
    request<{ generated: boolean; files: string[] }>("/api/demo/generate", { method: "POST" }),
  demoStatus: () => request<{ demo: boolean }>("/api/demo/status"),
  voice: (person_id?: string) =>
    request<{ recordings: Record<string, unknown>[] }>(
      "/api/voice" + (person_id ? `?person_id=${person_id}` : "")
    ),
  identitySuggestions: () =>
    request<{ suggestions: IdentitySuggestion[] }>("/api/identity/suggestions"),
  resolveSuggestion: (suggestion_id: string, merge: boolean) =>
    request<Record<string, unknown>>("/api/identity/suggestion", {
      method: "POST",
      body: JSON.stringify({ suggestion_id, merge }),
    }),
  mergePeople: (keep_id: string, remove_id: string) =>
    request<Record<string, unknown>>("/api/identity/merge", {
      method: "POST",
      body: JSON.stringify({ keep_id, remove_id }),
    }),
};

export function fmtWhen(value?: string | null): string {
  if (!value) return "none";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  const now = Date.now();
  const diff = now - d.getTime();
  const mins = Math.round(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

export function fmtDate(value?: string | null): string {
  if (!value) return "none";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/* Muted earth-tone markers, not a bright spectrum. */
export const STATUS_DOTS: Record<string, string> = {
  "Very Active": "bg-moss",
  Active: "bg-moss",
  Occasional: "bg-ochre",
  "Low Activity": "bg-bronze",
  "No Recent Activity": "bg-muted",
};
