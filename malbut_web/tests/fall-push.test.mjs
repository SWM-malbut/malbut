import assert from "node:assert/strict";
import { createHmac, timingSafeEqual } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { runInNewContext } from "node:vm";
import ts from "typescript";
import { buildFallNotification, isFallNotification } from "../infra/aws/push-broker/fall-notification.mjs";

const input = {
  deviceId: "robot-a", notificationId: "11111111-1111-4111-8111-111111111111",
  incidentId: "22222222-2222-4222-8222-222222222222",
  level: "check", reason: "person_no_response", occurredAt: "2026-09-18T00:00:00.000Z",
};
const target = {
  id: "33333333-3333-4333-8333-333333333333", displayName: "말벗",
  endpoint: "https://fcm.googleapis.com/fcm/send/test",
  keys: { p256dh: "a".repeat(60), auth: "b".repeat(24) },
};
const roundtrip = (value) => JSON.parse(JSON.stringify(value));

test("fall messages separate observation from notification grade without raw video/text", () => {
  for (const [level, reason] of [
    ["info", "fall_observed_person_okay"], ["check", "person_no_response"],
    ["check", "check_required_not_confirmed_fall"], ["urgent", "help_requested"],
  ]) {
    const notification = buildFallNotification({ ...input, level, reason });
    assert.ok(isFallNotification(notification));
    assert.equal(notification.data.level, level);
    assert.equal(notification.data.incidentId, input.incidentId);
    assert.equal(notification.data.url, "/?view=live&device=robot-a");
    assert.doesNotMatch(notification.body, /의식.*없|낙상 확정/);
    assert.equal(notification.data.image, undefined);
  }
});

test("fall payload rejects mismatched grades, injected text/URLs and invalid identifiers", () => {
  for (const change of [
    { level: "urgent" }, { reason: "toString" }, { reason: ["person_no_response"] },
    { notificationId: "bad" }, { incidentId: "bad" }, { deviceId: "../../other" },
    { occurredAt: "bad" }, { occurredAt: "2026-09-18" },
  ]) assert.equal(buildFallNotification({ ...input, ...change }), null);
  const notification = buildFallNotification(input);
  assert.equal(isFallNotification({ ...notification, body: "raw VLM instruction" }), false);
  assert.equal(isFallNotification({ ...notification, data: { ...notification.data, image: "private" } }), false);
  assert.equal(isFallNotification({ ...notification, data: { ...notification.data, url: "//evil.test" } }), false);
});

async function brokerHarness() {
  let source = await readFile(new URL("../infra/aws/push-broker/index.mjs", import.meta.url), "utf8");
  source = source.replace(/^import .*;\n/gm, "").replace("export async function handler", "async function handler");
  const commonJs = { exports: {} }, sends = [];
  runInNewContext(`${source}\nmodule.exports = { handler };`, {
    module: commonJs, createHmac, timingSafeEqual, isFallNotification, Buffer,
    process: { env: {
      BROKER_SHARED_SECRET: "test-secret", PUSH_VAPID_SUBJECT: "mailto:test@example.com",
      PUSH_VAPID_PUBLIC_KEY: "public", PUSH_VAPID_PRIVATE_KEY: "private",
    } },
    webpush: {
      setVapidDetails() {},
      async sendNotification(subscription, body, options) {
        sends.push({ subscription, body: JSON.parse(body), options });
        return { statusCode: 201 };
      },
    }, URL,
  });
  return {
    sends,
    async request(notification, signed = true) {
      const body = JSON.stringify({ notification, subscriptions: [{
        subscriptionId: target.id, endpoint: target.endpoint, keys: target.keys,
      }] });
      const timestamp = Math.floor(Date.now() / 1000).toString();
      const signature = createHmac("sha256", "test-secret").update(`${timestamp}.${body}`).digest("hex");
      return commonJs.exports.handler({
        requestContext: { http: { method: "POST" } }, body,
        headers: signed ? { "x-homecam-timestamp": timestamp, "x-homecam-signature": signature } : {},
      });
    },
  };
}

test("broker checks signatures and fall schema before forwarding to web push", async () => {
  const broker = await brokerHarness();
  const notification = { title: "말벗", ...buildFallNotification(input) };
  assert.equal((await broker.request(notification, false)).statusCode, 401);
  assert.equal((await broker.request({ ...notification, body: "anything" })).statusCode, 400);
  assert.equal(broker.sends.length, 0);
  assert.equal((await broker.request(notification)).statusCode, 200);
  assert.equal(broker.sends[0].options.urgency, "high");
  const info = { title: "말벗", ...buildFallNotification({
    ...input, level: "info", reason: "fall_observed_person_okay",
  }) };
  assert.equal((await broker.request(info)).statusCode, 200);
  assert.equal(broker.sends[1].options.urgency, "normal");
});

