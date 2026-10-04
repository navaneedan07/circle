/**
 * Schema creation and migration.
 *
 * The schema is created with `CREATE TABLE IF NOT EXISTS`, which quietly does
 * nothing when the table already exists. That is fine for a brand new database
 * and fatal for an existing one: a database written by an earlier build is
 * left exactly as it was, and the first statement that mentions a column
 * added since -- `CREATE INDEX ... ON people(display_name_lower)`, say --
 * fails with "no such column". The application then refuses to start, for
 * everyone who ran an earlier version.
 *
 * That is not a hypothetical: it is what happens on the first launch of any
 * upgrade, which is precisely the person most likely to install a new build.
 *
 * So the schema is applied in three passes -- tables, then any columns those
 * tables are missing, then indexes and virtual tables -- with the column list
 * read from the same SCHEMA constant that defines it. Deriving the migration
 * from the schema rather than hand-listing changes means the two cannot drift
 * apart and quietly miss one.
 */
import type { DatabaseSync } from "node:sqlite";
import { repairMojibake } from "./parsers/common.js";

interface ColumnDef {
  name: string;
  /** The full column definition, e.g. `display_name_lower TEXT NOT NULL`. */
  definition: string;
}

interface ParsedSchema {
  tables: { name: string; statement: string }[];
  /** Indexes and virtual tables, applied only after columns exist. */
  after: string[];
}

/** Split a CREATE TABLE body on commas that are not inside parentheses. */
function splitTopLevel(body: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const char of body) {
    if (char === "(") depth++;
    else if (char === ")") depth--;
    if (char === "," && depth === 0) {
      parts.push(current);
      current = "";
      continue;
    }
    current += char;
  }
  if (current.trim()) parts.push(current);
  return parts.map((p) => p.trim()).filter(Boolean);
}

