/**
 * Application context: one object wiring the store, watcher, pipeline and AI
 * providers, so the HTTP layer stays thin.
 */
import { broker } from "./events.js";
import {
  ensureOwnLayout,
  loadSettings,
  resolvePaths,
  saveSettings,
  type CirclePaths,
  type CircleSettings,
} from "./config.js";
import { Store } from "./store.js";
import { IdentityResolver } from "./identity.js";
import { IngestionPipeline } from "./pipeline.js";
import { FolderWatcher } from "./watcher.js";
import { refreshProfile } from "./relationship.js";
import { getEmbeddings, type EmbeddingProvider } from "./ai/embeddings.js";
import { getLlm, type LLMProvider } from "./ai/llm.js";
import type { RagDeps } from "./ai/rag.js";

export class AppContext {
  readonly paths: CirclePaths;
  settings: CircleSettings;
  readonly store: Store;
  readonly resolver: IdentityResolver;
  readonly pipeline: IngestionPipeline;
  readonly watcher: FolderWatcher;
  health: Record<string, unknown> = {};
  started = false;

  /**
   * The AI providers are rebuilt when the model or address changes.
   *
   * They used to be created once in the constructor, so choosing a different
   * model in Settings updated the saved setting and the label on screen while
   * the app kept answering with the old one until it was restarted.
   */
  private llmFor: LLMProvider | null = null;
  private llmKey = "";
  private embedderFor: EmbeddingProvider | null = null;
  private embedderKey = "";

  get llm(): LLMProvider {
    const key = `${this.settings.ollamaUrl}|${this.settings.ollamaModel}|${this.settings.llmMaxAnswerTokens}`;
    if (!this.llmFor || this.llmKey !== key) {
      this.llmFor = getLlm(this.settings.ollamaUrl, this.settings.ollamaModel, this.settings.llmMaxAnswerTokens);
      this.llmKey = key;
    }
    return this.llmFor;
  }

  get embedder(): EmbeddingProvider {
    const key = `${this.settings.ollamaUrl}|${this.settings.embeddingModel}`;
    if (!this.embedderFor || this.embedderKey !== key) {
      this.embedderFor = getEmbeddings(this.settings.ollamaUrl, this.settings.embeddingModel);
      this.embedderKey = key;
    }
    return this.embedderFor;
  }

  constructor(dataDir?: string) {
    this.paths = resolvePaths(dataDir);
    ensureOwnLayout(this.paths);
    this.settings = loadSettings(this.paths);
    this.store = new Store(this.paths.dbPath);
    this.resolver = new IdentityResolver(this.store);

    const llm = this.llm;

    this.pipeline = new IngestionPipeline({
      store: this.store,
      resolver: this.resolver,
      // A getter, not a snapshot: `updateSettings` replaces the settings
      // object, and a captured reference would silently keep the old values.
      settings: () => this.settings,
      embedder: () => this.embedder,
      paths: this.paths,
      emit: (type, payload) => broker.publish(type, payload),
    });

    this.watcher = new FolderWatcher(
      this.paths,
      () => this.settings.watchFolder,
      (filePath) => this.pipeline.processPath(filePath)
    );
    this.watcher.on("job", (payload: Record<string, unknown>) => broker.publish("job", payload));
  }

  ragDeps(): RagDeps {
    return {
      store: this.store,
      embedder: this.embedder,
      llm: this.llm,
      maxEvidence: this.settings.ragMaxEvidence,
      evidenceChars: this.settings.ragEvidenceChars,
      totalEvidenceChars: this.settings.ragTotalEvidenceChars,
      maxAnswerTokens: this.settings.llmMaxAnswerTokens,
    };
  }

  setWatchFolder(folder: string): void {
    this.settings.watchFolder = folder;
    saveSettings(this.paths, this.settings);
  }

  updateSettings(patch: Partial<CircleSettings>): CircleSettings {
    this.settings = { ...this.settings, ...patch };
    saveSettings(this.paths, this.settings);
    return this.settings;
  }

