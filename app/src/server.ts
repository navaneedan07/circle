/**
 * HTTP API for the renderer.
 *
 * The route surface matches the existing frontend's typed client (`api.ts`),
 * so the React UI is reused unchanged. Two additions are specific to this
 * build: `/api/setup/*` for the Ollama prerequisite installer, and the folder
 * endpoints now operate on a single user folder.
 */
import express, { type Request, type Response } from "express";
import fs from "node:fs";
import path from "node:path";
import { broker } from "./events.js";
import type { AppContext } from "./context.js";
import { PrerequisiteInstaller } from "./setup.js";
import { ask, prepareBrief, hybridRetrieve, type AnswerSource } from "./ai/rag.js";
import { computeProfile, refreshProfile } from "./relationship.js";
import { nowIso } from "./domain.js";
import { sanitizeFilename } from "./security.js";
import { mediaFilenameKey } from "./media.js";

export interface ServerOptions {
  ctx: AppContext;
  frontendDir?: string;
  port: number;
}

export function createServer(options: ServerOptions): { app: express.Express; installer: PrerequisiteInstaller } {
  const { ctx } = options;
  const app = express();
  app.use(express.json({ limit: "5mb" }));

  const installer = new PrerequisiteInstaller(ctx.settings.ollamaUrl, ctx.settings.ollamaModel, ctx.settings.embeddingModel);

  // ---------------------------------- health ----------------------------------
  app.get("/api/health", (_req, res) => {
    res.json({ status: ctx.started ? "ok" : "starting", health: ctx.health, started: ctx.started, offline_ready: Boolean((ctx.health.llm as { available?: boolean })?.available) });
  });

  app.get("/api/auth/status", (_req, res) => {
    const llm = (ctx.health.llm ?? {}) as { available?: boolean; installed_models?: string[] };
    res.json({
      access_key_required: false,
      key_accepted: true,
      remote_allowed: false,
      llm_available: Boolean(llm.available),
      model: ctx.settings.ollamaModel,
      model_installed: Boolean(llm.available),
      installed_models: llm.installed_models ?? [],
      embedding_model: ctx.settings.embeddingModel,
      embedding_installed: Boolean((ctx.health.embeddings as { available?: boolean })?.available),
      ollama_reachable: Boolean(llm.installed_models?.length) || Boolean(llm.available),
    });
  });

  // ---------------------------------- setup -----------------------------------
  app.get("/api/setup/status", (_req, res) => res.json(installer.status()));

  app.post("/api/setup/install", async (_req, res) => {
    try {
      const status = await installer.install();
      ctx.health.llm = ctx.llm.status();
      ctx.health.embeddings = ctx.embedder.status();
      res.json({ ok: true, status });
    } catch (err) {
      res.status(500).json({ ok: false, error: err instanceof Error ? err.message : String(err) });
    }
  });

  app.get("/api/setup/events", (req, res) => {
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
    const send = (data: unknown) => res.write(`data: ${JSON.stringify(data)}\n\n`);
    const onProgress = (p: unknown) => send(p);
    installer.on("progress", onProgress);
    req.on("close", () => installer.off("progress", onProgress));
  });

  app.post("/api/setup/pull-model", async (_req, res) => {
    try {
      await installer.pullModel(ctx.settings.ollamaModel);
      ctx.health.llm = ctx.llm.status();
      res.json({ ok: true, model: ctx.settings.ollamaModel });
    } catch (err) {
      res.status(500).json({ ok: false, error: err instanceof Error ? err.message : String(err) });
    }
  });

  // ---------------------------------- realtime ---------------------------------
  app.get("/api/events", (req, res) => {
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
    res.write(": connected\n\n");
    const unsubscribe = broker.subscribe((event) => res.write(`event: ${event.type}\ndata: ${JSON.stringify(event.payload)}\n\n`));
    const keepAlive = setInterval(() => res.write(": ping\n\n"), 20000);
    req.on("close", () => {
      clearInterval(keepAlive);
      unsubscribe();
    });
  });

  app.get("/api/sync/status", (_req, res) => res.json(ctx.syncStatus()));

  // ---------------------------------- people -----------------------------------
  app.get("/api/people", (req, res) => {
    const limit = Math.min(Number(req.query.limit ?? 50), 500);
    const offset = Number(req.query.offset ?? 0);
    const q = String(req.query.q ?? "").trim().toLowerCase();
    const sort = String(req.query.sort ?? "recent");
    let people = ctx.store.listPeople(100000, 0).map((p) => {
      const profile = ctx.store.getProfile(p.id);
      return {
        id: p.id,
        display_name: p.display_name,
        aliases: p.aliases,
        identities: p.identities.map((i) => ({ kind: i.kind, value: i.value, source: i.source, label: i.label ?? "" })),
        status: profile?.status ?? null,
        status_reason: profile?.status_reason ?? "",
        interaction_count: profile?.interaction_count ?? 0,
        interactions_14d: profile?.interactions_14d ?? 0,
        last_interaction_at: profile?.last_interaction_at ?? null,
        updated_at: p.updated_at,
      };
    });
    if (q) people = people.filter((p) => p.display_name.toLowerCase().includes(q) || p.aliases.some((a) => a.toLowerCase().includes(q)));
    if (sort === "name") people.sort((a, b) => a.display_name.localeCompare(b.display_name));
    else people.sort((a, b) => (b.interaction_count || 0) - (a.interaction_count || 0));
    const total = people.length;
    const top = people.slice(0, 5).map((p) => ({ id: p.id, display_name: p.display_name, interaction_count: p.interaction_count }));
    res.json({ people: people.slice(offset, offset + limit), total, offset, limit, sort, top });
  });

  app.get("/api/people/:id", (req, res) => {
    const person = ctx.store.getPerson(req.params.id);
    if (!person) return res.status(404).json({ detail: "person not found" });
    const profile = ctx.store.getProfile(person.id) ?? computeProfile(ctx.store, person.id);
    res.json({
      person,
      profile,
      conversations: ctx.store.listConversationsForPerson(person.id),
      counts: {
        messages: ctx.store.countMessagesForPerson(person.id),
        emails: ctx.store.countEmailsForPerson(person.id),
        notes: ctx.store.countNotesForPerson(person.id),
        media: ctx.store.countMediaForPerson(person.id),
      },
    });
  });

  app.get("/api/people/:id/timeline", (req, res) => {
    const personId = req.params.id;
    const limit = Math.min(Number(req.query.limit ?? 150), 500);
    const entries = buildTimeline(ctx, personId).slice(0, limit);
    res.json({ timeline: entries });
  });

  app.get("/api/people/:id/memories", (req, res) => {
    res.json({ memories: ctx.store.memoriesForPerson(req.params.id, 500) });
  });

  app.get("/api/people/:id/suggestions", (req, res) => {
    const person = ctx.store.getPerson(req.params.id);
    const prompts = [
      "What did we last talk about?",
      "What should I remember before meeting them?",
      "Any upcoming plans?",
    ];
    if (person) prompts.push(`What did we discuss with ${person.display_name}?`);
    res.json({ prompts });
  });

  app.get("/api/people/:id/media", (req, res) => {
    res.json({ media: ctx.store.listMediaForPerson(req.params.id, 200).map(toMediaDto) });
  });

  // ---------------------------------- timeline helper --------------------------
  function buildTimeline(ctx: AppContext, personId: string): Record<string, unknown>[] {
    const entries: Record<string, unknown>[] = [];
    for (const m of ctx.store.listMessagesForPerson(personId, 300)) {
      entries.push({
        type: "message", id: m.id, at: m.sent_at, title: m.sender_label,
        body: m.content, source: m.source, origin: m.origin,
        media: ctx.store.listMediaForMessage(m.id).map(toMediaDto),
      });
    }
    for (const e of ctx.store.listEmailsForPerson(personId, 100)) {
      entries.push({ type: "email", id: e.id, at: e.sent_at, title: e.subject || "(no subject)", body: e.body, source: "email", origin: e.origin });
    }
    for (const ev of ctx.store.listCalendarForPerson(personId, 100)) {
      entries.push({ type: "calendar", id: ev.id, at: ev.starts_at, title: ev.title, body: ev.description, source: "calendar", origin: ev.origin });
    }
    for (const n of ctx.store.listNotesForPerson(personId, 100)) {
      entries.push({ type: "note", id: n.id, at: n.noted_at, title: n.title, body: n.body, source: "notes", origin: n.origin });
    }
    return entries.sort((a, b) => String(b.at ?? "").localeCompare(String(a.at ?? "")));
  }

  // ---------------------------------- search / ask -----------------------------
  app.post("/api/search", async (req, res) => {
    const { query = "", person_id = null, limit = 50 } = req.body ?? {};
    try {
      const results = await hybridRetrieve(ctx.ragDeps(), String(query), {
        personId: person_id,
        k: Math.min(Number(limit), 100),
      });
      res.json({
        results: results.map((m) => ({
          id: m.id,
          kind: m.kind,
          source: m.source,
          occurred_at: m.occurred_at,
          text: m.text,
          snippet: m.text.slice(0, 240),
          person_id: m.person_id,
          score_label: m.citation,
        })),
        count: results.length,
      });
    } catch (err) {
      res.status(502).json({ detail: err instanceof Error ? err.message : "search failed" });
    }
  });

  app.post("/api/ask", async (req, res) => {
    const { question = "", person_id = null } = req.body ?? {};
    if (!String(question).trim()) return res.status(400).json({ detail: "question is required" });
    try {
      res.json(await ask(ctx.ragDeps(), String(question), person_id));
    } catch (err) {
      res.status(502).json({ detail: err instanceof Error ? err.message : "ask failed" });
    }
  });

  app.post("/api/ask/stream", async (req, res) => {
    const { question = "", person_id = null } = req.body ?? {};
    let result;
    try {
      result = await ask(ctx.ragDeps(), String(question), person_id);
    } catch (err) {
      res.status(502).json({ detail: err instanceof Error ? err.message : "ask failed" });
      return;
    }
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" });
    res.write(`data: ${JSON.stringify({ type: "answer", answer: result.answer })}\n\n`);
    res.write(`data: ${JSON.stringify({ type: "sources", sources: result.sources, insufficient: result.insufficient, intent: result.intent })}\n\n`);
    res.end();
  });

  app.post("/api/people/prepare", async (req, res) => {
    const { person_id = "", meeting_line = "" } = req.body ?? {};
    try {
      res.json(await prepareBrief(ctx.ragDeps(), String(person_id), String(meeting_line)));
    } catch (err) {
      res.status(502).json({ detail: err instanceof Error ? err.message : "prepare failed" });
    }
  });

  app.get("/api/relationship/:id", (req, res) => {
    const profile = ctx.store.getProfile(req.params.id) ?? computeProfile(ctx.store, req.params.id);
    res.json({
      profile,
      person_name: ctx.store.getPerson(req.params.id)?.display_name ?? "",
      recent_events: ctx.store.listRelationshipEvents(req.params.id, 30),
    });
  });

  // ---------------------------------- memories / media -------------------------
  app.get("/api/memories/:id", (req, res) => {
    const memory = ctx.store.getMemory(req.params.id);
    if (!memory) return res.status(404).json({ detail: "memory not found" });
    const record = memory.record_id ? ctx.store.getMessage(memory.record_id) : null;
    res.json({
      memory,
      record: record ?? {},
      media: record ? ctx.store.listMediaForMessage(record.id).map(toMediaDto) : [],
      person_name: memory.person_id ? ctx.store.getPerson(memory.person_id)?.display_name ?? null : null,
    });
  });

  app.get("/api/media", (_req, res) => res.json({ media: ctx.store.listMedia(200).map(toMediaDto) }));

  app.get("/api/media/:id/info", (req, res) => {
    const media = ctx.store.getMedia(req.params.id);
    if (!media) return res.status(404).json({ detail: "media not found" });
    res.json({ media: toMediaDto(media) });
  });

  app.get("/api/media/:id", (req, res) => {
    const media = ctx.store.getMedia(req.params.id);
    if (!media) return res.status(404).json({ detail: "media not found" });
    // The path is resolved from the store and checked to stay inside it, so a
    // request can never read an arbitrary file.
    const resolved = path.resolve(media.stored_path);
    const root = path.resolve(ctx.paths.mediaDir);
    if (!resolved.startsWith(root) || !fs.existsSync(resolved)) {
      return res.status(404).json({ detail: "media file missing" });
    }
    res.sendFile(resolved);
  });

  app.post("/api/media/:id/describe", (req, res) => {
    const media = ctx.store.getMedia(req.params.id);
    if (!media) return res.status(404).json({ detail: "media not found" });
    res.status(501).json({ ok: false, detail: "image captioning is not enabled in this build", media: toMediaDto(media) });
  });

  // ---------------------------------- imports ----------------------------------
  app.get("/api/imports", (_req, res) => {
    res.json({ jobs: ctx.store.listJobs(100), counts: ctx.store.countJobsByStatus() });
  });

  app.post("/api/imports/rescan", (_req, res) => {
    const queued = ctx.watcher.rescan();
    res.json({ queued, root: ctx.settings.watchFolder });
  });

  // ---------------------------------- watch folders ----------------------------
  app.get("/api/watch/folders", (_req, res) => {
    const folder = ctx.settings.watchFolder;
    res.json({
      folders: folder
        ? [{ path: folder, exists: fs.existsSync(folder), kind: "local", cloud: false, provider_installed: null, is_drive_letter: /^[a-zA-Z]:/.test(folder), primary: true, managed: false, watched: ctx.watcher.stats.running }]
        : [],
      cloud_drive: { installed: false, platform: process.platform, roots: [] },
      managed_root: false,
      archives_files: false,
    });
  });

  app.post("/api/watch/folders", async (req, res) => {
    const folder = String(req.body?.path ?? "").trim();
    if (!folder || !path.isAbsolute(folder)) return res.status(400).json({ detail: "use a full path to an existing folder" });
    if (!fs.existsSync(folder) || !fs.statSync(folder).isDirectory()) return res.status(400).json({ detail: "that folder does not exist" });
    ctx.setWatchFolder(folder);
    await ctx.watcher.restart();
    ctx.watcher.rescan();
    res.status(201).json({ folders: [{ path: folder, exists: true, primary: true, managed: false, watched: true }] });
  });

  app.delete("/api/watch/folders", async (_req, res) => {
    ctx.setWatchFolder("");
    await ctx.watcher.stop();
    res.json({ folders: [] });
  });

  // ---------------------------------- settings / bootstrap ---------------------
  app.get("/api/settings", (_req, res) => {
    res.json({
      storage_backend: "sqlite",
      watch_folder: ctx.settings.watchFolder,
      ollama_model: ctx.settings.ollamaModel,
      embedding_model: ctx.settings.embeddingModel,
      data_dir: ctx.paths.dataDir,
      db_path: ctx.paths.dbPath,
    });
  });

  app.post("/api/settings", (req, res) => {
    const patch: Record<string, unknown> = {};
    const body = req.body ?? {};
    if (typeof body.ollama_model === "string") patch.ollamaModel = body.ollama_model;
    if (typeof body.embedding_model === "string") patch.embeddingModel = body.embedding_model;
    ctx.updateSettings(patch);
    res.json({ ok: true });
  });

  app.get("/api/bootstrap", (_req, res) => {
    const needsFolder = !ctx.settings.watchFolder;
    res.json({
      first_run: !ctx.settings.onboarded || needsFolder,
      needs_folder: needsFolder,
      layout: [],
      roots: ctx.settings.watchFolder ? [ctx.settings.watchFolder] : [],
      health: ctx.health,
      cloud_drive: { installed: false, platform: process.platform, roots: [] },
    });
  });

  app.post("/api/bootstrap", async (req, res) => {
    const folder = String(req.body?.import_root ?? "").trim();
    if (!folder) return res.status(400).json({ detail: "choose a folder for Circle to read" });
    if (!path.isAbsolute(folder)) return res.status(400).json({ detail: "use a full path" });
    if (!fs.existsSync(folder) || !fs.statSync(folder).isDirectory()) {
      return res.status(400).json({ detail: `that folder does not exist: ${folder}` });
    }
    // The one folder the user chose. Circle creates nothing inside it.
    ctx.setWatchFolder(path.resolve(folder));
    ctx.updateSettings({ onboarded: true });
    await ctx.watcher.restart();
    ctx.watcher.rescan();
    res.json({ ok: true, root: ctx.settings.watchFolder, roots: [ctx.settings.watchFolder], first_run: false });
  });

  // ---------------------------------- identity / notes / voice -----------------
  app.get("/api/identity/suggestions", (_req, res) => res.json({ suggestions: ctx.store.listIdentitySuggestions("pending") }));

  app.post("/api/identity/suggestion", (req, res) => {
    const { suggestion_id = "", merge = false } = req.body ?? {};
    const suggestion = ctx.store.getIdentitySuggestion(String(suggestion_id));
    if (!suggestion) return res.status(404).json({ detail: "suggestion not found" });
    if (merge) {
      ctx.resolver.mergePeople(String(suggestion.person_a_id), String(suggestion.person_b_id));
    }
    ctx.store.setIdentitySuggestionStatus(String(suggestion_id), merge ? "accepted" : "rejected");
    res.json({ ok: true });
  });

  app.post("/api/identity/merge", (req, res) => {
    const { keep_id = "", remove_id = "" } = req.body ?? {};
    const moved = ctx.resolver.mergePeople(String(keep_id), String(remove_id));
    res.json({ ok: true, moved });
  });

  app.get("/api/notes", (req, res) => {
    const personId = req.query.person_id ? String(req.query.person_id) : undefined;
    res.json({ notes: personId ? ctx.store.listNotesForPerson(personId, 200) : ctx.store.listNotesRecent(200) });
  });

  app.post("/api/notes", (req, res) => {
    const { person_id = null, title = "", body = "" } = req.body ?? {};
    const id = `note-${Date.now().toString(36)}`;
    ctx.store.insertNoteIfNew({
      id, person_id, title: String(title).slice(0, 200), body: String(body).slice(0, 20000),
      noted_at: nowIso(), source: "notes", origin: "local", external_id: id, imported_at: nowIso(),
    });
    if (person_id) refreshProfile(ctx.store, person_id);
    res.status(201).json({ note: ctx.store.listNotesForPerson(String(person_id), 1)[0] ?? null });
  });

  app.get("/api/voice", (req, res) => {
    const personId = req.query.person_id ? String(req.query.person_id) : undefined;
    res.json({ recordings: personId ? ctx.store.listVoiceForPerson(personId, 100) : ctx.store.listVoiceRecent(100) });
  });

  app.get("/api/voice/:id/audio", (_req, res) => res.status(404).json({ detail: "voice audio is not available" }));

  app.post("/api/voice/associate", (req, res) => {
    const { voice_id = "", person_id = "" } = req.body ?? {};
    const rec = ctx.store.listVoiceRecent(1000).find((v) => v.id === voice_id);
    if (!rec) return res.status(404).json({ detail: "recording not found" });
    rec.person_id = person_id;
    ctx.store.updateVoice(rec);
    res.json({ ok: true });
  });

  // ---------------------------------- ai metrics -------------------------------
  app.get("/api/ai/metrics", (_req, res) => {
    res.json({ llm: ctx.llm.lastMetrics, embeddings: ctx.embedder.lastMetrics, advice: [] });
  });

  app.post("/api/ai/warmup", async (_req, res) => {
    const warm = await ctx.llm.warmup();
    ctx.health.llm = ctx.llm.status();
    res.json(warm);
  });

  app.post("/api/tts", (_req, res) => res.status(501).json({ detail: "voice output is not enabled" }));

  app.post("/api/demo/generate", (_req, res) => res.json({ generated: false, files: [], detail: "demo data is not part of this build" }));
  app.get("/api/demo/status", (_req, res) => res.json({ demo: false }));

  // ---------------------------------- static frontend --------------------------
  if (options.frontendDir && fs.existsSync(path.join(options.frontendDir, "index.html"))) {
    const dist = options.frontendDir;
    app.use(express.static(dist));
    // SPA fallback: client routes are not files on disk.
    app.get("*", (req: Request, res: Response) => {
      if (req.path.startsWith("/api/")) return res.status(404).json({ detail: "Not Found" });
      res.sendFile(path.join(dist, "index.html"));
    });
  } else {
    // Never leave the window on a bare "Cannot GET /" -- say what is missing.
    app.get("*", (_req: Request, res: Response) => {
      res.status(503).type("text/plain").send(
        "Circle's interface files were not found.\n\n" +
          `Looked in: ${options.frontendDir ?? "(not configured)"}\n` +
          "Build them with:  cd frontend && npm install && npm run build\n"
      );
    });
  }

  return { app, installer };
}

function toMediaDto(media: {
  id: string; filename: string; kind: string; mime_type: string; size_bytes: number;
  duration_seconds: number | null; transcript: string; caption: string; message_id: string | null;
  person_id: string | null; occurred_at: string | null; status: string; source: string; origin: string;
}): Record<string, unknown> {
  return {
    id: media.id,
    filename: media.filename,
    kind: media.kind,
    mime_type: media.mime_type,
    size_bytes: media.size_bytes,
    duration_seconds: media.duration_seconds,
    transcript: media.transcript,
    caption: media.caption,
    message_id: media.message_id,
    person_id: media.person_id,
    occurred_at: media.occurred_at,
    status: media.status,
    source: media.source,
    origin: media.origin,
    url: `/api/media/${media.id}`,
  };
}

export function mediaKey(filename: string): string {
  return mediaFilenameKey(filename);
}

export function safeName(name: string): string {
  return sanitizeFilename(name);
}

export type { AnswerSource };