test("broker still accepts existing homecam events", async () => {
  const broker = await brokerHarness();
  const notification = { title: "말벗", body: "사람 감지", data: {
    deviceId: input.deviceId, eventId: input.incidentId, eventType: "person",
    occurredAt: input.occurredAt,
    url: `/?view=events&device=${input.deviceId}&event=${input.incidentId}`,
  } };
  assert.equal((await broker.request(notification)).statusCode, 200);
  assert.equal(broker.sends.length, 1);
});

async function dispatchHarness({ targets = [target], statuses = [201], configured = true } = {}) {
  const source = await readFile(new URL("../app/push-broker.ts", import.meta.url), "utf8");
  const javascript = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText;
  const commonJs = { exports: {} }, requests = [], lookups = [], revoked = [];
  runInNewContext(javascript, {
    module: commonJs, exports: commonJs.exports, URL, TextEncoder, AbortSignal,
    crypto: globalThis.crypto,
    async fetch(url, init) {
      const body = JSON.parse(init.body);
      requests.push(body);
      assert.equal(String(url), "https://broker.example.com/");
      assert.ok(init.headers["x-homecam-signature"]);
      return Response.json({ results: body.subscriptions.map((s, i) => ({
        subscriptionId: s.subscriptionId, status: statuses[i],
      })) });
    },
    require(name) {
      if (name === "../infra/aws/push-broker/fall-notification.mjs") return { buildFallNotification };
      if (name === "./runtime-env") return { getRuntimeEnvironment: () => configured ? {
        PUSH_BROKER_URL: "https://broker.example.com/", PUSH_BROKER_SECRET: "test-secret",
      } : {} };
      if (name === "../db/homecam-validation") return { shouldPrunePushSubscription: (s) => [404, 410].includes(s) };
      if (name === "../db/homecam") return {
        async listActivePushTargets(deviceId) { lookups.push(deviceId); return targets; },
        async revokePushSubscriptionsById(ids) { revoked.push(...ids); return ids.length; },
      };
      throw new Error(`Unexpected dependency: ${name}`);
    },
  });
  return { api: commonJs.exports, requests, lookups, revoked };
}

test("fall dispatch selects device memberships and skips storage-session dependencies", async () => {
  const h = await dispatchHarness({ targets: [target, { ...target, id: "44444444-4444-4444-8444-444444444444" }], statuses: [201, 410] });
  const result = await h.api.dispatchFallPush(input);
  assert.deepEqual(h.lookups, [input.deviceId]);
  assert.equal(h.requests[0].subscriptions.length, 2);
  assert.equal(h.requests[0].notification.data.kind, "fall");
  assert.equal(h.requests[0].notification.data.notificationId, input.notificationId);
  assert.equal(result.delivered, 1); // push-service acceptance, not person read receipt
  assert.equal(result.pruned, 1);
  assert.equal(h.revoked.length, 1);
});

test("absent subscribers/config and push failure do not report delivery", async () => {
  for (const [options, reason] of [
    [{ targets: [] }, "no_subscribers"], [{ configured: false }, "not_configured"],
  ]) {
    const h = await dispatchHarness(options);
    const result = await h.api.dispatchFallPush(input);
    assert.equal(result.reason, reason);
    assert.equal(result.delivered, 0);
    assert.equal(result.dispatched, false);
    assert.equal(h.requests.length, 0);
  }
  const h = await dispatchHarness({ statuses: [503] });
  const result = await h.api.dispatchFallPush(input);
  assert.equal(result.delivered, 0);
  assert.equal(result.failed, 1);
  await assert.rejects(h.api.dispatchFallPush({ ...input, level: "urgent" }), /INVALID/);
});

test("service worker separates fall notification levels and opens the live view", async () => {
  const listeners = {}, displayed = [];
  const source = await readFile(new URL("../public/sw.js", import.meta.url), "utf8");
  runInNewContext(source, { URL, self: {
    location: { origin: "https://web.example.com" },
    addEventListener(name, callback) { listeners[name] = callback; },
    registration: { async showNotification(title, options) { displayed.push(options); } },
  } });
  for (const level of ["info", "check", "urgent"]) {
    let done;
    listeners.push({
      data: { json: () => ({ body: "test", data: { ...buildFallNotification(input).data, level } }) },
      waitUntil(promise) { done = promise; },
    });
    await done;
  }
  assert.equal(new Set(displayed.map((n) => n.tag)).size, 3);
  assert.ok(displayed.every((n) => n.tag.includes(input.incidentId)));
  assert.deepEqual(roundtrip(displayed[0].data), { url: "/?view=live&device=robot-a" });
});
