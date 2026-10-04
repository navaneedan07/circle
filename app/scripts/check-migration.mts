/**
 * Open a Circle database and report what an upgrade had to change.
 *
 * Useful when the application will not start: this reports the same migration
 * the app performs, without needing the app to start.
 *
 *   npx tsx scripts/check-migration.mts [path/to/circle.db]
 */
import fs from "node:fs";
import path from "node:path";
import { Store } from "../src/store.js";

const explicit = process.argv[2];
const dbPath = explicit ?? path.join(process.env.CIRCLE_DATA_DIR ?? path.join(process.homedir(), "Documents", "Circle"), "circle.db");

if (!fs.existsSync(dbPath)) {
  console.log(`No database at ${dbPath}`);
  console.log("Nothing to migrate. The schema is created on first run.");
  process.exit(0);
}

try {
  const store = new Store(dbPath);
  const { added, rebuilt, unfixable } = store.migration;
  console.log(`Opened ${dbPath}`);
  console.log(`  columns added:   ${added.length}${added.length ? `\n    ${added.join("\n    ")}` : ""}`);
  console.log(`  tables rebuilt:  ${rebuilt.length ? rebuilt.join(", ") : "(none)"}`);
  console.log(`  could not fix:   ${unfixable.length ? unfixable.join(", ") : "(none)"}`);
  console.log(`  people:          ${store.countPeople()}`);
  console.log(`  messages:        ${store.countMessages()}`);
  console.log(`  memories:        ${store.countMemories()}`);
  store.close();
  console.log(unfixable.length ? "\nSome columns could not be added; see the health panel in the app." : "\nDatabase is ready.");
  process.exit(unfixable.length ? 1 : 0);
} catch (err) {
  console.error(`This database cannot be opened: ${err instanceof Error ? err.message : String(err)}`);
  console.error("If it predates the current schema badly, move it aside and let Circle start fresh.");
  process.exit(1);
}