  async start(): Promise<Record<string, unknown>> {
    const health: Record<string, unknown> = {};
    health.database = { connected: this.store.ping(), engine: this.store.engineName() };
    this.store.resumeIncompleteJobs();

    const llmStatus = await this.llm.statusAsync();
    health.llm = llmStatus;
    health.embeddings = await this.embedder.statusAsync();
    health.stt = { available: false, provider: "none", detail: "voice transcription is not enabled in this build" };
    health.tts = { enabled: false, detail: "disabled" };

    if (this.settings.watchFolder) {
      this.watcher.start();
      health.watcher = {
        running: this.watcher.stats.running,
        root: this.settings.watchFolder,
      };
    } else {
      health.watcher = { running: false, detail: "no folder chosen yet" };
    }
    health.imports = {
      root: this.settings.watchFolder,
      processed: this.paths.processedDir,
      failed: this.paths.failedDir,
    };
    health.offline_ready = Boolean(llmStatus.available);
    this.health = health;
    this.started = true;
    // Build the vector index in the background, after the window is up. It is
    // not needed to answer the first question -- keyword search covers that --
    // and building it inline is what used to freeze startup.
    void this.store.warmEmbeddingIndex();
    // Same idea for cached relationship profiles: any whose stored totals no
    // longer match the events on disk get recomputed, so a bad count can never
    // outlive the fix that corrects it.
    void this.repairStaleProfiles();
    return health;
  }

  private async repairStaleProfiles(): Promise<void> {
    const ids = this.store.staleProfileIds();
    this.health.profiles_repaired = ids.length;
    for (let i = 0; i < ids.length; i++) {
      refreshProfile(this.store, ids[i]!);
      // Topic extraction walks a 1000-event window per person, so yielding
      // keeps the window responsive while this runs.
      if (i % 5 === 4) await new Promise((r) => setImmediate(r));
    }
  }

  async shutdown(): Promise<void> {
    await this.watcher.stop();
    this.store.close();
  }

  /**
   * Health with the parts that change after startup recomputed.
   *
   * The snapshot taken in `start()` is wrong for anything that becomes true
   * later: the watcher is not `running` until chokidar fires its ready event,
   * so a healthy watcher reported itself missing for the whole session. Ask
   * for health and get the state now, not the state at launch.
   */
  async liveHealth(): Promise<Record<string, unknown>> {
    const health: Record<string, unknown> = { ...this.health };
    health.watcher = {
      running: this.watcher.stats.running,
      root: this.settings.watchFolder || "",
      detail: this.settings.watchFolder
        ? this.watcher.stats.running
          ? "watching"
          : "starting"
        : "no folder chosen yet",
    };
    health.llm = await this.llm.statusAsync().catch(() => this.health.llm);
    health.embeddings = await this.embedder.statusAsync().catch(() => this.health.embeddings);
    // An upgrade that had to change the database is worth saying out loud:
    // it is the difference between "my data moved" and "nothing happened".
    health.migration = {
      columns_added: this.store.migration.added.length,
      tables_rebuilt: this.store.migration.rebuilt,
      unfixable: this.store.migration.unfixable,
      names_repaired: this.store.migration.names_repaired ?? 0,
      legacy_columns_dropped: this.store.migration.legacy_columns,
    };
    this.health = health;
    return health;
  }

  syncStatus(): Record<string, unknown> {
    return {
      watcher_running: this.watcher.stats.running,
      root: this.settings.watchFolder,
      roots: this.settings.watchFolder ? [this.settings.watchFolder] : [],
      folder_count: this.settings.watchFolder ? 1 : 0,
      files: {
        processed: this.watcher.stats.processed,
        failed: this.watcher.stats.failed,
        skipped: this.watcher.stats.skipped,
      },
      jobs: this.store.countJobsByStatus(),
      last_event_at: this.watcher.stats.lastEventAt,
      queue_size: this.watcher.queueSize(),
      subscribers: broker.subscriberCount,
      counts: {
        people: this.store.countPeople(),
        messages: this.store.countMessages(),
        memories: this.store.countMemories(),
      },
    };
  }
}