/** Column definitions only: table constraints are not columns. */
function columnsOf(body: string): ColumnDef[] {
  return splitTopLevel(body)
    .filter((part) => !/^(PRIMARY|UNIQUE|CHECK|FOREIGN|CONSTRAINT)\b/i.test(part))
    .map((part) => {
      const name = part.split(/[\s(]+/)[0] ?? "";
      return { name: name.replace(/["'`]/g, ""), definition: part };
    })
    .filter((col) => col.name.length > 0);
}

export function parseSchema(schema: string): ParsedSchema {
  const statements = schema
    .split(";")
    .map((s) => s.trim())
    .filter(Boolean);

  const tables: { name: string; statement: string }[] = [];
  const after: string[] = [];

  for (const statement of statements) {
    const table = statement.match(
      /^CREATE\s+(?:VIRTUAL\s+)?TABLE\s+IF\s+NOT\s+EXISTS\s+["'`\[]?(\w+)["'`\]]?\s*\(([\s\S]*)\)$/i
    );
    if (table) {
      // A virtual table (FTS5) is built on another table's content and is
      // created in the second pass, after any columns it needs exist.
      if (/^CREATE\s+VIRTUAL/i.test(statement)) after.push(`${statement};`);
      else tables.push({ name: table[1]!, statement: `${statement};` });
      continue;
    }
    after.push(`${statement};`);
  }

  return { tables, after };
}

/**
 * Make `definition` safe to add to a table that already has rows.
 *
 * SQLite refuses to add a NOT NULL column without a default, and every table
 * here may already hold data.
 */
function addableDefinition(definition: string): string | null {
  if (/\bPRIMARY\s+KEY\b|\bUNIQUE\b/i.test(definition)) {
    // Cannot be added to an existing table; the caller reports it.
    return null;
  }
  if (/\bNOT\s+NULL\b/i.test(definition) && !/\bDEFAULT\b/i.test(definition)) {
    const isNumeric = /\b(INTEGER|REAL|NUMERIC|NUM)\b/i.test(definition);
    return `${definition} DEFAULT ${isNumeric ? "0" : "''"}`;
  }
  return definition;
}

export interface MigrationReport {
  added: string[];
  rebuilt: string[];
  unfixable: string[];
  names_repaired?: number;
  /** Columns dropped by a rebuild because they made the table unwritable. */
  legacy_columns: string[];
}

/**
 * Undo UTF-8-as-Latin-1 mojibake in names already stored in the database.
 *
 * Exports reach us with mangled contact names, and an earlier build stored
 * them mangled. Fixing only the parser would leave every existing archive
 * showing "ÙØ¤Ù" forever, because those rows are never re-read. Person ids are
 * derived from identity hashes rather than display names, so rewriting the
 * name keeps every message attached to the same person.
 *
 * Aliases and identity values are repaired alongside the display name so
 * that a later re-import matches the repaired spelling instead of creating a
 * second copy of the same person.
 */
export function repairStoredNames(db: DatabaseSync): number {
  const rows = db.prepare("SELECT id, display_name, doc FROM people").all() as {
    id: string;
    display_name: string;
    doc: string;
  }[];
  const updates: { id: string; name: string; doc: string }[] = [];
  for (const row of rows) {
    const name = repairMojibake(row.display_name).trim();
    let doc: Record<string, unknown>;
    try {
      doc = JSON.parse(row.doc) as Record<string, unknown>;
    } catch {
      continue;
    }
    let touched = false;
    if (name && name !== row.display_name) {
      doc.display_name = name;
      touched = true;
    }
    if (Array.isArray(doc.aliases)) {
      const aliases = (doc.aliases as unknown[])
        .map((a) => repairMojibake(String(a)).trim())
        .filter(Boolean);
      if (aliases.join("\u0000") !== (doc.aliases as unknown[]).join("\u0000")) {
        doc.aliases = aliases;
        touched = true;
      }
    }
    if (touched) updates.push({ id: row.id, name: name || row.display_name, doc: JSON.stringify(doc) });
  }
  if (updates.length === 0) return 0;

  const stamp = new Date().toISOString();
  db.exec("BEGIN");
  try {
    const update = db.prepare(
      "UPDATE people SET display_name = ?, display_name_lower = ?, doc = ?, updated_at = ? WHERE id = ?"
    );
    const updateIdentity = db.prepare(
      "UPDATE identities SET value = ? WHERE person_id = ? AND value = ?"
    );
    for (const u of updates) {
      update.run(u.name, u.name.toLowerCase(), u.doc, stamp, u.id);
      const before = db
        .prepare("SELECT value FROM identities WHERE person_id = ?")
        .all(u.id) as { value: string }[];
      for (const row of before) {
        const fixed = repairMojibake(row.value).trim();
        if (fixed && fixed !== row.value) updateIdentity.run(fixed, u.id, row.value);
      }
    }
    db.exec("COMMIT");
  } catch {
    // A failed repair must never stop the app from starting; the names are
    // cosmetic, and the next launch tries again.
    db.exec("ROLLBACK");
    return 0;
  }
  return updates.length;
}

/**
 * Recreate a table with the current definition, keeping the rows.
 *
 * Needed when a table predates a column SQLite will not let us add -- a
 * PRIMARY KEY or UNIQUE column, most often. The old `app_settings` table had
 * `id, doc, value` while the application queries `WHERE key = ?`, and no
 * ALTER TABLE can conjure a primary key onto an existing table. So the table is
 * rebuilt and whatever the two have in common is copied across.
 */
function rebuildTable(db: DatabaseSync, table: { name: string; statement: string }): void {
  const body = table.statement.match(/\(([\s\S]*)\)\s*;?\s*$/)?.[1];
  if (!body) return;
  const wanted = columnsOf(body).map((c) => c.name);
  const have = new Set(
    (db.prepare(`PRAGMA table_info(${table.name})`).all() as { name: string }[]).map((c) => c.name)
  );
  const common = wanted.filter((name) => have.has(name));
  const temp = `${table.name}_migrate_rebuild`;

  db.exec(`DROP TABLE IF EXISTS ${temp};`);
  db.exec(table.statement.replace(table.name, temp).replace(`IF NOT EXISTS ${temp}`, `IF NOT EXISTS ${temp}`));
  if (common.length > 0) {
    const list = common.join(", ");
    db.exec(`INSERT INTO ${temp} (${list}) SELECT ${list} FROM ${table.name};`);
  }
  db.exec(`DROP TABLE ${table.name};`);
  db.exec(`ALTER TABLE ${temp} RENAME TO ${table.name};`);
}

/**
 * Values that are derived from a column and must be filled in for rows that
 * predate the derived column. Without this, every existing person would have
 * an empty lower-cased name and name lookups would silently return nothing.
 */
const BACKFILLS: { sql: string; label: string }[] = [
  {
    label: "people.display_name_lower",
    sql: "UPDATE people SET display_name_lower = lower(display_name) WHERE display_name_lower = '' OR display_name_lower IS NULL",
  },
];

/** Create or update the schema in an existing database. */
export function applySchema(db: DatabaseSync, schema: string): MigrationReport {
  const parsed = parseSchema(schema);
  const report: MigrationReport = { added: [], rebuilt: [], unfixable: [], legacy_columns: [] };

  db.exec("BEGIN");
  try {
    // Pass 1 -- create tables that do not exist yet.
    for (const table of parsed.tables) {
      db.exec(table.statement);
    }

    // Pass 2 -- add columns an existing table predates, rebuilding the table
    // when SQLite refuses the column outright.
    for (const table of parsed.tables) {
      const body = table.statement.match(/\(([\s\S]*)\)\s*;?\s*$/)?.[1];
      if (!body) continue;
      const info = db.prepare(`PRAGMA table_info(${table.name})`).all() as {
        name: string;
        notnull: number;
        dflt_value: string | null;
      }[];
      const existing = new Set(info.map((c) => c.name));
      if (existing.size === 0) continue; // just created with the right shape

      const missing = columnsOf(body).filter((column) => !existing.has(column.name));
      let needsRebuild = false;

      // The inverse problem: a column this build has never heard of, declared
      // NOT NULL with no default. Every INSERT omits it, so every write fails
      // with "NOT NULL constraint failed" -- while reads work fine, which is
      // what makes it look like the app is simply ignoring the files. An old
      // `processed_files(id, doc, ...)` is exactly this: `doc` is NOT NULL and
      // nothing ever writes it, so every single import died at the last step
      // after its records had already landed. Adding columns cannot fix this;
      // the table has to be rebuilt without them.
      const wanted = new Set(columnsOf(body).map((c) => c.name));
      const blockers = info.filter(
        (column) =>
          column.notnull === 1 && column.dflt_value === null && !wanted.has(column.name)
      );
      if (blockers.length > 0) {
        needsRebuild = true;
        report.legacy_columns.push(
          ...blockers.map((column) => `${table.name}.${column.name}`)
        );
      }

      for (const column of missing) {
        const definition = addableDefinition(column.definition);
        if (!definition) {
          needsRebuild = true;
          continue;
        }
        try {
          db.exec(`ALTER TABLE ${table.name} ADD COLUMN ${definition};`);
          report.added.push(`${table.name}.${column.name}`);
        } catch {
          needsRebuild = true;
        }
      }

      if (needsRebuild) {
        try {
          rebuildTable(db, table);
          report.rebuilt.push(table.name);
        } catch {
          for (const column of missing) report.unfixable.push(`${table.name}.${column.name}`);
        }
      }
    }

    // Derived values for rows that predate their column.
    for (const backfill of BACKFILLS) {
      try {
        db.exec(backfill.sql);
      } catch {
        report.unfixable.push(backfill.label);
      }
    }
  } finally {
    db.exec("COMMIT");
  }

  // Pass 3 -- indexes and virtual tables, which reference the columns above.
  // Outside the transaction: a failure here must not roll back the migration.
  for (const statement of parsed.after) {
    try {
      db.exec(statement);
    } catch {
      // An index this build cannot create costs speed, not correctness, and
      // must never stop the application from starting. It is reported on the
      // health endpoint instead.
    }
  }

  return report;
}
