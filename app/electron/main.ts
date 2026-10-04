/**
 * Electron main process.
 *
 * The API runs in this same Node process (Electron's main process is Node),
 * so there is no separate backend binary to ship or supervise. The window
 * loads the local address; the renderer never touches the filesystem directly.
 */
import { app, BrowserWindow, dialog, ipcMain, shell } from "electron";
import path from "node:path";
import fs from "node:fs";
import { fileURLToPath } from "node:url";
import { AppContext } from "../src/context.js";
import { createServer } from "../src/server.js";

const here = path.dirname(fileURLToPath(import.meta.url));

let ctx: AppContext | null = null;
let win: BrowserWindow | null = null;

function frontendDir(): string {
  const candidates = [
    // Packaged: extraResources copies frontend/dist next to the app resources.
    path.join(process.resourcesPath ?? "", "frontend"),
    // Dev: this bundle lives at <repo>/app/dist/electron, and the frontend is a
    // sibling of app/ -- so three levels up, not two. Resolving only two used
    // to point at <repo>/app/frontend/dist, which does not exist, leaving
    // Express with no static files and a bare "Cannot GET /" window.
    path.resolve(here, "..", "..", "..", "frontend", "dist"),
    path.resolve(here, "..", "..", "frontend", "dist"),
  ];
  for (const candidate of candidates) {
    if (candidate && fs.existsSync(path.join(candidate, "index.html"))) return candidate;
  }
  return candidates[1]!;
}

async function start(): Promise<string> {
  ctx = new AppContext();
  await ctx.start();
  const { app: expressApp } = createServer({ ctx, frontendDir: frontendDir(), port: 0 });
  const port = await new Promise<number>((resolve) => {
    const server = expressApp.listen(0, "127.0.0.1", () => {
      const address = server.address();
      resolve(typeof address === "object" && address ? address.port : 8477);
    });
  });
  return `http://127.0.0.1:${port}`;
}

async function createWindow(url: string): Promise<void> {
  win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: "Circle",
    backgroundColor: "#f7f4ee",
    webPreferences: {
      preload: path.join(here, "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });
  // External links open in the real browser, not inside the app.
  win.webContents.setWindowOpenHandler(({ url: target }) => {
    void shell.openExternal(target);
    return { action: "deny" };
  });
  await win.loadURL(url);
}

function registerIpc(): void {
  ipcMain.handle("circle:choose-folder", async () => {
    const result = await dialog.showOpenDialog({ properties: ["openDirectory"], title: "Choose the folder Circle should read" });
    if (result.canceled || result.filePaths.length === 0) return null;
    return result.filePaths[0];
  });
  ipcMain.handle("circle:data-dir", () => ctx?.paths.dataDir ?? "");
}

app.whenReady().then(async () => {
  registerIpc();
  try {
    const url = await start();
    await createWindow(url);
  } catch (err) {
    dialog.showErrorBox("Circle could not start", err instanceof Error ? err.message : String(err));
    app.quit();
  }

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0 && ctx) {
      // Re-open against the already-running server.
      void createWindow(`http://127.0.0.1:${process.env.CIRCLE_PORT ?? 8477}`);
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", async () => {
  if (ctx) await ctx.shutdown();
});
