/**
 * Prerequisites: Ollama and the two models.
 *
 * The app is not usable until a local model exists, so this both *checks* and
 * *installs*: it can download Ollama, run the installer silently, and pull
 * `gemma3:4b` plus `nomic-embed-text`. Nothing here sends any user data
 * anywhere; it only fetches public installer/model artifacts.
 */
import { execFile, execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { EventEmitter } from "node:events";

const OLLAMA_WINDOWS_URL = "https://ollama.com/download/OllamaSetup.exe";
const OLLAMA_MAC_URL = "https://ollama.com/download/Ollama-darwin.zip";
const OLLAMA_LINUX_INSTALL = "https://ollama.com/install.sh";

export interface PrereqStatus {
  ollamaInstalled: boolean;
  ollamaRunning: boolean;
  ollamaPath: string;
  models: string[];
  requiredModels: string[];
  missingModels: string[];
  ready: boolean;
  detail: string;
  platform: string;
}

export interface SetupProgress {
  stage: "check" | "download" | "install" | "pull" | "start" | "done" | "error";
  message: string;
  percent?: number;
}

export class PrerequisiteInstaller extends EventEmitter {
  constructor(
    private readonly baseUrl: string,
    private readonly model: string,
    private readonly embeddingModel: string
  ) {
    super();
  }

  private progress(stage: SetupProgress["stage"], message: string, percent?: number): void {
    this.emit("progress", { stage, message, percent } satisfies SetupProgress);
  }

  /** Locate the ollama executable without assuming it is on PATH. */
  findOllama(): string {
    const candidates =
      process.platform === "win32"
        ? [
            path.join(os.homedir(), "AppData", "Local", "Programs", "Ollama", "ollama.exe"),
            "C:/Program Files/Ollama/ollama.exe",
          ]
        : ["/usr/local/bin/ollama", "/opt/homebrew/bin/ollama", "/usr/bin/ollama"];
    for (const candidate of candidates) {
      if (fs.existsSync(candidate)) return candidate;
    }
    try {
      const which = process.platform === "win32" ? "where" : "which";
      const found = execFileSync(which, ["ollama"], { encoding: "utf-8" }).split(/\r?\n/)[0];
      if (found && fs.existsSync(found.trim())) return found.trim();
    } catch {
      /* not on PATH */
    }
    return "";
  }

  private tags(): { running: boolean; models: string[] } {
    try {
      const out = execFileSync("curl", ["-s", "-S", "--max-time", "5", `${this.baseUrl}/api/tags`], {
        encoding: "utf-8",
      });
      const parsed = JSON.parse(out) as { models?: { name?: string }[] };
      return { running: true, models: (parsed.models ?? []).map((m) => m.name ?? "").filter(Boolean) };
    } catch {
      return { running: false, models: [] };
    }
  }

  status(): PrereqStatus {
    const ollamaPath = this.findOllama();
    const { running, models } = this.tags();
    const required = [this.model, this.embeddingModel];
    const missing = required.filter(
      (req) => !models.some((m) => m === req || m.startsWith(`${req}:`))
    );
    return {
      ollamaInstalled: Boolean(ollamaPath),
      ollamaRunning: running,
      ollamaPath,
      models,
      requiredModels: required,
      missingModels: missing,
      ready: running && missing.length === 0,
      detail: !ollamaPath
        ? "Ollama is not installed"
        : !running
          ? "Ollama is installed but not running"
          : missing.length > 0
            ? `Missing model(s): ${missing.join(", ")}`
            : "Ready",
      platform: process.platform,
    };
  }

  /** Full install: Ollama if missing, then start it, then pull models. */
  async install(): Promise<PrereqStatus> {
    try {
      this.progress("check", "Checking prerequisites");
      let ollamaExe = this.findOllama();
      if (!ollamaExe) {
        this.progress("download", "Downloading Ollama");
        const downloaded = await this.downloadOllama();
        this.progress("install", "Installing Ollama");
        await this.installOllama(downloaded);
        ollamaExe = this.findOllama();
      }

      if (!this.tags().running) {
        this.progress("start", "Starting Ollama");
        this.startOllama(ollamaExe);
        await this.waitForRunning(60_000);
      }

      for (const model of [this.model, this.embeddingModel]) {
        const installed = this.tags().models.some((m) => m === model || m.startsWith(`${model}:`));
        if (installed) continue;
        this.progress("pull", `Downloading model ${model} (this can take a while)`);
        await this.pullModel(model);
      }

      this.progress("done", "Everything is ready");
      return this.status();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.progress("error", message);
      throw err;
    }
  }

  private async downloadOllama(): Promise<string> {
    const dest =
      process.platform === "win32"
        ? path.join(os.tmpdir(), "OllamaSetup.exe")
        : path.join(os.tmpdir(), "ollama-download");
    const url =
      process.platform === "win32"
        ? OLLAMA_WINDOWS_URL
        : process.platform === "darwin"
          ? OLLAMA_MAC_URL
          : OLLAMA_LINUX_INSTALL;
    await run("curl", ["-L", "--fail", "-o", dest, url], (chunk) => this.progress("download", `Downloading Ollama… ${chunk}`));
    if (!fs.existsSync(dest) || fs.statSync(dest).size === 0) {
      throw new Error("Ollama download failed");
    }
    return dest;
  }

  private async installOllama(downloaded: string): Promise<void> {
    if (process.platform === "win32") {
      // /SILENT installs without UI; the installer elevates itself if needed.
      await run(downloaded, ["/SILENT", "/NORESTART"]);
      return;
    }
    if (process.platform === "darwin") {
      await run("unzip", ["-o", downloaded, "-d", "/Applications"]);
      return;
    }
    await run("sh", [downloaded]);
  }

  private startOllama(exePath: string): void {
    const child = spawn(exePath || "ollama", ["serve"], {
      detached: true,
      stdio: "ignore",
      windowsHide: true,
    });
    child.unref();
  }

  private async waitForRunning(timeoutMs: number): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (this.tags().running) return;
      await delay(1000);
    }
    throw new Error("Ollama did not start in time");
  }

  async pullModel(model: string): Promise<void> {
    const exePath = this.findOllama() || "ollama";
    await new Promise<void>((resolve, reject) => {
      const child = spawn(exePath, ["pull", model], { windowsHide: true });
      child.stdout.on("data", (buf: Buffer) => {
        const text = buf.toString().trim();
        if (text) this.progress("pull", `${model}: ${text.split("\n").pop()}`);
      });
      child.stderr.on("data", (buf: Buffer) => this.progress("pull", `${model}: ${buf.toString().trim()}`));
      child.on("error", reject);
      child.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`ollama pull ${model} failed with code ${code}`))));
    });
  }
}

function run(command: string, args: string[], onData?: (text: string) => void): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = execFile(command, args, { windowsHide: true, maxBuffer: 64 * 1024 * 1024 });
    child.stdout?.on("data", (buf: Buffer) => onData?.(buf.toString().trim()));
    child.stderr?.on("data", (buf: Buffer) => onData?.(buf.toString().trim()));
    child.on("error", reject);
    child.on("close", (code) => (code === 0 ? resolve() : reject(new Error(`${command} exited with ${code}`))));
  });
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}