import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("event push delivery is durable and retryable by idempotency key", async () => {
  const [database, migration, route, maintenance, pushBroker, serviceWorker] =
    await Promise.all([
      readFile(new URL("../db/homecam.ts", import.meta.url), "utf8"),
      readFile(
        new URL("../db/migrations/0001_initial.sql", import.meta.url),
        "utf8",
      ),
      readFile(
        new URL("../app/api/device/v1/events/route.ts", import.meta.url),
        "utf8",
      ),
      readFile(
        new URL("../app/api/internal/maintenance/route.ts", import.meta.url),
        "utf8",
      ),
      readFile(new URL("../app/push-broker.ts", import.meta.url), "utf8"),
      readFile(new URL("../public/sw.js", import.meta.url), "utf8"),
    ]);

  assert.match(migration, /CREATE TABLE IF NOT EXISTS homecam_push_outbox/i);
  assert.match(migration, /AFTER INSERT ON homecam_events/i);
  assert.match(database, /claimPendingHomecamPushes/);
  assert.match(database, /finishHomecamPushAttempt/);
  assert.match(route, /preferredEventId:\s*result\.event\.id/);
  assert.match(route, /currentPushFailed \? 503/);
  assert.match(maintenance, /MAINTENANCE_SECRET/);
  assert.match(maintenance, /runHomecamRetentionCleanup/);
  assert.match(maintenance, /claimPendingHomecamPushes/);
  assert.match(pushBroker, /failed:/);
  assert.match(pushBroker, /PUSH_BROKER_BATCH_SIZE = 100/);
  assert.match(serviceWorker, /homecam-event-\$\{payloadData\.eventId\}/);
  assert.doesNotMatch(pushBroker, /\bimage\s*:/);
});
