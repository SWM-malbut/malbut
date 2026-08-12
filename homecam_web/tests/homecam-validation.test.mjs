import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

async function loadHelpers() {
  const source = await readFile(
    new URL("../db/homecam-validation.ts", import.meta.url),
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

test("owner and family permissions keep privacy settings owner-only", async () => {
  const { canManageHomecam, canViewHomecam } = await loadHelpers();
  assert.equal(canViewHomecam("owner"), true);
  assert.equal(canViewHomecam("family"), true);
  assert.equal(canViewHomecam("broadcaster"), true);
  assert.equal(canViewHomecam("unknown"), false);
  assert.equal(canManageHomecam("owner"), true);
  assert.equal(canManageHomecam("family"), false);
});

test("settings and event validation reject unknown or replay-prone input", async () => {
  const { parseDeviceSettingsPatch, parseHomecamEventInput } =
    await loadHelpers();
  assert.deepEqual(parseDeviceSettingsPatch({ monitoringEnabled: true }), {
    monitoringEnabled: true,
  });
  assert.equal(parseDeviceSettingsPatch({ cameraEnabled: true, admin: true }), null);
  assert.equal(parseDeviceSettingsPatch({}), null);

  const now = new Date("2026-07-26T12:00:00.000Z");
  const event = parseHomecamEventInput(
    {
      eventType: "person",
      confidence: 0.91,
      occurredAt: "2026-07-26T11:59:59.000Z",
      idempotencyKey: "person:000001",
      recordingOffsetMs: 1500,
    },
    now,
  );
  assert.equal(event.eventType, "person");
  assert.equal(event.confidence, 0.91);
  assert.equal(
    parseHomecamEventInput(
      {
        eventType: "person",
        confidence: 1.1,
        occurredAt: "2026-07-26T11:59:59.000Z",
        idempotencyKey: "person:000002",
      },
      now,
    ),
    null,
  );
  assert.equal(
    parseHomecamEventInput(
      {
        eventType: "dog",
        confidence: 0.8,
        occurredAt: "2026-07-18T11:59:59.000Z",
        idempotencyKey: "dog:000001",
      },
      now,
    ),
    null,
  );
});

test("expired Web Push endpoints are pruned on 404 and 410", async () => {
  const { shouldPrunePushSubscription } = await loadHelpers();
  assert.equal(shouldPrunePushSubscription(404), true);
  assert.equal(shouldPrunePushSubscription(410), true);
  assert.equal(shouldPrunePushSubscription(429), false);
});
