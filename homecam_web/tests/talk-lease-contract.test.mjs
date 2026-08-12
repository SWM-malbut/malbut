import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("PTT lease is bound to one viewer client and released on every exit path", async () => {
  const [database, route, page, migration] = await Promise.all([
    readFile(new URL("../db/homecam.ts", import.meta.url), "utf8"),
    readFile(
      new URL(
        "../app/api/devices/[deviceId]/talk-lease/route.ts",
        import.meta.url,
      ),
      "utf8",
    ),
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(
      new URL("../db/migrations/0001_initial.sql", import.meta.url),
      "utf8",
    ),
  ]);

  assert.match(migration, /client_id TEXT NOT NULL/i);
  assert.match(database, /talk_leases\.client_id = excluded\.client_id/);
  assert.match(
    database,
    /WHERE device_id = \? AND user_email = \? AND lease_id = \? AND client_id = \?/,
  );
  assert.match(route, /isValidClientId\(payload\.clientId\)/);
  assert.match(page, /clientId:\s*viewerClientIdRef\.current/);
  assert.match(page, /document\.visibilityState === "hidden"/);
  assert.match(page, /window\.addEventListener\("pagehide"/);
  assert.match(page, /onPointerCancel=\{\(\) => releaseTalkLease\(\)\}/);
});
