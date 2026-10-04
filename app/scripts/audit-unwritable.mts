import { DatabaseSync } from "node:sqlite";
import { parseSchema } from "../src/migrate.ts";
import { SCHEMA } from "../src/store.ts";

const dbPath = process.argv[2]!;
const db = new DatabaseSync(dbPath, { readOnly: true });
const parsed = parseSchema(SCHEMA);

console.log(`auditing ${dbPath}\n`);
let unwritable = 0;
for (const table of parsed.tables) {
  const info = db.prepare(`PRAGMA table_info(${table.name})`).all() as {
    name: string;
    notnull: number;
    dflt_value: string | null;
  }[];
  if (info.length === 0) continue;
  const wanted = new Set(
    parsed.tables.find((t) => t.name === table.name)!.statement
      .match(/\(([\s\S]*)\)\s*;?\s*$/)?.[1]!
      .split(",")
      .map((c) => c.trim().split(/\s+/)[0]!.replace(/^["'`]|["'`]$/g, ""))
      .filter(Boolean)
  );
  // A NOT NULL column with no default that this build does not know about means
  // every INSERT into the table fails.
  const blockers = info.filter(
    (c) => c.notnull === 1 && c.dflt_value === null && !wanted.has(c.name)
  );
  if (blockers.length === 0) continue;
  unwritable++;
  console.log(`  UNWRITABLE  ${table.name}`);
  console.log(`    legacy-only NOT NULL columns: ${blockers.map((b) => b.name).join(", ")}`);
  console.log(`    current schema columns:      ${[...wanted].join(", ")}`);
}
console.log(`\n${unwrapped(unwritable)} table(s) cannot be written to.`);
db.close();

function unwrapped(n: number): number {
  return n;
}