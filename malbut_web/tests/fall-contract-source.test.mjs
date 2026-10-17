import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { COPY, SOURCE, render } from "../scripts/sync-fall-contract.mjs";
import { FALL_CONTRACT } from "../infra/aws/push-broker/fall-contract.mjs";
import {
  FALL_NOTIFICATION_TEXTS, PER_REASON_FALL_REASONS, WEB_ONLY_FALL_REASONS,
} from "../infra/aws/push-broker/fall-notification.mjs";

test("the web copy matches the robot contract source", () => {
  assert.equal(readFileSync(COPY, "utf8"), render(readFileSync(SOURCE, "utf8")),
    "run `node scripts/sync-fall-contract.mjs` in malbut_web");
  assert.deepEqual(FALL_CONTRACT, JSON.parse(readFileSync(SOURCE, "utf8")));
});

test("every robot notification reason has web words and a known level", () => {
  const reasons = Object.keys(FALL_CONTRACT.notificationReasons);
  assert.deepEqual(Object.keys(FALL_NOTIFICATION_TEXTS).sort(), [...reasons].sort());
  for (const level of Object.values(FALL_CONTRACT.notificationReasons)) {
    assert.ok(FALL_CONTRACT.notificationLevels.includes(level), level);
  }
  for (const reason of [...WEB_ONLY_FALL_REASONS, ...PER_REASON_FALL_REASONS]) {
    assert.ok(reasons.includes(reason), reason);
  }
});

test("per-kind lists name only known event kinds", () => {
  for (const kind of [...Object.keys(FALL_CONTRACT.reasonsByKind),
    ...Object.keys(FALL_CONTRACT.analysisPurposes)]) {
    assert.ok(FALL_CONTRACT.eventKinds.includes(kind), kind);
  }
});
