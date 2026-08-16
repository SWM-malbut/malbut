import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { PGlite } from "@electric-sql/pglite";
import ts from "typescript";

async function loadPlaceholderConverter() {
  const source = await readFile(
    new URL("../db/sql-compat.ts", import.meta.url),
    "utf8",
  );
  const javascript = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ESNext,
      target: ts.ScriptTarget.ES2022,
    },
  }).outputText;
  return import(
    `data:text/javascript;base64,${Buffer.from(javascript).toString("base64")}#${Date.now()}`
  );
}

test("D1 placeholders become PostgreSQL parameters without touching literals", async () => {
  const { postgresPlaceholders } = await loadPlaceholderConverter();
  assert.equal(
    postgresPlaceholders(
      `SELECT '?' AS literal, "?" AS identifier, value FROM sample WHERE a = ? AND b = ?`,
    ),
    `SELECT '?' AS literal, "?" AS identifier, value FROM sample WHERE a = $1 AND b = $2`,
  );
  assert.equal(
    postgresPlaceholders("SELECT 'it''s ?' AS literal, id FROM sample WHERE id = ?"),
    "SELECT 'it''s ?' AS literal, id FROM sample WHERE id = $1",
  );
});

test("PostgreSQL migration creates the homecam schema and durable event outbox", async () => {
  const [initialMigration, authMigration] = await Promise.all([
    readFile(new URL("../db/migrations/0001_initial.sql", import.meta.url), "utf8"),
    readFile(
      new URL("../db/migrations/0002_web_auth_sessions.sql", import.meta.url),
      "utf8",
    ),
  ]);
  const database = new PGlite();
  try {
    await database.exec(initialMigration);
    await database.exec(authMigration);
    const now = "2026-08-12T04:00:00.000Z";
    await database.query(
      `INSERT INTO devices (id, display_name, kvs_channel_arn, created_at)
       VALUES ($1, $2, $3, $4)`,
      ["living-room", "거실 홈캠", "arn:test:p2p", now],
    );
    await database.query(
      `INSERT INTO stream_sessions
       (id, room_code, device_id, started_by, status, started_at, expires_at)
       VALUES ($1, $2, $3, $4, 'active', $5, $6)`,
      [
        "session-1",
        "ROOM01",
        "living-room",
        "owner@example.com",
        now,
        "2026-08-12T05:00:00.000Z",
      ],
    );
    await database.query(
      `INSERT INTO homecam_events
       (id, device_id, event_type, confidence, occurred_at, idempotency_key,
        request_fingerprint, recording_session_id, recording_offset_ms)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
      [
        "event-1",
        "living-room",
        "person",
        0.92,
        now,
        "person:1",
        "fingerprint",
        "session-1",
        1_500,
      ],
    );

    const outbox = await database.query(
      "SELECT event_id, device_id, attempt_count FROM homecam_push_outbox",
    );
    assert.deepEqual(outbox.rows, [
      { event_id: "event-1", device_id: "living-room", attempt_count: 0 },
    ]);
    const authTables = await database.query(
      `SELECT tablename FROM pg_tables
       WHERE schemaname = 'public'
         AND tablename IN ('web_auth_sessions', 'web_auth_challenges')
       ORDER BY tablename`,
    );
    assert.deepEqual(authTables.rows, [
      { tablename: "web_auth_challenges" },
      { tablename: "web_auth_sessions" },
    ]);
  } finally {
    await database.close();
  }
});

test("runtime source no longer imports Cloudflare D1", async () => {
  const [database, schema, packageJson] = await Promise.all([
    readFile(new URL("../db/index.ts", import.meta.url), "utf8"),
    readFile(new URL("../db/schema.ts", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);
  assert.doesNotMatch(database, /cloudflare:workers|drizzle-orm\/d1/);
  assert.match(database, /drizzle-orm\/node-postgres/);
  assert.match(schema, /drizzle-orm\/pg-core/);
  assert.doesNotMatch(packageJson, /wrangler|vinext|@cloudflare\/vite-plugin/);
});
