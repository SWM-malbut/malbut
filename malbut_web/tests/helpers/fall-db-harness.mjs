import { createRequire } from "node:module";
import { readFileSync, existsSync, statSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { PGlite } from "@electric-sql/pglite";
import ts from "typescript";

const require = createRequire(import.meta.url);
const root = fileURLToPath(new URL("../../", import.meta.url));

export function moduleLoader(overrides = {}) {
  const cache = new Map();
  function load(candidate) {
    const resolved = [candidate, `${candidate}.ts`, `${candidate}/index.ts`]
      .find((p) => existsSync(p) && statSync(p).isFile());
    if (!resolved) throw new Error(`Cannot load ${candidate}`);
    if (overrides[resolved]) return overrides[resolved];
    if (cache.has(resolved)) return cache.get(resolved).exports;
    const mod = { exports: {} };
    cache.set(resolved, mod);
    const source = ts.transpileModule(readFileSync(resolved, "utf8"), {
      fileName: `${resolved}.ts`, compilerOptions: {
        esModuleInterop: true, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
      },
    }).outputText;
    new Function("require", "module", "exports", source)(
      (name) => name.startsWith(".") ? load(path.resolve(path.dirname(resolved), name)) : require(name),
      mod, mod.exports,
    );
    return mod.exports;
  }
  return (relative) => load(path.resolve(root, relative));
}

export async function fallDatabase({ through } = {}) {
  const db = new PGlite();
  await db.exec("CREATE TABLE homecam_schema_migrations(version TEXT PRIMARY KEY)");
  for (const file of readdirSync(path.join(root, "db/migrations"))
    .filter((f) => f.endsWith(".sql") && (!through || f <= `${through}.sql`)).sort()) {
    await db.exec(readFileSync(path.join(root, "db/migrations", file), "utf8"));
    await db.query("INSERT INTO homecam_schema_migrations VALUES($1)", [file.replace(/\.sql$/, "")]);
  }
  await db.exec(`INSERT INTO devices(id,display_name,kvs_channel_arn) VALUES
    ('robot-a','말벗 A','arn:test:a'),('robot-b','말벗 B','arn:test:b');
    INSERT INTO device_memberships(device_id,user_email,role) VALUES
    ('robot-a','owner@example.com','owner'),('robot-a','family@example.com','family');`);
  const query = async (sql, values = []) => {
    const result = await db.query(sql, values);
    return { rows: result.rows.map((row) => Object.fromEntries(Object.entries(row).map(
      ([k, v]) => [k, v instanceof Date ? v.toISOString() : v],
    ))), rowCount: result.affectedRows || result.rows.length };
  };
  // PGlite has one connection; serialize transaction clients in this harness.
  let tail = Promise.resolve();
  const pool = { query, async connect() {
    const before = tail;
    let release;
    tail = new Promise((resolve) => { release = resolve; });
    await before;
    return { query, release };
  } };
  return { db, pool, root };
}
