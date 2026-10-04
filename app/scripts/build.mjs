/**
 * Bundle the Electron main process (and everything it imports) with esbuild.
 *
 * Node built-ins and `electron` stay external; `chokidar` is bundled so the
 * packaged app does not need a node_modules folder beside it.
 */
import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import fs from "node:fs";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "..");

await build({
  entryPoints: [path.join(root, "electron", "main.ts")],
  outfile: path.join(root, "dist", "electron", "main.cjs"),
  bundle: true,
  platform: "node",
  target: "node22",
  format: "cjs",
  sourcemap: false,
  external: ["electron"],
  define: { "import.meta.url": "importMetaUrl" },
  banner: { js: "const importMetaUrl = require('url').pathToFileURL(__filename).href;" },
  logLevel: "info",
});

// The preload is plain CommonJS and is loaded by path at runtime, so it is
// copied rather than bundled.
fs.mkdirSync(path.join(root, "dist", "electron"), { recursive: true });
fs.copyFileSync(
  path.join(root, "electron", "preload.cjs"),
  path.join(root, "dist", "electron", "preload.cjs")
);

console.log("bundled -> dist/electron/main.cjs (+ preload.cjs)");
