/** Dev entry: run the API + built frontend without Electron. */
import path from "node:path";
import { fileURLToPath } from "node:url";
import { AppContext } from "./context.js";
import { createServer } from "./server.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, "..", "..");

async function main(): Promise<void> {
  const ctx = new AppContext();
  await ctx.start();
  const frontendDir = process.env.CIRCLE_FRONTEND_DIST || path.join(repoRoot, "frontend", "dist");
  const { app } = createServer({ ctx, frontendDir, port: 8477 });
  const port = Number(process.env.CIRCLE_PORT ?? 8477);
  app.listen(port, "127.0.0.1", () => {
    console.log(`Circle dev server on http://127.0.0.1:${port}`);
    console.log(`  data:   ${ctx.paths.dataDir}`);
    console.log(`  watch:  ${ctx.settings.watchFolder || "(none chosen yet)"}`);
  });
  process.on("SIGINT", async () => {
    await ctx.shutdown();
    process.exit(0);
  });
}

void main();
