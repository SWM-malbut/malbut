import { readFile, readdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import pg from "pg";
import { postgresSsl } from "../db/postgres-ssl.mjs";

const databaseUrl = databaseConnectionUrl(process.env);
if (!databaseUrl) {
  throw new Error("DATABASE_URL is required to run PostgreSQL migrations.");
}

const migrationsDirectory = fileURLToPath(
  new URL("../db/migrations/", import.meta.url),
);
const migrationFiles = (await readdir(migrationsDirectory))
  .filter((name) => /^\d+_[a-z0-9_-]+\.sql$/i.test(name))
  .sort();

const pool = new pg.Pool({
  connectionString: databaseUrl,
  max: 1,
  ssl: postgresSsl(),
});
const client = await pool.connect();

try {
  await client.query("BEGIN");
  await client.query(
    "SELECT pg_advisory_xact_lock(hashtext('malbut-homecam-schema'))",
  );
  await client.query(`CREATE TABLE IF NOT EXISTS homecam_schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
  )`);

  for (const migrationFile of migrationFiles) {
    const version = path.basename(migrationFile, ".sql");
    const existing = await client.query(
      "SELECT 1 FROM homecam_schema_migrations WHERE version = $1",
      [version],
    );
    if (existing.rowCount) continue;

    const sql = await readFile(path.join(migrationsDirectory, migrationFile), "utf8");
    await client.query(sql);
    await client.query(
      "INSERT INTO homecam_schema_migrations (version) VALUES ($1)",
      [version],
    );
    process.stdout.write(`Applied ${version}\n`);
  }

  await client.query("COMMIT");
} catch (error) {
  await client.query("ROLLBACK");
  throw error;
} finally {
  client.release();
  await pool.end();
}

function databaseConnectionUrl(runtime) {
  const configured = runtime.DATABASE_URL?.trim();
  if (configured) return configured;
  const host = runtime.DATABASE_HOST?.trim();
  const database = runtime.DATABASE_NAME?.trim();
  const username = runtime.DATABASE_USERNAME?.trim();
  const password = runtime.DATABASE_PASSWORD;
  if (!host || !database || !username || !password) return undefined;
  const port = runtime.DATABASE_PORT?.trim() || "5432";
  if (!/^\d{1,5}$/.test(port) || Number(port) > 65_535) {
    throw new Error("DATABASE_PORT is invalid.");
  }
  return (
    `postgresql://${encodeURIComponent(username)}:${encodeURIComponent(password)}` +
    `@${host}:${port}/${encodeURIComponent(database)}`
  );
}
